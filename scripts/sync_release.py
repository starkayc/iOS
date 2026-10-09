#!/usr/bin/env python3
"""
Release asset synchronizer.

Makes the ipa-assets release exactly mirror ipas/*.ipa.  No shell word
splitting and no gh fuzzy asset matching.  It does four things:

  - creates the release if it doesn't exist
  - uploads every ipas/*.ipa whose asset is missing or has a different size
  - deletes .ipa assets with no matching file in ipas/, unless a
    downloadURL in repo.json still references them (external apps)
  - exits non-zero if the release doesn't match ipas/ afterwards

The CI workflows run it after generate_repo.py or update_source.py.

Usage:
    python scripts/sync_release.py [--token TOKEN] [--no-delete]
"""

import sys
from typing import Optional

import altstore_lib as lib
import cli_common as cli
import release as rel


def sync_release(
    token: str,
    no_delete: bool = False,
    client: Optional[rel.GitHubRelease] = None,
) -> int:
    """Mirror ipas/*.ipa onto the release.  Returns an exit code.

    A caller can pass its own ``client``, which the tests use to point the
    sync at a fake API backend.
    """
    lib.canonicalize_ipa_files(lib.IPAS_DIR)

    client = client or rel.GitHubRelease(token)
    if not client.get_release():
        client.create_release()

    assets = {a["name"]: a for a in client.list_assets()}
    files = {p.name: p for p in sorted(lib.IPAS_DIR.glob("*.ipa"))}
    referenced = lib.repo_json_asset_names()

    file_names_san = {lib.github_asset_name(n) for n in files}
    referenced_san = {lib.github_asset_name(n) for n in referenced}

    if files:
        print(f"Syncing {len(files)} IPA(s) to the {lib.RELEASE_TAG} release …")

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


def main(args) -> int:
    token = lib.get_token(args.token)
    if not token:
        cli.error(
            "No GitHub token found.  Pass --token, set GITHUB_TOKEN (or "
            "GH_TOKEN), or save it in a file named .github-token "
            "(it's gitignored)."
        )
        return 1
    return sync_release(token, args.no_delete)


if __name__ == "__main__":
    parser = cli.make_parser(__doc__)
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help="report stale release assets but do not delete them",
    )
    sys.exit(cli.run(parser, main))
