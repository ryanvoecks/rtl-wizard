# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

`rtl-wizard` is a research harness for evaluating LLM agents at the task of **post-synthesis timing optimisation** on real open-source RTL. It has two halves that share the same design registry:

1. **`eda_eval/`** -- non-LLM ORFS flow that runs every design through `synth -> floorplan -> place -> CTS -> route` on nangate45, calibrates per-design clock periods, and produces ranked logical-path reports off the routed `.odb`.
2. **`llm_eval/`** -- drives a Claude Code agent (OAUTH-token, not API key) against a sandboxed copy of a single design, exposing the EDA flow as MCP tools (`run_testbench`, `synth_report`) so the agent can iterate. Scored by `yosys` synthesisability plus the design's upstream testbench.

`common/` is the shared spine: `common/config.py` defines `StudyConfig`/`DesignConfig`/`TargetConfig`/`RunConfig` and resolves all path constants; `common/designs.py` is a hand-curated registry of every design; `common/scripts/<name>.sh` is the per-design testbench runner.

## Setup

```bash
./setup.sh                                 # submodules + patches + uv sync
```

Requires `uv`, `docker`, and (for `llm_eval`) a `.env` with `CLAUDE_CODE_OAUTH_TOKEN` set (run `claude setup-token` and copy the token in by hand). Python pinned to 3.12. The devcontainer (`.devcontainer/Dockerfile`) builds OpenROAD-flow-scripts + Verilator from source so `yosys`/`openroad`/`klayout`/`verilator` are on PATH inside the container. First build is long; subsequent rebuilds reuse the cache.

