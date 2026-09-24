#!/usr/bin/env python3
"""
Drop-in IPA uploader.

Drop your hand-built IPAs into ipas/ (or any folder) and run:

    python scripts/upload_ipa.py

Each IPA is uploaded to the ipa-assets GitHub release, repo.json is
regenerated (new apps are named after their file, e.g. "MyApp 2.0.ipa"
becomes "MyApp 2.0"), and the changes are committed and pushed.

Auth — needs a GitHub token with write access to this repo.  Supply it
one of three ways (in order of precedence):
  1. --token ghp_...
  2. the GITHUB_TOKEN environment variable
  3. a file named .github-token next to this script (gitignored)

Usage:
    python scripts/upload_ipa.py [--folder PATH] [--token TOKEN] [--no-push]
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Import the generator from this same folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_repo import (  # noqa: E402
    GITHUB_REPO,
    GITHUB_USER,
    RELEASE_TAG,
    generate_repo,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


def _api_request(
    url: str,
    token: str,
    data: Optional[bytes] = None,
    method: Optional[str] = None,
    content_type: str = "application/octet-stream",
):
    """Call the GitHub API; returns parsed JSON (or None for empty body)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "altstore-ipa-drop",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = resp.read()
        return json.loads(body) if body else None


def _get_token(flag: Optional[str]) -> Optional[str]:
    token = flag or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    token_file = REPO_ROOT / ".github-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return None


def _ensure_release(token: str) -> int:
    """Return the ipa-assets release id, creating the release if needed."""
    url = f"{API}/repos/{GITHUB_USER}/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}"
    try:
        return _api_request(url, token)["id"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    print(f"Creating release {RELEASE_TAG} …")
    payload = json.dumps(
        {"tag_name": RELEASE_TAG, "name": "IPA Assets"}
    ).encode()
    resp = _api_request(
        f"{API}/repos/{GITHUB_USER}/{GITHUB_REPO}/releases",
        token,
        payload,
        content_type="application/json",
    )
    return resp["id"]


def _upload_ipa(ipa_path: Path, release_id: int, token: str) -> bool:
    """Upload one IPA as a release asset, replacing any same-named asset."""
    name = ipa_path.name

    # GitHub refuses to upload over an existing asset with the same name,
    # so delete it first.
    listing = (
        f"{API}/repos/{GITHUB_USER}/{GITHUB_REPO}"
        f"/releases/{release_id}/assets"
    )
    for asset in _api_request(listing, token) or []:
        if asset["name"] == name:
            print(f"    replacing existing asset {name} …")
            _api_request(
                f"{API}/repos/{GITHUB_USER}/{GITHUB_REPO}"
                f"/releases/assets/{asset['id']}",
                token,
                method="DELETE",
            )
            break

    url = (
        f"{UPLOADS}/repos/{GITHUB_USER}/{GITHUB_REPO}/releases/{release_id}"
        f"/assets?name={quote(name)}"
    )
    print(
        f"  ↑ uploading {name} ({ipa_path.stat().st_size:,} bytes) … ",
        end="",
        flush=True,
    )
    try:
        _api_request(url, token, ipa_path.read_bytes())
        print("done")
        return True
    except Exception as e:
        print(f"failed: {e}")
        return False


def main() -> None:
    folder = REPO_ROOT / "ipas"
    token: Optional[str] = None
    push = True

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--folder":
            folder = Path(args[i + 1])
            i += 2
        elif arg.startswith("--token="):
            token = arg.split("=", 1)[1]
            i += 1
        elif arg == "--no-push":
            push = False
            i += 1
        else:
            print(f"Unknown argument: {arg}")
            sys.exit(1)

    token = _get_token(token)
    if not token:
        print(
            "No GitHub token found.  Create a token with repo access at\n"
            "https://github.com/settings/tokens and pass it with --token,\n"
            "set the GITHUB_TOKEN environment variable, or save it in a\n"
            "file named .github-token (it's gitignored)."
        )
        sys.exit(1)

    ipa_files = sorted(folder.glob("*.ipa"))
    if not ipa_files:
        print(f"No .ipa files found in {folder}")
        sys.exit(1)

    print("=" * 60)
    print("  Drop-in IPA uploader")
    print("=" * 60)

    release_id = _ensure_release(token)
    uploaded = {f.name for f in ipa_files if _upload_ipa(f, release_id, token)}
    if not uploaded:
        print("\nNothing was uploaded.")
        sys.exit(1)

    # Regenerate repo.json — new apps are named after their file name.
    print()
    generate_repo(manual_names=uploaded)

    if not push:
        print("\nSkipping commit/push (--no-push).")
        return

    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "add", "repo.json", "icons"],
        check=True,
    )
    if subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--cached", "--quiet"]
    ).returncode == 0:
        print("\nNothing to commit — repo.json is already up to date.")
        return

    names = ", ".join(sorted(uploaded))
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "commit", "-m", f"chore: add {names}"],
        check=True,
    )
    subprocess.run(["git", "-C", str(REPO_ROOT), "push"], check=True)
    print("\n✓ Pushed. AltStore will pick up the new app(s) on next refresh.")


if __name__ == "__main__":
    main()
