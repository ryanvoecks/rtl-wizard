#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Initializing and restoring git submodules..." >&2
git submodule update --init --recursive --force
git submodule foreach --recursive 'git reset --hard && git clean -fdx'

echo "Applying submodule patches..." >&2
for diff in "$WORKSPACE/common/patches/"*.diff; do
    [[ -e "$diff" ]] || continue
    repo="$WORKSPACE/external/$(basename "$diff" .diff)"
    git -C "$repo" apply "$diff"
done

echo "Syncing uv environment..." >&2
uv sync
