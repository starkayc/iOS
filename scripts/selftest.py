#!/usr/bin/env python3
"""
End-to-end selftest for the AltStore source pipeline.

Runs the real pipeline code (check_for_updates, update_source,
generate_repo, add_custom_ipa, sync_release) against the stateful fake
GitHub API in selftest_fake.py and against synthetic IPAs.  The real
GitHubRelease client is included, so a cache-staleness regression fails the
test.

The fake mirrors GitHub's asset-name sanitization, turning unsafe
characters such as spaces and parentheses into dots, so a name-
normalization regression fails too.

Run from the repo root:

    python scripts/selftest.py

Every test uses a throwaway temp directory, so nothing in the repo is
modified.
"""

import io
import http.client
import json
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import altstore_lib as lib  # noqa: E402
import cli_common as cli  # noqa: E402
import ipa  # noqa: E402
import release as rel  # noqa: E402
import sync_release as sr  # noqa: E402
from add_custom_ipa import run_custom_upload  # noqa: E402
from check_releases import run_check  # noqa: E402
from generate_repo import generate_repo  # noqa: E402
from update_source import update_source, update_source_report  # noqa: E402
from selftest_fake import (  # noqa: E402
    FakeGitHub,
    FakeReleaseFactory,
    SHA_KSIGN,
    SHA_KSIGN_NEW,
    app_entry,
    asset_url,
    build_fixture,
    crushed_icon_png,
    make_ipa,
    make_ipa_with_icon,
    plain_png,
    png_chunks,
    release,
    seed_current_releases,
    seed_repo_json,
    seed_up_to_date,
)

cli.setup_stdio()

_RealGithubApi = lib.github_api  # keep before any patching
_RealDownloadFile = lib.download_file  # keep before any patching
_RealGitHubRelease = rel.GitHubRelease  # ditto, the tests replace it



PASS = 0


def check(cond, msg):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ FAIL: {msg}")
        raise AssertionError(msg)



def test_naming():
    print("\n── naming helpers ──")
    check(lib.sanitize_version("0.5.1-beta") == "0.5.1", "sanitize beta suffix")
    check(lib.sanitize_version("2.0.0-rc.2") == "2.0.0", "sanitize rc suffix")
    check(lib.sanitize_version("1.15.11_3.7.1") == "1.15.11_3.7.1",
          "sanitize leaves underscore version alone")
    check(lib.sanitize_version("release-1.4.1") == "1.4.1",
          "sanitize drops a leading release prefix")
    check(lib.ipa_filename("qBitControl", version="release-1.4.1")
          == "qBitControl.rel-1.4.1.ipa",
          "release prefix not baked into the file name")
    check(lib.ipa_filename("Nuvio Enhanced", version="0.5.1-beta")
          == "Nuvio-Enhanced.rel-0.5.1.ipa", "stable filename (dot form)")
    check(lib.ipa_filename("Ksign", commit="03a3a9c1234")
          == "Ksign.pre-03a3a9c.ipa", "prerelease filename (dot form)")
    check(lib.parse_ipa_filename("Nuvio-Enhanced.rel-0.5.1.ipa")
          == ("Nuvio-Enhanced", "rel", "0.5.1"), "parse stable filename")
    check(lib.parse_ipa_filename("Ksign.pre-03a3a9c.ipa")
          == ("Ksign", "pre", "03a3a9c"), "parse prerelease filename")
    check(lib.parse_ipa_filename("My App.ipa") == ("My App", "", ""),
          "parse plain filename")
    check(lib.github_asset_name("Feather(rel-2.9.0).ipa")
          == "Feather.rel-2.9.0.ipa", "github sanitization: parens → dot")
    check(lib.github_asset_name("Nuvio Enhanced.ipa")
          == "Nuvio.Enhanced.ipa", "github sanitization: space → dot")
    check(lib.github_asset_name("Feather.rel-2.9.0.ipa")
          == "Feather.rel-2.9.0.ipa", "github sanitization: safe name unchanged")
    check(lib.canonical_filename("App(1).ipa") == "App.1.ipa",
          "canonical_filename matches GitHub's stored asset name")
    check(lib.canonical_filename("My App.ipa") == "My-App.ipa",
          "a space becomes a dash, not a dot")
    check(lib.safe_stem("com.example.app") == "com.example.app",
          "safe_stem leaves a normal bundle ID alone")
    check(lib.safe_stem("../../escape") == "_.._escape",
          "safe_stem drops separators and leading dots")
    check(lib.safe_stem("") == "app", "safe_stem never returns an empty name")
    check(lib.ipa_filename("App", version="1.0/../x") == "App.rel-1.0_.._x.ipa",
          "a hostile version cannot escape ipas/")
    check(lib.version_from_tag("v2.9.0") == "2.9.0",
          "version_from_tag strips a real v prefix")
    check(lib.version_from_tag("version-2.0") == "version-2.0",
          "a tag that merely starts with 'v' keeps its name")
    check(lib.version_from_tag("2026.1005.0-lazer") == "2026.1005.0-lazer",
          "a tag without a v prefix is untouched")


def test_ipa_without_dir_entries():
    print("\n── IPAs without zip directory entries ──")
    # Some re-zipped IPAs, like the real Balatro upload, omit the
    # "Payload/*.app/" directory entry.  Extraction must still work.
    tmp = Path(tempfile.mkdtemp())
    ipa_path = tmp / "NoDirs.ipa"
    ipa_path.write_bytes(make_ipa("com.test.nodirs", with_dir_entry=False))
    meta = ipa.extract_metadata(ipa_path)
    check(meta is not None, "metadata extracted without a directory entry")
    check(meta and meta["bundleIdentifier"] == "com.test.nodirs",
          "bundle ID correct")
    check(ipa.patch_bundle_id(ipa_path, "com.test.nodirs.patched"),
          "bundle-ID patch works on dir-entry-less IPAs")
    check(ipa.extract_metadata(ipa_path)["bundleIdentifier"]
          == "com.test.nodirs.patched", "patched bundle ID readable")


