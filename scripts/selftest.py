#!/usr/bin/env python3
"""
End-to-end selftest for the AltStore source pipeline.

Runs the real pipeline code (check_for_updates, update_source,
generate_repo, add_custom_ipa, sync_release) against a stateful fake
GitHub API and synthetic IPAs, including the REAL GitHubRelease client
so cache-staleness regressions fail the test.

Run from the repo root:

    python scripts/selftest.py

Every test uses throwaway temp directories — nothing in the repo is
modified.
"""

import io
import json
import plistlib
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import altstore_lib as lib  # noqa: E402
import sync_release as sr  # noqa: E402
from add_custom_ipa import add_custom_ipa  # noqa: E402
from check_releases import run_check  # noqa: E402
from generate_repo import generate_repo  # noqa: E402
from update_source import update_source  # noqa: E402

PASS = 0


def check(cond, msg):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ FAIL: {msg}")
        raise AssertionError(msg)


def make_ipa(bundle_id, version="0.5.1", name="Nuvio", build="133"):
    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleDisplayName": name,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": build,
        "MinimumOSVersion": "16.1",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Payload/App.app/", "")
        zf.writestr(
            "Payload/App.app/Info.plist",
            plistlib.dumps(info, fmt=plistlib.FMT_BINARY),
        )
        zf.writestr("Payload/App.app/binary", b"x" * 5000)
    return buf.getvalue()


# ── Fake GitHub backend ──────────────────────────────────────────────────────

class FakeGitHub:
    """Stateful stand-in for the GitHub REST API + file downloads.

    ``api`` serves repo info, releases lists, commit lookups, and the
    ipa-assets release.  ``download`` serves bytes from download_store.
    ``assets`` is the ipa-assets release's asset table, shared with the
    sync client's server below so both views agree.
    """

    def __init__(self):
        self.repos = {}      # "owner/repo" → {"desc", "owner", "releases": [...]}
        self.commits = {}    # "owner/repo" → {tag: full sha}
        self.assets = {}     # asset name → {"id", "name", "size", "updated_at"}
        self.download_store = {}  # url → bytes
        self.downloads = []  # recorded (url, filename)
        self.tag_fetches = 0
        self.next_id = 100

    # ── fixture builders ─────────────────────────────────────────────
    def add_repo(self, repo, desc, owner, releases):
        self.repos[repo] = {"desc": desc, "owner": owner, "releases": releases}

    def set_commit(self, repo, tag, sha):
        self.commits.setdefault(repo, {})[tag] = sha

    def add_download(self, url, data):
        self.download_store[url] = data

    def seed_asset(self, name, size):
        self.next_id += 1
        self.assets[name] = {
            "id": self.next_id, "name": name, "size": size,
            "updated_at": "2026-09-24T11:45:57Z",
        }

    # ── API handlers ─────────────────────────────────────────────────
    def api(self, url, token=None):
        if "/releases/tags/" in url:
            self.tag_fetches += 1
            return {
                "id": 1,
                "assets": [
                    dict(a) for a in self.assets.values()
                ],
            }
        if "/releases?per_page=10" in url:
            repo = url.split("/repos/", 1)[1].split("/releases", 1)[0]
            return self.repos[repo]["releases"]
        if "/commits/" in url:
            repo = url.split("/repos/", 1)[1].split("/commits/", 1)[0]
            tag = unquote(url.rsplit("/commits/", 1)[1])
            return {"sha": self.commits[repo][tag]}
        # Anything else under /repos/… is repo info.
        if "/repos/" in url:
            repo = url.split("/repos/", 1)[1]
            if repo in self.repos:
                r = self.repos[repo]
                return {"description": r["desc"], "owner": {"login": r["owner"]}}
        raise AssertionError(f"unexpected API call: {url}")

    def download(self, url, dest, token=None):
        data = self.download_store.get(url)
        if data is None:
            raise AssertionError(f"unexpected download: {url}")
        dest.write_bytes(data)
        self.downloads.append((url, dest.name))

    # ── sync client server (uploads/deletes, same asset table) ───────
    def sync_api(self, url, data=None, method=None,
                 content_type="application/octet-stream"):
        if method == "DELETE":
            asset_id = int(url.rstrip("/").rsplit("/", 1)[1])
            self.assets = {
                k: v for k, v in self.assets.items() if v["id"] != asset_id
            }
            return None
        if "uploads.github.com" in url and data is not None:
            name = unquote(url.split("name=", 1)[1])
            self.assets.pop(name, None)  # same-name replacement, like the API
            self.next_id += 1
            self.assets[name] = {
                "id": self.next_id, "name": name, "size": len(data),
                "updated_at": "2026-09-24T12:00:00Z",
            }
            return {"id": self.next_id, "name": name, "state": "uploaded"}
        if "/releases" in url and data is not None:
            return {"id": 1}  # create release
        if "/releases/tags/" in url:
            self.tag_fetches += 1
            return {
                "id": 1,
                "assets": [dict(a) for a in self.assets.values()],
            }
        raise AssertionError(f"unexpected sync call: {method} {url}")


