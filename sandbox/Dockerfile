FROM openroad/orfs:latest

# OpenROAD ships pre-built in the ORFS image at this path; expose it on PATH.
# yosys and make are already installed in the base.
ENV PATH="/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:${PATH}"

# Install Icarus Verilog (yosys + make are already in the ORFS image)
RUN apt-get update && apt-get install -y --no-install-recommends \
      iverilog \
      ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# inspect-tool-support is the sandbox bridge inspect_ai talks to; the previous
# base image baked it in, so install it (plus mcp) here.
RUN pip3 install --no-cache-dir inspect-tool-support mcp \
    && inspect-tool-support post-install

# Copy RTL MCP tools
COPY . /opt/

WORKDIR /workspace
