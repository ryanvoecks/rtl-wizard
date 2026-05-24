# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

`rtl-wizard` is a research harness for evaluating LLM agents at the task of **post-synthesis timing optimisation** on real open-source RTL. It has two halves that share the same design registry:

1. **`eda_eval/`** -- non-LLM ORFS flow that runs every design through `synth -> floorplan -> place -> CTS -> route` on nangate45, calibrates per-design clock periods, and produces ranked logical-path / congestion reports off the routed `.odb`.
2. **`llm_eval/`** -- drives a Claude Code agent (OAUTH-token, not API key) against a sandboxed copy of a single design, exposing the EDA flow as MCP tools (`run_testbench`, `synth_timing_report`) so the agent can iterate. Scored by `yosys` synthesisability plus the design's upstream testbench.

`common/` is the shared spine: `common/config.py` defines `StudyConfig`/`DesignConfig`/`TargetConfig`/`RunConfig` and resolves all path constants; `common/designs.py` is a hand-curated registry of every design; `common/scripts/<name>.sh` is the per-design testbench runner.

## Setup

```bash
./setup.sh                                 # submodules + patches + uv sync + claude OAUTH token -> .env
```

Requires `uv`, `docker`, and (for `llm_eval`) `claude setup-token` having been run. Python pinned to 3.12. The devcontainer (`.devcontainer/Dockerfile`) builds OpenROAD-flow-scripts + Verilator from source so `yosys`/`openroad`/`klayout`/`verilator` are on PATH inside the container. First build is long; subsequent rebuilds reuse the cache.