def test_crushed_icon_png():
    print("\n── crushed (CgBI) icons: rebuild, find, extract ──")
    chunks = png_chunks(ipa._uncrush_png(crushed_icon_png()))
    check(b"CgBI" not in chunks and b"IHDR" in chunks and b"IDAT" in chunks,
          "the CgBI chunk is gone and the image chunks are rebuilt")
    check(zlib.decompress(chunks[b"IDAT"]) == b"\x00\x12\x34\x00\x56\x78",
          "both sub-byte rows survive the round trip")
    check(chunks[b"PLTE"] == bytes([3, 2, 1, 6, 5, 4]),
          "the indexed palette's red and blue are swapped")
    plain = plain_png()
    check(ipa._uncrush_png(plain) == plain, "a normal PNG is returned as-is")

    tmp = Path(tempfile.mkdtemp())
    ipa_path = tmp / "Icon.ipa"
    ipa_path.write_bytes(make_ipa_with_icon())
    meta = ipa.extract_metadata(ipa_path)
    check(meta["icon_paths"] == ["Payload/App.app/AppIcon60x60@2x.png"],
          "find_icon_files resolves the plist's icon name to the real file")

    icons_dir = tmp / "icons"
    icons_dir.mkdir()
    name = ipa.extract_icon(
        ipa_path, meta["icon_paths"], meta["bundleIdentifier"], icons_dir
    )
    check(name == "com.test.icon.png", "icon saved under the bundle ID")
    saved = (icons_dir / name).read_bytes()
    check(saved[:8] == b"\x89PNG\r\n\x1a\n" and b"CgBI" not in saved,
          "the published icon is an uncrushed PNG")

    escaped = ipa.extract_icon(
        ipa_path, meta["icon_paths"], "../../escape", icons_dir
    )
    check(
        escaped is not None
        and "/" not in escaped
        and (icons_dir / escaped).exists()
        and not (tmp / "escape.png").exists(),
        f"a traversing bundle ID stays inside icons/ (got {escaped})",
    )


def test_prune_stale_icons():
    print("\n── generate_repo: stale icons are pruned; dry run writes nothing ──")
    tmp = Path(tempfile.mkdtemp())
    lib.SOURCES_JSON = tmp / "sources.json"
    lib.IPAS_DIR = tmp / "ipas"
    lib.ICONS_DIR = tmp / "icons"
    lib.REPO_JSON = tmp / "repo.json"
    lib.CURRENT_RELEASES_JSON = tmp / "current_releases.json"
    lib.IPAS_DIR.mkdir()
    lib.ICONS_DIR.mkdir()

    (lib.IPAS_DIR / "App.rel-1.0.ipa").write_bytes(
        make_ipa_with_icon("com.test.prune", "1.0")
    )
    (lib.ICONS_DIR / "orphan.png").write_bytes(plain_png())
    (lib.ICONS_DIR / "placeholder.png").write_bytes(plain_png())

    check(generate_repo() is True, "repo.json built")
    check((lib.ICONS_DIR / "com.test.prune.png").exists(),
          "the icon the new entry points at is kept")
    check((lib.ICONS_DIR / "placeholder.png").exists(),
          "the placeholder fallback is kept")
    check(not (lib.ICONS_DIR / "orphan.png").exists(),
          "an icon no entry points at is removed")

    # A dry run that would prune still writes nothing and deletes no icon.
    repo_before = lib.REPO_JSON.read_text(encoding="utf-8")
    (lib.ICONS_DIR / "orphan.png").write_bytes(plain_png())
    (lib.IPAS_DIR / "App.rel-1.0.ipa").write_bytes(
        make_ipa_with_icon("com.test.prune", "2.0")
    )
    check(generate_repo(dry_run=True) is True, "dry run reports a change")
    check(lib.REPO_JSON.read_text(encoding="utf-8") == repo_before,
          "a dry run does not write repo.json")
    check((lib.ICONS_DIR / "orphan.png").exists(),
          "a dry run does not delete a stale icon")


