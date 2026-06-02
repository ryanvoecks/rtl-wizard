FROM ubuntu:22.04

# Container time tracks the host: tzdata (installed below) supplies zoneinfo,
# and docker-compose.yml bind-mounts /etc/localtime read-only from the host.
# No TZ env var is needed -- glibc reads /etc/localtime directly.

ARG CLAUDE_CODE_VERSION=latest
ARG USERNAME=wizard
ARG ORFS_COMMIT=bd2a6b695156e957a332e644e2c1587a74fd705f

# Create the non-root user. `tzdata` ships /usr/share/zoneinfo so that the
# host's /etc/localtime bind-mount (see docker-compose.yml) resolves to a real
# zone -- without it, glibc has no zoneinfo to read and the container falls back
# to UTC regardless of TZ.
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      sudo ca-certificates curl tzdata \
    && apt-get clean && rm -rf /var/lib/apt/lists/* && \
  groupadd --gid 1000 ${USERNAME} && \
  useradd --uid 1000 --gid ${USERNAME} -m -s /bin/bash ${USERNAME} && \
  echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} && \
  chmod 0440 /etc/sudoers.d/${USERNAME}

# Install gh, firewall tooling, and HDL toolchain.
# Note: yosys is intentionally NOT installed via apt -- Ubuntu 22.04 ships an
# old version, and the ORFS install below provides a much newer one on PATH.
# iverilog stays for Icarus-based workflows (independent of Verilator).
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  git \
  gh \
  ssh \
  iptables \
  ipset \
  iproute2 \
  dnsutils \
  aggregate \
  jq \
  make \
  iverilog \
  gnupg \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Docker CLI only (no daemon). The container talks to a filtered
# socket-proxy via DOCKER_HOST=tcp://socket-proxy:2375 -- see docker-compose.yml.
RUN install -m 0755 -d /etc/apt/keyrings && \
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && \
  chmod a+r /etc/apt/keyrings/docker.asc && \
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
    > /etc/apt/sources.list.d/docker.list && \
  apt-get update && \
  apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin docker-buildx-plugin && \
  apt-get clean && rm -rf /var/lib/apt/lists/*

# Build OpenROAD-flow-scripts + Verilator from source so the same EDA stack
# Inspect's sandbox uses (yosys, openroad, klayout, verilator) is also available
# inside the devcontainer for interactive experimentation. This block mirrors
# sandbox/Dockerfile but uses a different mechanism to cap KLayout: instead of
# shimming `nproc` (which would also affect the running container), we sed-patch
# ORFS's etc/DependencyInstaller.sh so the `numThreads` it passes to KLayout's
# build.sh is hardcoded to 2. `numThreads` is consumed only by the KLayout
# build, so this leaves `nproc` untouched everywhere else.
# See sandbox/Dockerfile for the SUDO_USER=root workaround for setup.sh and
# the rationale for capping KLayout at 2 threads.
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential \
    python3 \
    python3-pip \
    autoconf \
    bison \
    ccache \
    flex \
    help2man \
    libfl-dev \
    perl \
    zlib1g-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Build pinned ORFS commit for reproducibility
RUN git init /OpenROAD-flow-scripts \
    && cd /OpenROAD-flow-scripts \
    && git remote add origin https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts \
    && git fetch --depth 1 origin ${ORFS_COMMIT} \
    && git checkout FETCH_HEAD \
    && git submodule update --init --recursive --depth 1 \
    && sed -i 's/^    numThreads=\$(nproc)$/    numThreads=2/' etc/DependencyInstaller.sh \
    && SUDO_USER=root ./setup.sh \
    && ./build_openroad.sh --local --threads 2 \
    && rm -rf /OpenROAD-flow-scripts/tools/OpenROAD/build \
              /OpenROAD-flow-scripts/tools/yosys/build \
              /root/.cache

ENV PATH="/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/OpenROAD-flow-scripts/tools/install/yosys/bin:${PATH}"

RUN git clone --depth 1 --branch stable \
         https://github.com/verilator/verilator /tmp/verilator \
    && cd /tmp/verilator \
    && autoconf \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    && rm -rf /tmp/verilator

# Create workspace and config directories and set permissions
RUN mkdir -p /workspace /home/${USERNAME}/.claude && \
  chown -R ${USERNAME}:${USERNAME} /workspace /home/${USERNAME}/.claude

WORKDIR /workspace

# Set up non-root user
USER ${USERNAME}

# Install Claude
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION}
ENV PATH=/home/${USERNAME}/.local/bin:$PATH

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV UV_LINK_MODE=copy

# Pre-create uv's data dir so the named volume mount in docker-compose.yml
# inherits wizard ownership. Docker copies the image's target-path ownership
# into a fresh named volume only if that path exists; otherwise the volume
# mounts as root:root and `uv sync` fails with EACCES when uv tries to
# install a managed Python (image ships 3.10; pyproject pins 3.12).
RUN mkdir -p /home/${USERNAME}/.local/share/uv

# Copy and run firewall setup
COPY init-firewall.sh /usr/local/bin/
USER root
RUN chmod +x /usr/local/bin/init-firewall.sh && \
  echo "${USERNAME} ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/${USERNAME}-firewall && \
  chmod 0440 /etc/sudoers.d/${USERNAME}-firewall

# Late-stage installs: kept at the end of the file so changes here don't
# bust the OpenROAD/Verilator build cache above.
#  - libpython3.10: runtime .so.1.0 OpenROAD's embedded Python links
#    against (apt's python3-{minimal,stdlib} subset doesn't ship it).
#  - python3-dev: Python headers required to build cocotb's VPI shim.
#  - cocotb stack: drives the forencich verilog_axi crossbar's TB.
#    Installed system-wide so `cocotb-config` is on PATH and iverilog's
#    VPI links to system python3.10. Pinned to cocotb<2.0 because 2.x
#    breaks an X-coerce path in the bench's reset. jinja2 backs the
#    bench's `axi_crossbar_wrap.py` wrapper generator.
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libpython3.10 \
    python3-dev \
    tmux \
  && apt-get clean && rm -rf /var/lib/apt/lists/* && \
  pip3 install --no-cache-dir \
    'cocotb<2.0' \
    cocotb-test \
    cocotbext-axi \
    jinja2

USER ${USERNAME}
