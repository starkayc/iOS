#!/usr/bin/env python3
"""
Update source, workflow 2.

Checks every sources.json app against current_releases.json and downloads
only the apps whose version or commit changed, or whose versioned asset is
missing from the ipa-assets release.  It applies bundle-ID overrides,
picks up manual release drops, records the new state in
current_releases.json, and rebuilds repo.json through generate_repo.py.

When nothing needs downloading, the script exits without touching
repo.json, the release or git, so an unchanged run uploads nothing.  A
release whose IPA asset is not attached yet, such as ppy/osu which ships
it separately, is skipped rather than failed.  When a download fails, or
the downloaded file is not a usable IPA, the recorded version stays
untouched so the next run retries it, and the script exits non-zero and
names the app.

Usage:
    python scripts/update_source.py [--github-token TOKEN] [--debug]
"""

import sys
from pathlib import Path
from typing import Optional

import altstore_lib as lib
import cli_common as cli
import ipa
import release as rel
from generate_repo import generate_repo


def _repo_facts(repo: str, token: Optional[str]) -> tuple[str, str]:
    """A repo's About text and owner login (defaults for a new app)."""
    try:
        info = lib.github_api(f"{lib.API_BASE}/repos/{repo}", token) or {}
    except Exception as ex:
        cli.warn(f"{repo}: could not read repo info: {ex}")
        return "", ""
    description = (info.get("description") or "").strip()
    if description:
        print(
            f"    About: {description[:80]}"
            f"{'…' if len(description) > 80 else ''}"
        )
    return description, (info.get("owner", {}) or {}).get("login", "")


def _apply_bundle_id(dest: Path, bundle_id: str) -> str:
    """Rewrite the IPA's bundle ID; "" on success, else the reason.

    AltStore re-signs on install, so two builds of the same app coexist
    only if one of them carries a different ID.  An unpatched build
    collides with the base app, so this is a failure, not a warning.
    """
    try:
        patched = ipa.patch_bundle_id(dest, bundle_id)
    except Exception as ex:
        return f"bundle-ID patch crashed: {type(ex).__name__}: {ex}"
    return "" if patched else f"could not patch the bundle ID to {bundle_id}"


def _fetch_one(
    entry: dict, token: Optional[str], dest: Path
) -> tuple[Optional[str], dict]:
    """Fetch one app's IPA and apply its bundle-ID override.

    Returns ``(problem, facts)``.  ``problem`` carries the same meaning as
    in release.fetch_ipa_from_release.  None means the release has no IPA
    asset yet, so skip it and retry next run.  An empty string means
    ``dest`` is ready to publish.  Anything else is the reason it is not.
    ``facts`` is what generate_repo needs to know about that file, and is
    empty unless the fetch worked.
    """
    source = entry["source"]
    description, developer = _repo_facts(entry["repo"], token)
    problem = rel.fetch_ipa_from_release(
        entry["info"]["release"], source, dest, token
    )
    if problem or problem is None:
        return problem, {}
    if source.get("bundle_id_override"):
        problem = _apply_bundle_id(dest, source["bundle_id_override"])
        if problem:
            return problem, {}
    facts = {
        "date": entry["info"]["published_at"],
        "description": description,
        "developer": developer,
        "display_name": source.get("display_name", ""),
    }
    if entry["release_type"] == "stable":
        # A build can keep CFBundleShortVersionString constant (osu! ships
        # 1.0 for every release) while its tag moves.  AltStore only offers an
        # update when the source's version is higher, so generate_repo gets
        # the tracked release version to compare against the plist's.
        facts["version"] = entry["latest"]
    return "", facts


