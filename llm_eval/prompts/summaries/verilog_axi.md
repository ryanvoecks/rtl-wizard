### Design summary

This is the **`verilog-axi` AXI4 crossbar interconnect** by Alex Forencich. It implements a fully-parameterizable, non-blocking AXI4 crossbar that connects `S_COUNT` AXI slave interfaces (upstream masters/initiators) to `M_COUNT` AXI master interfaces (downstream targets), routing transactions by address.

How it works: the top-level `axi_crossbar` splits each AXI transaction into independent **write** and **read** paths (`axi_crossbar_wr` / `axi_crossbar_rd`). Each path, per slave port, uses `axi_crossbar_addr` to decode the target address into a master-port selection and enforce admission/ordering control (thread tracking, in-flight issue limits, security regions). An `arbiter` (backed by a `priority_encoder`) resolves contention when multiple slaves target the same master port. Optional pipeline/skid registers (`axi_register_wr` / `axi_register_rd`) can be inserted on any channel of any port (configured via the `*_REG_TYPE` parameters: 0=bypass, 1=simple buffer, 2=skid buffer) for timing closure. Master IDs are widened from `S_ID_WIDTH` to `M_ID_WIDTH = S_ID_WIDTH + clog2(S_COUNT)` so responses can be routed back to the originating slave.

### Directory organisation

```
.
└── rtl/
    ├── rtl/                          # synthesizable RTL
    │   ├── axi_crossbar.v            # top-level crossbar (wraps wr + rd paths)
    │   ├── axi_crossbar_wr.v         # write-channel crossbar (AW/W/B)
    │   ├── axi_crossbar_rd.v         # read-channel crossbar (AR/R)
    │   ├── axi_crossbar_addr.v       # per-slave address decode + admission control
    │   ├── axi_register_wr.v         # optional write-channel pipeline registers
    │   ├── axi_register_rd.v         # optional read-channel pipeline registers
    │   ├── arbiter.v                 # round-robin / priority arbiter
    │   └── priority_encoder.v        # priority encoder (used by arbiter)
    └── tb/
        └── axi_crossbar/
            └── axi_crossbar_wrap_8x8.v   # generated 8x8 wrapper w/ flat per-port params
```

### Key modules/files

- **`axi_crossbar.v`** — Top-level entry point. Exposes the full set of `S_COUNT` slave and `M_COUNT` master AXI interfaces and all configuration parameters (counts, widths, `M_BASE_ADDR`/`M_ADDR_WIDTH` for address map, `M_CONNECT_READ`/`M_CONNECT_WRITE` connectivity matrices, `M_ISSUE`/`S_THREADS`/`S_ACCEPT` flow control, `*_REG_TYPE` register options). Instantiates one `axi_crossbar_wr` and one `axi_crossbar_rd` (line ~228 / ~314).

- **`axi_crossbar_wr.v`** / **`axi_crossbar_rd.v`** — The actual switch fabric for the write (AW/W/B) and read (AR/R) channels respectively. Each instantiates, per slave port, an `axi_crossbar_addr` for decode/admission; an `arbiter` per master port to grant access; and optional `axi_register_wr`/`axi_register_rd` on each interface. They handle the data-mux, ID prefixing, and response routing.

- **`axi_crossbar_addr.v`** — Per-slave decode + admission control engine. Compares the incoming address against each master's base/width regions to pick the target port (`M_CONNECT`), enforces `M_SECURE` regions, and tracks outstanding transactions per thread (`S_THREADS`, `S_ACCEPT`) to preserve AXI ordering rules. Parameter `S` identifies which slave instance it serves.

- **`arbiter.v`** — Configurable arbiter (priority or round-robin, with optional blocking/LSB-high modes) that selects among requesting slave ports for a given master port. Instantiates `priority_encoder` (line ~70 / ~87).

- **`priority_encoder.v`** — Small parameterizable priority encoder; pure combinational building block for the arbiter.

- **`axi_register_wr.v`** / **`axi_register_rd.v`** — Optional per-channel register slices (bypass / simple buffer / skid buffer) inserted by the crossbar paths for pipelining and timing.

- **`tb/axi_crossbar/axi_crossbar_wrap_8x8.v`** — A generated convenience wrapper hard-configured for an 8-slave × 8-master crossbar, flattening the packed array parameters into individual per-port parameters (`S00_*`…`S07_*`, `M00_*`…`M07_*`) and per-port AXI ports. Useful as an instantiation example and as the DUT for testing.

**Interaction flow:** `axi_crossbar` → {`axi_crossbar_wr`, `axi_crossbar_rd`} → each uses {`axi_crossbar_addr` (decode/admission), `arbiter`→`priority_encoder` (contention), `axi_register_*` (pipelining)}.
