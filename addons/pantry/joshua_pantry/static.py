"""Serve the built frontend at ``/``, with single-page-app fallback.

The Dockerfile's ``node:22-alpine`` build stage compiles ``frontend/``, and
the runtime stage copies its ``dist/`` output to ``PANTRY_STATIC_DIR``
(default ``/app/addons/pantry/static``). ``build_static_app`` returns
``None`` when that directory is missing -- a checkout with no frontend
build, or a unit test that only exercises the API and MCP routes -- so
``server.build_app`` can skip mounting it instead of failing outright.
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR_ENV = "PANTRY_STATIC_DIR"
DEFAULT_STATIC_DIR = "/app/addons/pantry/static"


def resolve_static_dir() -> Path:
    """The directory the built frontend lives in: ``PANTRY_STATIC_DIR``, or the default."""
    return Path(os.environ.get(STATIC_DIR_ENV, DEFAULT_STATIC_DIR))


class SPAStaticFiles(StaticFiles):
    """``StaticFiles`` in HTML mode, with every unmatched path falling back to ``index.html``.

    A single-page app draws every view from one HTML shell; a bare static
    mount 404s on a path the app itself would have handled (a reload on a
    deep link, for example). Falling back to ``index.html`` lets the app
    load and take over instead.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def build_static_app() -> SPAStaticFiles | None:
    """The static app to mount at ``/``, or ``None`` when the built frontend is missing."""
    static_dir = resolve_static_dir()
    if not static_dir.is_dir():
        return None
    return SPAStaticFiles(directory=str(static_dir), html=True)
