#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Initializing git submodules..." >&2
git submodule update --init --recursive

echo "Applying submodule patches..." >&2
for diff in "$WORKSPACE/common/patches/"*.diff; do
    [[ -e "$diff" ]] || continue
    repo="$WORKSPACE/external/$(basename "$diff" .diff)"
    git -C "$repo" apply --check -R "$diff" 2>/dev/null || git -C "$repo" apply "$diff"
done

echo "Syncing uv environment..." >&2
uv sync
