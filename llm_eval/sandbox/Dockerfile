FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

ARG CLAUDE_CODE_VERSION=latest

# Install dependencies. `ripgrep` is needed for claude's Glob tool -- it
# spawns `rg` by bare name, so it has to be resolvable via PATH.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    ripgrep \
    tzdata \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI under /opt (world-readable) and symlink into
# /usr/local/bin so the per-session unix users that ClaudeEnv creates at
# runtime can resolve `claude` via the default PATH. No login user is set
# up: the keepalive runs as root, ClaudeEnv explicitly passes user=root
# for setup (useradd/userdel) and user=<env> for everything else.
RUN mkdir -p /opt/claude \
    && curl -fsSL https://claude.ai/install.sh \
        | HOME=/opt/claude bash -s ${CLAUDE_CODE_VERSION} \
    && chmod -R a+rX /opt/claude \
    && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude

# Make any other bundled binaries (node, etc.) reachable on PATH too.
ENV PATH=/opt/claude/.local/bin:$PATH
