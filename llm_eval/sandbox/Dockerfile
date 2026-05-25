FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

ARG CLAUDE_CODE_VERSION=latest

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

# Tinyproxy baseline: localhost-only listener (so siblings on the shared
# network can't use us as an open proxy), deny-by-default filter. The
# filter file itself is (re)written at container start by container.py
# so it can include the dynamically-discovered MCP host IP.
RUN printf '%s\n' \
    'User tinyproxy' \
    'Group tinyproxy' \
    'Port 8888' \
    'Listen 127.0.0.1' \
    'Timeout 600' \
    'PidFile "/run/tinyproxy/tinyproxy.pid"' \
    'LogFile "/var/log/tinyproxy/tinyproxy.log"' \
    'LogLevel Info' \
    'Filter "/etc/tinyproxy/filter"' \
    'FilterDefaultDeny Yes' \
    'FilterURLs No' \
    'FilterExtended Yes' \
    'FilterCaseSensitive No' \
    > /etc/tinyproxy/tinyproxy.conf

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
