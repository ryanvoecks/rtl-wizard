### Design summary

This is an **RTL implementation of a configurable Viterbi decoder** for convolutional codes (the kind used in cellular/3GPP-style channel coding). It decodes a soft-input bitstream back into the original information bits by finding the most-likely path through a trellis.

It is **runtime-configurable**:
- **Constraint length / state count** via `register_num_i`: 64, 32, 16, or 8 trellis states.
- **Code rate** via `valid_polynomials_i`: R=1/2 through R=1/6, with up to 6 generator polynomials (`polynomial1_i`..`polynomial6_i`).
- **Termination mode** via `tail_biting_en_i`: zero-tail vs. tail-biting.

It operates as a hardware accelerator with three external SRAM-style interfaces: a **source buffer** (soft bits in, 6×4-bit packed in 24-bit words), a **traceback/survivor-path buffer** (64-bit words, read+write), and a **destination buffer** (decoded bytes out). A `frame_start_i` pulse kicks off one frame; `frame_done_o`/`busy_o` report status.

**How it works (classic Viterbi datapath, fully parallel across 64 states):**
1. **BMU** — 64 instances compute branch metrics from soft bits and the generator polynomials.
2. **ACS** — 64 instances perform Add-Compare-Select: add branch metrics to previous path metrics on the two converging trellis edges (low/high path), pick the survivor, emit the survivor decision bit.
3. **pm_normalize** — gathers all 64 new path metrics, normalizes them (to prevent overflow), and finds the global max-metric state via a tree of comparators; the normalized metrics feed back into the ACS units for the next trellis step.
4. **traceback** — survivor bits are stored in the traceback SRAM; this module walks backward through the survivor history, reconstructs the decoded bit sequence, and streams it to the output. A sliding-window FSM in the top module (`RUN_FULL_TB`/`RUN_HALF_TB`/`FLUSH_ALL`) processes the frame in 64-bit and 32-bit windows.

### Directory organisation

```
.
└── rtl/
    ├── viterbi_core.v   # top-level: control FSM, SRAM I/O, instantiates everything
    ├── BMU.v            # Branch Metric Unit (×64)
    ├── ACS.v            # Add-Compare-Select unit (×64)
    ├── pm_normalize.v   # path-metric normalization + max-state selection
    └── traceback.v      # survivor-path traceback / output bit reconstruction
```

### Key modules/files

- **`viterbi_core.v`** (top, ~3200 lines) — The integration and control hub. Holds the main decoding FSM (`IDLE → CHECK_REMIN → RUN_FULL_TB / RUN_HALF_TB / FLUSH_ALL`), generates the source-buffer read address/strobe, packs decoded bits into output bytes for the destination buffer, manages the traceback-buffer write address, and instantiates **64× BMU**, **64× ACS**, **1× pm_normalize**, and **1× traceback**. Most of its length is the repetitive 64-way instantiation of BMU (`bmu_inst_0..63`) and ACS (`acs_inst_0..63`).

- **`BMU.v`** (`module BMU`, param `WIDTH_BM=9`) — For a given trellis state `state_x_i`, XORs the state register bits with the generator polynomials to produce the expected codeword, then correlates it against the signed 4-bit soft bits (`soft_data_i`) to produce a branch metric `bm_o`. Selectable for 2–6 polynomials. Its `ready_o` (from instance 0) is what the top module uses to detect decoding start.

- **`ACS.v`** (`module ACS`, param `WIDTH_BM=9`) — The core recursion. Takes the branch metric and the previous-step path metrics of the two predecessor states (`prev_low_i` plus one of `prev_high1..4_i`, selected by `register_num_i` so the same hardware works for all state counts). Computes `pm_low = prev_low + bm` and `pm_high = prev_high − bm`, selects the larger, registers the new path metric `pm_o`, and outputs the 1-bit survivor decision `survivor_path_o`.

- **`pm_normalize.v`** (`module pm_normalize`, ~630 lines) — Takes all 64 ACS path metrics (`pm_tmp_0..63_i`), and through a hierarchical comparator tree (groups of 8 → 2nd level → final) finds the maximum-metric state index (`max_state_index_o`, used as the traceback start state) and normalizes/registers the metrics (`pm_nom_*_o`) that loop back to the ACS `prev_*` inputs. Handles the different active-state ranges for 8/16/32/64-state modes.

- **`traceback.v`** (`module traceback`, params `W_TB_LEN`, `W_HALF=32`, `W_FULL=64`) — Reads the survivor-path SRAM backward from `tb_start_addr_i` for `tb_len_i` steps. Starting from `start_state_index_i`, it indexes the 64-bit survivor word (`tb_rdata_i`) by the current state, shifts the state register, and accumulates decoded bits. Emits two output widths — `half_tb_bits_o` (32-bit, mid-frame windows) and `full_tb_bits_o` (64-bit, final flush) — qualified by `tb_bits_valid_o`, which the top module serializes into output bytes.

**Interaction / dataflow loop:** `src buffer → BMU (branch metrics) → ACS (add-compare-select) → pm_normalize (normalize + max state) → back to ACS` for each trellis step; survivor bits from ACS → `tb buffer`; then `traceback` reads `tb buffer` → reconstructs bits → `viterbi_core` packs → `dst buffer`. The whole thing is 64-way spatially parallel (one BMU+ACS per state) with `register_num_i`/`valid_polynomials_i` masking the active subset for lower-order codes.

Note: there are no testbenches, constraints, or build scripts in this directory — it is pure synthesizable RTL (Verilog-2001, Xilinx ISE-style headers).
