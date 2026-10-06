# syntax=docker/dockerfile:1

# The developer worker. The developer manager starts one container of this
# image for each task. No person runs it by hand.

# Build stage: resolve and install the member and its dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-developer-worker

# Runtime stage: git, Node, the npm Claude Code CLI, the venv, and the package.
FROM python:3.13-slim AS runtime

# git clones, commits, and pushes the task branch.
#
# The session normally does not run the npm CLI. claude-agent-sdk starts the
# CLI that its wheel bundles (2.1.239 at SDK 0.2.144), and looks on PATH only
# when that file is missing. The npm CLI, at the pin of joshua-ai core, is
# that fallback. Node stays for two reasons: the npm CLI needs it, and the model
# runs the formatter and the tests of a JavaScript project with Bash.
#
# The CLI needs Node 22 or newer, and Debian's nodejs is 20.x, so Node comes
# from NodeSource. npm 11 blocks install scripts by default; the CLI's
# postinstall links the platform binary, so --allow-scripts permits that one
# package. "claude --version" proves the fallback works.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g npm@11.18.0 \
    && npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@2.1.201 \
    && claude --version

RUN useradd --uid 1000 --create-home worker \
    && mkdir -p /work/home/.claude \
    && chown -R 1000:1000 /work
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder --chown=1000:1000 /app/addons/developer-worker /app/addons/developer-worker
WORKDIR /work
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/work/home \
    CLAUDE_CONFIG_DIR=/work/home/.claude
# What the image knows about itself. The workflows pass these; a plain build
# has none and reports `dev`. The server reports ADDON_VERSION as
# serverInfo.version at initialize, and the start log names both.
ARG ADDON_VERSION=dev
ARG ADDON_REVISION=""
ENV ADDON_VERSION=$ADDON_VERSION \
    ADDON_REVISION=$ADDON_REVISION
LABEL org.opencontainers.image.version=$ADDON_VERSION \
      org.opencontainers.image.revision=$ADDON_REVISION
USER 1000
ENTRYPOINT ["python", "-m", "joshua_developer_worker"]
