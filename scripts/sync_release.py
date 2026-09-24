#!/usr/bin/env python3
"""
Release asset synchronizer.

Makes the ipa-assets release exactly mirror ipas/*.ipa, deterministically —
no shell word splitting, no gh fuzzy asset matching:

  - creates the release if it doesn't exist
  - uploads every ipas/*.ipa whose asset is missing or has a different size
  - deletes .ipa assets with no matching file in ipas/, unless a
    downloadURL in repo.json still references them (external apps)
  - exits non-zero if the release doesn't match ipas/ afterwards

Used by the CI workflow in place of the gh release upload/delete steps,
and by upload_ipa.py for local drop-in uploads.

Usage:
    python scripts/sync_release.py [--token TOKEN] [--no-delete]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_repo import (  # noqa: E402
    GITHUB_REPO,
    GITHUB_USER,
    IPAS_DIR,
    RELEASE_TAG,
    REPO_JSON,
)

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


class GitHubRelease:
    """Thin client for the GitHub Releases API (asset level).

    Kept as a class so tests can swap in a fake backend.
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
        """Call the GitHub API; returns parsed JSON (or None for no body)."""
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "altstore-release-sync",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = content_type
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )
                with urllib.request.urlopen(req, timeout=600) as resp:
                    body = resp.read()
                    return json.loads(body) if body else None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2 * attempt)
        raise last_err

    def get_release(self) -> Optional[dict]:
        """Return the release dict, or None if it doesn't exist yet."""
        if self._release is None:
            url = (
                f"{API}/repos/{GITHUB_USER}/{GITHUB_REPO}"
                f"/releases/tags/{RELEASE_TAG}"
            )
            try:
                self._release = self.api(url)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                self._release = None
        return self._release

    def create_release(self) -> dict:
        """Create the ipa-assets release and return it."""
        print(f"Creating release {RELEASE_TAG} …")
        self._release = self.api(
            f"{API}/repos/{GITHUB_USER}/{GITHUB_REPO}/releases",
            json.dumps(
                {"tag_name": RELEASE_TAG, "name": "IPA Assets"}
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
            f"{API}/repos/{GITHUB_USER}/{GITHUB_REPO}"
            f"/releases/assets/{asset_id}",
            method="DELETE",
        )

    def upload_asset(self, name: str, data: bytes) -> None:
        """Upload an asset, replacing any existing asset of the same name."""
        release = self.get_release()
        if not release:
            release = self.create_release()
        for asset in self.list_assets():
            if asset["name"] == name:
                print(f"    replacing existing asset {name} …")
                self.delete_asset(asset["id"])
                break
        url = (
            f"{UPLOADS}/repos/{GITHUB_USER}/{GITHUB_REPO}"
            f"/releases/{release['id']}/assets?name={quote(name)}"
        )
        self.api(url, data)


def referenced_asset_names(repo_json: Path) -> set[str]:
    """Names of assets that downloadURLs in repo.json point at."""
    if not repo_json.exists():
        return set()
    with open(repo_json, encoding="utf-8") as f:
        repo = json.load(f)
    names: set[str] = set()
    for app in repo.get("apps", []):
        urls = [app.get("downloadURL", "")]
        for v in app.get("versions", []):
            urls.append(v.get("downloadURL", ""))
        for url in urls:
            if RELEASE_TAG in url:
                names.add(unquote(url.rsplit("/", 1)[-1]))
    return names


def sync_release(token: str, no_delete: bool = False) -> int:
    """Mirror ipas/*.ipa onto the release.  Returns an exit code."""
    client = GitHubRelease(token)
    if not client.get_release():
        client.create_release()

    assets = {a["name"]: a for a in client.list_assets()}
    files = {p.name: p for p in sorted(IPAS_DIR.glob("*.ipa"))}
    referenced = referenced_asset_names(REPO_JSON)

    if files:
        print(f"Syncing {len(files)} IPA(s) to the {RELEASE_TAG} release …")

    # ── Upload missing / size-changed files ────────────────────────────────
    for name, path in sorted(files.items()):
        existing = assets.get(name)
        if existing is not None and existing["size"] == path.stat().st_size:
            continue
        print(
            f"  ↑ uploading {name} ({path.stat().st_size:,} bytes) … ",
            end="",
            flush=True,
        )
        try:
            client.upload_asset(name, path.read_bytes())
            print("done")
        except Exception as e:
            print(f"failed: {e}")
            return 1

    # ── Delete stale assets ─────────────────────────────────────────────────
    for name, asset in sorted(assets.items()):
        if not name.lower().endswith(".ipa"):
            continue
        if name in files:
            continue
        if name in referenced:
            print(
                f"  ⚠ {name}: referenced by repo.json but not in ipas/ — "
                f"keeping (external app)"
            )
            continue
        if no_delete:
            print(f"  (would delete {name})")
            continue
        print(f"  🗑 deleting stale asset {name} … ", end="", flush=True)
        try:
            client.delete_asset(asset["id"])
            print("done")
        except Exception as e:
            print(f"failed: {e}")
            return 1

    # ── Verify the final state ──────────────────────────────────────────────
    final = {a["name"]: a["size"] for a in client.list_assets()}
    errors = 0
    for name, path in files.items():
        if final.get(name) != path.stat().st_size:
            print(f"  ✗ {name}: not in the release with the right size")
            errors += 1
    if errors:
        print("  ✗ release is out of sync")
        return 1
    print("  ✓ release in sync")
    return 0


def get_token(flag: Optional[str]) -> Optional[str]:
    token = flag or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    token_file = Path(__file__).resolve().parent.parent / ".github-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return None


def main() -> int:
    token_flag: Optional[str] = None
    no_delete = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--token="):
            token_flag = arg.split("=", 1)[1]
            i += 1
        elif arg == "--no-delete":
            no_delete = True
            i += 1
        else:
            print(f"Unknown argument: {arg}")
            return 1

    token = get_token(token_flag)
    if not token:
        print(
            "No GitHub token found.  Pass --token, set GITHUB_TOKEN (or "
            "GH_TOKEN), or save it in a file named .github-token "
            "(it's gitignored)."
        )
        return 1

    return sync_release(token, no_delete)


if __name__ == "__main__":
    sys.exit(main())
