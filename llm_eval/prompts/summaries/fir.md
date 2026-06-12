### Design summary

A **parameterizable FIR (Finite Impulse Response) digital filter** written in Verilog (© 2019 Ahmed Shahein). The top module `filt_fir` implements an N‑tap fixed‑point FIR filter that, at elaboration time, can be configured into one of two architectures and can exploit coefficient symmetry to roughly halve the multiplier count:

- **Transposed Form (TF)** — multipliers fan out from the single input sample; partial sums propagate through a chain of pipeline registers (one `dff` per tap). Selected when `gp_tf_df = 1`.
- **Direct Form (DF)** — the input sample is pushed through a delay line (tapped delay chain of `dff`s), each tap is multiplied by its coefficient, and the products are summed combinationally. Selected when `gp_tf_df = 0`.

Symmetry (`gp_symm = 1`) reuses the same coefficient for mirror‑image taps (`c_coeff[gp_coeff_length-1-i]`), so only `ceil(gp_coeff_length/2)` unique coefficients are stored. Output bit‑width auto‑grows to prevent overflow: `gp_oup_width = gp_inp_width + gp_coeff_width + clog2(gp_coeff_length)`. All arithmetic is **signed**. The whole datapath is built structurally with Verilog `generate`/`genvar` loops rather than behavioral always‑blocks.

Key parameters (defaults): `gp_inp_width=16` (input width), `gp_coeff_length=80` (taps), `gp_coeff_width=16` (coeff width), `gp_tf_df=1` (TF vs DF), `gp_symm=1` (symmetry on).

### Directory organisation

```
.
└── rtl/
    ├── dff.v          # Generic parameterized D flip-flop (register primitive)
    ├── filt_coeff.v   # Coefficient values (included, not a standalone module)
    └── filt_fir.v     # Top-level FIR filter (TF/DF, symmetric/asymmetric)
```

### Key modules/files

- **`rtl/filt_fir.v`** — Top‑level and only true module of interest. Contains all the architectural elaboration logic:
  - Defines a `DIV(N,D)` ceiling‑division macro and several `localparam`s for internal widths (`c_mul_oup_width`, `c_add_oup_width`, `c_coeff_2`).
  - Declares packed wire buses `w_mul` (products) and `w_add` (running sums), plus an unpacked `c_coeff` array.
  - `` `include "filt_coeff.v" `` pulls the coefficient `assign` statements directly into the module body (so `filt_coeff.v` is a code fragment, **not** a module).
  - A large `generate` block branches on `gp_tf_df` to build either the TF or DF datapath, and on `gp_symm` to fold coefficients. It **instantiates `dff`** for every pipeline/delay register.
  - A final `generate` selects which slice of `w_add` drives `o_data` (TF takes the last accumulator stage; DF takes the top of the summed bus).
  - Ports: `i_clk`, `i_rst_an` (async active‑low reset), `i_ena` (sync enable), signed `i_data` in, signed `o_data` out.

- **`rtl/dff.v`** — Leaf primitive. A `gp_data_width`‑wide D flip‑flop with asynchronous active‑low reset (`i_rst_an`), synchronous active‑high enable (`i_ena`), rising‑edge clock. It is the **only instantiated submodule**; `filt_fir` uses many copies as the delay line (DF) or partial‑sum pipeline (TF). Width is passed per‑instance (`gp_inp_width` for DF taps, `c_add_oup_width` for TF accumulator stages).

- **`rtl/filt_coeff.v`** — Not a module; a flat list of `assign c_coeff[k] = …` statements (signed 16‑bit constants) that is `include`d into `filt_fir`. The shipped table holds 40 entries (indices 0–39) — the unique half of an 80‑tap symmetric filter, consistent with `gp_symm=1`. **Swap this file (and matching parameters) to retune the filter.**

**Interaction flow:** `filt_fir` is the parent; it textually includes `filt_coeff.v` for its coefficient ROM and structurally instantiates `dff` for every storage element. There is no testbench, build script, or sub‑hierarchy beyond these three files — `dff` is the sole reusable component and `filt_coeff.v` is pure data.
