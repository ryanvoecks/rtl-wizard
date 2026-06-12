# RTL Wizard

Research harness for evaluating LLM agents at post-synthesis timing optimisation on real open-source RTL. The repo accompanies a three-chapter thesis: a 16-design benchmark, an analysis of the divergence between synthesis and post-route feedback, and a rank-correction predictor that closes most of that gap without running place-and-route in the loop.

## Setup

The only requirement is `docker`. After building the container run `./setup.sh` to init submodules, apply patches, and `uv sync`.

## Layout

- `common/` -- shared spine: design registry (`designs.py`), calibrated `TargetConfig` set (`targets.py`), per-design testbench scripts, submodule patches.
- `eda_eval/` -- non-LLM ORFS flow. Runs `synth -> floorplan -> place -> CTS -> route` on nangate45 and produces ranked critical-path reports.
- `llm_eval/` -- drives a Claude Code agent against a sandboxed copy of one design via MCP tools (`run_testbench`, `synth_report`). Scored by `yosys` synthesisability and the upstream testbench.
- `analysis/` -- post-hoc analysis scripts that produce every table and figure in Chapters 1 and 2 (divergence, RBO, design features, edit categories).
- `predictor/` -- Chapter 3 rank-correction model. Calibrates the slack offsets in Equation 3.1, evaluates leave-one-out RBO, and produces the corrected ranking the LLM agent consumes.
- `artifacts/` -- saved `.eval` logs and per-sample diffs from the LLM runs reported in the thesis.
- `docs/` -- per-design notes documenting cell counts and any RTL modifications.

## Key scripts

```bash
# Full ORFS flow on one calibrated target
uv run eda_eval/run.py --target aes_target

# Find the clock period for a design from scratch
uv run eda_eval/calibrate.py --design aes_reference

# Analyse a routed phase dir -> critical_paths.rpt, logical_paths.rpt
uv run eda_eval/analyse.py eda_results/<batch>/<benchmark>/<name>/<variant>/

# Run the LLM eval (Claude Code agent, one sample per target)
uv run python llm_eval/run.py
uv run python llm_eval/view.py            # browse llm_results/ in inspect view

# Chapter 2 tables -- rerun any of these to refresh analysis/data/*.csv
uv run analysis/llm_design_uplift.py      # design-level divergence
uv run analysis/llm_topk_rbo.py           # critical-path rank-biased overlap
uv run analysis/port_incidence.py         # IO-fraction design feature

# Chapter 3 predictor
uv run predictor/calibrate_iters.py       # fit MoM beta parameters (LOO)
uv run predictor/per_design_rbo_table.py  # rank-correction results table
```

Outputs land in `eda_results/<timestamp>/...` and `llm_results/<timestamp>/...` (both gitignored). The MCP topology that bridges the LLM sandbox container to the host's EDA toolchain is documented in `CLAUDE.md`.

## AI usage

Claude code was used to prototype ideas, monitor jobs and generate code in this repository. It is also required for some of the methodlogy outlined in the corresponding thesis.
