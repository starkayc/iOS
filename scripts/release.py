#!/usr/bin/env python3
"""
Release assets: finding, fetching and publishing the IPAs on GitHub.
"""

import json
import zipfile
from pathlib import Path
from typing import BinaryIO, NamedTuple, Optional, Union
from urllib.parse import quote

import altstore_lib as lib
import cli_common as diag
import ipa


class FetchOutcome(NamedTuple):
    missing: bool
    problem: str


def _pick_ipa_asset(release: dict, source: dict) -> Optional[dict]:
    asset = lib.find_ipa_asset(release, source.get("asset_pattern"))
    if asset:
        return asset
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
    return zipped


def _extract_ipa(zip_path: Path, dest: Path, zip_name: str) -> str:
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
) -> FetchOutcome:
    asset = _pick_ipa_asset(release, source)
    if asset is None:
        return FetchOutcome(missing=True, problem="")
    zipped = asset["name"].lower().endswith(".ipa.zip")

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
        return FetchOutcome(missing=False, problem="")
    return FetchOutcome(
        missing=False,
        problem=(
            f"{asset['name']} from release {release.get('tag_name', '?')}: "
            f"{problem} (url: {url})"
        ),
    )


def _ingest_zipped(
    url: str, dest: Path, size: Optional[int], zip_name: str,
    token: Optional[str],
) -> str:
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
    so a verification step always reads fresh state.
    """

    def __init__(self, token: str):
        self.token = token
        self._release: Optional[dict] = None

    def api(
        self,
        url: str,
        data: Optional[Union[bytes, BinaryIO]] = None,
        method: Optional[str] = None,
        content_type: str = "application/octet-stream",
    ):
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

    def upload_asset(self, name: str, data: BinaryIO) -> None:
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