Submodule patches live in `common/patches/<repo>.diff` and are applied idempotently by `setup.sh`. If you re-clone a submodule manually, re-run `setup.sh` so its patch is re-applied (otherwise testbenches / cell-count targets drift from what's documented in `common/designs.py`).

## Commands

```bash
# Run the full ORFS flow on a single calibrated target from common/targets.py
uv run eda_eval/run.py --target aes_target

# Iterative k-based calibration for a single design (converges on clock period for target slack/period ratio)
uv run eda_eval/calibrate.py --design aes_reference

# Find the optimisation "sweet spot" period: tightest T at which fix scope stays small
uv run eda_eval/scope.py --design aes_reference

# Analyse a routed phase dir -> critical_paths.rpt, logical_paths.rpt
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

Every design is a `DesignConfig` (frozen dataclass) emitted inline in `designs.py` as a module-level `<name>_<variant>` binding (e.g. `aes_reference`). Consumers either import the binding directly (`common/targets.py`) or resolve it by name via `resolve_design("aes_reference")` (the calibrate / scope CLIs use `--design <name>`). Each entry locks down:

- `rtl_files` (ordered tuple, paths relative to `rtl_dir`): explicit because RTLLM-style auto-globbing breaks on repos that ship sim-only TB models next to RTL.
- `top_module`, `clock_port` (default `clk` -- override per design, several use `clk_i`/`CLK`/`i_controller_clk`).
- `test_script` (`common/scripts/<name>.sh`): runs from `design.root`, returns 0 on pass. The script is the *only* contract for testbench pass/fail -- `DesignConfig.run_tb()` shells out to it and treats stdout/stderr as opaque diagnostics.
- `tb_timeout_s` per design -- defaults to 60s, raised for slow benches (jpeg_encoder at 600, wb_dma at 600).
- `include_dirs` for designs with `\`include` directives (e203 needs `core/`, jpeg_encoder needs `dct/rtl/verilog/`).

Each design has comments above its block documenting *why* the file list and patch are what they are -- read those before editing, they record load-bearing decisions (e.g. uberddr3 only synthesizes the controller, not the PHY; verilog_axi uses the 8x8 wrapper not the parameterized crossbar; e203 deliberately removes ITCM/DTCM RAMs).

When adding a design: drop a `.diff` in `common/patches/` (applied by `setup.sh`), add a `<name>.sh` test script, add a module-level `<name>_<variant>` `DesignConfig` binding in `common/designs.py`, and document it in `docs/designs.md` (single source of truth for cell counts and modifications).

### Single-target ORFS run (`eda_eval/run.py`)

`run.py` drives one `TargetConfig` (looked up from `common/targets.py` by variable name via `--target <name>`) through the full ORFS flow. The target already carries its calibrated `(period_ns, side_um)`, so there is no calibration step and no parallelism -- it builds one `RunConfig` and calls `run_job` directly. Output lands at `eda_results/<timestamp>/<benchmark>/<name>/<variant>/`; non-zero rc exits with the log path.

`run_job` materialises one `inputs/Makefile` from `eda_eval/templates/Makefile.template` (parameterised on top module / RTL files / SDC / floorplan / flow targets). The two flow target groups are `SYNTH_FLOW_TARGETS = ("synth", "synth-report")` for synth-only runs (used by the MCP `synth_report` tool) and `ALL_FLOW_TARGETS` for full P&R. `run_job` itself is reused by `calibrate.py`, `scope.py`, and `llm_eval/components/mcp_servers.py`; `derive_final_side_um` lives in `calibrate.py` (the only consumer).

### Calibration scripts -- two modes, different jobs

`run.py` itself does not calibrate; it consumes the calibrated targets in `common/targets.py`. The two entry points that *produce* calibrations are:

- `eda_eval/calibrate.py` -- three-step deterministic calibration for a single design (no iteration loops): (1) synth-only at an over-constrained clock (default 0.5 ns) on a 1mm x 1mm die; record `synth_ws_ns` and `synth_area_um2`. (2) first full P&R at `T2 = T1 - synth_ws_1` (synth's projected period) and `side2 = derive_final_side_um(synth_area_1, cfg)`. (3) second full P&R at `T3 = T2 - route_ws_2` (predicts `route_ws -> 0`) and `side3 = derive_final_side_um(route_area_2, cfg)` -- re-deriving the die from post-route stdcell area corrects the utilisation for CTS + repair_timing buffers that synth didn't see. Step 3's `(period, side)` are the calibrated outputs. Writes `calibration.json` plus three phase dirs `iter_synth_0/`, `iter_pnr_0/`, `iter_pnr_1/` under `eda_results/<batch>/__iter_calibration__/<benchmark>/<name>/`.
- `eda_eval/scope.py` -- three-phase scoping: (A) fixed-point to `T_baseline` with `k=0`, (B) shrink period until fix scope (modules / start stems touched by negative-slack paths) exceeds budget, (C) bisect to refine `T_sweet`. Used to decide whether a design is interesting to optimise at all.

### Logical-path analysis (`eda_eval/analyse.py`)

`analyse(run)` picks the stage from `run.flow_targets`: post-route (`6_final.odb`+SPEF, post-parasitics) when any P&R target was run, otherwise post-synth (`1_synth.odb`, zero-RC estimate). The post-synth path is what the MCP `synth_report` tool returns to the agent. Output filenames are the same in both modes; the `# stage` header line in each report names the source.

The pipeline: pipe a TCL script into one `openroad -no_init -exit` invocation that runs `extract_critical_paths.tcl` (samples a `pool` of worst paths via `find_timing_paths`, emits TSV of `slack | startpoint | endpoint | cells`). Then yosys dumps a hierarchy JSON (`read_verilog ... ; hierarchy -top <top>; proc; write_json`) so each timing path's cell chain can be mapped back to the union of containing RTL modules. **Do not run `flatten` or `synth`** in that yosys pass -- you want one cells entry per submodule instantiation, not a primitive-level netlist.

The "logical group" of a path is `(start_stem, end_stem)` after stripping yosys cell-type suffixes (`$_DFFE_*`) and array indices (`[N]`). Groups are ranked by `(worst_slack, -count)` so high-multiplicity ties break in favour of the more-replicated path.

### LLM eval -- agent + sandbox + MCP server

`llm_eval/run.py` is the entry point; it creates `llm_results/<timestamp>/`, asks `inspect_eval` to run the `optimize_timing` task, and writes the `.eval` log alongside per-sample artifacts (`diff.patch`, `synthesis.log`, `testbench.log`, `target_config.json`).

The task is built from `common/targets.py` (one `<name>_target` per calibrated design, aggregated as `all_targets`) -- each `TargetConfig` carries the design + the calibrated `(period_ns, side_um)`. Per sample, `_build_sample` mounts the design's RTL into the sandbox under `rtl/<file>` (`SANDBOX_RTL_ROOT = "rtl"`).

`claude_code_agentic_solver` (in `llm_eval/components/solvers.py`) is a **custom Inspect agent** that shells out to `claude -p --output-format stream-json --strict-mcp-config ...` inside the sandbox and translates each NDJSON event back into Inspect `ChatMessage{Assistant,Tool}` objects so `inspect view` renders the transcript correctly. Auth is OAUTH-token-only (`CLAUDE_CODE_OAUTH_TOKEN` from `.env`); `ANTHROPIC_API_KEY` is deliberately *not* forwarded because the Claude CLI prefers it over the OAUTH token. Model name comes from Inspect's `--model` flag at solve time via `active_model()`. Resumes across multiple `solve` calls are wired through `--resume <session_id>` (session id stored in `store()`), though the current task only invokes `claude` once per sample.

**MCP topology** (this is load-bearing -- read it before touching `mcp_connect.py`):

- The sandbox container is a thin Ubuntu image with just `claude` -- it does **not** have yosys/openroad installed.
- The MCP server runs on the *host* (devcontainer) where the EDA toolchain lives. `MCPService` (in `mcp_connect.py`) starts uvicorn on a kernel-assigned port (`port=0`) and serves the FastMCP `sse_app()` on `0.0.0.0`.
- The two containers need to share a Docker network so the sandbox can reach the host's IP on that network. `discover_shared_network()` runs `docker inspect $(hostname)` to find a Docker network the host is already on, then `_build_sandbox_compose` rewrites the sandbox's `compose.yaml` `networks.shared.name` field to that network *at task-build time* (the YAML default is `bridge`). The sandbox-side `mcp.json` then points at `http://<host_ip>:<port>/sse`.
- In a devcontainer setup `host.docker.internal` from a sibling container resolves to the Docker VM gateway, **not** to us -- that's the entire reason for the network-discovery dance. Fallback is `(bridge, host.docker.internal)` for non-Docker runs.
- DNS-rebinding protection is disabled on the FastMCP (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) so docker-in-docker container hostnames don't trip the validator.

**Diff-based scoring** (`llm_eval/components/scorers.py`):

Both the `synthesis` and `testbench` scorers, plus both MCP tools (`run_testbench`, `synth_report`), follow the same pattern:

1. `build_diff_from_sandbox` -- read each `rtl_files[i]` out of the sandbox, unified-diff against the on-disk original, paths formatted as `a/<rel>` `b/<rel>`.
2. `_create_copy` -- `cp -R --reflink=auto` the entire `design.root` into a fresh tempdir.
3. `_apply_diff` -- `patch -p1 -d <copy>/<rtl_dir>` (so the `a/` `b/` prefixes line up).
4. Run the check (`yosys hierarchy -check`, the test script, or the ORFS synth flow) against the copy.

This keeps the agent's working tree separable from the host's git checkout, lets the scorers replay a saved `diff.patch` after the run, and means an in-flight MCP tool call doesn't race the scorer (each gets its own tempdir copy).

## Conventions specific to this repo

- **Import paths assume the repo root is on sys.path.** Top-level packages are `common`, `eda_eval`, `llm_eval`; `pyproject.toml` declares `packages = ["common", "eda_eval"]` for the wheel build, but in dev you're relying on `uv run` setting the cwd correctly. If you add a script, run it via `uv run <path>` from the repo root, never `cd <subdir> && uv run script.py`.
- **ASCII-only.** Pre-commit's pygrep `[^\x00-\x7F]` blocks non-ASCII characters in tracked files. If you copy text from an external doc, scrub smart quotes / em-dashes before committing.
- **No emojis anywhere** (in code, in commits, in tool outputs). The ASCII check enforces this mechanically.
- **`tmp/` is scratch and gitignored.** Use it for one-off experiments; don't commit anything from it.
- **Timestamp format `%Y-%m-%d_%H-%M-%S`** is used everywhere (`eda_results/`, `llm_results/`, etc.) -- it sorts lexicographically, so "latest run" is `sorted(...)[-1]`. Don't switch formats.
- **Per-key NaN, not zero, signals "scoring skipped/failed".** Inspect's per-key score aggregation filters NaN out of means automatically; zeros would be averaged in.
- **`StudyConfig`, `DesignConfig`, `TargetConfig`, `RunConfig` are all frozen dataclasses.** Mutate via `dataclasses.replace(...)`, not field assignment. The scorers / MCP tools rely on this (they `replace(design, root=<copy>)` to redirect at the patched tempdir without touching the canonical registry entry).
- **The devcontainer caps KLayout's build parallelism via a sed-patch on `ORFS/etc/DependencyInstaller.sh`** (changing `numThreads=$(nproc)` to `numThreads=2`). KLayout templates can use 4+ GB per cc1plus and OOM hosts under 16 GB. The sandbox Dockerfile doesn't build EDA tools at all so doesn't need this.
- **The CI workflow only runs pre-commit.** There is no separate test suite job. If you add tests under `pytest`, wire them in explicitly.
