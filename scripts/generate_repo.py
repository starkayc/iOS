#!/usr/bin/env python3
"""
AltStore Source Generator, repo.json builder.

Scans the ipas/ folder for .ipa files, reads the metadata of each one
(bundle ID, version, icon and the rest), and generates or updates
repo.json.

Fields a human set on an existing repo.json entry stay put: descriptions,
subtitles, developer names and tint colors.  Apps that point at an external
download URL, so their file is not in ipas/, are kept as-is.

Downloading IPAs from GitHub is not this module's job.  See update_source.py,
which fetches changed apps and then calls generate_repo, and
add_custom_ipa.py, which downloads a single IPA.

The fetch phase passes ``per_app``, a dict keyed by file name that holds
what the fetch learned about each file (``date``, ``description``,
``developer``, ``display_name``) and ``manual`` for a hand-dropped or
custom-uploaded file.  A file with no entry there is scanned on its own
metadata alone.

Usage:
    python scripts/generate_repo.py                 # scan ipas/ → repo.json
    python scripts/generate_repo.py --dry-run       # show changes, don't write
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import altstore_lib as lib
import cli_common as cli
import ipa

PRESERVED_AS_IS = ("name", "tintColor", "category", "appPermissions")
PRESERVED_IF_SET = ("developerName", "localizedDescription", "subtitle")

COMPARED_ENTRY = PRESERVED_IF_SET + ("iconURL", "downloadURL")
COMPARED_VERSION = ("version", "buildVersion", "size", "minOSVersion")


def load_existing_repo() -> Optional[dict]:
    """Load the current repo.json, or None if it doesn't exist."""
    if lib.REPO_JSON.exists():
        with open(lib.REPO_JSON, encoding="utf-8") as f:
            return json.load(f)
    return None


def index_by_bundle_id(repo: Optional[dict]) -> dict[str, dict]:
    """Build {bundleIdentifier: app_entry} from an existing repo.json."""
    if not repo:
        return {}
    return {app["bundleIdentifier"]: app for app in repo.get("apps", [])}


def prune_stale_icons(repo: dict) -> None:
    """Delete icons/ files the new repo.json no longer points at.

    placeholder.png stays, because it is the fallback for an app without an
    icon.  Without this, a renamed bundle ID or a dropped app leaves its
    icon in the repo forever.
    """
    keep = {"placeholder.png"}
    keep.update(
        unquote(url.rsplit("/", 1)[-1]) for url in
        (a.get("iconURL", "") for a in repo["apps"]) if url
    )
    for icon in sorted(lib.ICONS_DIR.glob("*.png")):
        if icon.name in keep:
            continue
        print(f"  🗑 removing stale icon {icon.name}")
        icon.unlink()


