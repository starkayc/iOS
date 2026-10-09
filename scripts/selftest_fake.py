#!/usr/bin/env python3
"""
Fake GitHub backend and shared fixtures for selftest.py.

This module stays apart from the tests, so selftest.py reads as a list of
cases.  ``build_fixture`` points the whole pipeline at a temp directory and
at this fake network, then returns the fake so a test can break one thing
in it.
"""

import io
import json
import plistlib
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import altstore_lib as lib  # noqa: E402

from release import GitHubRelease as _RealGitHubRelease  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FakeReleaseFactory:
    """Builds the real client wired to the fake sync server."""

    def __init__(self, fake):
        self.fake = fake

    def __call__(self, token):
        client = _RealGitHubRelease(token)
        client.api = self.fake.sync_api
        return client


def make_ipa(bundle_id, version="0.5.1", name="Nuvio", build="133",
             with_dir_entry=True):
    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleDisplayName": name,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": build,
        "MinimumOSVersion": "16.1",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if with_dir_entry:
            zf.writestr("Payload/App.app/", "")
        zf.writestr(
            "Payload/App.app/Info.plist",
            plistlib.dumps(info, fmt=plistlib.FMT_BINARY),
        )
        zf.writestr("Payload/App.app/binary", b"x" * 5000)
    return buf.getvalue()


def png_chunk(ctype: bytes, data: bytes) -> bytes:
    """One PNG chunk: length, type, payload, CRC."""
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))


def png_chunks(data: bytes) -> dict:
    """{chunk type: first payload} of a PNG."""
    out: dict[bytes, bytes] = {}
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        out[ctype] = data[pos + 8:pos + 8 + length]
        pos += 12 + length
    return out


def crushed_icon_png() -> bytes:
    """An Apple-crushed (CgBI) 4-bit indexed PNG, 4x2 pixels.

    Sub-byte depth is where a row is *not* width * bytes-per-pixel, and the
    palette is where an indexed icon's red/blue swap has to happen.
    """
    width, height, bit_depth = 4, 2, 4
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, 3, 0, 0, 0)
    co = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw deflate, as CgBI has
    idat = co.compress(bytes([0x00, 0x12, 0x34, 0x00, 0x56, 0x78]))
    return (PNG_MAGIC + png_chunk(b"CgBI", b"")
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"PLTE", bytes([1, 2, 3, 4, 5, 6]))
            + png_chunk(b"IDAT", idat + co.flush())
            + png_chunk(b"IEND", b""))


def plain_png() -> bytes:
    """A tiny ordinary PNG (nothing to uncrush)."""
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (PNG_MAGIC + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
            + png_chunk(b"IEND", b""))


def make_ipa_with_icon(bundle_id="com.test.icon", version="1.0") -> bytes:
    """An IPA whose Info.plist names an icon that is really in the bundle."""
    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleDisplayName": "Icon",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": "1",
        "CFBundleIcons": {
            "CFBundlePrimaryIcon": {"CFBundleIconFiles": ["AppIcon60x60"]}
        },
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Payload/App.app/", "")
        zf.writestr(
            "Payload/App.app/Info.plist",
            plistlib.dumps(info, fmt=plistlib.FMT_BINARY),
        )
        zf.writestr("Payload/App.app/AppIcon60x60@2x.png", crushed_icon_png())
        zf.writestr("Payload/App.app/binary", b"x" * 1000)
    return buf.getvalue()


