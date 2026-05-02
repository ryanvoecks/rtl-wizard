FROM aisiuk/inspect-tool-support

RUN apt-get update && apt-get install -y --no-install-recommends \
      make \
      iverilog \
      yosys \
      ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir mcp

COPY yosys_runner.py /opt/yosys_runner.py

# The inspect-tool-support base image sets LD_LIBRARY_PATH to point at its
# bundled Python venv libs, which breaks EDA tools (vcs/iverilog/yosys) that
# link against system libs. Clear it so every shell/tool in the sandbox starts
# clean.
ENV LD_LIBRARY_PATH=""

WORKDIR /workspace
