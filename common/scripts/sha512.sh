#!/usr/bin/env bash
# Upstream ships a core-level TB (tb_sha512_core.v) alongside the top-level
# TB. Running both isolates wrapper vs. core failures.
set -eo pipefail
cd toolruns
make all
for s in top core; do
    "./${s}.sim" | tee "${s}.log"
    grep -q 'test cases completed successfully' "${s}.log"
done
