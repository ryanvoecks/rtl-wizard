### Design summary

This is **UberDDR3**, an open-source, parameterizable DDR3 memory controller + PHY-control RTL (by Angelo C. Jacobo, GPLv3). It is a **4:1 memory controller** (controller clock = 4× DDR3 clock, fixed `serdes_ratio=4`) that presents a **Wishbone** bus to user logic and drives an external SERDES-based PHY (ISERDES/OSERDES, IDELAY/ODELAY, bitslip) to talk to DDR3 SDRAM. It targets Xilinx 7-series (Kintex-7) at up to 1600 Mbps and was built for the ZipCPU eth10g network-switch project.

Core functionality:
- **JEDEC reset/init sequence** stored as a ROM of timed commands.
- A **bank-status tracking + command-issue pipeline** (stage0→stage1→stage2) that handles ACT/PRE/RD/WR scheduling, refresh, and timing-parameter enforcement; it's designed for sustained sequential throughput.
- **Read/write calibration FSM** (~25 states) doing DQS bitslip training, MPR reads, write-leveling, IDELAY/ODELAY tuning, and a **BIST** self-test (burst/random/alternating r-w).
- Optional **ECC** (sideband per-burst, sideband per-8-bursts, or inline) via external `ecc_enc`/`ecc_dec`.
- A **second Wishbone (WB2) interface** for register-level PHY access, plus optional UART debug output, dual-rank DIMM support, and self-refresh.
- An extensive `` `ifdef FORMAL `` section for formal verification.

### Directory organisation

```
.
└── rtl/
    └── ddr3_controller.v    # entire design (6078 lines)
```

### Key modules/files

There is a single source file, `rtl/ddr3_controller.v`, containing two modules plus references to external ones:

- **`module ddr3_controller` (lines 66–6006)** — the top-level controller. It is one large module organized into clearly delimited `/***** ... *****/` sections:
  - **Parameters & port list (66–167):** timing (`CONTROLLER_CLK_PERIOD`, `DDR3_CLK_PERIOD`, `SPEED_BIN`, `TRCD/TRP/TRAS`), geometry (`ROW/COL/BA/DQ_BITS`, `LANES`, `SDRAM_CAPACITY`), feature toggles (`ECC_ENABLE`, `DUAL_RANK_DIMM`, `BIST_MODE`, `ODELAY_SUPPORTED`, `SECOND_WISHBONE`, `DLL_OFF`). Ports: Wishbone (data/PHY-config WB2), the PHY interface (ISERDES data/DQS, command bus `o_phy_cmd`, delay loads, bitslip), `o_calib_complete`, debug, and `uart_tx`.
  - **Command / timing / computed-delay / calibration / MRS parameter blocks (170–436):** DDR3 command encodings, `localparam` timing values, and the calibration FSM state encodings (`IDLE`, `MPR_READ`, `ANALYZE_DQS`, `START_WRITE_LEVEL`, `BURST_WRITE`, …, `DONE_CALIBRATE` at 362–387).
  - **Registers & wires (439–725):** including the pipeline state (`stage1_pending/we/data`, `stage2_pending/we`, `stage*_stall`, `ecc_stage*`).
  - **Reset sequence + ROM controller (729–960):** `read_rom_instruction()` constant-function ROM holds the timed init commands; a small controller sequences them.
  - **Track Bank Status & Issue Command (963–2137):** the heart of the controller — the stage0/1/2 pipeline that tracks per-bank open/active state and emits DDR3 commands while honoring timing.
  - **Align Read Data from ISERDES (2139–2385).**
  - **Read/Write Calibration Sequence (2388–3588):** the big calibration FSM + BIST.
  - **Calibration Test Receiver (3590–3676)** and **Wishbone 2 / PHY register interface (3678–3862)** (with UART debug helpers `hex_to_ascii`, etc.).
  - **Functions (3865–4074):** unit-conversion/util helpers — `ps_to_cycles`, `nCK_to_cycles`, `ps_to_nCK`, `nCK_to_ps`, `max`, `find_delay`, `undecoded_data`.
  - **Module Instantiations (4076–4250):** a `generate` block that, depending on `ECC_ENABLE` (0/1/2/3), instantiates external **`ecc_enc`**/**`ecc_dec`** encoder/decoder modules (not defined in this file). A `mini_fifo` is also instantiated (line 4718).
  - **Formal verification & `$display` parameter dump (4252–6006):** `` `ifndef YOSYS `` info prints and `` `ifdef FORMAL `` assertions/cover properties.

- **`module mini_fifo` (6011–6076):** a tiny parameterized (`FIFO_WIDTH`, `DATA_WIDTH`) circular-buffer FIFO with `empty/full` flags and `read_data`/`read_data_next` outputs; used internally by the controller for buffering, and carries its own formal assertions.

- **External dependencies (referenced, not in this file):** `ecc_enc` and `ecc_dec` (ECC encode/decode) must be provided elsewhere when `ECC_ENABLE != 0`. The PHY primitives (ISERDES/OSERDES/IDELAY/ODELAY) live outside this file and are driven through the `o_phy_*` / `i_phy_*` ports.

Interaction flow: user logic drives the **primary Wishbone** → requests enter the **stage0→stage1→stage2 command pipeline** → which (after the **reset ROM** and **calibration FSM** complete and assert `o_calib_complete`) emits packed commands on `o_phy_cmd` and write data on `o_phy_data` to the external PHY; read data returns via `i_phy_iserdes_*`, is aligned, optionally ECC-decoded, and returned on the Wishbone. **WB2** provides side-channel access to PHY/delay registers; `mini_fifo` buffers in-flight transactions.