class FakeGitHub:
    """Stateful stand-in for the GitHub REST API and file downloads.

    ``api`` serves repo info, release lists, commit lookups and the
    ipa-assets release.  ``download`` serves bytes from download_store.
    ``assets`` is the ipa-assets release's asset table.  Uploads store names
    the way GitHub does, with unsafe characters replaced by dots.
    """

    def __init__(self):
        self.repos = {}
        self.commits = {}
        self.assets = {}
        self.download_store = {}
        self.downloads = []
        self.fail_downloads = {}
        self.tag_fetches = 0
        self.next_id = 100

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
            "browser_download_url": (
                "https://github.com/starkayc/iOS/releases/"
                f"download/ipa-assets/{name}"
            ),
        }

    def api(self, url, token=None):
        if "/releases/tags/" in url:
            self.tag_fetches += 1
            return {
                "id": 1,
                "assets": [
                    dict(a) for a in self.assets.values()
                ],
            }
        if "/releases?per_page=100" in url:
            repo = url.split("/repos/", 1)[1].split("/releases", 1)[0]
            return self.repos[repo]["releases"]
        if "/commits/" in url:
            repo = url.split("/repos/", 1)[1].split("/commits/", 1)[0]
            tag = unquote(url.rsplit("/commits/", 1)[1])
            return {"sha": self.commits[repo][tag]}
        if "/repos/" in url:
            repo = url.split("/repos/", 1)[1]
            if repo in self.repos:
                r = self.repos[repo]
                return {"description": r["desc"], "owner": {"login": r["owner"]}}
        raise AssertionError(f"unexpected API call: {url}")

    def download(self, url, dest, token=None, expected_size=None):
        if url in self.fail_downloads:
            raise RuntimeError(self.fail_downloads[url])
        data = self.download_store.get(url)
        if data is None:
            raise AssertionError(f"unexpected download: {url}")
        dest.write_bytes(data)
        self.downloads.append((url, dest.name))

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
            stored = lib.github_asset_name(name)
            self.assets.pop(stored, None)
            self.next_id += 1
            self.assets[stored] = {
                "id": self.next_id, "name": stored, "size": len(data),
                "updated_at": "2026-09-24T12:00:00Z",
            }
            return {"id": self.next_id, "name": stored, "state": "uploaded"}
        if "/releases" in url and data is not None:
            return {"id": 1}
        if "/releases/tags/" in url:
            self.tag_fetches += 1
            return {
                "id": 1,
                "assets": [dict(a) for a in self.assets.values()],
            }
        raise AssertionError(f"unexpected sync call: {method} {url}")

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
    """Point the pipeline at a temp directory and set up a fake GitHub.

    Four sources are configured: Feather and Ferrite as stable apps, Ksign
    as a prerelease app, and Nuvio Enhanced with a bundle-ID override.
    """
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

    lib.SOURCES_JSON = sources
    lib.IPAS_DIR = tmp / "ipas"
    lib.ICONS_DIR = tmp / "icons"
    lib.REPO_JSON = tmp / "repo.json"
    lib.CURRENT_RELEASES_JSON = tmp / "current_releases.json"
    lib.github_api = fake.api
    lib.download_file = fake.download
    return fake


def seed_current_releases(tmp: Path, feather="2.9.0",
                          ksign="03a3a9c", ferrite="0.7.4"):
    data = {
        "Feather": {"release_type": "stable", "version": feather},
        "Ksign": {"release_type": "prerelease", "commit": ksign},
        "Nuvio Enhanced": {"release_type": "stable", "version": "0.5.1-beta"},
        "Ferrite": {"release_type": "stable", "version": ferrite},
    }
    path = tmp / "current_releases.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def seed_repo_json(tmp: Path, apps: list[dict]):
    """Write a minimal repo.json with the given apps."""
    path = tmp / "repo.json"
    path.write_text(json.dumps({"apps": apps}, indent=2), encoding="utf-8")


def app_entry(bundle_id, name, url):
    return {
        "name": name,
        "bundleIdentifier": bundle_id,
        "developerName": "dev",
        "iconURL": "",
        "localizedDescription": "",
        "subtitle": "",
        "tintColor": "3c94fc",
        "category": "utilities",
        "versions": [{
            "downloadURL": url,
            "size": 1000,
            "version": "1.0",
            "buildVersion": "1",
            "date": "2026-07-14T20:15:52Z",
            "localizedDescription": "",
            "minOSVersion": "16.0",
        }],
        "appPermissions": {},
        "version": "1.0",
        "versionDate": "2026-07-14T20:15:52Z",
        "size": 1000,
        "downloadURL": url,
    }


def asset_url(filename):
    return ("https://github.com/starkayc/iOS/releases/"
            f"download/ipa-assets/{filename}")


def seed_up_to_date(fake, tmp, feather="2.9.0", ksign="03a3a9c",
                    ferrite="0.7.4", missing=()):
    """Record every app as current and publish what it points at.

    Seeds current_releases.json, the release asset and the repo.json entry
    for each app, so a test only has to break the one app it cares about.
    The rest are skipped as up to date.  ``missing`` leaves that app's
    release asset out, which makes the run re-fetch it while the committed
    repo.json entry stays, the way a real one would.
    """
    seed_current_releases(tmp, feather=feather, ksign=ksign, ferrite=ferrite)
    published = {
        "Ksign": ("nya.asami.ksign", f"Ksign.pre-{ksign}.ipa"),
        "Nuvio Enhanced": (
            "com.nuvio.enhancedmedia", "Nuvio-Enhanced.rel-0.5.1.ipa",
        ),
        "Feather": ("thewonderofyou.Feather", f"Feather.rel-{feather}.ipa"),
        "Ferrite": ("me.kingbri.Ferrite", f"Ferrite.rel-{ferrite}.ipa"),
    }
    apps = []
    for name, (bundle_id, filename) in published.items():
        if name not in missing:
            fake.seed_asset(filename, 1000)
        apps.append(app_entry(bundle_id, name, asset_url(filename)))
    seed_repo_json(tmp, apps)
