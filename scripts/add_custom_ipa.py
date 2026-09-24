#!/usr/bin/env python3
"""
Custom IPA uploader — workflow 3.

Downloads a single IPA from a URL, names it with the canonical
versioned scheme ("Balatro" + "1.0" → ipas/Balatro.rel-1.0.ipa),
validates it, rebuilds repo.json, and syncs it to the ipa-assets
release.  Every step fails loudly so a bad link or unreadable IPA
can't silently leave an orphaned release asset.

Usage:
    python scripts/add_custom_ipa.py \
        --name "Balatro" --version "1.0" --url "https://…/Balatro.ipa" \
        [--description "…"] [--subtitle "…"] [--token TOKEN]
"""

import json
import sys
from pathlib import Path
from typing import Optional

import altstore_lib as lib
from generate_repo import generate_repo
from sync_release import sync_release

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _repo_contains(filename: str) -> bool:
    """Whether any downloadURL in repo.json points at this asset name."""
    if not lib.REPO_JSON.exists():
        return False
    with open(lib.REPO_JSON, encoding="utf-8") as f:
        repo = json.load(f)
    for app in repo.get("apps", []):
        urls = [app.get("downloadURL", "")]
        for v in app.get("versions", []):
            urls.append(v.get("downloadURL", ""))
        for url in urls:
            if url.rsplit("/", 1)[-1] == filename:
                return True
    return False


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
        print(
            f"    ✗ {filename} is not a readable IPA — expected a "
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
    if not _repo_contains(filename):
        print(
            f"    ✗ {filename} did not make it into repo.json — a "
            f"duplicate bundle ID may have been skipped.  Nothing was uploaded."
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


def main() -> int:
    name: Optional[str] = None
    version: Optional[str] = None
    url: Optional[str] = None
    description = ""
    subtitle = ""
    token: Optional[str] = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--name="):
            name = arg.split("=", 1)[1]
        elif arg.startswith("--version="):
            version = arg.split("=", 1)[1]
        elif arg.startswith("--url="):
            url = arg.split("=", 1)[1]
        elif arg.startswith("--description="):
            description = arg.split("=", 1)[1]
        elif arg.startswith("--subtitle="):
            subtitle = arg.split("=", 1)[1]
        elif arg.startswith("--token="):
            token = arg.split("=", 1)[1]
        else:
            print(f"Unknown argument: {arg}")
            return 1
        i += 1

    if not name or not version or not url:
        print(
            "Missing required arguments.  Usage:\n"
            "  python scripts/add_custom_ipa.py --name NAME --version VERSION "
            "--url URL [--description DESC] [--subtitle SUB] [--token TOKEN]"
        )
        return 1

    token = lib.get_token(token)
    if not token:
        print(
            "No GitHub token found.  Pass --token, set GITHUB_TOKEN (or "
            "GH_TOKEN), or save it in a file named .github-token."
        )
        return 1

    try:
        return run_custom_upload(name, version, url, description, subtitle, token)
    except Exception as e:
        print(f"failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
