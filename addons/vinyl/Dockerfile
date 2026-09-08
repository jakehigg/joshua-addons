# syntax=docker/dockerfile:1

# Build stage: resolve and install the member and its dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-vinyl

# Runtime stage: copy the venv and only the code this member needs. The
# browse interface is plain static files inside the package, so there is no
# frontend build stage.
FROM python:3.13-slim AS runtime
RUN useradd --uid 1000 --create-home app
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder --chown=1000:1000 /app/addons/vinyl /app/addons/vinyl
# The data directory: the SQLite file, the cached art, and the bundle. Owned
# by app so the addon can write here with no volume mounted, and so the named
# volume in docker-compose.yml (mounted over this directory) inherits the
# same owner.
RUN mkdir /data && chown 1000:1000 /data
ENV PATH="/app/.venv/bin:$PATH" \
    VINYL_DATA_DIR=/data
USER 1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "joshua_vinyl"]
