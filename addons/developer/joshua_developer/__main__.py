from __future__ import annotations

import asyncio
import os

import uvicorn

from joshua_developer import server
from joshua_developer.config import GIT_PROXY_PORT, WORKER_API_PORT
from joshua_developer.log import configure_from_env

HOST = "0.0.0.0"
MCP_PORT = 8000


async def serve() -> None:
    """Serve MCP on 8000, the worker API on 8001, and the git host tunnel on 8002."""
    app = server.build_app()
    assert server.worker_app is not None and server.git_proxy is not None
    await server.git_proxy.start(HOST, GIT_PROXY_PORT)
    mcp_server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=MCP_PORT, log_config=None))
    worker_server = uvicorn.Server(
        uvicorn.Config(server.worker_app, host=HOST, port=WORKER_API_PORT, log_config=None)
    )
    try:
        # Each server stops on SIGTERM. The second one to start passes the
        # signal on to the first when it stops.
        await asyncio.gather(mcp_server.serve(), worker_server.serve())
    finally:
        await server.git_proxy.close()


def main() -> None:
    logger = configure_from_env("joshua-developer")
    logger.info(
        {
            "message": "build",
            "version": os.environ.get("ADDON_VERSION", "dev"),
            "revision": os.environ.get("ADDON_REVISION", ""),
        }
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
