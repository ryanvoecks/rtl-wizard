#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$WORKSPACE/.env.example"
ENV_FILE="$WORKSPACE/.env"

echo "Initializing git submodules..." >&2
git submodule update --init --recursive

echo "Applying patches..." >&2
for patch_file in "$WORKSPACE"/common/patches/*.diff; do
    name=$(basename "$patch_file" .diff)
    target="$WORKSPACE/external/$name"
    patch -p1 -d "$target" --dry-run -R -s -i "$patch_file" >/dev/null 2>&1 \
        || patch -p1 -d "$target" --no-backup-if-mismatch -i "$patch_file"
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
