#!/usr/bin/env python3
"""
Release checker.

Usage:
    python scripts/check_releases.py [--token TOKEN] [--dry-run]
"""

import json
import sys
from typing import Optional

import altstore_lib as lib
import cli_common as cli


def run_check(token: Optional[str] = None, dry_run: bool = False) -> int:
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
    unchecked = 0

    for e in entries:
        name = e["name"]
        release_type = e["release_type"]

        if e["info"] is None:
            unchecked += 1
            if name in old_recorded:
                new_recorded[name] = old_recorded[name]
                print(f"  ⚠ {name}: could not check (API error)")
            continue

        new_recorded[name] = lib.release_record(
            e["source"], release_type, e["latest"],
            previous=old_recorded.get(name),
        )

        if e["changed"]:
            if e["recorded"] == e["latest"]:
                pattern = e["source"].get("asset_pattern") or "default"
                changes.append(f"  ↻ {name}: asset variant → {pattern}")
            else:
                changes.append(
                    f"  ↑ {name}: {e['recorded']} → {e['latest']}"
                )
        else:
            print(f"  ✓ {name}: {e['latest']}")

    if entries and unchecked == len(entries):
        print("\n✗ No app could be checked — see the errors above.\n")
        return 1

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
