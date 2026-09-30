#!/usr/bin/env python3
"""
Release checker — workflow 1.

Compares every sources.json app against current_releases.json.  If a
stable app's version tag changed, or a prerelease app's commit moved,
current_releases.json is updated (and the workflow commits it, which
triggers the update-source workflow).

If nothing changed, nothing is written and the job exits cleanly.

Usage:
    python scripts/check_releases.py [--token TOKEN] [--dry-run]
"""

import json
import sys
from typing import Optional

import altstore_lib as lib
import cli_common as cli


def run_check(token: Optional[str] = None, dry_run: bool = False) -> int:
    """Compare sources.json against current_releases.json.

    Writes current_releases.json when anything changed (unless
    --dry-run).  Always returns 0 — the workflow decides whether to
    commit based on the git diff.
    """
    print("=" * 60)
    print("  Release Checker")
    print("=" * 60)

    report = lib.check_for_updates(token)
    entries = report["entries"]
    if not entries:
        print("\nNo sources configured.\n")
        return 0

    old_recorded = lib.load_current_releases()
    new_recorded: dict[str, dict] = {}
    changes: list[str] = []

    for e in entries:
        name = e["name"]
        release_type = e["release_type"]

        if e["info"] is None:
            # API error — keep the old value so we don't lose track.
            if name in old_recorded:
                new_recorded[name] = old_recorded[name]
                print(f"  ⚠ {name}: could not check (API error)")
            continue

        value_key = "commit" if release_type == "prerelease" else "version"
        new_recorded[name] = {"release_type": release_type, value_key: e["latest"]}

        if e["changed"]:
            changes.append(f"  ↑ {name}: {e['recorded']} → {e['latest']}")
        else:
            print(f"  ✓ {name}: {e['latest']}")

    if not changes:
        print("\n✓ No version changes.\n")
        return 0

    if dry_run:
        print("\n── DRY RUN — would write current_releases.json ──")
        print(json.dumps(new_recorded, indent=2, ensure_ascii=False))
        print()
        return 0

    lib.save_current_releases(new_recorded)
    print(f"\n  {len(changes)} change(s):")
    for c in changes:
        print(c)
    print("  ✓ current_releases.json written\n")
    return 0


def main(args) -> int:
    return run_check(lib.get_token(args.token), args.dry_run)


if __name__ == "__main__":
    parser = cli.make_parser(__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the changes without writing current_releases.json",
    )
    sys.exit(cli.run(parser, main))