def generate_repo(
    per_app: Optional[dict] = None,
    dry_run: bool = False,
    cleanup_losers: bool = False,
) -> bool:
    """Scan ipas/, update repo.json, return True when anything changed.

    ``per_app`` comes from update_source.py and supplies per-file facts:
    the release date, the upstream About text, the developer name, a
    sources.json display name, and whether the file was a manual drop.
    ``cleanup_losers`` deletes the file that lost a bundle-ID collision, so
    its stale release asset gets cleaned up too.  The CI runs enable it,
    local runs do not.
    """
    print("=" * 60)
    print("  AltStore Source Generator")
    print("=" * 60)

    facts_by_file = per_app or {}
    custom_meta = lib.load_custom_meta()

    lib.ICONS_DIR.mkdir(exist_ok=True)
    lib.IPAS_DIR.mkdir(exist_ok=True)

    existing = load_existing_repo()
    existing_apps = index_by_bundle_id(existing)
    print(f"Loaded existing repo.json — {len(existing_apps)} app(s)")

    lib.canonicalize_ipa_files(lib.IPAS_DIR)
    ipa_files = sorted(lib.IPAS_DIR.glob("*.ipa"))
    if not ipa_files:
        print("\nNo .ipa files found in ipas/ folder.")
        print("Add your .ipa files there and run again.\n")
        return False

    print(f"Scanning {len(ipa_files)} IPA(s) in ipas/ …\n")

    processed: dict[str, dict] = {}
    processed_manual: dict[str, bool] = {}
    processed_file: dict[str, Path] = {}
    changes: list[str] = []

    for ipa_path in ipa_files:
        print(f"  📦 {ipa_path.name}")

        meta = ipa.extract_metadata(ipa_path)
        if not meta:
            continue

        bundle_id = meta["bundleIdentifier"]
        facts = facts_by_file.get(ipa_path.name, {})
        old = existing_apps.get(bundle_id)
        is_manual = (
            bool(facts.get("manual"))
            or ipa_path.name in custom_meta
        )

        if bundle_id in processed:
            prev_ver = processed[bundle_id]["version"]
            new_t = lib.parse_version_tuple(meta["version"])
            prev_t = lib.parse_version_tuple(prev_ver)
            sources_beats_manual_on_tie = (
                new_t == prev_t
                and processed_manual[bundle_id]
                and not is_manual
            )
            if new_t > prev_t or sources_beats_manual_on_tie:
                print(f"    (replacing v{prev_ver} with v{meta['version']})")
                if cleanup_losers:
                    loser = processed_file[bundle_id]
                    print(f"      (removing {loser.name} — same bundle ID)")
                    loser.unlink()
            else:
                print(
                    f"    ⚠ duplicate bundle ID {bundle_id} — keeping "
                    f"{processed[bundle_id].get('name', '?')} v{prev_ver}, "
                    f"skipping {ipa_path.name}"
                )
                if cleanup_losers:
                    print(f"      (removing {ipa_path.name})")
                    ipa_path.unlink()
                continue

        # Extract the icon and fall back to the placeholder when the IPA has
        # none, because an empty iconURL breaks Feather's source loading.
        icon_filename = ipa.extract_icon(
            ipa_path, meta["icon_paths"], bundle_id, lib.ICONS_DIR
        )
        if icon_filename:
            icon_url = f"{lib.PAGES_BASE}/icons/{lib.url_encode_path(icon_filename)}"
        else:
            icon_url = f"{lib.PAGES_BASE}/icons/placeholder.png"

        # IPA size and download URL.  The binaries come from GitHub Releases
        # rather than Pages, which would serve a Git-LFS pointer instead of
        # the IPA.  The local file name is canonical: the workflow uploads
        # fetched files under it, and it downloads a manual drop by its asset
        # name, so the two always match.
        ipa_size = ipa_path.stat().st_size
        safe_ipa_name = lib.url_encode_path(ipa_path.name)
        download_url = f"{lib.RELEASE_BASE}/{safe_ipa_name}"

        release_date_str = facts.get("date")
        if release_date_str:
            ipa_mtime = datetime.fromisoformat(
                release_date_str.replace("Z", "+00:00")
            )
        else:
            ipa_mtime = datetime.fromtimestamp(
                ipa_path.stat().st_mtime, tz=timezone.utc
            )

        # Build the version object.  ``version`` is what AltStore compares to
        # the installed app.  An upstream build can keep
        # CFBundleShortVersionString constant, as osu! does with 1.0 on every
        # release, while its tag moves.  When the tracked release version is
        # the newer of the two, it wins.
        version_str = meta["version"]
        release_version = facts.get("version", "")
        if release_version and lib.parse_version_tuple(
            release_version
        ) > lib.parse_version_tuple(version_str):
            version_str = release_version
        version_obj = {
            "downloadURL": download_url,
            "size": ipa_size,
            "version": version_str,
            "buildVersion": meta["buildVersion"],
            "date": ipa_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "localizedDescription": "",
            "minOSVersion": meta["minOSVersion"],
        }

        # Display name, in order of preference: the sources.json override,
        # then the file name without its (rel-…) or (pre-…) suffix for a
        # manual or custom file, then the IPA's own display name.
        custom_entry = custom_meta.get(ipa_path.name, {})
        override = facts.get("display_name", "")
        default_name = override or (
            lib.parse_ipa_filename(ipa_path.name)[0]
            if is_manual
            else meta["name"]
        )

        app_entry = {
            "name": default_name,
            "bundleIdentifier": bundle_id,
            "developerName": facts.get("developer", ""),
            "iconURL": icon_url,
            "localizedDescription": custom_entry.get("description")
            or facts.get("description", ""),
            # No description fallback here.  AltStore shows the subtitle
            # under the name and the description below it, so putting the
            # About text in both prints it twice.
            "subtitle": custom_entry.get("subtitle", ""),
            "tintColor": lib.SOURCE_TINT_COLOR,
            "category": "utilities",
            "versions": [version_obj],
            "appPermissions": {},
            # Top-level convenience fields (AltStore uses these too).
            "version": version_str,
            "versionDate": ipa_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size": ipa_size,
            "downloadURL": download_url,
        }

        if old:
            for key in PRESERVED_AS_IS:
                if key in old:
                    app_entry[key] = old[key]
            for key in PRESERVED_IF_SET:
                # An older run copied the description into the subtitle.
                # Keeping it would preserve the duplicate forever.
                if key == "subtitle" and old.get(key) == old.get(
                    "localizedDescription"
                ):
                    continue
                app_entry[key] = old.get(key) or app_entry[key]

        if override and override != app_entry["name"]:
            changes.append(f"  ✎ {meta['name']}: display name → {override}")
            app_entry["name"] = override

        if not old:
            changes.append(f"  ✦ NEW: {default_name} ({bundle_id})")
        elif old.get("version", "—") != version_str:
            changes.append(
                f"  ↑ {meta['name']}: {old.get('version', '—')} → "
                f"{version_str}"
            )
        else:
            # The version string is unchanged, but a rebuild can still move
            # the binary (build number, size, minOSVersion) or other
            # metadata, so compare the generated entry field by field.  The
            # date is deliberately left out, because it falls back to the
            # file mtime when no release date is known and would churn on
            # every run.
            old_ver0 = (old.get("versions") or [{}])[0]
            changed_fields = [
                key
                for key in COMPARED_ENTRY
                if old.get(key) != app_entry.get(key)
            ]
            changed_fields += [
                key
                for key in COMPARED_VERSION
                if old_ver0.get(key) != version_obj.get(key)
            ]
            if changed_fields:
                changes.append(
                    f"  ✎ {meta['name']}: metadata updated "
                    f"({', '.join(changed_fields)})"
                )
            else:
                print(f"    (unchanged — v{meta['version']})")

        processed[bundle_id] = app_entry
        processed_manual[bundle_id] = is_manual
        processed_file[bundle_id] = ipa_path

    external_count = 0
    for bid, app in existing_apps.items():
        if bid not in processed:
            print(f"  🔗 {app.get('name', bid)} (external — preserved)")
            processed[bid] = app
            external_count += 1

    if external_count:
        print(f"\n  ({external_count} external app(s) preserved)")

    if not changes:
        print("\n✓ No changes. repo.json is already up to date.\n")
        return False

    repo = {
        "name": lib.SOURCE_NAME,
        "identifier": lib.SOURCE_IDENTIFIER,
        "subtitle": lib.SOURCE_SUBTITLE,
        "description": lib.SOURCE_DESCRIPTION,
        "iconURL": lib.SOURCE_ICON_URL,
        "website": lib.SOURCE_WEBSITE,
        "tintColor": lib.SOURCE_TINT_COLOR,
        "apps": list(processed.values()),
    }

    if dry_run:
        print(f"\n── DRY RUN — would write repo.json ──")
        print(json.dumps(repo, indent=2, ensure_ascii=False))
        print(f"\n  {len(changes)} change(s):")
        for c in changes:
            print(c)
        print()
        return True

    lib.write_json(lib.REPO_JSON, repo)
    prune_stale_icons(repo)

    print(f"\n{'=' * 60}")
    print(f"  {len(changes)} change(s):")
    for c in changes:
        print(c)
    print(f"  ✓ repo.json written ({len(processed)} total apps)")
    print(f"{'=' * 60}\n")

    return True


def main(args) -> int:
    generate_repo(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    parser = cli.make_parser(__doc__, token=False)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the changes without writing repo.json",
    )
    sys.exit(cli.run(parser, main))
