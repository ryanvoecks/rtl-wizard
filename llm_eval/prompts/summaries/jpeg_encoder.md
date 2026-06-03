### Design summary

This is the **OpenCores Baseline JPEG Encoder** (by Richard Herveille, ~2002). It takes a stream of 8×8 pixel blocks and produces the symbol stream needed for JPEG entropy coding — i.e. it covers everything *up to* (but not including) the final Huffman/entropy bit-packing.

The pipeline, driven by a common `clk`/`ena`/`rst` and a `dstrb` data-strobe handshake, is three stages:

1. **FDCT + ZigZag** — performs the 2-D Forward Discrete Cosine Transform on the 8×8 block (separable row/column passes using MAC units) and reorders the 64 coefficients into JPEG zig-zag scan order.
2. **Quantization & Rounding (QNR)** — divides each coefficient by a per-coefficient quantization table value (`qnt_val`, addressed by `qnt_cnt`) using a signed/unsigned serial divider, with rounding.
3. **Run-Length Encoder (RLE)** — converts the quantized zig-zag stream into JPEG `(run-length, size, amplitude)` symbol tuples, including zero-run collapsing and end-of-block handling.

The top level emits `size`, `rlen`, `amp`, and `douten`. Note: the DC-differential generator is a documented `TODO` (currently a passthrough), so this is the transform/quantize/RLE core rather than a complete bitstream generator.

### Directory organisation

```
rtl/
├── jpeg/rtl/verilog/
│   └── jpeg_encoder.v        # top level — wires the 3 stages together
├── dct/rtl/verilog/
│   ├── fdct.v                # FDCT + zigzag wrapper
│   ├── dct.v                 # 2-D DCT engine
│   ├── dctub.v               # DCT unit block (8 dctu)
│   ├── dctu.v                # single DCT unit (wraps a MAC)
│   ├── dct_mac.v             # multiply-accumulate primitive
│   └── zigzag.v              # zig-zag reordering buffer
├── qnr/rtl/verilog/
│   ├── jpeg_qnr.v            # quantization + rounding
│   ├── div_su.v              # signed/unsigned divider wrapper
│   └── div_uu.v              # unsigned serial divider core
└── run_length_coding/rtl/verilog/
    ├── jpeg_rle.v            # RLE top (rle1 + 4× rzs)
    ├── jpeg_rle1.v           # core run-length / size / amplitude coder
    └── jpeg_rzs.v            # zero-run / EOB stage (cascaded ×4)
```

### Key modules / files

- **`jpeg_encoder.v`** (top): Instantiates `fdct`, `jpeg_qnr`, and `jpeg_rle` in sequence and inserts the 1-cycle delay registers that keep the stages synchronized with the synchronous quantization RAM/ROM. Key params: `coef_width=11`, `di_width=8`. Holds the placeholder `dc_diff` (DC-differential) passthrough.

- **DCT subsystem** (`dct/`):
  - `fdct.v` — wrapper that connects the `dct` engine to the `zigzag` reorderer; this is what the top level instantiates as `fdct_zigzag`.
  - `dct.v` — the 2-D DCT; instantiates **8× `dctub`** (one per output frequency), manages the 64-sample (`sample_cnt`) counting and `go`/`douten` control.
  - `dctub.v` — instantiates **8× `dctu`**; `dctu.v` wraps a single **`dct_mac.v`** multiply-accumulate cell. So the DCT array is structurally dct → 8×dctub → 8×dctu → dct_mac.
  - `zigzag.v` — 64-entry buffer that emits coefficients in JPEG zig-zag order.

- **QNR subsystem** (`qnr/`):
  - `jpeg_qnr.v` — drives the `qnt_cnt` address, feeds coefficient + `qnt_val` into the divider, and rounds/slices the result (`dout`). Uses `dstrb`-aligned delay pipeline (`dep`) to produce `douten`.
  - `div_su.v` — handles signedness, wrapping `div_uu.v`, the actual unsigned serial (radix-2) divider producing quotient `q` and remainder `s`.

- **RLE subsystem** (`run_length_coding/`):
  - `jpeg_rle.v` — top; instantiates one **`jpeg_rle1`** followed by a chain of **4× `jpeg_rzs`** (`rz1..rz4`), then maps the final stage outputs to `rlen`/`size`/`amp`/`douten`/`bstart`.
  - `jpeg_rle1.v` — core coder: a DC/AC state machine that computes `size` (magnitude category) and `amp` (amplitude) per coefficient and flags `dcterm` (block start).
  - `jpeg_rzs.v` — a single zero-run/EOB stage; the 4-deep cascade accumulates zero runs and detects end-of-block (`eob`)/zero-block (`zerobl`) conditions.

**Data flow:** `din → fdct(dct→zigzag) → [delay] → jpeg_qnr(div_su→div_uu) → [delay] → jpeg_rle(jpeg_rle1→rzs×4) → {size, rlen, amp, douten}`.
