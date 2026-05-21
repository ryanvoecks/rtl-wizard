#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$WORKSPACE/.env.example"
ENV_FILE="$WORKSPACE/.env"

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

echo "Generating Claude Code OAuth token..." >&2
RAW="$(claude setup-token)"
TOKEN="$(printf '%s' "$RAW" | grep -aoE 'sk-ant-oat[0-9]+-[A-Za-z0-9_-]+' | tail -n 1)"

if [[ -z "$TOKEN" ]]; then
    echo "error: no token captured from 'claude setup-token'" >&2
    exit 1
fi

awk -v tok="$TOKEN" '{ gsub(/\{token\}/, tok); print }' "$TEMPLATE" > "$ENV_FILE"

echo "wrote $ENV_FILE" >&2
