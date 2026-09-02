# syntax=docker/dockerfile:1

# Frontend build stage: compile the web UI. A separate stage so the runtime
# image never carries node or the frontend's own dependencies.
FROM node:22-alpine AS frontend-builder
WORKDIR /frontend
COPY addons/pantry/frontend/package.json addons/pantry/frontend/package-lock.json ./
RUN npm ci
COPY addons/pantry/frontend/ ./
RUN npm run build

# Build stage: resolve and install the member and its dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-pantry

# Runtime stage: copy the venv, the code this member needs, and the built UI.
FROM python:3.13-slim AS runtime
RUN useradd --uid 1000 --create-home app
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder --chown=1000:1000 /app/addons/pantry /app/addons/pantry
COPY --from=frontend-builder --chown=1000:1000 /frontend/dist /app/addons/pantry/static
# The SQLite file's home, for the DATABASE_URL-unset default. Owned by app so
# the addon can create pantry.db here with no volume mounted, and so the
# named volume in docker-compose.yml (mounted over this directory) inherits
# the same owner.
RUN mkdir /data && chown 1000:1000 /data
ENV PATH="/app/.venv/bin:$PATH" \
    PANTRY_DB=/data/pantry.db
USER 1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "joshua_pantry"]
