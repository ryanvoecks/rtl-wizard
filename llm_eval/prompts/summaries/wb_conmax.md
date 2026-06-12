### Design summary

This is the **WISHBONE Conmax (Connection Matrix) IP core** from OpenCores (by Rudolf Usselmann) — a configurable, fully-connected crossbar interconnect for the WISHBONE SoC bus. It connects **8 WISHBONE masters to 16 WISHBONE slaves**, allowing any master to address any slave concurrently as long as they don't target the same slave.

How it works:
- **Address-based slave routing:** Each master's interface decodes the **top 4 address bits** (`wb_addr_i[aw-1:aw-4]`) to steer that master's transaction to one of the 16 slaves, then muxes the selected slave's response (`data`/`ack`/`err`/`rty`) back to the master.
- **Per-slave arbitration:** Each slave's interface arbitrates among the 8 masters contending for it and muxes the winning master onto the slave port. Each slave can independently use either simple **round-robin** arbitration (`pri_sel = 0`) or **priority-based** arbitration (2-level or 4-level, `pri_sel = 1` or `2`).
- **Runtime configuration:** A register file (16 × 16-bit `conf` registers, one per slave) is memory-mapped on the internal slave-15 bus at `rf_addr` (default `4'hf`). These registers program each master's arbitration priority for each slave. The register file passes through to the external slave-15 port when the RF address isn't selected.

The whole thing is parameterizable: `dw=32` (data width), `aw=32` (address width), `sw=dw/8` (byte selects), `rf_addr`, and `pri_sel0..15` (per-slave arbitration mode).

### Directory organisation

```
.
└── rtl/
    ├── wb_conmax_defines.v      # `timescale + global defines
    ├── wb_conmax_top.v          # top level: 8 masters × 16 slaves crossbar (largest file)
    ├── wb_conmax_master_if.v    # per-master interface (×8): address-decode → slave select
    ├── wb_conmax_slave_if.v     # per-slave interface (×16): arbitrate masters → slave
    ├── wb_conmax_msel.v         # priority-based master-select wrapper
    ├── wb_conmax_pri_enc.v      # priority encoder (8 masters → winning priority level)
    ├── wb_conmax_pri_dec.v      # priority decoder helper (per-master priority expansion)
    ├── wb_conmax_arb.v          # generic round-robin arbiter (8 req → 3-bit grant)
    └── wb_conmax_rf.v           # configuration register file (16 conf registers)
```

### Key modules/files

- **`wb_conmax_top.v`** — Top-level integration. Declares all 8 master ports and 16 slave ports plus parameters, instantiates **8 `wb_conmax_master_if` + 16 `wb_conmax_slave_if` + 1 `wb_conmax_rf`**, and wires the full crossbar using internal nets named `m<X>s<Y>_*` (master X ↔ slave Y). Each master_if fans out to all 16 slaves; each slave_if collects from all 8 masters. Distributes `conf0..conf15` from the RF to each slave's `conf` input.

- **`wb_conmax_master_if.v`** (instanced ×8 as `m0..m7`) — A master's view of the matrix. Computes `slv_sel = wb_addr_i[aw-1:aw-4]` and broadcasts the master's `addr/data/sel/we` to all slaves while gating only the selected slave's `cyc`/`stb`; muxes the selected slave's `data_o`/`ack`/`err`/`rty` back to the master via `case(slv_sel)`. Registers per-slave `cyc` for transaction continuity.

- **`wb_conmax_slave_if.v`** (instanced ×16 as `s0..s15`, parameterized by `pri_selN`) — A slave's view. Builds an 8-bit request vector from masters' `cyc`. Produces `mast_sel` from **two sources**: `mast_sel_simple` (a `wb_conmax_arb` round-robin) when `pri_sel==0`, else `mast_sel_pe` (from `wb_conmax_msel`). Uses `mast_sel` to mux the granted master's `addr/data/sel/we/cyc/stb` onto the shared slave port and route `ack`/`err`/`rty` back to that master. Takes the slave's 16-bit `conf` register as priority input.

- **`wb_conmax_msel.v`** — Priority-based master selector used by slave_if. Decodes each master's 2-bit priority from `conf`, splits the request vector into 4 per-priority-level request vectors, runs **four `wb_conmax_arb` instances (arb0..arb3)** in parallel (one per priority level), uses **`wb_conmax_pri_enc`** to pick the highest active priority level, then selects that level's grant as the final `sel`. Honors `pri_sel` (0/1/2 → round-robin / 2-level / 4-level).

- **`wb_conmax_arb.v`** — Generic round-robin arbiter: 8-bit `req` → 3-bit `gnt`, with a `next` input to advance the round-robin pointer. The reusable arbitration primitive (used directly in slave_if and ×4 inside msel).

- **`wb_conmax_pri_enc.v` / `wb_conmax_pri_dec.v`** — Priority encode/decode helpers. `pri_enc` instantiates 8 `pri_dec` blocks (one per master) to expand each master's configured priority, then encodes the 8 masters' priorities into the winning 2-bit `pri_out` level for msel.

- **`wb_conmax_rf.v`** — Configuration register file holding `conf0..conf15` (16-bit each). Sits **inline on the internal slave-15 WISHBONE bus**: when the address matches `rf_addr` (`i_wb_addr_i[aw-5:aw-8]`), it services config reads/writes (word index `addr[5:2]`); otherwise it transparently forwards the cycle to the external slave-15 port. Its `confN` outputs feed slave_if `s<N>`'s arbitration priority.

- **`wb_conmax_defines.v`** — Sets `` `timescale 1ns / 10ps `` and global defines, `` `include ``d by the other files.

**Interaction flow:** master_if (address-decodes which slave) → crossbar nets → slave_if (arbitrates which master, via arb directly or via msel→pri_enc/pri_dec+arb) → response muxed back. The RF (programmed over the bus through slave-15) sets the `conf` priorities that drive each slave's arbitration.