Submodule patches live in `common/patches/<repo>.diff` and are applied idempotently by `setup.sh`. If you re-clone a submodule manually, re-run `setup.sh` so its patch is re-applied (otherwise testbenches / cell-count targets drift from what's documented in `common/designs.py`).

## Commands

```bash
# Run the full ORFS flow over every (benchmark, name, variant) design
uv run eda_eval/run.py

# Restrict by glob -- each flag is repeatable; --variant matches "reference", "claude", etc.
uv run eda_eval/run.py --benchmark secworks --name aes

# Iterative k-based calibration for a single design (converges on clock period for target slack/period ratio)
uv run eda_eval/calibrate.py --benchmark secworks --name aes

# Find the optimisation "sweet spot" period: tightest T at which fix scope stays small
uv run eda_eval/scope.py --benchmark secworks --name aes

# Analyse a routed phase dir -> critical_paths.rpt, logical_paths.rpt, congestion_hotspots.rpt
uv run eda_eval/analyse.py eda_results/<batch>/<benchmark>/<name>/<variant>/

# Run the LLM eval (Claude Code agent against a single hard-coded target, currently AES)
uv run python llm_eval/run.py
uv run python llm_eval/view.py                   # browse llm_results/ in inspect view

# Pre-commit (matches CI)
uv run pre-commit run --all-files
```

Outputs go to `eda_results/<timestamp>/...` and `llm_results/<timestamp>/...`; both are gitignored. CI (`.github/workflows/integrate.yaml`) runs only `pre-commit` -- pyright + ruff (`check` and `format --check`) + trailing-whitespace / EOF / AST / ASCII-only checks.

## Architecture

### The design registry (`common/designs.py`)

Every design is a `DesignConfig` (frozen dataclass) emitted inline in `designs.py`, aggregated into a 3-level `all_designs: {benchmark: {name: {variant: DesignConfig}}}` tree. Each entry locks down:

- `rtl_files` (ordered tuple, paths relative to `rtl_dir`): explicit because RTLLM-style auto-globbing breaks on repos that ship sim-only TB models next to RTL.
- `top_module`, `clock_port` (default `clk` -- override per design, several use `clk_i`/`CLK`/`i_controller_clk`).
- `test_script` (`common/scripts/<name>.sh`): runs from `design.root`, returns 0 on pass. The script is the *only* contract for testbench pass/fail -- `DesignConfig.run_tb()` shells out to it and treats stdout/stderr as opaque diagnostics.
- `tb_timeout_s` per design -- defaults to 60s, raised for slow benches (jpeg_encoder at 600, wb_dma at 600).
- `include_dirs` for designs with `\`include` directives (e203 needs `core/`, jpeg_encoder needs `dct/rtl/verilog/`).
- Optional `pre_cts_tcl`: written to `inputs/pre_cts.tcl` and exported as `PRE_CTS_TCL` so ORFS's `source_step_tcl PRE CTS` hook picks it up. Use for storage-heavy designs that need repair_timing's `max_buffer_percent` raised.

Each design has comments above its block documenting *why* the file list and patch are what they are -- read those before editing, they record load-bearing decisions (e.g. uberddr3 only synthesizes the controller, not the PHY; verilog_axi uses the 8x8 wrapper not the parameterized crossbar; e203 deliberately removes ITCM/DTCM RAMs).

When adding a design: drop a `.diff` in `common/patches/` (applied by `setup.sh`), add a `<name>.sh` test script, add the entry under the right benchmark in `all_designs`, and document it in `docs/designs.md` (single source of truth for cell counts and modifications).

### Two-phase ORFS run (`eda_eval/run.py`)

Per `(benchmark, name)` group, the runner first executes a `__calibration__` pass on the `reference` variant at a loose period (`StudyConfig.calibration_period_ns`, default 10ns) and a large die (1000um square). Phase 1 reads back `route_ws_ns` and `synth_area_um2` to compute the per-group `period_ns` (`(calibration_period - ws) * target_multiplier`) and `side_um` (from `area_multiplier * cell_area`). Phase 2 then runs every variant in the group at those derived values.

Calibration failures are tracked per group: the variants in a failed group still appear in `runs.csv` with an `error` field but are skipped at phase 2. Per-key NaN (not zero) is the signal for "measurement unavailable" so downstream tools' mean aggregations filter the rows out correctly. **Do not return zeros** when a metric is missing.

`run_job` materialises one `inputs/Makefile` per phase from `eda_eval/templates/Makefile.template` (parameterised on top module / RTL files / SDC / floorplan / flow targets). The two flow target groups are `SYNTH_FLOW_TARGETS = ("synth", "synth-report")` for synth-only runs (used by the MCP `synth_timing_report` tool) and `ALL_FLOW_TARGETS` for full P&R.

### Calibration scripts -- three modes, different jobs

There are three calibration entry points; pick the right one:

- `eda_eval/run.py` -- single-shot calibration (10ns + 1000um) per group, then full P&R at the derived period for every variant. The default end-to-end flow.
- `eda_eval/calibrate.py` -- two-phase fmax convergence for a single design. (1) Loose-period synth-only run on the large calibration die; the post-synth cell area feeds `derive_final_side_um` to lock in the target floorplan. (2) Iterate synth-only with `t_next = t - synth_wns_ns` until consecutive runs differ on fmax by less than `--tolerance` (default 2.5%). (3) Switch to full synth + P&R starting from the synth-converged period and iterate `t_next = t - route_ws_ns` to the same tolerance. Writes `calibration.json` plus per-iteration phase dirs under `eda_results/<batch>/__iter_calibration__/<benchmark>/<name>/{iter_synth_<i>,iter_pnr_<j>}/` -- the directory naming is what `llm_eval/generate.py` and `_synth_and_report` look for (they expect `iter_pnr_0` for the first routed output).
- `eda_eval/scope.py` -- three-phase scoping: (A) fixed-point to `T_baseline` with `k=0`, (B) shrink period until fix scope (modules / start stems / LOC touched by negative-slack paths) exceeds budget, (C) bisect to refine `T_sweet`. Used to decide whether a design is interesting to optimise at all.

### Logical-path analysis (`eda_eval/analyse.py`)

`analyse.py` and `analyse_synth.py` share extraction logic (`analyse_synth` imports the helpers). The two differ only by which ODB they load: `6_final.odb`+SPEF (post-route, post-parasitics) vs `1_synth.odb` (post-synth, zero-RC estimate). The post-synth version is what the MCP `synth_timing_report` tool returns to the agent.

The pipeline: pipe a TCL script into one `openroad -no_init -exit` invocation that runs `extract_critical_paths.tcl` (samples a `pool` of worst paths via `find_timing_paths`, emits TSV of `slack | startpoint | endpoint | cells`) and `extract_congestion.tcl` (enriches the GR-emitted overflow tiles with the instances + IO bterms driving each contributing net). Then yosys dumps a hierarchy JSON (`read_verilog ... ; hierarchy -top <top>; proc; write_json`) so each timing path's cell chain can be mapped back to the union of containing RTL modules + their LOC. **Do not run `flatten` or `synth`** in that yosys pass -- you want one cells entry per submodule instantiation, not a primitive-level netlist.

The "logical group" of a path is `(start_stem, end_stem)` after stripping yosys cell-type suffixes (`$_DFFE_*`) and array indices (`[N]`). Groups are ranked by `(worst_slack, -count)` so high-multiplicity ties break in favour of the more-replicated path.

Congestion rpt selection priority (most-final wins) is `congestion_post_recover_power -> ..._repair_timing -> ..._repair_design -> congestion.rpt`. ORFS only writes these when overflow tiles exist; an absent file is the *clean routing* case, not an error.

### LLM eval -- agent + sandbox + MCP server

`llm_eval/run.py` is the entry point; it creates `llm_results/<timestamp>/`, asks `inspect_eval` to run the `optimize_timing` task, and writes the `.eval` log alongside per-sample artifacts (`diff.patch`, `synthesis.log`, `testbench.log`, `target_config.json`).

The task is built from `common/targets.py` (currently just `aes_target`, a calibrated AES variant) -- each `TargetConfig` carries the design + the calibrated `(period_ns, side_um)`. Per sample, `_build_sample` mounts the design's RTL into the sandbox under `rtl/<file>` (`SANDBOX_RTL_ROOT = "rtl"`).

`claude_code_solver` (in `llm_eval/components/solvers.py`) is a **custom Inspect agent** that shells out to `claude -p --output-format stream-json --strict-mcp-config ...` inside the sandbox and translates each NDJSON event back into Inspect `ChatMessage{Assistant,Tool}` objects so `inspect view` renders the transcript correctly. Auth is OAUTH-token-only (`CLAUDE_CODE_OAUTH_TOKEN` from `.env`); `ANTHROPIC_API_KEY` is deliberately *not* forwarded because the Claude CLI prefers it over the OAUTH token. Model name comes from Inspect's `--model` flag at solve time via `active_model()`. Resumes across multiple `solve` calls are wired through `--resume <session_id>` (session id stored in `store()`), though the current task only invokes `claude` once per sample.

**MCP topology** (this is load-bearing -- read it before touching `mcp_connect.py`):

- The sandbox container is a thin Ubuntu image with just `claude` -- it does **not** have yosys/openroad installed.
- The MCP server runs on the *host* (devcontainer) where the EDA toolchain lives. `MCPService` (in `mcp_connect.py`) starts uvicorn on a kernel-assigned port (`port=0`) and serves the FastMCP `sse_app()` on `0.0.0.0`.
- The two containers need to share a Docker network so the sandbox can reach the host's IP on that network. `discover_shared_network()` runs `docker inspect $(hostname)` to find a Docker network the host is already on, then `_build_sandbox_compose` rewrites the sandbox's `compose.yaml` `networks.shared.name` field to that network *at task-build time* (the YAML default is `bridge`). The sandbox-side `mcp.json` then points at `http://<host_ip>:<port>/sse`.
- In a devcontainer setup `host.docker.internal` from a sibling container resolves to the Docker VM gateway, **not** to us -- that's the entire reason for the network-discovery dance. Fallback is `(bridge, host.docker.internal)` for non-Docker runs.
- DNS-rebinding protection is disabled on the FastMCP (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) so docker-in-docker container hostnames don't trip the validator.

**Diff-based scoring** (`llm_eval/components/scorers.py`):

Both the `synthesis` and `testbench` scorers, plus both MCP tools (`run_testbench`, `synth_timing_report`), follow the same pattern:

1. `build_diff_from_sandbox` -- read each `rtl_files[i]` out of the sandbox, unified-diff against the on-disk original, paths formatted as `a/<rel>` `b/<rel>`.
2. `_create_copy` -- `cp -R --reflink=auto` the entire `design.root` into a fresh tempdir.
3. `_apply_diff` -- `patch -p1 -d <copy>/<rtl_dir>` (so the `a/` `b/` prefixes line up).
4. Run the check (`yosys hierarchy -check`, the test script, or the ORFS synth flow) against the copy.

This keeps the agent's working tree separable from the host's git checkout, lets the scorers replay a saved `diff.patch` after the run, and means an in-flight MCP tool call doesn't race the scorer (each gets its own tempdir copy).

### `llm_eval/generate.py` -- one-shot seeded Claude CLI

Older / simpler path: `generate.py` stages a fresh copy of `external/aes/` into `llm_results/<ts>/secworks/aes/` and invokes `claude -p --dangerously-skip-permissions <prompt>` once, inlining the most recent `iter_calibration` run's `logical_paths.rpt`. This bypasses Inspect entirely and has no scorer -- artifacts are just the edited copy. Useful for quick prompt iteration; the canonical entry point is `llm_eval/run.py`.

## Conventions specific to this repo

- **Import paths assume the repo root is on sys.path.** Top-level packages are `common`, `eda_eval`, `llm_eval`; `pyproject.toml` declares `packages = ["common", "eda_eval"]` for the wheel build, but in dev you're relying on `uv run` setting the cwd correctly. If you add a script, run it via `uv run <path>` from the repo root, never `cd <subdir> && uv run script.py`.
- **ASCII-only.** Pre-commit's pygrep `[^\x00-\x7F]` blocks non-ASCII characters in tracked files. If you copy text from an external doc, scrub smart quotes / em-dashes before committing.
- **No emojis anywhere** (in code, in commits, in tool outputs). The ASCII check enforces this mechanically.
- **`tmp/` is scratch and gitignored.** Use it for one-off experiments; don't commit anything from it.
- **Timestamp format `%Y-%m-%d_%H-%M-%S`** is used everywhere (`eda_results/`, `llm_results/`, generate.py, etc.) -- it sorts lexicographically, so "latest run" is `sorted(...)[-1]`. Don't switch formats.
- **Per-key NaN, not zero, signals "scoring skipped/failed".** Inspect's per-key score aggregation filters NaN out of means automatically; zeros would be averaged in.
- **`StudyConfig`, `DesignConfig`, `TargetConfig`, `RunConfig` are all frozen dataclasses.** Mutate via `dataclasses.replace(...)`, not field assignment. The scorers / MCP tools rely on this (they `replace(design, root=<copy>)` to redirect at the patched tempdir without touching the canonical registry entry).
- **The devcontainer caps KLayout's build parallelism via a sed-patch on `ORFS/etc/DependencyInstaller.sh`** (changing `numThreads=$(nproc)` to `numThreads=2`). KLayout templates can use 4+ GB per cc1plus and OOM hosts under 16 GB. The sandbox Dockerfile doesn't build EDA tools at all so doesn't need this.
- **The CI workflow only runs pre-commit.** There is no separate test suite job. If you add tests under `pytest`, wire them in explicitly.
