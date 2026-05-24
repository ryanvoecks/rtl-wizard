#!/usr/bin/env bash
# Runs block-level testbenches (top, core, keymem, encipher, decipher)
set -eo pipefail
cd toolruns
make all
for s in top core keymem encipher decipher; do
    "./${s}.sim" | tee "${s}.log"
    grep -q 'test cases completed successfully' "${s}.log"
done
