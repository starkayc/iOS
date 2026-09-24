#!/usr/bin/env python3
"""
Custom IPA downloader — workflow 3 step.

Downloads a single IPA from a URL and names it with the canonical
versioned scheme: "Balatro" + "1.0" → ipas/Balatro(rel-1.0).ipa.
Writes a sidecar (ipas/.custom_meta.json) with the description and
subtitle so generate_repo.py picks them up — blank inputs stay blank.

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

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def add_custom_ipa(
    name: str,
    version: str,
    url: str,
    description: str = "",
    subtitle: str = "",
    token: Optional[str] = None,
) -> Path:
    """Download the IPA, name it per the scheme, update the sidecar.

    Returns the path of the downloaded file.
    """
    filename = lib.ipa_filename(name, version=version)
    lib.IPAS_DIR.mkdir(exist_ok=True)
    dest = lib.IPAS_DIR / filename

    print(f"  ↓ downloading {url} … ", end="", flush=True)
    lib.download_file(url, dest, token)
    print(f"done")
    print(f"    ✓ saved as {filename} ({dest.stat().st_size:,} bytes)")

    meta = lib.load_custom_meta()
    meta[filename] = {
        "description": description or "",
        "subtitle": subtitle or "",
    }
    sidecar = lib.IPAS_DIR / ".custom_meta.json"
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(
        f"    ✓ sidecar updated "
        f"(description={description!r}, subtitle={subtitle!r})"
    )
    return dest


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

    try:
        add_custom_ipa(
            name, version, url, description, subtitle, lib.get_token(token)
        )
    except Exception as e:
        print(f"failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