def update_source_report(
    token: Optional[str] = None,
) -> tuple[bool, list[str]]:
    """Fetch changed apps and rebuild repo.json.

    Returns ``(worked, failures)``.  ``worked`` is True when the run did
    any work, whether or not repo.json changed.  ``failures`` lists the
    apps that could not be fetched, including manual drops that failed,
    so the caller can exit non-zero and report them.
    """
    print("=" * 60)
    print("  AltStore Source Updater")
    print("=" * 60)

    report = lib.check_for_updates(token)
    entries = report["entries"]
    release_assets = report["release_assets"]

    lib.IPAS_DIR.mkdir(exist_ok=True)

    per_app: dict[str, dict] = {}
    new_recorded: dict[str, dict] = {}
    old_recorded = lib.load_current_releases()
    repo_assets = lib.repo_json_asset_names()
    downloaded_any = False
    failures: list[str] = []
    # Every sources.json app owns files named "<stem>.rel|pre-….ipa".  An
    # asset that carries one of these stems is an older version of a watched
    # app, never a manual drop, even when this run could not reach its API.
    expected_stems = {
        lib.ipa_stem(e["source"]["name"]) for e in entries
    }

    def _keep_recorded(app_name: str) -> None:
        """Leave the previously recorded version so the next run retries."""
        if app_name in old_recorded:
            new_recorded[app_name] = old_recorded[app_name]
        else:
            new_recorded.pop(app_name, None)

    def _defer(app_name: str, drop_file: Optional[Path] = None) -> None:
        """Mark an app failed: keep its recorded version, drop its file.

        The caller then ``continue``s, so the version is never recorded as
        installed and the next run retries the app.
        """
        failures.append(app_name)
        _keep_recorded(app_name)
        if drop_file is not None:
            drop_file.unlink(missing_ok=True)

    # A manual drop exists only on the release until it is ingested.  List
    # every candidate up front, so the download loop below only touches
    # files that are not already on disk, not a watched app, and not yet
    # referenced by repo.json.
    manual_candidates: set[str] = set()
    for name in release_assets:
        if not name.lower().endswith(".ipa"):
            continue
        local_name = lib.canonical_filename(name)
        if (lib.IPAS_DIR / local_name).exists():
            continue
        if lib.parse_ipa_filename(local_name)[0] in expected_stems:
            continue
        if lib.repo_json_references(local_name, repo_assets):
            continue
        manual_candidates.add(name)

    for e in entries:
        name = e["name"]
        release_type = e["release_type"]
        info = e["info"]
        value_key = "commit" if release_type == "prerelease" else "version"
        expected = e["expected_filename"]

        if info is None:
            cli.error(f"{name}: could not determine the latest release")
            _keep_recorded(name)
            continue

        # Skip only when the release holds the asset and the committed
        # repo.json already points at it.  Otherwise rebuild the entry,
        # which recovers from a name-scheme change or a failed run.
        if (
            not e["changed"]
            and e["asset_in_release"]
            and lib.repo_json_references(expected, repo_assets)
        ):
            new_recorded[name] = {"release_type": release_type,
                                  value_key: e["latest"]}
            print(f"  ✓ {name} up to date  ({e['latest']})")
            continue

        reason = "updated" if e["changed"] else "asset missing — re-fetching"
        print(f"  🔍 {name} — {reason}: {e['recorded']} → {e['latest']}")

        dest_path = lib.IPAS_DIR / expected
        problem, facts = _fetch_one(e, token, dest_path)
        if problem is None:
            # A new version is published but no IPA is attached yet, so
            # there is nothing to download.  Leave the recorded value and
            # retry next run instead of failing the workflow.
            cli.warn(
                f"{name}: {info['release'].get('tag_name', '?')} has no "
                ".ipa asset yet — skipping"
            )
            _keep_recorded(name)
            continue
        if problem:
            cli.error(f"{name}: {problem}")
            _defer(name, dest_path)
            continue

        downloaded_any = True
        new_recorded[name] = {"release_type": release_type,
                              value_key: e["latest"]}
        per_app[expected] = facts

    for asset_name in sorted(manual_candidates):
        a = release_assets[asset_name]
        local_name = lib.canonical_filename(asset_name)
        print(f"  📥 manual drop: {asset_name} ({a['size']:,} bytes)")
        target = lib.IPAS_DIR / local_name
        problem = rel.ingest_ipa(
            a["browser_download_url"], target, a.get("size"), token
        )
        if problem:
            cli.error(f"manual drop {asset_name}: {problem}")
            _defer(asset_name, target)
            continue
        per_app[local_name] = {
            "date": a.get("updated_at", ""),
            "manual": True,
        }
        downloaded_any = True

    print()

    if not downloaded_any:
        print("✓ Nothing to do — all apps up to date.\n")
        return False, failures

    if new_recorded != old_recorded:
        lib.save_current_releases(new_recorded)
        print("  ✓ current_releases.json updated")

    worked = generate_repo(per_app, cleanup_losers=True)
    return worked, failures


def update_source(token: Optional[str] = None) -> bool:
    """Return only the "did work" flag, for the tests that call it."""
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
