from __future__ import annotations

import os

import uvicorn

from joshua_pantry.log import configure_from_env
from joshua_pantry.server import build_app


def main() -> None:
    logger = configure_from_env("joshua-pantry")
    logger.info(
        {
            "message": "build",
            "version": os.environ.get("ADDON_VERSION", "dev"),
            "revision": os.environ.get("ADDON_REVISION", ""),
        }
    )
    uvicorn.run(build_app(), host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
