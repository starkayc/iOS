#!/usr/bin/env python3
"""
Release assets: finding, fetching and publishing the IPAs on GitHub.

This module owns everything that talks about a release asset rather than a
raw request.  That is the ipa-assets client (list, upload, delete) and the
fetch path that turns one asset into a usable file in ipas/.

altstore_lib owns the low-level HTTP client, the paths and the naming
rules.  This module calls into altstore_lib through the module object
(``lib.download_file``, ``lib.request_bytes``), so the tests can still
point the whole pipeline at a fake network.
"""

import json
import re
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import altstore_lib as lib
import cli_common as diag
import ipa


def find_ipa_asset(release: dict, pattern: Optional[str] = None) -> Optional[dict]:
    """Return the best .ipa asset from a GitHub release, or None.

    When a release has multiple IPA variants (e.g. base, GLASS,
    NOEXTENSIONS), the shortest name is usually the plain/default build.
    Pass ``pattern`` to match a specific variant instead (e.g.
    pattern="GLASS" matches *-GLASS.ipa but not *-GLASSICONS.ipa or
    *-GLASS-NOEXTENSIONS.ipa).
    """
    ipa_assets = [
        a for a in release.get("assets", [])
        if a["name"].lower().endswith(".ipa")
    ]
    if not ipa_assets:
        return None

    if pattern:
        pat = pattern.lower()

        def _segment_count(asset: dict) -> int:
            base = asset["name"].rsplit(".", 1)[0]
            return len(re.split(r"[-.]", base))

        matching = [
            a for a in ipa_assets
            if pat in re.split(r"[-.]", a["name"].lower().rsplit(".", 1)[0])
        ]

        if matching:
            matching.sort(key=lambda a: (_segment_count(a), len(a["name"])))
            return matching[0]

        diag.warn(f"No IPA with segment '{pattern}', falling back to default")

    ipa_assets.sort(key=lambda a: len(a["name"]))
    return ipa_assets[0]



def _pick_ipa_asset(release: dict, source: dict) -> tuple[Optional[dict], bool]:
    """The asset to download and whether it is a zipped IPA."""
    asset = find_ipa_asset(release, source.get("asset_pattern"))
    if asset:
        return asset, False
    zipped = next(
        (
            a
            for a in release.get("assets", [])
            if a["name"].lower().endswith(".ipa.zip")
        ),
        None,
    )
    if zipped is None:
        diag.debug(f"no .ipa asset in release {release.get('tag_name', '?')}")
    return zipped, zipped is not None


def _extract_ipa(zip_path: Path, dest: Path, zip_name: str) -> str:
    """Write the .ipa inside ``zip_path`` to ``dest``; "" when it worked."""
    with zipfile.ZipFile(zip_path) as zf:
        entry = next(
            (n for n in zf.namelist() if n.lower().endswith(".ipa")), None
        )
        ipa_bytes = zf.read(entry) if entry else None
    if ipa_bytes is None:
        return f"no .ipa found inside {zip_name}"
    dest.write_bytes(ipa_bytes)
    return ""


def _gate(dest: Path) -> str:
    """Drop ``dest`` unless it is a usable IPA; return the reason, or "".

    This is the one place the ingest path judges an IPA, so a file that
    fails the check never stays on disk for the next step to trip over.
    """
    problem = ipa.ipa_problem(dest)
    if problem:
        dest.unlink(missing_ok=True)
    return problem


def ingest_ipa(
    url: str,
    dest: Path,
    size: Optional[int] = None,
    token: Optional[str] = None,
) -> str:
    """Download one IPA into ``dest``; "" on success, else the reason.

    Every IPA that enters the repo comes through here: download it, run the
    validity check, delete the file when it fails.  Callers report the
    reason once.
    """
    try:
        lib.download_file(url, dest, token, size)
    except Exception as ex:
        return f"download failed: {ex}"
    return _gate(dest)


