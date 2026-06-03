### Design summary

This is an **IEEE-754 double-precision (64-bit) Floating Point Unit** (author: David Lundgren, 2009). It performs four operations selected by the 3-bit `fpu_op` input — add (0), subtract (1), multiply (2), divide (3) — under one of four IEEE rounding modes selected by `rmode` (round-nearest-even, round-to-zero, round-up, round-down).

It is a **clocked, multi-cycle pipeline**. The top module latches operands on a one-cycle `enable` pulse, dispatches to the appropriate arithmetic datapath, then feeds the result through a shared rounding stage and an exception/special-case stage before driving `out` and asserting `ready`. Each operation has a fixed latency tracked by a cycle counter (add ≈20, sub ≈21, mul ≈24, div ≈71 cycles). Status flags `underflow`, `overflow`, `inexact`, `exception`, and `invalid` are produced alongside the result.

Internally, mantissas/exponents are carried in widened intermediate buses (56-bit mantissa `[55:0]` with guard/round bits, 12-bit exponent `[11:0]`) so the rounding and exception logic can be shared across all four operations.

### Directory organisation

```
.
└── rtl/
    ├── fpu_double.v      top-level FPU (module `fpu`)
    ├── fpu_add.v         addition datapath
    ├── fpu_sub.v         subtraction datapath
    ├── fpu_mul.v         multiplication datapath
    ├── fpu_div.v         division datapath (iterative)
    ├── fpu_round.v       shared rounding stage
    └── fpu_exceptions.v  special-case / IEEE flag handling
```

### Key modules/files

- **`fpu_double.v` — `module fpu` (top level).** The integration and control hub. Ports: `clk, rst, enable, rmode[1:0], fpu_op[2:0], opa[63:0], opb[63:0]` → `out[63:0], ready`, and flags `underflow/overflow/inexact/exception/invalid`. It:
  - Registers operands/op/mode on the `enable_reg_1` pulse and generates per-unit enables. Note the **add/sub steering**: an "add" op with differing operand signs is routed to the subtractor and vice-versa (`add_enable_*` / `sub_enable_*` logic at lines 94–99).
  - Instantiates the six submodules: `u1 fpu_add`, `u2 fpu_sub`, `u3 fpu_mul`, `u4 fpu_div`, `u5 fpu_round`, `u6 fpu_exceptions`.
  - Multiplexes each unit's mantissa/exponent/sign into the shared rounding inputs (`mantissa_round`, `exponent_round`, `sign_round`) based on `fpu_op_reg` (lines 153–184).
  - Runs the cycle counter (`count_cycles` per-op latency vs `count_ready`) to time `ready`, and selects the final output as `out_except` when a special case fires (`except_enable`) else the rounded `out_round` (line 299).

- **`fpu_add.v` — `module fpu_add`.** Adds two like-signed operands. Inputs `opa/opb[63:0]`; outputs `sign`, `sum_2[55:0]`, `exponent_2[10:0]`. Handles exponent compare/alignment (shift smaller mantissa), denormal cases, carry/overflow normalization. Feeds `sum_out`/`exp_add_out`/`add_sign` in the top.

- **`fpu_sub.v` — `module fpu_sub`.** Subtraction datapath; also takes `fpu_op` to resolve result sign. Outputs `sign`, `diff_2[55:0]`, `exponent_2[10:0]`. Aligns operands, computes minuend−subtrahend, normalizes (leading-one shift) including norm→denorm cases. Feeds `diff_out`/`exp_sub_out`/`sub_sign`.

- **`fpu_mul.v` — `module fpu_mul`.** Multiply datapath. Outputs `sign`, `product_7[55:0]`, `exponent_5[11:0]`. Adds exponents (with bias offset), multiplies mantissas, detects zero/denorm and over/underflow on the exponent. Feeds `mul_out`/`exp_mul_out`/`mul_sign`.

- **`fpu_div.v` — `module fpu_div`.** Iterative (restoring-style) divider, the longest-latency unit (~71 cycles), needs its `enable` held two cycles (see `enable_reg_3` gating at line 212). Has internal `parameter preset = 53`. Outputs `sign`, `mantissa_7[55:0]`, `exponent_out[11:0]`. Subtracts exponents and iterates quotient/remainder bits; handles div-by-zero / 0÷0 / inf cases. Feeds `div_out`/`exp_div_out`/`div_sign`.

- **`fpu_round.v` — `module fpu_round` (shared).** Single rounding stage used by all ops. Inputs `round_mode[1:0]`, `sign_term`, `mantissa_term[55:0]`, `exponent_term[11:0]`; outputs the packed `round_out[63:0]` and `exponent_final[11:0]`. Implements the four rounding modes via `round_trigger`, adds the rounding ULP, handles rounding-induced carry/renormalization, and packs `{sign, exponent[10:0], mantissa[53:2]}` into the 64-bit result.

- **`fpu_exceptions.v` — `module fpu_exceptions` (shared).** Final special-case and IEEE flag stage. Inputs the rounded result `in_except[63:0]`, `exponent_in`, low mantissa bits `mantissa_in[1:0]`, plus `opa/opb/fpu_op/rmode`. Detects NaN (QNaN/SNaN), ±inf, zero, and operation-specific invalids (div 0/0, inf/inf, inf×0, etc.). Drives the IEEE flags and `ex_enable` (telling the top to substitute its `out` for special results).

**Interaction flow:** `fpu` latches inputs → routes to one of `fpu_add/sub/mul/div` → muxes that unit's mantissa/exp/sign into `fpu_round` → `fpu_round` output goes into `fpu_exceptions` → top selects rounded-vs-exception result and asserts `ready` when the per-op cycle count completes. `fpu_round` and `fpu_exceptions` are shared back-end stages; the four arithmetic modules are parallel front-end datapaths.
