#!/usr/bin/env python3
"""
Custom IPA uploader, workflow 3.

Downloads a single IPA from a URL and names it with the canonical
versioned scheme, so "Balatro" plus "1.0" becomes
ipas/Balatro.rel-1.0.ipa.  Then it validates the file, rebuilds repo.json
and syncs it to the ipa-assets release.  Every step fails loudly, so a bad
link or an unreadable IPA cannot leave an orphaned release asset behind.

Usage:
    python scripts/add_custom_ipa.py \
        --name "Balatro" --version "1.0" --url "https://…/Balatro.ipa" \
        [--description "…"] [--subtitle "…"] [--token TOKEN] [--debug]
"""

import json
import sys
from typing import Optional

import altstore_lib as lib
import cli_common as cli
import ipa
import release as rel
from generate_repo import generate_repo
from sync_release import sync_release


def run_custom_upload(
    name: str,
    version: str,
    url: str,
    description: str = "",
    subtitle: str = "",
    token: Optional[str] = None,
) -> int:
    """Download the IPA, validate it, rebuild repo.json and sync the release."""
    filename = lib.ipa_filename(name, version=version)
    lib.IPAS_DIR.mkdir(exist_ok=True)
    dest = lib.IPAS_DIR / filename

    print(f"  ↓ downloading {url} … ", end="", flush=True)
    problem = rel.ingest_ipa(url, dest, None, token)
    print("failed" if problem else "done")
    if problem:
        cli.error(
            f"{filename} is not a usable IPA — {problem}.  Nothing was "
            f"uploaded."
        )
        return 1

    meta = ipa.extract_metadata(dest) or {}
    print(
        f"    ✓ saved as {filename} ({dest.stat().st_size:,} bytes), "
        f"bundle ID {meta.get('bundleIdentifier', '?')} "
        f"(display name in the IPA: {meta.get('name', '?')!r})"
    )

    meta_file = lib.IPAS_DIR / ".custom_meta.json"
    sidecar = lib.load_custom_meta()
    sidecar[filename] = {
        "description": description or "",
        "subtitle": subtitle or "",
    }
    lib.write_json(meta_file, sidecar)
    print(
        f"    ✓ sidecar updated "
        f"(description={description!r}, subtitle={subtitle!r})"
    )

    generate_repo(cleanup_losers=True)

    if not lib.repo_json_references(filename):
        cli.error(
            f"{filename} did not make it into repo.json — a duplicate "
            f"bundle ID may have been skipped.  Nothing was uploaded."
        )
        return 1

    # Upload to the ipa-assets release.  This run never deletes other apps'
    # assets, because its checkout can lag behind the other workflows.
    # Stale-asset cleanup belongs to the update-source workflow, where
    # repo.json is freshly generated.
    return sync_release(
        token or "",
        no_delete=True,
        client=rel.GitHubRelease(token or ""),
    )


def main(args) -> int:
    token = lib.get_token(args.token)
    if not token:
        cli.error(
            "No GitHub token found.  Pass --token, set GITHUB_TOKEN (or "
            "GH_TOKEN), or save it in a file named .github-token."
        )
        return 1
    return run_custom_upload(
        args.name,
        args.version,
        args.url,
        args.description,
        args.subtitle,
        token,
    )


if __name__ == "__main__":
    parser = cli.make_parser(__doc__)
    parser.add_argument("--name", required=True, help="app name, e.g. Balatro")
    parser.add_argument("--version", required=True, help="version, e.g. 1.0")
    parser.add_argument("--url", required=True, help="direct download link to the .ipa")
    parser.add_argument("--description", default="", help="AltStore description")
    parser.add_argument("--subtitle", default="", help="AltStore subtitle")
    sys.exit(cli.run(parser, main))
