#!/usr/bin/env bash
set -euo pipefail

for _ in 1 2 3; do
    if git push; then
        exit 0
    fi
    git pull --rebase origin master
    sleep 5
done
exit 1
