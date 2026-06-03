### Design summary

This is the **Secworks AES block cipher core** (v0.60), implementing the Advanced Encryption Standard supporting **128-bit and 256-bit keys** for both **encryption and decryption** of 128-bit data blocks.

It works as a memory-mapped peripheral: software writes the key, plaintext/ciphertext block, and configuration (encrypt/decrypt, key length) through a 32-bit register interface, then triggers `init` (expand the key schedule) and `next` (process one block). The core uses an iterative round-based datapath — a small FSM steps through the AES rounds, reusing shared S-box hardware across rounds rather than unrolling. Results and status (`ready`, `valid`) are read back over the same bus.

### Directory organisation

```
.
└── rtl/
    ├── aes.v                  # Top-level bus wrapper / register interface
    ├── aes_core.v             # Core FSM + datapath muxing, instantiates submodules
    ├── aes_encipher_block.v   # Encryption round datapath
    ├── aes_decipher_block.v   # Decryption round datapath
    ├── aes_key_mem.v          # Key schedule storage + round-key generation
    ├── aes_sbox.v             # Forward S-box (4× parallel, 32-bit word)
    └── aes_inv_sbox.v         # Inverse S-box (4× parallel, 32-bit word)
```

### Key modules/files

- **`aes.v`** — Top-level wrapper. Provides the external bus interface (`cs`, `we`, `address`, `write_data`, `read_data`). Decodes addresses into registers: NAME/VERSION (RO ID), CTRL (`init`/`next`), STATUS (`ready`/`valid`), CONFIG (`encdec`/`keylen`), KEY0–KEY7 (256-bit key), BLOCK0–BLOCK3 (128-bit input), RESULT0–RESULT3 (128-bit output). Assembles register banks into wide buses and instantiates a single `aes_core`.

- **`aes_core.v`** — The heart of the design. Instantiates all functional submodules and contains the control FSM (`CTRL_IDLE → CTRL_INIT → CTRL_NEXT`). Key responsibilities:
  - On `init`, drives `aes_key_mem` to expand the key (waits for `key_ready`).
  - On `next`, routes the operation to either `aes_encipher_block` or `aes_decipher_block` via the `encdec_mux` (selecting which datapath gets `next`, supplies the round number to key memory, and produces the result).
  - **Shared S-box arbitration**: a single forward `aes_sbox` instance is shared between the key-expansion logic and the encipher datapath. The `sbox_mux` selects `keymem_sboxw` during key init (`init_state`) and `enc_sboxw` otherwise; the result `new_sboxw` fans back to both `aes_key_mem` and `aes_encipher_block`.

- **`aes_key_mem.v`** — Stores the expanded round keys and generates the AES key schedule. Takes the raw `key` + `keylen`, and on `init` computes all round keys (using the shared S-box via its `sboxw`/`new_sboxw` ports). Serves the appropriate `round_key` to the active datapath based on the requested `round`, asserting `ready` when expansion completes.

- **`aes_encipher_block.v`** — Iterative encryption datapath. Given a `block` and per-round `round_key`, performs AddRoundKey / SubBytes / ShiftRows / MixColumns across the rounds, requesting S-box lookups via `sboxw`/`new_sboxw` (its S-box is the shared core instance). Outputs the requested `round` number, the `new_block` result, and `ready`.

- **`aes_decipher_block.v`** — Iterative decryption datapath, the inverse of the encipher block (InvShiftRows / InvSubBytes / InvMixColumns / AddRoundKey). It instantiates its **own `aes_inv_sbox`** internally (note: it does *not* use the shared core S-box; it has no external sbox ports). Outputs `round`, `new_block`, and `ready`.

- **`aes_sbox.v`** — Forward AES S-box implemented as a 256-byte ROM, replicated 4× to substitute a full 32-bit word in one combinational lookup. Shared at the core level between key expansion and encryption.

- **`aes_inv_sbox.v`** — Inverse S-box, same 4×-parallel 32-bit ROM structure, used exclusively inside the decipher block.

**Interaction flow:** `aes.v` (bus) → `aes_core.v` (FSM + muxes) → during `init`, `aes_key_mem` builds round keys using the shared `aes_sbox`; during `next`, either `aes_encipher_block` (shared `aes_sbox`) or `aes_decipher_block` (private `aes_inv_sbox`) consumes round keys from `aes_key_mem` and produces the result that flows back out through `aes_core` to `aes.v`'s RESULT registers.