def fetch_ipa_from_release(
    release: dict,
    source: dict,
    dest: Path,
    token: Optional[str] = None,
) -> Optional[str]:
    """Write an app's IPA to ``dest`` and say whether it is usable.

    None means the release has no IPA asset yet, so there is nothing to do
    and the next run retries.  An empty string means ``dest`` holds a
    usable IPA.  Anything else is the reason it failed, and the caller
    reports it.  This function only prints progress.
    """
    asset, zipped = _pick_ipa_asset(release, source)
    if asset is None:
        return None

    url = asset["browser_download_url"]
    print(
        f"    ↓ downloading {asset['name']} ({asset['size']:,} bytes"
        f"{', zipped' if zipped else ''}) … ",
        end="",
        flush=True,
    )
    if zipped:
        problem = _ingest_zipped(url, dest, asset["size"], asset["name"], token)
    else:
        problem = ingest_ipa(url, dest, asset["size"], token)
    print("failed" if problem else "done")
    if not problem:
        return ""
    return (
        f"{asset['name']} from release {release.get('tag_name', '?')}: "
        f"{problem} (url: {url})"
    )


def _ingest_zipped(
    url: str, dest: Path, size: Optional[int], zip_name: str,
    token: Optional[str],
) -> str:
    """Ingest a release that ships its IPA inside a zip archive."""
    tmp_zip = dest.with_name(dest.name + ".zip")
    try:
        try:
            lib.download_file(url, tmp_zip, token, size)
        except Exception as ex:
            return f"download failed: {ex}"
        return _extract_ipa(tmp_zip, dest, zip_name) or _gate(dest)
    finally:
        # Windows can't delete a file that still has a handle open, so the
        # temp archive goes after the ZipFile context has closed.
        tmp_zip.unlink(missing_ok=True)


class GitHubRelease:
    """Client for the asset level of the GitHub Releases API.

    It caches the release object and drops that cache after every mutation,
    so a verification step always reads fresh state.  The tests swap in a
    fake API backend by replacing the ``api`` method.
    """

    def __init__(self, token: str):
        self.token = token
        self._release: Optional[dict] = None

    def api(
        self,
        url: str,
        data: Optional[bytes] = None,
        method: Optional[str] = None,
        content_type: str = "application/octet-stream",
    ):
        """Call the GitHub API and return the parsed JSON, or None for an empty body."""
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "altstore-release-sync",
            **lib._auth(self.token, url),
        }
        if data is not None:
            headers["Content-Type"] = content_type
        diag.debug(f"{method or 'GET'} {url}")
        raw = lib.request_bytes(
            url, headers, data=data, method=method, timeout=600
        )
        return json.loads(raw) if raw else None

    def get_release(self) -> Optional[dict]:
        """Return the release dict, or None if it doesn't exist yet."""
        if self._release is None:
            url = (
                f"{lib.API_BASE}/repos/{lib.GITHUB_USER}/{lib.GITHUB_REPO}"
                f"/releases/tags/{lib.RELEASE_TAG}"
            )
            try:
                self._release = self.api(url)
            except lib.GitHubError as e:
                if e.status != 404:
                    raise
                self._release = None
        return self._release

    def create_release(self) -> dict:
        """Create the ipa-assets release and return it."""
        print(f"Creating release {lib.RELEASE_TAG} …")
        self._release = self.api(
            f"{lib.API_BASE}/repos/{lib.GITHUB_USER}/{lib.GITHUB_REPO}/releases",
            json.dumps(
                {"tag_name": lib.RELEASE_TAG, "name": "IPA Assets"}
            ).encode(),
            content_type="application/json",
        )
        return self._release

    def list_assets(self) -> list[dict]:
        release = self.get_release()
        if not release:
            return []
        return release.get("assets", [])

    def delete_asset(self, asset_id: int) -> None:
        self.api(
            f"{lib.API_BASE}/repos/{lib.GITHUB_USER}/{lib.GITHUB_REPO}"
            f"/releases/assets/{asset_id}",
            method="DELETE",
        )
        self._release = None

    def upload_asset(self, name: str, data: bytes) -> None:
        """Upload an asset, replacing any existing asset of the same name.

        Same-name matching uses GitHub's sanitization, so a stale asset
        stored under a normalized name is removed first.
        """
        release = self.get_release()
        if not release:
            release = self.create_release()
        for asset in self.list_assets():
            if lib.github_asset_name(asset["name"]) == lib.github_asset_name(name):
                print(f"    replacing existing asset {asset['name']} …")
                self.delete_asset(asset["id"])
                break
        url = (
            f"{lib.UPLOADS_BASE}/repos/{lib.GITHUB_USER}/{lib.GITHUB_REPO}"
            f"/releases/{release['id']}/assets?name={quote(name)}"
        )
        self.api(url, data)
        self._release = None


