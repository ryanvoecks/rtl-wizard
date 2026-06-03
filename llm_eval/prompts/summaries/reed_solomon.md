### Design summary

This is a **Reed–Solomon (RS) error-correction codec** in Verilog, operating on 8-bit symbols over **GF(2⁸)** with the primitive polynomial **0x11D** (`x⁸+x⁴+x³+x²+1`, confirmed in `RsDecodeMult.v`). The code has **22 parity symbols** (syndrome/feedback registers `0..21`), giving error-correction capability **t = 11**, and the decoder additionally supports **erasure** correction (a per-symbol `erasureIn` flag).

The design splits into two independent top levels:
- **Encoder** (`RsEncodeTop`): a single-module LFSR-style systematic encoder. It runs the incoming data through a 22-tap GF feedback shift register, then appends the 22 computed parity bytes to the output stream.
- **Decoder** (`RsDecodeTop`): a multi-stage pipeline implementing the classic syndrome → key-equation → root-search flow. Stages: compute syndromes; compute erasure locator polynomial; multiply syndromes by the erasure polynomial (modified syndromes); solve the key equation via the **Euclidean algorithm** to get error-locator σ and error-evaluator ω polynomials; compute polynomial degrees; run a **Chien search** to find error locations and a Forney-style evaluation to find error magnitudes; meanwhile the original data is buffered in a dual-port RAM delay line and corrected on the way out. It also reports `errorNum`, `erasureNum`, and a `fail` flag (uncorrectable).

### Directory organisation

```
.
└── rtl/
    ├── RsEncodeTop.v          # RS encoder (standalone top)
    ├── RsDecodeTop.v          # RS decoder top — instantiates all decode stages
    ├── RsDecodeSyndrome.v     # Syndrome computation (22 syndromes)
    ├── RsDecodeErasure.v      # Erasure locator polynomial (epsilon)
    ├── RsDecodePolymul.v      # Modified-syndrome polynomial multiply
    ├── RsDecodeEuclide.v      # Euclidean key-equation solver (sigma, omega)
    ├── RsDecodeShiftOmega.v   # Shift/align error-evaluator (omega) polynomial
    ├── RsDecodeDegree.v       # Polynomial degree calculation
    ├── RsDecodeChien.v        # Chien search + Forney error-value evaluation
    ├── RsDecodeDelay.v        # Data delay-line controller (wraps DpRam)
    ├── RsDecodeDpRam.v        # Dual-port RAM (data buffer)
    ├── RsDecodeMult.v         # Combinational GF(2^8) multiplier (0x11D)
    └── RsDecodeInv.v          # GF(2^8) multiplicative inverse (lookup)
```

### Key modules/files

**Top levels (entry points)**
- `RsEncodeTop.v` — Standalone encoder. Self-contained; instantiates no submodules. I/O: `CLK/RESET/enable/startPls/dataIn` → `dataOut`.
- `RsDecodeTop.v` — Decoder orchestrator. Instantiates and wires every decode stage in pipeline order (see instantiation map below). I/O includes `erasureIn`, status outputs (`errorNum`, `erasureNum`, `fail`), `delayedData`, and corrected `outData`.

**Decoder pipeline (in dataflow order, all under `RsDecodeTop`)**
1. `RsDecodeSyndrome` — turns the received codeword into 22 syndromes + `doneSyndrome`.
2. `RsDecodeErasure` — builds the erasure-locator polynomial `epsilon_0..22` from the erasure flags.
3. `RsDecodePolymul` — multiplies syndromes by the erasure polynomial → modified syndrome polynomial.
4. `RsDecodeEuclide` — the core key-equation solver (largest module, ~1200 lines); runs the Euclidean algorithm to produce the error-locator (σ) and error-evaluator (ω) polynomials.
5. `RsDecodeShiftOmega` — aligns/shifts the ω polynomial for evaluation.
6. `RsDecodeDegree` (two instances, `_1` and `_2`) — computes polynomial degrees needed for control/decision logic.
7. `RsDecodeChien` — Chien search over GF roots to locate errors, plus Forney evaluation for error magnitudes; uses inverses and many multipliers.
8. `RsDecodeDelay` → `RsDecodeDpRam` — buffer the original input symbols for the full decode latency so they can be XOR-corrected and emitted as `outData`.

**Shared GF(2⁸) primitives (leaf modules)**
- `RsDecodeMult.v` — purely combinational Galois-field multiplier; this is the most heavily reused block (instantiated dozens of times across `RsDecodeEuclide`, `RsDecodeErasure`, `RsDecodePolymul`, `RsDecodeChien`). It encodes the field’s reduction polynomial (0x11D).
- `RsDecodeInv.v` — GF multiplicative inverse (used by `RsDecodeChien` and `RsDecodeEuclide`), needed for Forney/normalization.
- `RsDecodeDpRam.v` — generic dual-port RAM, used only via `RsDecodeDelay` as the codeword delay buffer.

**Interaction summary:** `RsDecodeTop` is the only place the stages connect; data flows linearly Syndrome → Erasure → Polymul → Euclide → ShiftOmega/Degree → Chien, gated by per-stage `done` handshake signals, while `RsDecodeDelay`/`RsDecodeDpRam` run in parallel holding the raw data. `RsDecodeMult` and `RsDecodeInv` are stateless arithmetic helpers shared by the computational stages. The encoder (`RsEncodeTop`) is fully independent of the decoder tree.
