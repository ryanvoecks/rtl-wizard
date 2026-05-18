FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

ARG CLAUDE_CODE_VERSION=latest
ARG USERNAME=wizard

# Minimal base for hosting the Claude Code CLI under inspect_ai. This image
# intentionally does NOT install the ORFS/Verilator EDA stack that
# sandbox/Dockerfile builds from source — those tools take ~60–120 min to
# compile and aren't needed for a Hello-World smoke test. The pieces below
# mirror only the lightweight portions of .devcontainer/Dockerfile (non-root
# user, Claude CLI install) plus the inspect-tool-support bridge from
# sandbox/Dockerfile. Keep this Dockerfile aligned with .devcontainer/Dockerfile
# for the duplicated bits — drift will silently change the agent's runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      python3 \
      python3-pip \
      sudo \
      tzdata \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 ${USERNAME} \
    && useradd --uid 1000 --gid ${USERNAME} -m -s /bin/bash ${USERNAME} \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME}

# inspect-tool-support is the sandbox bridge inspect_ai talks to (matches
# sandbox/Dockerfile:81). Must run as root so the post-install hooks can write
# to system paths.
RUN pip3 install --no-cache-dir inspect-tool-support mcp \
    && inspect-tool-support post-install

RUN mkdir -p /workspace /home/${USERNAME}/.claude \
    && chown -R ${USERNAME}:${USERNAME} /workspace /home/${USERNAME}/.claude

WORKDIR /workspace
USER ${USERNAME}

# Install the Claude Code CLI. Same install line as .devcontainer/Dockerfile:111
# so dev-machine and sandbox stay on the same CLI version. The OAUTH token is
# NOT baked into the image — it's injected per-`exec` by the solver.
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION}
ENV PATH=/home/${USERNAME}/.local/bin:$PATH
