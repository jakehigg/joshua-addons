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

# Runtime stage: git, Node, the Claude Code CLI, the venv, and the package.
FROM python:3.13-slim AS runtime

# The same Node, npm, and CLI pins as joshua-ai core. The CLI needs Node 22
# or newer, and Debian's nodejs is 20.x, so Node comes from NodeSource. npm 11
# blocks install scripts by default; the CLI's postinstall links the platform
# binary, so --allow-scripts permits that one package. "claude --version"
# proves the binary works. git clones, commits, and pushes the task branch.
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
USER 1000
ENTRYPOINT ["python", "-m", "joshua_developer_worker"]
