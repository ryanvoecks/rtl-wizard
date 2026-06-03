### Design summary

This is the **OpenCores WISHBONE DMA/Bridge core** (`wb_dma`, by Rudolf Usselmann, ~2001–2002). It is a parameterizable multi-channel DMA controller that bridges **two independent WISHBONE buses** (WB0 and WB1). Each bus has both a slave interface (for the host CPU to read/write DMA configuration registers) and a master interface (which the DMA engine drives to move data). The core supports **up to 31 DMA channels** with **2/4/8 configurable priority levels**, hardware DMA request/acknowledge handshaking (`dma_req_i`/`dma_ack_o`), linked-list ("descriptor"/ED) chaining, channel-buffer mode, automatic restart (`dma_rest_i`), and two interrupt outputs (`inta_o`, `intb_o`).

Data flow: the host configures channels through the register file via a slave port; the channel-select logic arbitrates among enabled/requesting channels by priority and grants one channel to the DMA engine; the engine reads from a source on one WISHBONE master and writes to a destination on the other, incrementing addresses and counting down the chunk/total size until done.

### Directory organisation

```
.
└── rtl/
    ├── wb_dma_defines.v       # `define macros: register bit positions, config selects
    ├── wb_dma_top.v           # top-level: ties everything together
    ├── wb_dma_rf.v            # register file (all channels)
    ├── wb_dma_ch_rf.v         # per-channel register file (+ _dummy variant)
    ├── wb_dma_ch_sel.v        # channel selection / priority arbitration
    ├── wb_dma_ch_pri_enc.v    # channel priority encoder
    ├── wb_dma_pri_enc_sub.v   # priority-encoder sub-block (per channel)
    ├── wb_dma_ch_arb.v        # round-robin channel arbiter
    ├── wb_dma_de.v            # DMA engine core (the actual data mover)
    ├── wb_dma_wb_if.v         # WISHBONE interface wrapper (master + slave)
    ├── wb_dma_wb_mast.v       # WISHBONE master interface
    ├── wb_dma_wb_slv.v        # WISHBONE slave interface
    └── wb_dma_inc30r.v        # primitive: registered 30-bit incrementer
```

### Key modules/files

- **`wb_dma_top.v`** — Top-level module `wb_dma_top`. Exposes both full WISHBONE buses (WB0/WB1, each with slave + master signal groups), the hardware DMA handshake ports, and interrupts. Heavily parameterized: `ch_count`, `pri_sel`, `rf_addr`, and 31× `chXX_conf` parameters (`{CBUF,ED,ARS,EN}`). It instantiates the four major sub-blocks:
  - `wb_dma_rf` (u: register file)
  - `wb_dma_ch_sel` (channel selection/arbitration)
  - `wb_dma_de` (u2: DMA engine)
  - `wb_dma_wb_if` ×2 (u3, u4: one WISHBONE interface per bus)

- **`wb_dma_rf.v`** — `wb_dma_rf`, the global register file. Holds per-channel CSR/SZ/source/destination/pointer registers and generates interrupt and status signals. Instantiates one **`wb_dma_ch_rf`** (per-channel register file) for each channel; disabled channels use the lightweight **`wb_dma_ch_rf_dummy`** to save area. Register bit layout comes from `wb_dma_defines.v`.

- **`wb_dma_ch_sel.v`** — `wb_dma_ch_sel`, decides which channel runs next. Instantiates **`wb_dma_ch_pri_enc`** (priority encoder over all channels) and a tree of 8× **`wb_dma_ch_arb`** round-robin arbiters to resolve channels of equal priority. Outputs the granted channel to the DMA engine and register file.
  - **`wb_dma_ch_pri_enc.v`** instantiates 31× **`wb_dma_pri_enc_sub.v`** (one per channel; emits each channel's effective request priority).
  - **`wb_dma_ch_arb.v`** is a generic round-robin arbiter (`req`→`gnt` with `advance`).

- **`wb_dma_de.v`** — `wb_dma_de`, the DMA engine core: the FSM/datapath that performs the actual transfers. It drives the two master interfaces, tracks chunk/total counts, and increments source/destination addresses using two instances of **`wb_dma_inc30r`** (registered 30-bit incrementer primitive in `wb_dma_inc30r.v`).

- **`wb_dma_wb_if.v`** — `wb_dma_wb_if`, per-bus WISHBONE wrapper. Instantiates **`wb_dma_wb_mast`** (u0, master used by the DMA engine) and **`wb_dma_wb_slv`** (u1, slave used by the host CPU to access the register file). The slave decodes register accesses via the `WDMA_REG_SEL`/`rf_addr` macro in `wb_dma_defines.v`.

- **`wb_dma_defines.v`** — Shared `` `include `` file. Defines register bit-field positions (`WDMA_CH_EN`, `DST_SEL`, `SRC_SEL`, `MODE`, `BUSY`, `DONE`, `ERR`, `ED_EOL`, etc.) and the slave register-select scheme. Included by `wb_dma_top.v` and referenced throughout.

**Interaction flow:** `wb_dma_top` → host writes config through `wb_dma_wb_if`→`wb_dma_wb_slv` into `wb_dma_rf`/`wb_dma_ch_rf`; `wb_dma_ch_sel` (via `ch_pri_enc`→`pri_enc_sub` and `ch_arb`) picks the active channel; `wb_dma_de` executes the transfer through both `wb_dma_wb_if`→`wb_dma_wb_mast` ports, using `wb_dma_inc30r` for address stepping; status/interrupts flow back through `wb_dma_rf`.
