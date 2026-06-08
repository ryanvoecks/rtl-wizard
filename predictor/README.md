# Synth-slack ranking corrector

Post-synth logical-blocks ranking predictor for post-route timing closure.
Calibrated on 12 designs with cross-validated headline reported.

## Final correction (median-of-medians, β_internal free, β_both derived)

| port_class | β (ns) | how |
|---|---|---|
| `input_only`  | **+0.226** | median of per-design medians of Δ |
| `output_only` | **-0.323** | median of per-design medians of Δ |
| `internal`    | **-0.050** | median of per-design medians of Δ |
| `both`        | **-0.047** | derived: β_input + β_output - β_internal |

Δ = `slack_route_ns - slack_synth_ns` per logical block.

## Inference recipe

1. Read the post-synth logical_blocks rpt for the design (top-100 by worst slack).
2. **Quarantine** any path whose endpoint pin name is not `D` (the only
   functional-data pin in nangate45 + yosys generic cells). This drops paths
   ending at async-reset/preset (RN/SN), scan (SI/SE), clock-gate (A1/A2),
   and latch-enable (E) pins. The filter is **synth-side only** -- the route
   ranking is the ground truth and is never filtered.
3. Classify each remaining path's `port_class`:
   - `input_only`   -- startpoint is a top-level input/inout port, endpoint is not an output port
   - `output_only`  -- endpoint is a top-level output/inout port, startpoint is not an input port
   - `both`         -- both
   - `internal`     -- neither
4. Compute `slack_synth_corr_ns = slack_synth_ns + β[port_class]`.
5. Rank by `slack_synth_corr_ns` ascending (worst first); top-K is the
   corrected synth prediction.

## Headline performance

Top-100 RBO_EXT between corrected-synth and route, in-sample mean over 12
designs (LOO mean is within 0.01 of in-sample for this estimator):

| baseline                                | p=0.6 | p=0.9 | p=0.96 |
|-----------------------------------------|-------|-------|--------|
| raw (no filter, no correction)          | 0.434 | 0.472 | 0.510 |
| + synth-side async quarantine           | 0.543 | 0.588 | 0.617 |
| **+ I/O correction (this predictor)**   | **0.652** | **0.738** | **0.786** |

LOO mean RBO at p=0.9 = 0.736, vs in-sample 0.738 -- the cross-validated
penalty is negligible because β_input and β_output are stable across folds
(std/|mean| ≤ 0.01).

## Repo layout

```
predictor/
├── README.md                              -- this file
├── correction_calibration.py              -- 8-step protocol: filter, classify, fit, LOO, sweep
├── correction_per_design_summary.py       -- per-design RBO summary table (p ∈ {0.6, 0.9, 0.96})
├── dump_topk_corrected_v2.py              -- per-design debug TSV of the corrected ranking
├── plots/
│   ├── plot_path_slack_delta_by_pin.py
│   ├── plot_path_slack_delta_by_pin_vs_slack.py
│   ├── plot_path_slack_delta_async_origin_vs_slack.py
│   ├── plot_path_slack_delta_async_filtered_vs_slack.py
│   ├── plot_path_slack_delta_async_filtered_shifted_vs_slack.py
│   └── output/                            -- PNGs (gitignored by repo root .gitignore)
└── data/
    ├── calibration/                       -- correction_calibration outputs at p=0.9
    │   ├── quarantine.csv
    │   ├── per_design_class_medians.csv
    │   ├── global_beta.csv
    │   ├── mixed_effects.txt
    │   ├── loo_median_of_medians.csv
    │   ├── loo_max_rbo_sweep.csv
    │   ├── headline.txt
    │   └── per_design_rbo_summary.csv     -- 3-stage × 3-p RBO table
    ├── calibration_p06/                   -- same protocol re-run at p=0.6
    └── dumps_v2/                          -- per-design corrected-synth dumps
        └── verilog_axi.tsv
```

## Reproducing

```bash
# Recompute calibration tables (~3 min, mostly Step 6 sweep)
uv run --with pandas --with numpy --with statsmodels --with scipy \
    python predictor/correction_calibration.py

# Per-design summary at p ∈ {0.6, 0.9, 0.96}
uv run --with pandas --with numpy --with statsmodels --with scipy \
    python predictor/correction_per_design_summary.py

# Inspect a single design's corrected ranking
uv run --with pandas --with numpy --with statsmodels --with scipy \
    python predictor/dump_topk_corrected_v2.py --design verilog_axi

# Regenerate plots (output to predictor/plots/output/)
uv run --with matplotlib python predictor/plots/plot_path_slack_delta_async_filtered_vs_slack.py
```

## Notes on what's NOT here

- **Older variants kept under `analysis/`**: `llm_topk_rbo_corrected.py`,
  `llm_topk_rbo_corrected_union.py`, `dump_topk_corrected_synth.py`,
  `dump_topk_corrected_union.py`. These used the prior ±0.3 eyeballed β
  and/or the shared-driver async-detection rule (now superseded by the
  endpoint-pin-only rule). They're retained as development history but
  shouldn't be used for new work.
- **Upstream EDA flow** (`eda_eval/`) produces the `path_slack_union/` and
  `logical_blocks/` inputs; not part of the predictor itself.

## Caveats

- **No held-out test split.** With only 12 designs, LOO is both the
  validation and the headline. A separate 4-design held-out test would be
  preferable when more designs become available.
- **Async detection by pin-name suffix, not liberty.** Liberty parsing is
  not wired up in this repo; the pin-name fallback is the protocol's
  documented fallback and is sufficient for nangate45 + yosys.
- **Internal paths can't be re-ranked within their class.** β_internal is a
  uniform offset and doesn't change rank order within the internal class.
  Designs whose worst paths are internal-to-internal with routing-variance
  scatter (verilog_axi crossbar, uberddr3 high-fanout `lane[I]` driver) hit
  an irreducible ceiling around RBO ~0.5 at p=0.9 that no synth-only
  correction can fix.
