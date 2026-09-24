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

Used by the CI workflows after generate_repo.py/update_source.py.

Usage:
    python scripts/sync_release.py [--token TOKEN] [--no-delete]
"""

import json
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import altstore_lib as lib

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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
            if lib.RELEASE_TAG in url:
                names.add(unquote(url.rsplit("/", 1)[-1]))
    return names


def sync_release(
    token: str,
    no_delete: bool = False,
    client: Optional[lib.GitHubRelease] = None,
) -> int:
    """Mirror ipas/*.ipa onto the release.  Returns an exit code.

    ``client`` is injectable so tests can use a fake API backend.
    """
    # Normalize file names first (spaces → dashes) so upload names and
    # release assets always match.
    lib.canonicalize_ipa_files(lib.IPAS_DIR)

    client = client or lib.GitHubRelease(token)
    if not client.get_release():
        client.create_release()

    assets = {a["name"]: a for a in client.list_assets()}
    files = {p.name: p for p in sorted(lib.IPAS_DIR.glob("*.ipa"))}
    referenced = referenced_asset_names(lib.REPO_JSON)

    # GitHub normalizes asset names on upload (spaces/parens → dots), so
    # every local-name ↔ server-name comparison goes through the same
    # sanitization.
    file_names_san = {lib.github_asset_name(n) for n in files}
    referenced_san = {lib.github_asset_name(n) for n in referenced}

    if files:
        print(f"Syncing {len(files)} IPA(s) to the {lib.RELEASE_TAG} release …")

    # ── Upload missing / size-changed files ────────────────────────────────
    for name, path in sorted(files.items()):
        san = lib.github_asset_name(name)
        existing = next(
            (
                a for a in assets.values()
                if lib.github_asset_name(a["name"]) == san
            ),
            None,
        )
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
        san = lib.github_asset_name(name)
        if san in file_names_san:
            continue
        if san in referenced_san:
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
    final = {
        lib.github_asset_name(a["name"]): a["size"]
        for a in client.list_assets()
    }
    errors = 0
    for name, path in files.items():
        if final.get(lib.github_asset_name(name)) != path.stat().st_size:
            print(f"  ✗ {name}: not in the release with the right size")
            errors += 1
    if errors:
        print("  ✗ release is out of sync")
        return 1
    print("  ✓ release in sync")
    return 0


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

    token = lib.get_token(token_flag)
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
