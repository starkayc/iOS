#!/usr/bin/env python3
"""
Update source — workflow 2.

Checks every sources.json app against current_releases.json and
downloads only apps whose version/commit changed or whose versioned
asset is missing from the ipa-assets release.  Applies bundle-ID
overrides, picks up manual release drops, records the new state in
current_releases.json, then rebuilds repo.json via generate_repo.py.

If nothing needs downloading, exits cleanly without touching repo.json,
the release, or git — so unchanged runs upload nothing.  If an app's
download fails, its recorded version is left untouched (so the next run
retries it) and the script exits non-zero.

Usage:
    python scripts/update_source.py [--github-token TOKEN] [--debug]
"""

import sys
from typing import Optional

import altstore_lib as lib
import cli_common as cli
from generate_repo import generate_repo


def update_source_report(
    token: Optional[str] = None,
) -> tuple[bool, list[str]]:
    """Fetch changed apps and rebuild repo.json.

    Returns ``(worked, failures)``: ``worked`` is True if any work was
    done (repo.json may or may not have changed); ``failures`` lists the
    apps that could not be fetched (or manual drops that failed), so the
    caller can exit non-zero and surface exactly what went wrong.
    """
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
    failures: list[str] = []
    expected_stems: set[str] = set()

    for e in entries:
        name = e["name"]
        repo = e["repo"]
        release_type = e["release_type"]
        source = e["source"]
        info = e["info"]

        if info is None:
            cli.error(f"{name}: could not determine the latest release")
            if name in old_recorded:
                new_recorded[name] = old_recorded[name]
            continue

        value_key = "commit" if release_type == "prerelease" else "version"
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
            new_recorded[name] = {"release_type": release_type,
                                  value_key: e["latest"]}
            print(f"  ✓ {name} up to date  ({e['latest']})")
            continue

        reason = "updated" if e["changed"] else "asset missing — re-fetching"
        print(f"  🔍 {name} — {reason}: {e['recorded']} → {e['latest']}")

        # Repo "About" text + owner become defaults for new apps.
        try:
            repo_info = lib.github_api(f"{lib.API_BASE}/repos/{repo}", token) or {}
            repo_desc = (repo_info.get("description") or "").strip()
            repo_owner = (repo_info.get("owner", {}) or {}).get("login", "")
            if repo_desc:
                print(
                    f"    About: {repo_desc[:80]}"
                    f"{'…' if len(repo_desc) > 80 else ''}"
                )
        except Exception as ex:
            cli.warn(f"{name}: could not read repo info ({repo}): {ex}")
            repo_desc, repo_owner = "", ""

        dest_path = lib.IPAS_DIR / expected
        if not lib.fetch_ipa_from_release(
            info["release"], source, dest_path, token
        ):
            cli.error(f"{name}: could not download {expected}")
            failures.append(name)
            # Do NOT record the new version — the download failed, so
            # leave the recorded value so the next run retries this app.
            if name in old_recorded:
                new_recorded[name] = old_recorded[name]
            continue

        downloaded_any = True
        new_recorded[name] = {"release_type": release_type,
                              value_key: e["latest"]}

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
            lib.download_file(
                a["browser_download_url"], lib.IPAS_DIR / local_name, token
            )
            manual_names.add(local_name)
            release_dates[local_name] = a.get("updated_at", "")
            downloaded_any = True
        except Exception as ex:
            cli.error(f"manual drop {asset_name} failed: {ex}")
            failures.append(asset_name)

    print()

    if not downloaded_any:
        print("✓ Nothing to do — all apps up to date.\n")
        return False, failures

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
    worked = generate_repo(fetch_report=fetch_report, cleanup_losers=True)
    return worked, failures


def update_source(token: Optional[str] = None) -> bool:
    """Thin wrapper returning just the 'did work' flag (used by tests)."""
    worked, _failures = update_source_report(token)
    return worked


def main(args) -> int:
    token = lib.get_token(args.token)
    if not token:
        cli.warn(
            "no GitHub token found — running unauthenticated (rate-limited). "
            "Pass --token, set GITHUB_TOKEN/GH_TOKEN, or save .github-token."
        )
    _worked, failures = update_source_report(token)
    if failures:
        cli.error(
            f"{len(failures)} app(s) could not be fetched: "
            + ", ".join(failures)
        )
        return 1
    return 0


if __name__ == "__main__":
    parser = cli.make_parser(__doc__)
    sys.exit(cli.run(parser, main))
