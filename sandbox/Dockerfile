FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Build prerequisites for ORFS bootstrap. setup.sh / DependencyInstaller.sh
# pulls in everything else (Qt, Boost, OR-Tools-from-source on aarch64, etc.)
# but it expects git, sudo, and a working toolchain to already exist.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      git \
      sudo \
      build-essential \
      python3 \
      python3-pip \
      curl \
      iverilog \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Build OpenROAD-flow-scripts from source. ORFS supports aarch64 natively as of
# OpenROAD PR #4613 (Aug 2024); DependencyInstaller.sh detects `uname -m` and
# builds OR-Tools from source on arm64 (no prebuilt archive exists upstream).
#
# Memory note: KLayout's heavy template files (e.g. dbHierProcessor2.cc) can use
# 4+ GB per cc1plus, so unrestricted parallelism OOMs Docker hosts <16 GB. Cap
# parallelism via a `nproc` shim that returns BUILD_JOBS (default 2). Override
# at build time with `--build-arg BUILD_JOBS=N` if you have more memory.
ARG BUILD_JOBS=2
RUN printf '#!/bin/sh\necho %s\n' "${BUILD_JOBS}" > /usr/local/bin/nproc \
    && chmod +x /usr/local/bin/nproc \
    && ln -sf /usr/local/bin/nproc /usr/bin/nproc

# SUDO_USER=root: setup.sh runs `sudo -u $SUDO_USER DependencyInstaller.sh -common`
# expecting to have been launched via sudo from a regular user. Inside Docker we
# run as root with no sudo invocation, so SUDO_USER is unbound and the script
# aborts under `set -u`. Setting it to root makes `sudo -u root` a no-op.
RUN git clone --recursive --depth 1 \
      https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts \
      /OpenROAD-flow-scripts \
    && cd /OpenROAD-flow-scripts \
    && SUDO_USER=root ./setup.sh \
    && ./build_openroad.sh --local --threads $(nproc) \
    && rm -rf /OpenROAD-flow-scripts/tools/OpenROAD/build \
              /OpenROAD-flow-scripts/tools/yosys/build \
              /root/.cache

ENV PATH="/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/OpenROAD-flow-scripts/tools/install/yosys/bin:${PATH}"

# Build Verilator v5 from source for SystemVerilog simulation. Ubuntu 22.04's
# apt verilator is 4.038, which lacks `--binary` and `--timing` (added in v5)
# and has weaker SV2012/2017 coverage. Placed after the ORFS layer so the long
# OpenROAD build stays cached.
RUN apt-get update && apt-get install -y --no-install-recommends \
      autoconf \
      bison \
      ccache \
      flex \
      help2man \
      libfl-dev \
      perl \
      zlib1g-dev \
    && git clone --depth 1 --branch stable \
         https://github.com/verilator/verilator /tmp/verilator \
    && cd /tmp/verilator \
    && autoconf \
    && ./configure --prefix=/usr/local \
    && make -j$(nproc) \
    && make install \
    && rm -rf /tmp/verilator \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# inspect-tool-support is the sandbox bridge inspect_ai talks to.
RUN pip3 install --no-cache-dir inspect-tool-support mcp \
    && inspect-tool-support post-install

# Copy RTL MCP tools
COPY . /opt/

WORKDIR /workspace