def test_check_releases():
    print("\n── check_for_updates / check_releases ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_current_releases(tmp)

    fake.seed_asset("Feather.rel-2.9.0.ipa", 1000)
    fake.seed_asset("Ksign.pre-03a3a9c.ipa", 1000)
    fake.seed_asset("Nuvio-Enhanced.rel-0.5.1.ipa", 1000)
    fake.seed_asset("Ferrite.rel-0.7.4.ipa", 1000)

    report = lib.check_for_updates()
    by_name = {e["name"]: e for e in report["entries"]}
    check(not report["any_changed"], "no changes detected when all current")
    check(by_name["Feather"]["asset_in_release"], "Feather asset present")
    check(by_name["Ksign"]["expected_filename"] == "Ksign.pre-03a3a9c.ipa",
          "Ksign tracked by commit")
    check(by_name["Nuvio Enhanced"]["expected_filename"]
          == "Nuvio-Enhanced.rel-0.5.1.ipa", "Enhanced filename sanitized")

    before = (tmp / "current_releases.json").read_text(encoding="utf-8")
    check(run_check() == 0, "run_check exits 0 when unchanged")
    check((tmp / "current_releases.json").read_text(encoding="utf-8") == before,
          "current_releases.json untouched when unchanged")

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
    seed_current_releases(tmp)
    fake.seed_asset("Feather.rel-2.9.0.ipa", 1000)
    fake.seed_asset("Ksign.pre-03a3a9c.ipa", 1000)
    fake.seed_asset("Nuvio-Enhanced.rel-0.5.1.ipa", 1000)
    fake.seed_asset("Ferrite.rel-0.7.4.ipa", 1000)

    # The committed repo.json must already reference the versioned
    # assets, otherwise the updater rebuilds the entries.
    seed_repo_json(tmp, [
        app_entry("thewonderofyou.Feather", "Feather",
                  asset_url("Feather.rel-2.9.0.ipa")),
        app_entry("nya.asami.ksign", "Ksign",
                  asset_url("Ksign.pre-03a3a9c.ipa")),
        app_entry("com.nuvio.enhancedmedia", "Nuvio Enhanced",
                  asset_url("Nuvio-Enhanced.rel-0.5.1.ipa")),
        app_entry("me.kingbri.Ferrite", "Ferrite",
                  asset_url("Ferrite.rel-0.7.4.ipa")),
    ])
    repo_before = lib.REPO_JSON.read_text(encoding="utf-8")

    check(update_source() is False, "update_source returns False (clean exit)")
    check(fake.downloads == [], "nothing was downloaded")
    check(lib.REPO_JSON.read_text(encoding="utf-8") == repo_before,
          "repo.json untouched")


def test_update_source_bump_and_recovery():
    print("\n── update_source (bump + recovery + override + zip) ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    # Feather is recorded stale.  Enhanced and Ferrite are recorded current
    # with their assets present, but repo.json still references legacy names,
    # which is what a scheme migration or a failed run leaves behind.  All
    # three re-fetch.
    seed_current_releases(tmp, feather="2.8.0")
    fake.repos["claration/Feather"]["releases"].insert(0, release(
        "v2.10.0", False, ("Feather.ipa", 1000, "https://up/feather")))

    fake.seed_asset("Ksign.pre-03a3a9c.ipa", 1000)
    fake.seed_asset("Nuvio Enhanced.ipa", 999)
    fake.seed_asset("Feather.rel-2.8.0.ipa", 1000)

    seed_repo_json(tmp, [
        app_entry("nya.asami.ksign", "Ksign",
                  asset_url("Ksign.pre-03a3a9c.ipa")),
        app_entry("thewonderofyou.Feather", "Feather",
                  asset_url("Feather.ipa")),
        app_entry("com.nuvio.enhancedmedia", "Nuvio Enhanced",
                  asset_url("Nuvio%20Enhanced.ipa")),
        app_entry("me.kingbri.Ferrite", "Ferrite",
                  asset_url("Ferrite.ipa")),
    ])

    check(update_source() is True, "update_source did work")

    # The raw downloads: two .ipas plus Ferrite's .ipa.zip (extracted
    # into the final .ipa afterwards).
    downloaded = {name for _, name in fake.downloads}
    check(downloaded == {
        "Feather.rel-2.10.0.ipa",
        "Nuvio-Enhanced.rel-0.5.1.ipa",
        "Ferrite.rel-0.7.4.ipa.zip",
    }, f"only changed/missing apps downloaded (got {sorted(downloaded)})")

    enhanced_meta = ipa.extract_metadata(
        lib.IPAS_DIR / "Nuvio-Enhanced.rel-0.5.1.ipa")
    check(enhanced_meta["bundleIdentifier"] == "com.nuvio.enhancedmedia",
          "bundle-ID override applied after download")

    recorded = lib.load_current_releases()
    check(recorded["Feather"]["version"] == "2.10.0",
          "current_releases.json records the new Feather version")

    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    apps = {a["bundleIdentifier"]: a for a in repo["apps"]}
    check(apps["thewonderofyou.Feather"]["downloadURL"]
          .endswith("Feather.rel-2.10.0.ipa"), "Feather URL versioned")
    check(apps["com.nuvio.enhancedmedia"]["name"] == "Nuvio Enhanced",
          "Enhanced app named via display_name")
    check(apps["com.nuvio.enhancedmedia"]["downloadURL"]
          .endswith("Nuvio-Enhanced.rel-0.5.1.ipa"),
          "Enhanced URL dashed + versioned")
    check(apps["me.kingbri.Ferrite"]["downloadURL"]
          .endswith("Ferrite.rel-0.7.4.ipa"), "Ferrite came through the zip fallback")
    check("nya.asami.ksign" in apps,
          "Ksign preserved as an external app (not re-downloaded)")
    return fake


def test_add_custom_ipa(fake: FakeGitHub):
    print("\n── add_custom_ipa (download + validate + generate + sync) ──")
    rel.GitHubRelease = FakeReleaseFactory(fake)

    rc = run_custom_upload("Balatro", "1.0", "https://up/balatro",
                           description="A card game", subtitle="",
                           token="test")
    check(rc == 0, "run_custom_upload exited 0")
    check((lib.IPAS_DIR / "Balatro.rel-1.0.ipa").exists(),
          "custom IPA named Balatro.rel-1.0.ipa")
    meta = lib.load_custom_meta()
    check(meta.get("Balatro.rel-1.0.ipa", {}).get("description")
          == "A card game", "sidecar records the description")

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
    check(balatro and balatro["downloadURL"].endswith("Balatro.rel-1.0.ipa"),
          "custom URL versioned")
    check("Balatro.rel-1.0.ipa" in fake.assets,
          "the asset was uploaded to the release")
    # A custom upload must never delete other apps' assets, because its
    # repo.json view can lag behind the other workflows.  Cleanup belongs to
    # the full sync in the update-source workflow.
    check("Nuvio Enhanced.ipa" in fake.assets
          and "Feather.rel-2.8.0.ipa" in fake.assets,
          "custom upload leaves other assets alone")
    return balatro


def test_invalid_custom_ipa():
    print("\n── add_custom_ipa rejects an unreadable IPA ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    rel.GitHubRelease = FakeReleaseFactory(fake)
    fake.add_download("https://up/bad", b"this is not a zip file")
    fake.seed_asset("existing.ipa", 123)

    rc = run_custom_upload("BadApp", "1.0", "https://up/bad", token="test")
    check(rc == 1, "run_custom_upload fails on an unreadable IPA")
    check(set(fake.assets) == {"existing.ipa"},
          "nothing was uploaded for the bad IPA")
    check("BadApp" not in (lib.REPO_JSON.read_text(encoding="utf-8")
          if lib.REPO_JSON.exists() else ""),
          "repo.json has no BadApp entry")


def test_sync_release(fake: FakeGitHub):
    print("\n── sync_release (real client on the fake server) ──")
    real = rel.GitHubRelease("test")
    real.api = fake.sync_api

    rc = sr.sync_release("test", client=real)
    check(rc == 0, "sync_release exited 0")

    final = set(fake.assets)
    check(final == {
        "Balatro.rel-1.0.ipa",
        "Feather.rel-2.10.0.ipa",
        "Ferrite.rel-0.7.4.ipa",
        "Ksign.pre-03a3a9c.ipa",
        "Nuvio-Enhanced.rel-0.5.1.ipa",
    }, f"release ends with exactly the expected assets (got {sorted(final)})")
    check("Nuvio Enhanced.ipa" not in final
          and "Feather.rel-2.8.0.ipa" not in final,
          "legacy and old-version assets deleted")
    check(fake.tag_fetches >= 2,
          f"client re-fetched after mutations ({fake.tag_fetches} fetches)")


def test_http_error_verbose_no_retry():
    print("\n── HTTP errors: verbose, no 4xx retry, 5xx retried ──")
    calls = {"n": 0}
    orig_urlopen = lib.urllib.request.urlopen
    orig_sleep = lib.time.sleep

    def raise_404(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", None,
            io.BytesIO(b'{"message":"Not Found"}'),
        )

    lib.urllib.request.urlopen = raise_404
    lib.time.sleep = lambda _s: None
    try:
        try:
            _RealGithubApi("https://api.github.com/repos/x/y")
            check(False, "404 should raise GitHubError")
        except lib.GitHubError as e:
            check(e.status == 404, "GitHubError carries the 404 status")
            check("404" in str(e) and "Not Found" in str(e),
                  "error message is verbose")
            check("message" in e.body, "GitHub's response body is captured")
        check(calls["n"] == 1, f"4xx is not retried (attempts={calls['n']})")

        calls["n"] = 0

        def raise_500(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 500, "Server Error", None, io.BytesIO(b"boom")
            )

        lib.urllib.request.urlopen = raise_500
        try:
            _RealGithubApi("https://api.github.com/repos/x/y")
            check(False, "500 should raise GitHubError")
        except lib.GitHubError as e:
            check(e.status == 500, "5xx surfaced as GitHubError")
        check(calls["n"] == 3, f"5xx is retried (attempts={calls['n']})")
    finally:
        lib.urllib.request.urlopen = orig_urlopen
        lib.time.sleep = orig_sleep


def test_real_release_client_api():
    print("\n── GitHubRelease.api sends the token on the real code path ──")
    seen: list[dict] = []
    orig_urlopen = lib.urllib.request.urlopen

    class Resp:
        def read(self, n=-1):
            return b'{"id": 7}'
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def spy(req, timeout=None):
        seen.append(dict(req.headers))
        return Resp()

    lib.urllib.request.urlopen = spy
    try:
        client = _RealGitHubRelease("SECRET")
        check(client.api(f"{lib.API_BASE}/repos/x/y") == {"id": 7},
              "real api() returns the parsed JSON")
        check(seen and seen[0].get("Authorization") == "Bearer SECRET",
              "real api() sends the token (see _auth in altstore_lib)")
    finally:
        lib.urllib.request.urlopen = orig_urlopen


def test_redirects_never_carry_the_token_off_github():
    print("\n── redirects: the token is dropped when the host changes ──")
    handler = lib._SafeRedirect()

    def headers_after(url: str) -> dict:
        req = urllib.request.Request(
            "https://api.github.com/repos/x/y",
            headers={"Authorization": "Bearer SECRET"},
        )
        new_req = handler.redirect_request(req, None, 302, "Found", {}, url)
        return dict(new_req.headers)

    check(
        headers_after("https://github.com/o/r/releases/download/t/a.ipa")
        .get("Authorization") == "Bearer SECRET",
        "a redirect that stays on GitHub keeps the token",
    )
    check(
        "Authorization" not in headers_after("https://evil.example/x.ipa"),
        "a redirect to a foreign host drops the token",
    )


def test_same_version_rebuild():
    print("\n── generate_repo: same-version rebuild is detected ──")
    tmp = Path(tempfile.mkdtemp())
    lib.SOURCES_JSON = tmp / "sources.json"
    lib.IPAS_DIR = tmp / "ipas"
    lib.ICONS_DIR = tmp / "icons"
    lib.REPO_JSON = tmp / "repo.json"
    lib.CURRENT_RELEASES_JSON = tmp / "current_releases.json"
    lib.IPAS_DIR.mkdir()

    ipa = lib.IPAS_DIR / "App.rel-1.0.ipa"
    ipa.write_bytes(make_ipa("com.test.rebuild", "1.0", "App", build="1"))
    check(generate_repo() is True, "initial repo.json built")
    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    apps = {a["bundleIdentifier"]: a for a in repo["apps"]}
    check(apps["com.test.rebuild"]["versions"][0]["buildVersion"] == "1",
          "build 1 recorded")
    before = lib.REPO_JSON.read_text(encoding="utf-8")

    # The version string is the same but the build changed, which must still
    # be picked up.
    ipa.write_bytes(make_ipa("com.test.rebuild", "1.0", "App", build="2"))
    check(generate_repo() is True, "same-version rebuild detected")
    check(lib.REPO_JSON.read_text(encoding="utf-8") != before,
          "repo.json rewritten")
    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    apps = {a["bundleIdentifier"]: a for a in repo["apps"]}
    check(apps["com.test.rebuild"]["versions"][0]["buildVersion"] == "2",
          "new build recorded")

    check(generate_repo() is False, "a genuinely unchanged run is a no-op")


def test_release_version_wins_only_when_it_is_newer():
    print("\n── generate_repo: a constant IPA version can't hide an update ──")
    tmp = Path(tempfile.mkdtemp())
    lib.SOURCES_JSON = tmp / "sources.json"
    lib.IPAS_DIR = tmp / "ipas"
    lib.ICONS_DIR = tmp / "icons"
    lib.REPO_JSON = tmp / "repo.json"
    lib.CURRENT_RELEASES_JSON = tmp / "current_releases.json"
    lib.IPAS_DIR.mkdir()

    (lib.IPAS_DIR / "Osu.rel-2026.1005.0-lazer.ipa").write_bytes(
        make_ipa("sh.ppy.osulazer", "1.0", "osu!", build="2026.1005.0")
    )
    (lib.IPAS_DIR / "Apollo-Reborn.rel-1.15.11_3.8.5.ipa").write_bytes(
        make_ipa("com.christianselig.Apollo", "3.8.5", "Apollo")
    )
    check(generate_repo({
        "Osu.rel-2026.1005.0-lazer.ipa": {"version": "2026.1005.0-lazer"},
        "Apollo-Reborn.rel-1.15.11_3.8.5.ipa": {"version": "1.15.11_3.8.5"},
    }) is True, "repo.json built")

    apps = {
        app["bundleIdentifier"]: app
        for app in json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))["apps"]
    }
    check(apps["sh.ppy.osulazer"]["version"] == "2026.1005.0-lazer"
          and apps["sh.ppy.osulazer"]["versions"][0]["version"]
          == "2026.1005.0-lazer",
          "the release version is published when the IPA's own is stale")
    check(apps["com.christianselig.Apollo"]["version"] == "3.8.5",
          "a tag older than the IPA's own version is ignored")


def test_subtitle_never_repeats_the_description():
    print("\n── generate_repo: a copied-in subtitle is dropped ──")
    tmp = Path(tempfile.mkdtemp())
    lib.SOURCES_JSON = tmp / "sources.json"
    lib.IPAS_DIR = tmp / "ipas"
    lib.ICONS_DIR = tmp / "icons"
    lib.REPO_JSON = tmp / "repo.json"
    lib.CURRENT_RELEASES_JSON = tmp / "current_releases.json"
    lib.IPAS_DIR.mkdir()

    (lib.IPAS_DIR / "App.rel-1.0.ipa").write_bytes(
        make_ipa("com.test.subtitle", "1.0", "App")
    )
    check(generate_repo() is True, "repo.json built")
    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    repo["apps"][0]["localizedDescription"] = "About text"
    repo["apps"][0]["subtitle"] = "About text"
    lib.REPO_JSON.write_text(json.dumps(repo, indent=2), encoding="utf-8")

    check(generate_repo() is True, "the duplicate counts as a change")
    app = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))["apps"][0]
    check(app["subtitle"] == "", "the duplicated subtitle is dropped")
    check(app["localizedDescription"] == "About text",
          "the description itself is kept")


