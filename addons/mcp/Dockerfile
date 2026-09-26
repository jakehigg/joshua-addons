# syntax=docker/dockerfile:1

# Build stage: resolve and install the member and its dependencies.
# joshua-shared comes from the joshua-ai repository by git tag, so uv needs git.
FROM python:3.13-slim AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-mcp

# Runtime stage: copy the venv and only the code this member needs. git stays
# in the image, because every write commits to the wiki repository.
FROM python:3.13-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 1000 --create-home app
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder --chown=1000:1000 /app/addons/mcp /app/addons/mcp
# The addon writes the joshua-ai data volume, mounted at /data. It runs as
# uid 1000, the same user as joshua-ai core and gateway, so the files it
# writes have the same owner as the files Joshua writes.
ENV PATH="/app/.venv/bin:$PATH" \
    MCP_DATA_DIR=/data
USER 1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "joshua_mcp"]
