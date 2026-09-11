"""The two commands: serve (the default) and ``sync``.

``python -m joshua_vinyl`` serves the interface and MCP on port 8000.
``python -m joshua_vinyl sync`` runs one sync against Discogs and exits, 0 on
success. The scheduled nightly sync calls the same function.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from joshua_vinyl import db
from joshua_vinyl.config import Settings, load_shelf_config, settings_from_env
from joshua_vinyl.discogs import DiscogsClient
from joshua_vinyl.log import configure_from_env, get_logger
from joshua_vinyl.musicbrainz import MusicBrainzClient
from joshua_vinyl.sync import SyncResult, run_sync

logger = get_logger("vinyl.cli")

USAGE = "usage: python -m joshua_vinyl [sync]"


def sync_once(settings: Settings) -> SyncResult:
    """One sync with real clients. Raises when ``DISCOGS_TOKEN`` is not set."""
    if not settings.discogs_token:
        raise RuntimeError("DISCOGS_TOKEN is not set. The sync needs a Discogs personal token.")
    config = load_shelf_config(settings.config_path)
    discogs = DiscogsClient(settings.discogs_token, settings.user_agent)
    musicbrainz = MusicBrainzClient(settings.user_agent)
    conn = db.connect(settings.db_path)
    try:
        username = settings.discogs_username or discogs.identity()["username"]
        return run_sync(
            conn,
            discogs=discogs,
            musicbrainz=musicbrainz,
            username=username,
            config=config,
            art_dir=settings.art_dir,
            bundle_dir=settings.bundle_dir,
            currency=settings.currency,
        )
    finally:
        conn.close()
        discogs.close()
        musicbrainz.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command in ``argv``. Return the exit code."""
    configure_from_env("joshua-vinyl")
    # httpx logs every request URL at INFO, and the collection URL names the
    # account. The addon's own loggers say what happened without it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["sync"]:
        try:
            result = sync_once(settings_from_env())
        except Exception as exc:
            logger.error({"message": "sync failed", "error": str(exc)})
            return 1
        print(f"synced {result.count} records", file=sys.stderr)
        return 0
    if args:
        print(USAGE, file=sys.stderr)
        return 2
    import uvicorn

    from joshua_vinyl.server import build_app

    uvicorn.run(build_app(), host="0.0.0.0", port=8000, log_config=None)
    return 0