# ── Fixtures ─────────────────────────────────────────────────────────────────

SHA_KSIGN = "03a3a9c1234567890123456789012345678901234"
SHA_KSIGN_NEW = "0abc123456789012345678901234567890123456"


def release(tag, prerelease, *assets):
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "published_at": "2026-09-23T06:37:37Z",
        "assets": [
            {"name": n, "size": s, "browser_download_url": u}
            for n, s, u in assets
        ],
    }


def build_fixture(tmp: Path, feather_tag="v2.9.0", ksign_sha=SHA_KSIGN):
    """Set up sources, patches, and the fake GitHub with 4 sources."""
    sources = tmp / "sources.json"
    sources.write_text(json.dumps({"sources": [
        {"name": "Feather", "repo": "claration/Feather", "release_type": "stable"},
        {"name": "Ksign", "repo": "Nyasami/Ksign", "release_type": "prerelease"},
        {"name": "Nuvio Enhanced", "repo": "luqmanfadlli/NuvioMobile-iOS",
         "release_type": "stable",
         "bundle_id_override": "com.nuvio.enhancedmedia",
         "display_name": "Nuvio Enhanced"},
        {"name": "Ferrite", "repo": "Ferrite-iOS/Ferrite", "release_type": "stable"},
    ]}, indent=2), encoding="utf-8")

    fake = FakeGitHub()
    fake.add_repo("claration/Feather", "Feather desc", "claration", [
        release(feather_tag, False, ("Feather.ipa", 1000, "https://up/feather")),
    ])
    fake.add_repo("Nyasami/Ksign", "Ksign desc", "Nyasami", [
        release("beta", True, ("Ksign.ipa", 1000, "https://up/ksign")),
    ])
    fake.set_commit("Nyasami/Ksign", "beta", ksign_sha)
    fake.add_repo("luqmanfadlli/NuvioMobile-iOS", "Enhanced desc", "luqmanfadlli", [
        release("0.5.1-beta", False,
                ("Nuvio-0.5.1-Enhanced.ipa", 1000, "https://up/enhanced")),
    ])
    fake.add_repo("Ferrite-iOS/Ferrite", "Ferrite desc", "Ferrite-iOS", [
        release("v0.7.4", False,
                ("Ferrite-iOS_v0.7.4.ipa.zip", 1000, "https://up/ferrite.zip")),
    ])

    fake.add_download("https://up/feather",
                      make_ipa("thewonderofyou.Feather", "2.9.0", "Feather"))
    fake.add_download("https://up/ksign",
                      make_ipa("nya.asami.ksign", "1.6.1", "Ksign"))
    fake.add_download("https://up/enhanced",
                      make_ipa("com.nuvio.media", "0.5.1", "Nuvio"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Ferrite-iOS_v0.7.4.ipa",
            make_ipa("me.kingbri.Ferrite", "0.7.4", "Ferrite", "26"),
        )
    fake.add_download("https://up/ferrite.zip", buf.getvalue())
    fake.add_download("https://up/balatro",
                      make_ipa("com.example.balatro", "1.0", "Balatro"))

    # Point the whole pipeline at temp dirs + the fake network.
    lib.SOURCES_JSON = sources
    lib.IPAS_DIR = tmp / "ipas"
    lib.ICONS_DIR = tmp / "icons"
    lib.REPO_JSON = tmp / "repo.json"
    lib.CURRENT_RELEASES_JSON = tmp / "current_releases.json"
    lib.github_api = fake.api
    lib.download_file = fake.download
    return fake


def seed_current_releases(fake: FakeGitHub, tmp: Path, feather="2.9.0",
                          ksign="03a3a9c"):
    data = {
        "Feather": {"release_type": "stable", "version": feather},
        "Ksign": {"release_type": "prerelease", "commit": ksign},
        "Nuvio Enhanced": {"release_type": "stable", "version": "0.5.1-beta"},
        "Ferrite": {"release_type": "stable", "version": "0.7.4"},
    }
    path = tmp / "current_releases.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


# ── Tests ────────────────────────────────────────────────────────────────────

def test_naming():
    print("\n── naming helpers ──")
    check(lib.sanitize_version("0.5.1-beta") == "0.5.1", "sanitize beta suffix")
    check(lib.sanitize_version("2.0.0-rc.2") == "2.0.0", "sanitize rc suffix")
    check(lib.sanitize_version("v1.0".lstrip("v")) == "1.0", "sanitize no-op")
    check(lib.sanitize_version("1.15.11_3.7.1") == "1.15.11_3.7.1",
          "sanitize leaves underscore version alone")
    check(lib.ipa_filename("Nuvio Enhanced", version="0.5.1-beta")
          == "Nuvio-Enhanced(rel-0.5.1).ipa", "stable filename")
    check(lib.ipa_filename("Ksign", commit="03a3a9c1234")
          == "Ksign(pre-03a3a9c).ipa", "prerelease filename")
    check(lib.parse_ipa_filename("Nuvio-Enhanced(rel-0.5.1).ipa")
          == ("Nuvio-Enhanced", "rel", "0.5.1"), "parse stable filename")
    check(lib.parse_ipa_filename("Ksign(pre-03a3a9c).ipa")
          == ("Ksign", "pre", "03a3a9c"), "parse prerelease filename")
    check(lib.parse_ipa_filename("My App.ipa") == ("My App", "", ""),
          "parse plain filename")


def test_check_releases():
    print("\n── check_for_updates / check_releases ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_current_releases(fake, tmp)

    fake.seed_asset("Feather(rel-2.9.0).ipa", 1000)
    fake.seed_asset("Ksign(pre-03a3a9c).ipa", 1000)
    fake.seed_asset("Nuvio-Enhanced(rel-0.5.1).ipa", 1000)
    fake.seed_asset("Ferrite(rel-0.7.4).ipa", 1000)

    report = lib.check_for_updates()
    by_name = {e["name"]: e for e in report["entries"]}
    check(not report["any_changed"], "no changes detected when all current")
    check(by_name["Feather"]["asset_in_release"], "Feather asset present")
    check(by_name["Ksign"]["expected_filename"] == "Ksign(pre-03a3a9c).ipa",
          "Ksign tracked by commit")
    check(by_name["Nuvio Enhanced"]["expected_filename"]
          == "Nuvio-Enhanced(rel-0.5.1).ipa", "Enhanced filename sanitized")

    before = (tmp / "current_releases.json").read_text(encoding="utf-8")
    check(run_check() == 0, "run_check exits 0 when unchanged")
    check((tmp / "current_releases.json").read_text(encoding="utf-8") == before,
          "current_releases.json untouched when unchanged")

    # Bump Feather and move Ksign's commit.
    fake.repos["claration/Feather"]["releases"].insert(0, release(
        "v2.10.0", False, ("Feather.ipa", 1000, "https://up/feather")))
    fake.set_commit("Nyasami/Ksign", "beta", SHA_KSIGN_NEW)

    report = lib.check_for_updates()
    by_name = {e["name"]: e for e in report["entries"]}
    check(report["any_changed"], "changes detected after bumps")
    check(by_name["Feather"]["changed"]
          and by_name["Feather"]["latest"] == "2.10.0", "Feather bump detected")
    check(by_name["Ksign"]["changed"]
          and by_name["Ksign"]["latest"] == "0abc123", "Ksign sha move detected")
    check(by_name["Nuvio Enhanced"]["changed"] is False,
          "Enhanced unchanged")

    check(run_check() == 0, "run_check exits 0 after changes")
    written = json.loads((tmp / "current_releases.json").read_text(encoding="utf-8"))
    check(written["Feather"]["version"] == "2.10.0"
          and written["Ksign"]["commit"] == "0abc123",
          "current_releases.json updated")


def test_update_source_unchanged():
    print("\n── update_source (nothing to do) ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_current_releases(fake, tmp)
    fake.seed_asset("Feather(rel-2.9.0).ipa", 1000)
    fake.seed_asset("Ksign(pre-03a3a9c).ipa", 1000)
    fake.seed_asset("Nuvio-Enhanced(rel-0.5.1).ipa", 1000)
    fake.seed_asset("Ferrite(rel-0.7.4).ipa", 1000)

    check(update_source() is False, "update_source returns False (clean exit)")
    check(fake.downloads == [], "nothing was downloaded")
    check(not lib.REPO_JSON.exists(), "repo.json untouched")


def test_update_source_bump_and_migration():
    print("\n── update_source (bump + migration + override + zip) ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    # Feather recorded stale; Enhanced + Ferrite recorded current but their
    # versioned assets are missing (legacy names only) → re-fetch.
    seed_current_releases(fake, tmp, feather="2.8.0")
    fake.repos["claration/Feather"]["releases"].insert(0, release(
        "v2.10.0", False, ("Feather.ipa", 1000, "https://up/feather")))

    fake.seed_asset("Ksign(pre-03a3a9c).ipa", 1000)      # current — skipped
    fake.seed_asset("Nuvio Enhanced.ipa", 999)           # legacy manual-drop bait
    fake.seed_asset("Feather(rel-2.8.0).ipa", 1000)      # old version

    # Seed an existing repo.json so Ksign's entry is preserved as an
    # external app (its asset must stay protected during the sync).
    lib.REPO_JSON.write_text(json.dumps({
        "apps": [{
            "name": "Ksign",
            "bundleIdentifier": "nya.asami.ksign",
            "developerName": "Nyasami",
            "iconURL": "",
            "localizedDescription": "Ksign desc",
            "subtitle": "Ksign desc",
            "tintColor": "3c94fc",
            "category": "utilities",
            "versions": [{
                "downloadURL": "https://github.com/starkayc/iOS/releases/"
                               "download/ipa-assets/Ksign(pre-03a3a9c).ipa",
                "size": 1000,
                "version": "1.6.1",
                "buildVersion": "1",
                "date": "2026-07-14T20:15:52Z",
                "localizedDescription": "",
                "minOSVersion": "16.0",
            }],
            "appPermissions": {},
            "version": "1.6.1",
            "versionDate": "2026-07-14T20:15:52Z",
            "size": 1000,
            "downloadURL": "https://github.com/starkayc/iOS/releases/"
                           "download/ipa-assets/Ksign(pre-03a3a9c).ipa",
        }],
    }, indent=2), encoding="utf-8")

    check(update_source() is True, "update_source did work")

    # The raw downloads: two .ipas plus Ferrite's .ipa.zip (extracted
    # into the final .ipa afterwards).
    downloaded = {name for _, name in fake.downloads}
    check(downloaded == {
        "Feather(rel-2.10.0).ipa",
        "Nuvio-Enhanced(rel-0.5.1).ipa",
        "Ferrite(rel-0.7.4).ipa.zip",
    }, f"only changed/missing apps downloaded (got {sorted(downloaded)})")

    enhanced_meta = lib.extract_metadata(
        lib.IPAS_DIR / "Nuvio-Enhanced(rel-0.5.1).ipa")
    check(enhanced_meta["bundleIdentifier"] == "com.nuvio.enhancedmedia",
          "bundle-ID override applied after download")

    recorded = lib.load_current_releases()
    check(recorded["Feather"]["version"] == "2.10.0",
          "current_releases.json records the new Feather version")

    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    apps = {a["bundleIdentifier"]: a for a in repo["apps"]}
    check(apps["thewonderofyou.Feather"]["downloadURL"]
          .endswith("Feather(rel-2.10.0).ipa"), "Feather URL versioned")
    check(apps["com.nuvio.enhancedmedia"]["name"] == "Nuvio Enhanced",
          "Enhanced app named via display_name")
    check(apps["com.nuvio.enhancedmedia"]["downloadURL"]
          .endswith("Nuvio-Enhanced(rel-0.5.1).ipa"), "Enhanced URL dashed + versioned")
    check(apps["me.kingbri.Ferrite"]["downloadURL"]
          .endswith("Ferrite(rel-0.7.4).ipa"), "Ferrite came through the zip fallback")
    check("nya.asami.ksign" in apps,
          "Ksign preserved as an external app (not re-downloaded)")
    return fake


def test_add_custom_ipa(fake: FakeGitHub):
    print("\n── add_custom_ipa + generate_repo ──")
    add_custom_ipa("Balatro", "1.0", "https://up/balatro",
                   description="A card game", subtitle="")
    check((lib.IPAS_DIR / "Balatro(rel-1.0).ipa").exists(),
          "custom IPA named Balatro(rel-1.0).ipa")
    meta = lib.load_custom_meta()
    check(meta.get("Balatro(rel-1.0).ipa", {}).get("description")
          == "A card game", "sidecar records the description")

    generate_repo(fetch_report={
        "release_dates": {},
        "repo_descriptions": {},
        "developer_names": {},
        "manual_names": set(),
        "display_names": {},
    }, cleanup_losers=True)
    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    apps = {a["bundleIdentifier"]: a for a in repo["apps"]}
    balatro = apps.get("com.example.balatro")
    check(balatro is not None, "Balatro added to repo.json")
    check(balatro and balatro["name"] == "Balatro",
          "display name comes from the file, not the IPA")
    check(balatro and balatro["localizedDescription"] == "A card game",
          "description from the sidecar")
    check(balatro and balatro["subtitle"] == "",
          "blank subtitle stays blank")
    check(balatro and balatro["downloadURL"].endswith("Balatro(rel-1.0).ipa"),
          "custom URL versioned")
    return balatro


def test_sync_release(fake: FakeGitHub):
    print("\n── sync_release (real client on the fake server) ──")
    real = sr.lib.GitHubRelease("test")
    real.api = fake.sync_api

    rc = sr.sync_release("test", client=real)
    check(rc == 0, "sync_release exited 0")

    final = set(fake.assets)
    check(final == {
        "Balatro(rel-1.0).ipa",
        "Feather(rel-2.10.0).ipa",
        "Ferrite(rel-0.7.4).ipa",
        "Ksign(pre-03a3a9c).ipa",
        "Nuvio-Enhanced(rel-0.5.1).ipa",
    }, f"release ends with exactly the expected assets (got {sorted(final)})")
    check("Nuvio Enhanced.ipa" not in final
          and "Feather(rel-2.8.0).ipa" not in final,
          "legacy and old-version assets deleted")
    check(fake.tag_fetches >= 2,
          f"client re-fetched after mutations ({fake.tag_fetches} fetches)")


def main():
    print("=" * 60)
    print("  AltStore pipeline selftest")
    print("=" * 60)

    test_naming()
    test_check_releases()
    test_update_source_unchanged()
    fake = test_update_source_bump_and_migration()
    test_add_custom_ipa(fake)
    test_sync_release(fake)

    print(f"\n✅ ALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    main()
