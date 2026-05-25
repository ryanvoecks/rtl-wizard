FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Pin specific version (May 2026) for reproducibility
ARG CLAUDE_CODE_VERSION=2.1.139

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    ripgrep \
    tinyproxy \
    tzdata \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Configure network proxy. Only Anthropic and the MCP server are allowlisted
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf

# Install Claude Code CLI under /opt and symlink into
# /usr/local/bin so per-session unix users can run it
RUN mkdir -p /opt/claude \
    && curl -fsSL https://claude.ai/install.sh \
        | HOME=/opt/claude bash -s ${CLAUDE_CODE_VERSION} \
    && chmod -R a+rX /opt/claude \
    && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude

# Make bundled binaries reachable on PATH
ENV PATH=/opt/claude/.local/bin:$PATH

# Lock down shared writable temp dirs
RUN chmod 700 /tmp /var/tmp
