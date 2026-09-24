#!/usr/bin/env python3
"""
Drop-in IPA uploader.

Drop your hand-built IPAs into ipas/ (or any folder) and run:

    python scripts/upload_ipa.py

Each IPA is uploaded to the ipa-assets GitHub release, repo.json is
regenerated (new apps are named after their file, e.g. "MyApp 2.0.ipa"
becomes "MyApp 2.0"), and the changes are committed and pushed.

Auth — needs a GitHub token with write access to this repo.  Supply it
one of three ways (in order of precedence):
  1. --token ghp_...
  2. the GITHUB_TOKEN / GH_TOKEN environment variable
  3. a file named .github-token next to this script (gitignored)

Usage:
    python scripts/upload_ipa.py [--folder PATH] [--token TOKEN] [--no-push]
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Import the generator and release client from this same folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_repo import generate_repo  # noqa: E402
from sync_release import GitHubRelease, get_token  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    folder = REPO_ROOT / "ipas"
    token_flag: Optional[str] = None
    push = True

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--folder":
            folder = Path(args[i + 1])
            i += 2
        elif arg.startswith("--token="):
            token_flag = arg.split("=", 1)[1]
            i += 1
        elif arg == "--no-push":
            push = False
            i += 1
        else:
            print(f"Unknown argument: {arg}")
            sys.exit(1)

    token = get_token(token_flag)
    if not token:
        print(
            "No GitHub token found.  Create a token with repo access at\n"
            "https://github.com/settings/tokens and pass it with --token,\n"
            "set the GITHUB_TOKEN environment variable, or save it in a\n"
            "file named .github-token (it's gitignored)."
        )
        sys.exit(1)

    ipa_files = sorted(folder.glob("*.ipa"))
    if not ipa_files:
        print(f"No .ipa files found in {folder}")
        sys.exit(1)

    print("=" * 60)
    print("  Drop-in IPA uploader")
    print("=" * 60)

    client = GitHubRelease(token)
    if not client.get_release():
        client.create_release()

    uploaded = set()
    for ipa_path in ipa_files:
        name = ipa_path.name
        print(
            f"  ↑ uploading {name} ({ipa_path.stat().st_size:,} bytes) … ",
            end="",
            flush=True,
        )
        try:
            client.upload_asset(name, ipa_path.read_bytes())
            print("done")
            uploaded.add(name)
        except Exception as e:
            print(f"failed: {e}")

    if not uploaded:
        print("\nNothing was uploaded.")
        sys.exit(1)

    # Regenerate repo.json — new apps are named after their file name.
    print()
    generate_repo(manual_names=uploaded)

    if not push:
        print("\nSkipping commit/push (--no-push).")
        return

    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "add", "repo.json", "icons"],
        check=True,
    )
    if subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--cached", "--quiet"]
    ).returncode == 0:
        print("\nNothing to commit — repo.json is already up to date.")
        return

    names = ", ".join(sorted(uploaded))
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "commit", "-m", f"chore: add {names}"],
        check=True,
    )
    subprocess.run(["git", "-C", str(REPO_ROOT), "push"], check=True)
    print("\n✓ Pushed. AltStore will pick up the new app(s) on next refresh.")


if __name__ == "__main__":
    main()
