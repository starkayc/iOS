#!/usr/bin/env python3
"""
Update source — workflow 2.

Checks every sources.json app against current_releases.json and
downloads only apps whose version/commit changed or whose versioned
asset is missing from the ipa-assets release.  Applies bundle-ID
overrides, picks up manual release drops, records the new state in
current_releases.json, then rebuilds repo.json via generate_repo.py.

If nothing needs downloading, exits cleanly without touching repo.json,
the release, or git — so unchanged runs upload nothing.

Usage:
    python scripts/update_source.py [--github-token TOKEN]
"""

import sys
import zipfile
from typing import Optional

import altstore_lib as lib
from generate_repo import generate_repo

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def update_source(token: Optional[str] = None) -> bool:
    """Fetch changed apps and rebuild repo.json.  Returns True if work
    was done (repo.json may or may not have changed)."""
    print("=" * 60)
    print("  AltStore Source Updater")
    print("=" * 60)

    report = lib.check_for_updates(token)
    entries = report["entries"]
    release_assets = report["release_assets"]

    lib.IPAS_DIR.mkdir(exist_ok=True)

    release_dates: dict[str, str] = {}
    repo_descriptions: dict[str, str] = {}
    developer_names: dict[str, str] = {}
    manual_names: set[str] = set()
    display_names: dict[str, str] = {}

    new_recorded: dict[str, dict] = {}
    old_recorded = lib.load_current_releases()
    downloaded_any = False
    expected_stems: set[str] = set()

    for e in entries:
        name = e["name"]
        repo = e["repo"]
        release_type = e["release_type"]
        source = e["source"]
        info = e["info"]

        if info is None:
            print(f"  ✗ {name}: could not check for updates")
            if name in old_recorded:
                new_recorded[name] = old_recorded[name]
            continue

        value_key = "commit" if release_type == "prerelease" else "version"
        new_recorded[name] = {
            "release_type": release_type,
            value_key: e["latest"],
        }

        expected = e["expected_filename"]
        expected_stems.add(lib.parse_ipa_filename(expected)[0])

        # Skip only when the release has the asset AND the committed
        # repo.json already points at it — otherwise rebuild the entry
        # (recovers from name-scheme migrations and failed runs).
        if (
            not e["changed"]
            and e["asset_in_release"]
            and lib.repo_json_references(expected)
        ):
            print(f"  ✓ {name} up to date  ({e['latest']})")
            continue

        reason = "updated" if e["changed"] else "asset missing — re-fetching"
        print(f"  🔍 {name} — {reason}: {e['recorded']} → {e['latest']}")
        downloaded_any = True

        # Repo "About" text + owner become defaults for new apps.
        try:
            repo_info = lib.github_api(f"{lib.API_BASE}/repos/{repo}", token)
            repo_desc = (repo_info.get("description") or "").strip()
            repo_owner = (repo_info.get("owner", {}) or {}).get("login", "")
            if repo_desc:
                print(
                    f"    About: {repo_desc[:80]}"
                    f"{'…' if len(repo_desc) > 80 else ''}"
                )
        except Exception:
            repo_desc, repo_owner = "", ""

        dest_path = lib.IPAS_DIR / expected
        release = info["release"]

        asset = lib.find_ipa_asset(release, source.get("asset_pattern"))
        if not asset:
            # Some repos ship the IPA zipped (e.g. Ferrite) — extract it.
            zip_assets = [
                a for a in release.get("assets", [])
                if a["name"].lower().endswith(".ipa.zip")
            ]
            if not zip_assets:
                print(f"    ✗ No .ipa asset in {info['tag']}")
                continue
            zip_asset = zip_assets[0]
            print(
                f"    ↓ downloading {zip_asset['name']} "
                f"({zip_asset['size']:,} bytes, zipped) … ",
                end="",
                flush=True,
            )
            tmp_zip = lib.IPAS_DIR / (expected + ".zip")
            try:
                lib.download_file(
                    zip_asset["browser_download_url"], tmp_zip, token
                )
                with zipfile.ZipFile(tmp_zip) as zf:
                    ipa_entry = next(
                        (
                            n for n in zf.namelist()
                            if n.lower().endswith(".ipa")
                        ),
                        None,
                    )
                    ipa_bytes = zf.read(ipa_entry) if ipa_entry else None
                # Unlink after the zip is closed — Windows can't delete
                # a file that still has a handle open.
                tmp_zip.unlink()
                if ipa_bytes is None:
                    print(f"\n    ✗ no .ipa found inside {zip_asset['name']}")
                    continue
                dest_path.write_bytes(ipa_bytes)
                print(f"done (extracted {ipa_entry})")
            except Exception as ex:
                print(f"failed: {ex}")
                tmp_zip.unlink(missing_ok=True)
                continue
        else:
            print(
                f"    ↓ downloading {asset['name']} "
                f"({asset['size']:,} bytes) … ",
                end="",
                flush=True,
            )
            try:
                lib.download_file(asset["browser_download_url"], dest_path, token)
                print("done")
            except Exception as ex:
                print(f"failed: {ex}")
                continue

        # Bundle-ID override: lets two builds of the same app coexist in
        # one source (AltStore re-signs on install).
        if source.get("bundle_id_override"):
            lib.patch_bundle_id(dest_path, source["bundle_id_override"])

        release_dates[expected] = info["published_at"]
        repo_descriptions[expected] = repo_desc
        developer_names[expected] = repo_owner
        if source.get("display_name"):
            display_names[expected] = source["display_name"]

    # ── Pick up IPAs manually dropped onto the ipa-assets release ─────────
    # Anything uploaded there that isn't a current source artifact is a
    # hand-built IPA — download it so it joins the source on the scan.
    # Assets whose stem matches a sources app (stale/older versions) are
    # left alone: sync_release deletes them once repo.json no longer
    # references them.
    for asset_name, a in sorted(release_assets.items()):
        if not asset_name.lower().endswith(".ipa"):
            continue
        local_name = lib.canonical_filename(asset_name)
        if (lib.IPAS_DIR / local_name).exists():
            continue
        if lib.parse_ipa_filename(local_name)[0] in expected_stems:
            continue
        print(f"  📥 manual drop: {asset_name} ({a['size']:,} bytes)")
        try:
            lib.download_file(a["browser_download_url"], lib.IPAS_DIR / local_name, token)
            manual_names.add(local_name)
            release_dates[local_name] = a.get("updated_at", "")
            downloaded_any = True
        except Exception as e:
            print(f"    ✗ failed: {e}")

    print()

    if not downloaded_any:
        print("✓ Nothing to do — all apps up to date.\n")
        return False

    # ── Record the new release state ──────────────────────────────────────
    if new_recorded != old_recorded:
        lib.save_current_releases(new_recorded)
        print("  ✓ current_releases.json updated")

    # ── Rebuild repo.json from the files we downloaded ────────────────────
    fetch_report = {
        "release_dates": release_dates,
        "repo_descriptions": repo_descriptions,
        "developer_names": developer_names,
        "manual_names": manual_names,
        "display_names": display_names,
    }
    return generate_repo(fetch_report=fetch_report, cleanup_losers=True)


def main() -> int:
    token = None
    for arg in sys.argv[1:]:
        if arg.startswith("--github-token="):
            token = arg.split("=", 1)[1]
        else:
            print(f"Unknown argument: {arg}")
            return 1

    update_source(lib.get_token(token))
    return 0


if __name__ == "__main__":
    sys.exit(main())
