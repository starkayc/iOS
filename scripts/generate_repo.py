#!/usr/bin/env python3
"""
AltStore Source Generator — repo.json builder.

Scans the ipas/ folder for .ipa files, extracts metadata (bundle ID,
version, icon, etc.) from each, and generates/updates repo.json.

Preserves manually-set fields from existing repo.json entries
(descriptions, subtitles, developer names, tint colors) so you only
need to write them once.  Apps that reference external download URLs
(not in ipas/) are kept as-is.

Downloading IPAs from GitHub is NOT this module's job — see
update_source.py (which fetches changed apps and then calls
generate_repo) and add_custom_ipa.py (which downloads a single IPA).

The fetch phase passes a ``fetch_report`` with per-file metadata:
  release_dates, repo_descriptions, developer_names (keyed by file
  name), manual_names (hand-dropped/custom files) and display_names
  (sources.json display-name overrides).

Usage:
    python scripts/generate_repo.py                 # scan ipas/ → repo.json
    python scripts/generate_repo.py --dry-run       # show changes, don't write
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import altstore_lib as lib

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── Repo.json management ─────────────────────────────────────────────────────

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


# ── Main ─────────────────────────────────────────────────────────────────────

def generate_repo(
    fetch_report: Optional[dict] = None,
    dry_run: bool = False,
    cleanup_losers: bool = False,
) -> bool:
    """Scan ipas/, update repo.json, return True if changes were made.

    fetch_report (from update_source.py) supplies per-file release dates,
    descriptions and developer names; manual_names marks hand-dropped or
    custom-uploaded files (named after their file).  cleanup_losers
    deletes collision-loser files so their stale release assets get
    cleaned up too — enabled in CI, off for local runs.
    """
    print("=" * 60)
    print("  AltStore Source Generator")
    print("=" * 60)

    report = fetch_report or {}
    release_dates = report.get("release_dates", {})
    repo_descriptions = report.get("repo_descriptions", {})
    developer_names = report.get("developer_names", {})
    manual_names: set[str] = report.get("manual_names", set())
    display_names: dict[str, str] = report.get("display_names", {})
    custom_meta = lib.load_custom_meta()

    # Ensure directories exist.
    lib.ICONS_DIR.mkdir(exist_ok=True)
    lib.IPAS_DIR.mkdir(exist_ok=True)

    # Load existing state.
    existing = load_existing_repo()
    existing_apps = index_by_bundle_id(existing)
    print(f"Loaded existing repo.json — {len(existing_apps)} app(s)")

    # ── Scan IPAs ─────────────────────────────────────────────────────────
    # Normalize file names first (spaces → dashes) so download URLs and
    # release assets always match.
    lib.canonicalize_ipa_files(lib.IPAS_DIR)
    ipa_files = sorted(lib.IPAS_DIR.glob("*.ipa"))
    if not ipa_files:
        print("\nNo .ipa files found in ipas/ folder.")
        print("Add your .ipa files there and run again.\n")
        return False

    print(f"Scanning {len(ipa_files)} IPA(s) in ipas/ …\n")

    processed: dict[str, dict] = {}   # bundle_id → new app entry
    processed_manual: dict[str, bool] = {}  # bundle_id → came from manual drop
    processed_file: dict[str, Path] = {}    # bundle_id → source IPA file
    changes: list[str] = []           # human-readable change log

    for ipa_path in ipa_files:
        print(f"  📦 {ipa_path.name}")

        meta = lib.extract_metadata(ipa_path)
        if not meta:
            continue

        bundle_id = meta["bundleIdentifier"]
        old = existing_apps.get(bundle_id)
        # Manual = hand-dropped onto the release, or a custom upload
        # (tracked in the add_custom_ipa.py sidecar).
        is_manual = (
            ipa_path.name in manual_names
            or ipa_path.name in custom_meta
        )

        # If we've already seen this bundle ID (duplicate IPAs), keep the
        # higher version.  On a tie, a sources.json build wins over a
        # manual drop.
        if bundle_id in processed:
            prev_ver = processed[bundle_id]["version"]
            new_t = lib.parse_version_tuple(meta["version"])
            prev_t = lib.parse_version_tuple(prev_ver)
            keep = (
                new_t > prev_t
                or (new_t == prev_t and processed_manual[bundle_id] and not is_manual)
            )
            if keep:
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
                # Collision losers are dropped so the workflow cleans up
                # their stale release assets too.
                if cleanup_losers:
                    print(f"      (removing {ipa_path.name})")
                    ipa_path.unlink()
                continue

        # Extract icon, falling back to the placeholder when the IPA has
        # none — an empty iconURL breaks Feather's source loading.
        icon_filename = lib.extract_icon(ipa_path, meta["icon_paths"], bundle_id)
        if icon_filename:
            icon_url = f"{lib.PAGES_BASE}/icons/{lib.url_encode_path(icon_filename)}"
        else:
            icon_url = f"{lib.PAGES_BASE}/icons/placeholder.png"

        # IPA size + download URL (served from GitHub Releases, not
        # Pages, to avoid Git-LFS pointer files being served as IPAs).
        # The local file name is canonical: files fetched from sources
        # are uploaded under it by the workflow, and manual drops are
        # downloaded by their asset name, so the two always match.
        ipa_size = ipa_path.stat().st_size
        safe_ipa_name = lib.url_encode_path(ipa_path.name)
        download_url = f"{lib.RELEASE_BASE}/{safe_ipa_name}"

        # Use the GitHub release date when available, otherwise fall
        # back to the file modification time.
        release_date_str = release_dates.get(ipa_path.name)
        if release_date_str:
            ipa_mtime = datetime.fromisoformat(
                release_date_str.replace("Z", "+00:00")
            )
        else:
            ipa_mtime = datetime.fromtimestamp(
                ipa_path.stat().st_mtime, tz=timezone.utc
            )

        # Build the version object.
        version_obj = {
            "downloadURL": download_url,
            "size": ipa_size,
            "version": meta["version"],
            "buildVersion": meta["buildVersion"],
            "date": ipa_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "localizedDescription": "",
            "minOSVersion": meta["minOSVersion"],
        }

        # Display name: sources.json override, then the file name (minus
        # any (rel-…)/(pre-…) suffix) for manual/custom files, then the
        # IPA's own display name.
        custom_entry = custom_meta.get(ipa_path.name, {})
        default_name = display_names.get(ipa_path.name, "") or (
            lib.parse_ipa_filename(ipa_path.name)[0]
            if is_manual
            else meta["name"]
        )

        # ── Merge with existing entry (preserve manual fields) ──────────
        if old:
            app_entry = {
                "name": old.get("name", meta["name"]),
                "bundleIdentifier": bundle_id,
                "developerName": old.get("developerName") or developer_names.get(ipa_path.name, ""),
                "iconURL": icon_url,
                "localizedDescription": old.get("localizedDescription") or custom_entry.get("description") or repo_descriptions.get(ipa_path.name, ""),
                "subtitle": old.get("subtitle") or custom_entry.get("subtitle") or repo_descriptions.get(ipa_path.name, ""),
                "tintColor": old.get("tintColor", lib.SOURCE_TINT_COLOR),
                "category": old.get("category", "utilities"),
                "versions": [version_obj],
                "appPermissions": old.get("appPermissions", {}),
                # Top-level convenience fields (AltStore uses these too).
                "version": meta["version"],
                "versionDate": ipa_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": ipa_size,
                "downloadURL": download_url,
            }
            prev_ver = old.get("version", "—")
            if prev_ver != meta["version"]:
                changes.append(
                    f"  ↑ {meta['name']}: {prev_ver} → {meta['version']}"
                )
            else:
                # Version didn't change, but metadata might have (e.g.
                # back-filled developer name or description).
                meta_keys = (
                    "developerName", "localizedDescription",
                    "subtitle", "iconURL", "downloadURL",
                )
                if any(
                    old.get(k) != app_entry.get(k)
                    for k in meta_keys
                ):
                    changes.append(
                        f"  ✎ {meta['name']}: metadata updated"
                    )
                else:
                    print(f"    (unchanged — v{meta['version']})")
        else:
            # Use the repo "About" text and owner as defaults for new
            # apps.  You can override any field in repo.json and it
            # won't be overwritten on subsequent runs.
            gh_desc = custom_entry.get("description") or repo_descriptions.get(ipa_path.name, "")
            gh_sub = custom_entry.get("subtitle") or repo_descriptions.get(ipa_path.name, "")
            gh_dev = developer_names.get(ipa_path.name, "")
            app_entry = {
                "name": default_name,
                "bundleIdentifier": bundle_id,
                "developerName": gh_dev,
                "iconURL": icon_url,
                "localizedDescription": gh_desc,
                "subtitle": gh_sub,
                "tintColor": lib.SOURCE_TINT_COLOR,
                "category": "utilities",
                "versions": [version_obj],
                "appPermissions": {},
                "version": meta["version"],
                "versionDate": ipa_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": ipa_size,
                "downloadURL": download_url,
            }
            changes.append(f"  ✦ NEW: {default_name} ({bundle_id})")

        processed[bundle_id] = app_entry
        processed_manual[bundle_id] = is_manual
        processed_file[bundle_id] = ipa_path

    # ── Preserve external-only apps ───────────────────────────────────────
    external_count = 0
    for bid, app in existing_apps.items():
        if bid not in processed:
            print(f"  🔗 {app.get('name', bid)} (external — preserved)")
            processed[bid] = app
            external_count += 1

    if external_count:
        print(f"\n  ({external_count} external app(s) preserved)")

    # ── Check if anything changed ─────────────────────────────────────────
    if not changes:
        print("\n✓ No changes. repo.json is already up to date.\n")
        return False

    # ── Build output ──────────────────────────────────────────────────────
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

    # Write.
    with open(lib.REPO_JSON, "w", encoding="utf-8") as f:
        json.dump(repo, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\n{'=' * 60}")
    print(f"  {len(changes)} change(s):")
    for c in changes:
        print(c)
    print(f"  ✓ repo.json written ({len(processed)} total apps)")
    print(f"{'=' * 60}\n")

    return True


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    changed = generate_repo(dry_run=dry)
    sys.exit(0)
