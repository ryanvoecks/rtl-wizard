# RTL Wizard

MCP server with tools for generating high-quality PPA-optimised RTL. Includes full Verilog generation benchmarking suite.

## Setup

Requires `uv` and `docker`.

```bash
git submodule update --init --recursive
```

## Running

Use `inspect` to run tests. Example:

```bash
uv run inspect eval benchmark/tasks.py -T design=adder_8bit
```
