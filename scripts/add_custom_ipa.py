#!/usr/bin/env python3
"""
Custom IPA uploader — workflow 3.

Downloads a single IPA from a URL, names it with the canonical versioned
scheme ("Balatro" + "1.0" → ipas/Balatro.rel-1.0.ipa), validates it,
rebuilds repo.json, and syncs it to the ipa-assets release.  Every step
fails loudly so a bad link or unreadable IPA can't silently leave an
orphaned release asset.

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
    """Download, validate, rebuild repo.json and sync.  Returns an exit code."""
    filename = lib.ipa_filename(name, version=version)
    lib.IPAS_DIR.mkdir(exist_ok=True)
    dest = lib.IPAS_DIR / filename

    print(f"  ↓ downloading {url} … ", end="", flush=True)
    lib.download_file(url, dest, token)
    print(f"done")
    print(f"    ✓ saved as {filename} ({dest.stat().st_size:,} bytes)")

    # Validate BEFORE uploading anywhere — an unreadable IPA must fail
    # the run instead of leaving an orphaned release asset.
    meta = lib.extract_metadata(dest)
    if not meta:
        cli.error(
            f"{filename} is not a readable IPA — expected a "
            f"Payload/*.app bundle with an Info.plist.  Nothing was uploaded."
        )
        return 1
    print(
        f"    ✓ bundle ID {meta['bundleIdentifier']} "
        f"(display name in the IPA: {meta['name']!r})"
    )

    # Sidecar: description/subtitle for this file (blank inputs stay blank).
    meta_file = lib.IPAS_DIR / ".custom_meta.json"
    sidecar = lib.load_custom_meta()
    sidecar[filename] = {
        "description": description or "",
        "subtitle": subtitle or "",
    }
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(
        f"    ✓ sidecar updated "
        f"(description={description!r}, subtitle={subtitle!r})"
    )

    # Rebuild repo.json from the files in ipas/.
    generate_repo(fetch_report={}, cleanup_losers=True)

    # The app must actually have landed — otherwise something is wrong
    # (e.g. a duplicate bundle ID) and we must not upload the asset.
    if not lib.repo_json_references(filename):
        cli.error(
            f"{filename} did not make it into repo.json — a duplicate "
            f"bundle ID may have been skipped.  Nothing was uploaded."
        )
        return 1

    # Upload to the ipa-assets release.  Never delete other apps'
    # assets here — this run's checkout can lag behind other workflows,
    # and stale-asset cleanup belongs to the update-source workflow
    # where repo.json is freshly generated.
    return sync_release(
        token or "",
        no_delete=True,
        client=lib.GitHubRelease(token or ""),
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
