FROM ubuntu:22.04

ARG TZ
ENV TZ="$TZ"

ARG CLAUDE_CODE_VERSION=latest
ARG USERNAME=wizard

# Create the non-root user
RUN apt-get update && apt-get install -y --no-install-recommends \
    sudo ca-certificates curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/* && \
  groupadd --gid 1000 ${USERNAME} && \
  useradd --uid 1000 --gid ${USERNAME} -m -s /bin/bash ${USERNAME} && \
  echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} && \
  chmod 0440 /etc/sudoers.d/${USERNAME}

# Install gh, firewall tooling, and HDL toolchain
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
  yosys \
  gnupg \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Docker CLI only (no daemon). The container talks to a filtered
# socket-proxy via DOCKER_HOST=tcp://socket-proxy:2375 — see docker-compose.yml.
RUN install -m 0755 -d /etc/apt/keyrings && \
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && \
  chmod a+r /etc/apt/keyrings/docker.asc && \
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
    > /etc/apt/sources.list.d/docker.list && \
  apt-get update && \
  apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin docker-buildx-plugin && \
  apt-get clean && rm -rf /var/lib/apt/lists/*

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

# Copy and run firewall setup
COPY init-firewall.sh /usr/local/bin/
USER root
RUN chmod +x /usr/local/bin/init-firewall.sh && \
  echo "${USERNAME} ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/${USERNAME}-firewall && \
  chmod 0440 /etc/sudoers.d/${USERNAME}-firewall
USER ${USERNAME}
