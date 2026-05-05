# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

`rtl-wizard` is two things sharing one repo:

1. An **MCP server** (`server/rtl_wizard.py`) that exposes RTL-design tools — Verilog simulation (iverilog+vvp), yosys synthesis stats, and longest-combinational-path reconstruction back to RTL.
2. A **benchmark** (`benchmark/`) that drives an `inspect_ai` react agent against the RTLLM design suite, has the agent generate Verilog for each problem, and scores it for correctness *and* PPA (delay × area × power) against the golden reference.

The MCP server is consumed by the benchmark's solver as one of its tools, but is intended to also be usable on its own.

## Setup

```bash
git submodule update --init --recursive   # pulls external/RTLLM
```

Requires `uv` and `docker`. Python is pinned to 3.12 (`.python-version`); dependencies are managed by `uv` against `pyproject.toml` / `uv.lock`.

The first benchmark run builds `sandbox/Dockerfile`, which clones and builds OpenROAD-flow-scripts from source (yosys, OpenROAD/OpenSTA, nangate45 PDK). Expect a long first build; subsequent runs reuse the image.

## Commands

```bash
# Run the full benchmark
uv run inspect eval benchmark/tasks.py

# Run a single design (sample id == folder name in external/RTLLM/**), always use --cache for speed
uv run inspect eval benchmark/tasks.py --sample-id adder_8bit --cache

# Pre-commit (matches CI)
uv run pre-commit run --all-files
```

Logs land in `./logs/` per Inspect's defaults; `tmp/` is scratch and gitignored.

## Architecture

### Two sandboxes per sample

`sandbox/compose.yaml` defines two services that share the same image (`rtl-wizard-sandbox:latest`):

- **`solver`** — where the agent works. Receives only `design_description.txt` (plus any auxiliary files RTLLM ships, but **not** `verified_*.v`, `testbench.v`, or the upstream `Makefile` — see `SKIP_COPY_PATTERNS` in `benchmark/tasks.py`). The agent must write its own testbench to verify itself.
- **`scorer`** — a sibling container the agent never sees. At score time, the agent's design files are copied here, the *hidden golden testbench* is dropped in alongside them, and a glob-based makefile (`benchmark/scorers.py:IVERILOG_MAKEFILE`) runs `iverilog` over `*.v`.

This split is load-bearing: it's why a working-but-overfit agent testbench can still fail grading. Don't merge the two sandboxes.

### Testbench-vs-design file detection

When copying the agent's work out of the solver sandbox, files containing `^\s*module\s+testbench\b` are dropped (they're assumed to be the agent's own testbench, replaced by the golden one at scoring). This is documented in the system prompt — if the agent inlines its testbench into `<design>.v`, the design itself is dropped and grading reports a specific error. Keep the regex (`_TESTBENCH_MODULE_RE` in `benchmark/scorers.py`) and the system prompt in `benchmark/solvers.py` in sync.

### Two-stage scoring

`rtllm_generate_and_test` registers three scorers in order:

1. `rtllm_make_passes` — boolean correctness. Stages files into the scorer sandbox and runs `make vcs && make sim`; CORRECT iff stdout contains `Passed`.
2. `golden_ppa` — synthesizes the golden reference to nangate45 and runs OpenSTA, reporting `golden_delay`, `golden_area`, `golden_power`, and `golden_ppa_score = 1/(d·a·p)`. Runs unconditionally — golden values are dataset metadata, independent of the agent.
3. `openroad_ppa` — runs only if `state.scores["rtllm_make_passes"]` is CORRECT (it's gated explicitly), else returns all-NaN per key so Inspect's per-key means only average over correct runs. Synthesizes the agent's design to nangate45 via yosys, runs OpenSTA, and reports `delay`, `area`, `power`, `ppa_score = 1/(d·a·p)`, and `relative_ppa_score = ppa_score(agent) / ppa_score(golden)`. The golden product is read from `state.scores["golden_ppa"]` rather than re-synthesized, so `golden_ppa` must run first.

The OpenSTA pass is **STA on the linked netlist** — there is no real placement or routing despite the "post-P&R" framing. Absolute numbers aren't physically meaningful; the relative score is the comparable metric.

OpenSTA needs all three of: tech LEF, cell LEF, and Liberty (`NANGATE_LIB`, `NANGATE_TECH_LEF`, `NANGATE_CELL_LEF` in `benchmark/scorers.py`). The TCL clock setup tries `clk`/`clock`/`i_clk`/`clk_i` and falls back to a virtual clock for combinational designs.

Section sentinels (`===PPA_DELAY===` etc.) scope each parser regex to one OpenSTA report so format drift in one command can't bleed into another.

### Sample construction

`_build_sample` in `benchmark/tasks.py` skips RTLLM folders that lack a `testbench.v`, `verified_<design>.v`, or a detectable top-module name (RTLLM is inconsistent: some references declare `module <design>`, others `module verified_<design>` — `_detect_golden_top` tries both). The dataset is the result of walking `external/RTLLM` for `design_description.txt` files and filtering with this rule.

### MCP server tools

`server/rtl_wizard.py` registers four FastMCP tools:

- `rtl_helper` — returns static RTL best-practice text. Cheap; the prompt instructs the agent to call it before generating RTL.
- `simulate(verilog_paths)` — `iverilog -g2012` + `vvp`, returns combined stdout/stderr (truncated to 20 KB tail).
- `yosys_synth(verilog_path, top=None)` — synth + `stat`, returns the cell/wire breakdown (or yosys log tail on failure).
- `reconstruct_critical_path(verilog_path)` — runs `ltp -noff` after techmap and maps each step back to the originating RTL line. Used to direct the agent at depth bottlenecks.

All three EDA-tool wrappers (`server/iverilog_sim.py`, `server/yosys_synth.py`, `server/reconstruct_from_path.py`) shell out via `eda_env()` from `server/util.py`. **Don't skip `eda_env()`**: the inspect-tool-support PyInstaller bootstrap stashes the original `LD_LIBRARY_PATH` in `LD_LIBRARY_PATH_ORIG` and replaces it with paths to bundled venv libs that break system-linked EDA tools. `eda_env()` restores the original (or drops the var) so yosys/iverilog/openroad link correctly.

The server is launched **inside the sandbox** by the solver via `mcp_server_sandbox(...)`; the `MCPServerConfigStdio` variant is kept around for a now-broken `codex_cli` solver path (see comment block at the bottom of `benchmark/solvers.py`).

### Solver

`benchmark/solvers.py:rtllm_react_solver` wires `inspect_ai.agent.react` with the rtl-wizard MCP server plus the standard inspect tool set (bash, python, text_editor, etc.). The system prompt is design-agnostic — per-sample design name comes through the user message built in `_build_sample`. The prompt explicitly tells the agent that **combinational depth** is the primary PPA objective (it gates clock period); cell count is secondary.

## Conventions specific to this repo

- The `benchmark/` directory is loaded by `inspect_ai` via `SourceFileLoader`, which does **not** put the repo root on `sys.path`. `benchmark/tasks.py` does the `sys.path.insert` itself before importing siblings — preserve that prelude if you add new top-level imports there.
- Per-key NaN is the signal for "scoring skipped/failed"; Inspect filters NaN out of per-key dict-score means automatically. Don't return zeros when a measurement is unavailable.
- The sandbox image build caps `make` parallelism via a `nproc` shim (`BUILD_JOBS`, default 2) — KLayout templates can use 4+ GB per cc1plus and OOM hosts <16 GB. Override with `--build-arg BUILD_JOBS=N` if you have memory.
