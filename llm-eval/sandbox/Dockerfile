FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

ARG CLAUDE_CODE_VERSION=latest
ARG USERNAME=wizard

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    sudo \
    tzdata \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create user
RUN groupadd --gid 1000 ${USERNAME} \
    && useradd --uid 1000 --gid ${USERNAME} -m -s /bin/bash ${USERNAME} \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME}

# Set working dir and user
RUN mkdir -p /workspace && chown -R ${USERNAME}:${USERNAME} /workspace
WORKDIR /workspace
USER ${USERNAME}

# Install Claude Code CLI
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION}
ENV PATH=/home/${USERNAME}/.local/bin:$PATH
