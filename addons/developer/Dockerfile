# syntax=docker/dockerfile:1

# Build stage: resolve and install the member and its dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-developer

# Runtime stage: copy the venv and only the code this member needs.
FROM python:3.13-slim AS runtime
RUN useradd --uid 1000 --create-home app
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder --chown=1000:1000 /app/addons/developer /app/addons/developer
# The home of developer.db. Owned by app, so the named volume in
# docker-compose.yml, mounted over this directory, gets the same owner.
RUN mkdir /data && chown 1000:1000 /data
ENV PATH="/app/.venv/bin:$PATH" \
    DEVELOPER_DATA_DIR=/data
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
# 8000 MCP, 8001 the worker API, 8002 the git host tunnel.
EXPOSE 8000 8001 8002
ENTRYPOINT ["python", "-m", "joshua_developer"]