def test_update_source_missing_asset():
    print("\n── update_source: release without an IPA asset is skipped ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    # Feather publishes v2.10.0 but ships no .ipa (like ppy/osu, which
    # attaches the iOS build later or not at all).  The run must finish
    # cleanly and leave Feather's recorded version alone.
    seed_current_releases(tmp, feather="2.9.0")
    fake.repos["claration/Feather"]["releases"].insert(0, release(
        "v2.10.0", False, ("Feather-installer.exe", 1000, "https://up/nope")))

    fake.seed_asset("Ksign.pre-03a3a9c.ipa", 1000)
    fake.seed_asset("Nuvio-Enhanced.rel-0.5.1.ipa", 1000)
    fake.seed_asset("Ferrite.rel-0.7.4.ipa", 1000)

    seed_repo_json(tmp, [
        app_entry("thewonderofyou.Feather", "Feather",
                  asset_url("Feather.rel-2.9.0.ipa")),
        app_entry("nya.asami.ksign", "Ksign",
                  asset_url("Ksign.pre-03a3a9c.ipa")),
        app_entry("com.nuvio.enhancedmedia", "Nuvio Enhanced",
                  asset_url("Nuvio-Enhanced.rel-0.5.1.ipa")),
        app_entry("me.kingbri.Ferrite", "Ferrite",
                  asset_url("Ferrite.rel-0.7.4.ipa")),
    ])

    worked, failures = update_source_report()
    check(failures == [], f"no failure reported (got {failures})")
    check(worked is False, "nothing was downloaded")
    check(fake.downloads == [], "no asset was fetched")
    check(lib.load_current_releases()["Feather"]["version"] == "2.9.0",
          "recorded version left untouched for the next run")


def test_failed_download_keeps_state():
    print("\n── update_source: failed download does not advance state ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    # Feather is recorded stale and will fail to download.  Ferrite is stale
    # too, so a *successful* download happens and current_releases.json gets
    # written.  That proves the failure path, not the early return, is what
    # preserves Feather's version.
    seed_up_to_date(fake, tmp, feather="2.8.0", ferrite="0.7.3")
    fake.repos["claration/Feather"]["releases"].insert(0, release(
        "v2.10.0", False, ("Feather.ipa", 1000, "https://up/feather")))
    fake.fail_downloads["https://up/feather"] = "connection reset by peer"

    worked, failures = update_source_report()
    check("Feather" in failures, "the failed app is reported")
    check(worked is True, "the successful app still produced work")
    recorded = lib.load_current_releases()
    check(recorded["Feather"]["version"] == "2.8.0",
          "failed app's recorded version was NOT advanced")
    check(recorded["Ferrite"]["version"] == "0.7.4",
          "successful app's recorded version WAS advanced")


def test_failed_api_never_ingests_a_stale_asset():
    print("\n── update_source: an API error must not publish a stale asset ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_up_to_date(fake, tmp, feather="2.10.0")
    # A leftover older Feather build on the release, and Feather's release
    # lookup fails this run (rate limit / 5xx / renamed repo).
    fake.seed_asset("Feather.rel-2.9.0.ipa", 1000)
    fake.add_download(asset_url("Feather.rel-2.9.0.ipa"),
                      make_ipa("thewonderofyou.Feather", "2.9.0", "Feather"))
    real_api = fake.api

    def flaky(url, token=None):
        if "claration/Feather" in url and "/releases" in url:
            raise RuntimeError("503 after retries")
        return real_api(url, token)

    lib.github_api = flaky
    worked, failures = update_source_report()
    check(fake.downloads == [],
          f"the stale asset was left alone (got {fake.downloads})")
    check(worked is False, "nothing was published")
    urls = {
        app["bundleIdentifier"]: app["downloadURL"].rsplit("/", 1)[-1]
        for app in json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))["apps"]
    }
    check(urls["thewonderofyou.Feather"] == "Feather.rel-2.10.0.ipa",
          f"repo.json still points at the current build "
          f"(got {urls['thewonderofyou.Feather']})")


def test_download_file_streams_to_disk():
    print("\n── download_file streams to disk (atomic, no partial) ──")
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "payload.bin"
    payload = b"\x00\x01\x02" * ((1024 * 1024 // 3) + 1)
    orig_urlopen = lib.urllib.request.urlopen
    orig_sleep = lib.time.sleep

    class FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def read(self, n=-1):
            return self._buf.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._buf.close()

    lib.urllib.request.urlopen = lambda req, timeout=None: FakeResp(payload)
    lib.time.sleep = lambda _s: None
    try:
        _RealDownloadFile("https://example.com/big", dest)
        check(dest.read_bytes() == payload, "streamed file matches payload")
        check(not (tmp / "payload.bin.part").exists(),
              ".part cleaned up on success")

        # A mid-stream failure must leave no partial file behind.
        dest2 = tmp / "fail.bin"

        class FailingResp:
            def read(self, n=-1):
                raise OSError("connection reset")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        lib.urllib.request.urlopen = lambda req, timeout=None: FailingResp()
        try:
            _RealDownloadFile("https://example.com/fail", dest2)
            check(False, "a failed download should raise")
        except lib.GitHubError:
            pass
        check(not dest2.exists(), "no partial file left on failure")
        check(not (tmp / "fail.bin.part").exists(), ".part removed on failure")

        dest3 = tmp / "short.bin"
        attempts = []

        class TruncatedResp(FakeResp):
            def read(self, n=-1):
                raise http.client.IncompleteRead(b"x" * 10, 90)

        class TruncateOnce:
            def __init__(self):
                self.n = 0

            def __call__(self, req, timeout=None):
                self.n += 1
                attempts.append(self.n)
                return TruncatedResp(payload) if self.n == 1 else FakeResp(payload)

        lib.urllib.request.urlopen = TruncateOnce()
        _RealDownloadFile("https://example.com/short", dest3)
        check(len(attempts) == 2,
              f"a truncated body is retried (attempts={len(attempts)})")
        check(dest3.read_bytes() == payload, "the retry wrote the whole body")

        dest3b = tmp / "short2.bin"
        lib.urllib.request.urlopen = lambda req, timeout=None: TruncatedResp(
            payload
        )
        try:
            _RealDownloadFile("https://example.com/short2", dest3b)
            check(False, "a body that always truncates should raise")
        except lib.GitHubError as ex:
            check("IncompleteRead" in str(ex),
                  "the real exception is reported")
        check(not dest3b.exists(), "no truncated file left behind")
        check(not (tmp / "short2.bin.part").exists(),
              ".part removed for a truncated body")

        dest4 = tmp / "want.bin"
        lib.urllib.request.urlopen = lambda req, timeout=None: FakeResp(
            payload[:50]
        )
        try:
            _RealDownloadFile("https://example.com/want", dest4, None,
                              len(payload))
            check(False, "a body shorter than expected should raise")
        except lib.GitHubError as ex:
            check("short download" in str(ex),
                  "body vs expected size is reported")
        check(not dest4.exists(), "no partial file left behind")

        dest5 = tmp / "exact.bin"
        lib.urllib.request.urlopen = lambda req, timeout=None: FakeResp(payload)
        _RealDownloadFile("https://example.com/exact", dest5, None,
                          len(payload))
        check(dest5.read_bytes() == payload, "expected size honoured")
    finally:
        lib.urllib.request.urlopen = orig_urlopen
        lib.time.sleep = orig_sleep


def test_ipa_problem_detects_junk():
    print("\n── ipa_problem: junk, truncation, no bundle ──")
    tmp = Path(tempfile.mkdtemp())
    good = tmp / "good.ipa"
    good.write_bytes(make_ipa("com.example.good", "1.0", "Good"))
    check(ipa.ipa_problem(good) == "", "a real IPA passes")

    junk = tmp / "junk.ipa"
    junk.write_bytes(b"<!DOCTYPE html>\n<html>not an ipa</html>")
    check("not a zip archive" in ipa.ipa_problem(junk),
          "non-zip body is named as such")

    cut = tmp / "cut.ipa"
    raw = good.read_bytes()
    cut.write_bytes(raw[: len(raw) // 2])
    check("broken zip" in ipa.ipa_problem(cut), "truncated zip is reported")

    nobundle = tmp / "nobundle.ipa"
    with zipfile.ZipFile(nobundle, "w") as zf:
        zf.writestr("Payload.txt", "hi")
    check("no Payload/*.app" in ipa.ipa_problem(nobundle),
          "zip without an app bundle is reported")

    # Every entry the pipeline publishes is built from the bundle's
    # Info.plist, so an unreadable one is not a usable IPA either.
    badplist = tmp / "badplist.ipa"
    with zipfile.ZipFile(badplist, "w") as zf:
        zf.writestr("Payload/App.app/", "")
        zf.writestr("Payload/App.app/Info.plist", b"<plist><dict><key>x")
        zf.writestr("Payload/App.app/bin", b"x" * 100)
    check("Info.plist" in ipa.ipa_problem(badplist),
          "a malformed Info.plist is reported")
    check(ipa.extract_metadata(badplist) is None,
          "a malformed Info.plist returns None instead of raising")

    noplist = tmp / "noplist.ipa"
    with zipfile.ZipFile(noplist, "w") as zf:
        zf.writestr("Payload/App.app/", "")
        zf.writestr("Payload/App.app/bin", b"x" * 100)
    check("Info.plist" in ipa.ipa_problem(noplist),
          "an app bundle without an Info.plist is reported")


def test_corrupt_upstream_ipa():
    print("\n── update_source: non-zip upstream IPA fails cleanly ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_up_to_date(fake, tmp, feather="2.9.0", ferrite="0.7.3")
    junk = b"\x1f\x8b\x08\x00" + b"x" * 4096
    fake.repos["claration/Feather"]["releases"].insert(0, release(
        "v2.10.0", False,
        ("Feather.ipa", len(junk), "https://up/feather-junk")))
    fake.add_download("https://up/feather-junk", junk)

    worked, failures = update_source_report()
    check("Feather" in failures, "the bad-IPA app is reported")
    check(worked is True, "the healthy app still downloaded")
    check(lib.load_current_releases()["Feather"]["version"] == "2.9.0",
          "recorded version was NOT advanced for the bad IPA")
    check(not list(lib.IPAS_DIR.glob("Feather*.ipa")),
          "the unusable download was not left in ipas/")


def test_per_app_failure_is_isolated():
    print("\n── update_source: one app's bad IPA fails that app only ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    # Nuvio Enhanced's build carries no Info.plist, so the validity gate
    # rejects it.  That app fails while the rest of the run carries on.
    seed_up_to_date(fake, tmp, feather="2.9.0", missing=("Nuvio Enhanced",))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Payload/App.app/", "")
        zf.writestr("Payload/App.app/binary", b"x" * 1000)
    fake.download_store["https://up/enhanced"] = buf.getvalue()

    worked, failures = update_source_report()
    check(failures == ["Nuvio Enhanced"],
          f"only the unreadable app failed (got {failures})")
    check(worked is False, "nothing was published")
    check(lib.load_current_releases()["Nuvio Enhanced"]["version"]
          == "0.5.1-beta", "recorded version was NOT advanced")
    check(not (lib.IPAS_DIR / "Nuvio-Enhanced.rel-0.5.1.ipa").exists(),
          "the rejected IPA was dropped")


def test_token_never_leaves_github_hosts():
    print("\n── download_file: token is scoped to GitHub hosts ──")
    tmp = Path(tempfile.mkdtemp())
    seen: list[dict] = []
    orig_urlopen = lib.urllib.request.urlopen

    class Resp:
        def read(self, n=-1):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def spy(req, timeout=None):
        seen.append(dict(req.headers))
        return Resp()

    lib.urllib.request.urlopen = spy
    try:
        _RealDownloadFile("https://evil.example/x.ipa",
                          tmp / "a.ipa", "SECRET")
        _RealDownloadFile("https://github.com/o/r/releases/download/t/a.ipa",
                          tmp / "b.ipa", "SECRET")
        check("Authorization" not in seen[0],
              "no token sent to a foreign host")
        check(seen[1].get("Authorization") == "Bearer SECRET",
              "token still sent to github.com")
        check(lib._is_github_host("https://objects.githubusercontent.com/x")
              and not lib._is_github_host("https://evil-github.com/x"),
              "github host check covers subdomains only")
    finally:
        lib.urllib.request.urlopen = orig_urlopen


def test_write_json_is_atomic():
    print("\n── write_json: a crash mid-write keeps the old file ──")
    tmp = Path(tempfile.mkdtemp())
    target = tmp / "state.json"
    lib.write_json(target, {"a": 1})
    check(json.loads(target.read_text()) ["a"] == 1, "writes the file")
    try:
        lib.write_json(target, {"b": object()})
        check(False, "a non-serialisable value should raise")
    except TypeError:
        pass
    check(json.loads(target.read_text())["a"] == 1,
          "previous file left intact")
    check(not (tmp / "state.json.part").exists(), "no .part left behind")


def test_manual_drop_must_be_a_real_ipa():
    print("\n── update_source: a junk manual drop is rejected ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_up_to_date(fake, tmp, feather="2.9.0")
    fake.seed_asset("HandMade.ipa", 4096)
    fake.add_download(asset_url("HandMade.ipa"), b"<!DOCTYPE html>nope")

    worked, failures = update_source_report()
    check(failures == ["HandMade.ipa"], f"the junk drop is reported ({failures})")
    check(worked is False, "nothing was published")
    check(not (lib.IPAS_DIR / "HandMade.ipa").exists(),
          "the unusable drop was removed from ipas/")


def test_manual_drop_name_matches_stored_asset():
    print("\n── update_source: a manual drop lands under GitHub's name ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_up_to_date(fake, tmp, feather="2.9.0")
    # GitHub stores the asset "App(1).ipa" as "App.1.ipa".
    fake.seed_asset("App(1).ipa", 4096)
    fake.add_download(asset_url("App(1).ipa"),
                      make_ipa("com.test.drop", "1.0", "Drop"))

    worked, failures = update_source_report()
    check(failures == [], f"the drop ingested cleanly ({failures})")
    check(worked is True, "the drop produced work")
    check((lib.IPAS_DIR / "App.1.ipa").exists(),
          "the file on disk carries GitHub's stored name")
    check(not (lib.IPAS_DIR / "App(1).ipa").exists(),
          "the raw name is never left behind")
    urls = {
        app["bundleIdentifier"]: app["downloadURL"].rsplit("/", 1)[-1]
        for app in json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))["apps"]
    }
    check(urls.get("com.test.drop") == "App.1.ipa",
          f"repo.json points at the stored name (got {urls.get('com.test.drop')})")


def test_referenced_manual_drop_is_not_refetched():
    print("\n── update_source: an already-referenced asset is not re-downloaded ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_up_to_date(fake, tmp, feather="2.9.0")
    fake.seed_asset("Custom.rel-1.0.ipa", 4096)
    fake.add_download(asset_url("Custom.rel-1.0.ipa"),
                      make_ipa("com.test.custom", "1.0", "Custom"))
    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    repo["apps"].append(
        app_entry("com.test.custom", "Custom", asset_url("Custom.rel-1.0.ipa"))
    )
    lib.REPO_JSON.write_text(json.dumps(repo, indent=2), encoding="utf-8")

    worked, failures = update_source_report()
    check(failures == [], f"nothing failed ({failures})")
    check(fake.downloads == [],
          f"the referenced asset was not re-downloaded (got {fake.downloads})")
    check(worked is False, "the run did no work")
    check(not (lib.IPAS_DIR / "Custom.rel-1.0.ipa").exists(),
          "the referenced asset was left off disk")


def test_sync_no_delete_uploads_but_deletes_nothing():
    print("\n── sync_release: --no-delete uploads but never deletes ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    lib.IPAS_DIR.mkdir(exist_ok=True)
    fake.seed_asset("Stale.ipa", 4096)
    (lib.IPAS_DIR / "Local.rel-1.0.ipa").write_bytes(
        make_ipa("com.test.local", "1.0", "Local"))

    real = rel.GitHubRelease("test")
    real.api = fake.sync_api
    check(sr.sync_release("test", client=real, no_delete=True) == 0,
          "sync_release --no-delete exited 0")
    check("Stale.ipa" in fake.assets, "the stale asset was not deleted")
    check("Local.rel-1.0.ipa" in fake.assets,
          "the local IPA was uploaded")


def test_display_name_override_renames_existing_app():
    print("\n── generate_repo: display_name overrides an existing entry ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_up_to_date(fake, tmp, feather="2.9.0", missing=("Nuvio Enhanced",))
    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    for app in repo["apps"]:
        if app["bundleIdentifier"] == "com.nuvio.enhancedmedia":
            app["name"] = "Old Name"
    lib.REPO_JSON.write_text(json.dumps(repo, indent=2), encoding="utf-8")

    worked, failures = update_source_report()
    check(failures == [], f"nothing failed ({failures})")
    check(worked is True, "the app was rebuilt")
    names = {
        app["bundleIdentifier"]: app["name"]
        for app in json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))["apps"]
    }
    check(names["com.nuvio.enhancedmedia"] == "Nuvio Enhanced",
          f"display_name applied (got {names['com.nuvio.enhancedmedia']!r})")


def test_human_fields_survive_rebuild():
    print("\n── generate_repo: human-set fields survive a rebuild ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_up_to_date(fake, tmp, feather="2.9.0", missing=("Nuvio Enhanced",))
    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    for app in repo["apps"]:
        if app["bundleIdentifier"] == "com.nuvio.enhancedmedia":
            app.update({
                "tintColor": "ff00ff",
                "category": "games",
                "developerName": "Someone",
                "subtitle": "mine",
            })
    lib.REPO_JSON.write_text(json.dumps(repo, indent=2), encoding="utf-8")

    worked, failures = update_source_report()
    check(failures == [], f"nothing failed ({failures})")
    check(worked is True, "the app was rebuilt")
    entry = next(
        app
        for app in json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))["apps"]
        if app["bundleIdentifier"] == "com.nuvio.enhancedmedia"
    )
    check(entry["tintColor"] == "ff00ff", "tintColor kept")
    check(entry["category"] == "games", "category kept")
    check(entry["developerName"] == "Someone", "developerName kept")
    check(entry["subtitle"] == "mine", "subtitle kept")


def test_name_survives_rebuild_without_display_name():
    print("\n── generate_repo: a hand-set name survives with no display_name ──")
    tmp = Path(tempfile.mkdtemp())
    fake = build_fixture(tmp)
    seed_up_to_date(fake, tmp, feather="2.9.0", missing=("Feather",))
    repo = json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))
    for app in repo["apps"]:
        if app["bundleIdentifier"] == "thewonderofyou.Feather":
            app["name"] = "Feather Renamed"
    lib.REPO_JSON.write_text(json.dumps(repo, indent=2), encoding="utf-8")

    worked, failures = update_source_report()
    check(failures == [], f"nothing failed ({failures})")
    check(worked is True, "the app was rebuilt")
    names = {
        app["bundleIdentifier"]: app["name"]
        for app in json.loads(lib.REPO_JSON.read_text(encoding="utf-8"))["apps"]
    }
    check(names["thewonderofyou.Feather"] == "Feather Renamed",
          f"hand-set name survived (got {names['thewonderofyou.Feather']!r})")


def test_asset_pattern_picks_the_variant():
    print("\n── find_ipa_asset: asset_pattern picks one variant ──")
    release_dict = {
        "tag_name": "v1",
        "assets": [
            {"name": "App.ipa", "size": 1,
             "browser_download_url": "https://up/app"},
            {"name": "App-GLASS.ipa", "size": 1,
             "browser_download_url": "https://up/glass"},
            {"name": "App-GLASSICONS.ipa", "size": 1,
             "browser_download_url": "https://up/icons"},
        ],
    }
    picked = rel.find_ipa_asset(release_dict, "GLASS")
    check(picked["name"] == "App-GLASS.ipa",
          f"the pattern picks its variant (got {picked['name']})")
    check(rel.find_ipa_asset(release_dict, None)["name"] == "App.ipa",
          "without a pattern the shortest name wins")


def main():
    print("=" * 60)
    print("  AltStore pipeline selftest")
    print("=" * 60)

    test_naming()
    test_ipa_without_dir_entries()
    test_crushed_icon_png()
    test_prune_stale_icons()
    test_check_releases()
    test_update_source_unchanged()
    test_update_source_missing_asset()
    test_http_error_verbose_no_retry()
    test_real_release_client_api()
    test_download_file_streams_to_disk()
    test_token_never_leaves_github_hosts()
    test_redirects_never_carry_the_token_off_github()
    test_same_version_rebuild()
    test_release_version_wins_only_when_it_is_newer()
    test_subtitle_never_repeats_the_description()
    test_failed_download_keeps_state()
    test_failed_api_never_ingests_a_stale_asset()
    test_ipa_problem_detects_junk()
    test_write_json_is_atomic()
    test_asset_pattern_picks_the_variant()
    test_corrupt_upstream_ipa()
    test_per_app_failure_is_isolated()
    test_manual_drop_must_be_a_real_ipa()
    test_manual_drop_name_matches_stored_asset()
    test_referenced_manual_drop_is_not_refetched()
    test_sync_no_delete_uploads_but_deletes_nothing()
    test_display_name_override_renames_existing_app()
    test_human_fields_survive_rebuild()
    test_name_survives_rebuild_without_display_name()
    test_invalid_custom_ipa()  # own fixture; the next test re-patches lib
    fake = test_update_source_bump_and_recovery()
    test_add_custom_ipa(fake)
    test_sync_release(fake)

    print(f"\n✅ ALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    main()
