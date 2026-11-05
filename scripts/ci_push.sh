#!/usr/bin/env bash
# Push the commit a job just made, rebasing onto whatever the other jobs
# pushed meanwhile. Fails after three attempts.
set -euo pipefail

for _ in 1 2 3; do
    if git push; then
        exit 0
    fi
    git pull --rebase origin main
    sleep 5
done
exit 1
