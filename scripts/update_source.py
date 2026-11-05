#!/usr/bin/env python3
"""
Update source.

Usage:
    python scripts/update_source.py [--github-token TOKEN] [--debug]
"""

import html
import re
import sys
from pathlib import Path
from typing import Optional

import altstore_lib as lib
import cli_common as cli
import ipa
import release as rel
from generate_repo import generate_repo

CHANGELOG_LIMIT = 4000

# Release notes are written in Markdown, and neither AltStore nor Feather
# renders it in the What's New section, so the syntax has to go.
_MD_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_FENCE = re.compile(r"^[ \t]*```.*$", re.MULTILINE)
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.MULTILINE)
_MD_ALERT = re.compile(
    r"^[ \t]{0,3}(?:>[ \t]?)*\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]"
    r"[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_MD_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_MD_RULE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.MULTILINE)
_MD_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_MD_TASK = re.compile(r"^([•\-])[ \t]*\[([ xX])\][ \t]*", re.MULTILINE)
_MD_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_MD_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_MD_EM_STAR = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])")
_MD_EM_UNDER = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)_(?![\w_])")
_MD_BLANK_RUN = re.compile(r"\n{3,}")


def _markdown_as_text(body: str) -> str:
    text = _MD_COMMENT.sub("", body.replace("\r\n", "\n").replace("\r", "\n"))
    text = _MD_FENCE.sub("", text)
    text = _MD_IMAGE.sub(lambda m: m.group(1), text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _MD_AUTOLINK.sub(lambda m: m.group(1), text)
    text = _MD_RULE.sub("", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_ALERT.sub("", text)
    text = _MD_QUOTE.sub("", text)
    text = _MD_BULLET.sub("• ", text)
    text = _MD_TASK.sub(
        lambda m: f"{m.group(1)} [{'x' if m.group(2).lower() == 'x' else ' '}] ",
        text,
    )
    text = _MD_STRONG.sub(lambda m: m.group(2), text)
    text = _MD_STRIKE.sub(lambda m: m.group(1), text)
    text = _MD_EM_STAR.sub(lambda m: m.group(1), text)
    text = _MD_EM_UNDER.sub(lambda m: m.group(1), text)
    text = text.replace("`", "")
    text = html.unescape(text)

    lines = [line.strip() for line in text.splitlines()]
    text = _MD_BLANK_RUN.sub("\n\n", "\n".join(lines))
    return text.strip()


def _changelog(release: dict) -> str:
    body = _markdown_as_text(release.get("body") or "")
    if len(body) <= CHANGELOG_LIMIT:
        return body
    cut = body.rfind("\n", 0, CHANGELOG_LIMIT)
    return body[:cut if cut > 0 else CHANGELOG_LIMIT].rstrip() + "…"


def _repo_facts(repo: str, token: Optional[str]) -> tuple[str, str]:
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
) -> tuple[rel.FetchOutcome, dict]:
    source = entry["source"]
    description, developer = _repo_facts(entry["repo"], token)
    outcome = rel.fetch_ipa_from_release(
        entry["info"]["release"], source, dest, token
    )
    if outcome.missing or outcome.problem:
        return outcome, {}
    if source.get("bundle_id_override"):
        problem = _apply_bundle_id(dest, source["bundle_id_override"])
        if problem:
            return rel.FetchOutcome(missing=False, problem=problem), {}
    facts = {
        "date": entry["info"]["published_at"],
        "description": description,
        "developer": developer,
        "display_name": source.get("display_name", ""),
        "changelog": _changelog(entry["info"]["release"]),
    }
    if entry["release_type"] != "prerelease":
        # A build can keep CFBundleShortVersionString constant (osu! ships
        # 1.0 for every release) while its tag moves.  AltStore only offers an
        # update when the source's version is higher, so generate_repo gets
        # the tracked release version to compare against the plist's.
        facts["version"] = entry["latest"]
    return rel.FetchOutcome(missing=False, problem=""), facts


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
    expected_stems = {
        lib.ipa_stem(e["source"]["name"]) for e in entries
    }

    def _keep_recorded(app_name: str) -> None:
        if app_name in old_recorded:
            new_recorded[app_name] = old_recorded[app_name]
        else:
            new_recorded.pop(app_name, None)

    def _defer(app_name: str, drop_file: Optional[Path] = None) -> None:
        failures.append(app_name)
        _keep_recorded(app_name)
        if drop_file is not None:
            drop_file.unlink(missing_ok=True)

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
        expected = e["expected_filename"]

        if info is None:
            cli.error(f"{name}: could not determine the latest release")
            # A channel with nothing publishable is not a failure; only an
            # API error is, because that leaves the app unread, not skipped.
            if e["api_error"]:
                _defer(name)
            else:
                _keep_recorded(name)
            continue

        if (
            not e["changed"]
            and e["asset_in_release"]
            and lib.repo_json_references(expected, repo_assets)
        ):
            new_recorded[name] = lib.release_record(
                e["source"], release_type, e["latest"], fetched=True
            )
            print(f"  ✓ {name} up to date  ({e['latest']})")
            continue

        reason = "updated" if e["changed"] else "asset missing — re-fetching"
        print(f"  🔍 {name} — {reason}: {e['recorded']} → {e['latest']}")

        dest_path = lib.IPAS_DIR / expected
        outcome, facts = _fetch_one(e, token, dest_path)
        if outcome.missing:
            cli.warn(
                f"{name}: {info['release'].get('tag_name', '?')} has no "
                ".ipa asset yet — skipping"
            )
            _keep_recorded(name)
            continue
        if outcome.problem:
            cli.error(f"{name}: {outcome.problem}")
            _defer(name, dest_path)
            continue

        downloaded_any = True
        new_recorded[name] = lib.release_record(
            e["source"], release_type, e["latest"], fetched=True
        )
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
