### Design summary

This RTL implements a **modular exponentiation core** (`modexp`) that computes **C = M^e mod N** — the fundamental operation behind public‑key crypto such as RSA, DH and ElGamal. It originates from the Cryptech project (J. Strömbergson / P. Magnusson). Operands are big integers stored as arrays of 32‑bit words; `n` and `m` are configurable in 32‑bit steps (up to 8192 bits in principle, though the block memories here are instantiated 32 words deep).

The algorithm is **left‑to‑right square‑and‑multiply performed in the Montgomery domain**:
1. A `residue` unit precomputes the Montgomery conversion factor `Nr = 2^(2N) mod N` (only when the modulus changes).
2. Operands are mapped into Montgomery form, then the FSM iterates over the exponent bits doing a `montprod` square (`P=P*P`) every bit and a conditional `montprod` multiply (`Z=Z*P`) when the exponent bit is 1.
3. A final `montprod` with 1 maps the result back out of Montgomery form.
All multiplication is done bit‑serial / word‑serial using only 32‑bit adders and shifters, so no hardware multiplier is needed. A host drives everything through a 32‑bit memory‑mapped register interface and polls a `ready`/status bit; a 64‑bit cycle counter measures latency.

### Directory organisation

```
./
└── rtl/
    ├── modexp.v             # Top-level wrapper + 32-bit register/API interface
    ├── modexp_core.v        # Main engine: square-and-multiply FSM + memories
    ├── montprod.v           # Montgomery product calculator (A*B*2^-n mod M)
    ├── residue.v            # Montgomery residue 2^(2N) mod N calculator
    ├── adder32.v            # 32-bit adder with carry in/out
    ├── shl32.v              # 32-bit left shift (x2) with carry
    ├── shr32.v              # 32-bit right shift (/2) with carry
    ├── blockmem1r1w.v       # 32x32 RAM: 1 read / 1 write port
    ├── blockmem2r1w.v       # 32x32 RAM: 2 read / 1 write port
    ├── blockmem2r1wptr.v    # 2r/1w RAM, port1 + write via auto-inc pointer
    └── blockmem2rptr1w.v    # 2r/1w RAM, port1 read via pointer, explicit write addr
```

### Key modules/files

**`modexp.v`** — Top-level. Exposes the bus interface (`clk`, `reset_n`, `cs`, `we`, `address[11:0]`, `write_data`, `read_data`). An address‑decode `always` block (`api`) maps offsets to: core name/version, control (`start`), status (`ready`), cycle counter, `modulus_length`/`exponent_length` registers, and the four operand memory "data + pointer‑reset" windows (modulus, exponent, message, result). It holds the two length registers and the `start` pulse, then instantiates one `modexp_core`, forwarding the memory API strobes straight through.

**`modexp_core.v`** — The heart of the design. Contains:
- The master FSM `modexp_ctrl` (states `IDLE → RESIDUE → CALCULATE_Z0 → CALCULATE_P0 → ITERATE/ITERATE_Z_P/ITERATE_P_P/ITERATE_END → CALCULATE_ZN → DONE`).
- Operand‑select logic (`montprod_op_select`, the `MONTPROD_SELECT_*` codes) that routes the right pair of memories into `montprod`, and a write mux (`MONTPROD_DEST_*`) that routes `montprod` results back to the `Z` (result) or `P` memory.
- Exponent‑bit reader (`E_word_index`/`E_bit_index`/`ei`), loop counter, residue‑valid caching (recompute residue only when modulus is rewritten), and the 64‑bit cycle counter.
- Instantiates the worker units **`montprod`** and **`residue`**, plus six memories: `residue_mem` and `p_mem` (`blockmem2r1w`), `exponent_mem`/`modulus_mem`/`message_mem` (`blockmem2r1wptr`, host loads them sequentially via pointer), and `result_mem` (`blockmem2rptr1w`).
- Note a documented hide‑timing option: `EXPONATION_MODE_SECRET_SECURE` vs `_PUBLIC_FAST` controls whether the fake multiply is skipped (constant‑time vs fast).

**`montprod.v`** — Montgomery product `S = A·B·2^-n mod M`, the inner kernel called repeatedly by the core. Its FSM walks the bits of B; per bit it computes `s = (s + q·M + b·A) >> 1` word‑serially. Uses two **`adder32`** instances (one for `+A`, one for `+M`), one **`shr32`**, and a private **`blockmem1r1w`** (`s_mem`) holding the running sum `S`. Exposes addr/data ports for operands A, B, M and a result write port that the core wires to result/P memory.

**`residue.v`** — Computes `Nr = 2^(2N) mod M` by the "shift‑left then conditional subtract" loop over `2N` iterations (`nn`). Uses **`adder32`** (as subtract/compare via `~opm_data`) and **`shl32`**, driving the core's `residue_mem`. (Contains a noted TODO bug: it tests carry for "less than" but not zero for "equal".)

**Arithmetic primitives** — `adder32.v` (33‑bit add giving sum+carry), `shl32.v` (×2 with carry), `shr32.v` (÷2 with carry). All purely combinational and shared by `montprod` and `residue`.

**Memory family** — Four single‑clock block‑RAM variants (32×32‑bit) differing only in port style: plain `1r1w` and `2r1w`; the `*ptr*` versions add an auto‑incrementing internal pointer so the host can stream operand words in/out through a single data register (pointer reset via `rst`, advance via `cs`). These are the glue that lets the bus interface and the compute FSMs share the big‑integer storage.

**Typical interaction flow:** host writes lengths + operands → sets `start` → `modexp_core` FSM runs `residue` (if needed) then orchestrates many `montprod` calls over the exponent bits, shuttling intermediate values between `result_mem`/`p_mem`/`residue_mem` → asserts `ready` → host reads the result memory.
