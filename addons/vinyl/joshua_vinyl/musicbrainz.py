"""Resolve an artist name to its MusicBrainz ``sort-name`` and ``type``.

The sort-name encodes the house filing rules: ``Dylan, Bob`` and ``Beatles,
The``. MusicBrainz allows about one request each second and wants a
descriptive ``User-Agent``. It answers 503 when a client goes faster, so a
503 waits and tries again.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel

from joshua_vinyl.log import get_logger
from joshua_vinyl.throttle import Throttle

logger = get_logger("vinyl.musicbrainz")

BASE_URL = "https://musicbrainz.org/ws/2"
RETRIES = 3
MIN_SCORE = 90


class ArtistSort(BaseModel):
    """One resolved artist."""

    name: str
    sort_name: str
    type: str | None
    mbid: str
    score: int


def normalize_name(name: str) -> str:
    """A loose form for comparison: no accents or case, no leading ``The``, ``&`` as ``and``."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^the ", "", text)


def strip_discogs_suffix(name: str) -> str:
    """Remove the ``(2)`` suffix Discogs adds to a name that two artists share."""
    return re.sub(r"\s*\(\d+\)$", "", name.strip()).strip()


def _escape(text: str) -> str:
    """Make a name safe inside a Lucene phrase.

    A name with a double quote in it (`"Weird Al" Yankovic`) closes the
    phrase early and the whole query is malformed, so the artist never
    resolves and costs a request every night.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def best_match(name: str, artists: list[dict[str, Any]]) -> ArtistSort | None:
    """The first result that scores at least ``MIN_SCORE`` and has the same name."""
    wanted = normalize_name(name)
    for artist in artists:
        score = int(artist.get("score", 0))
        if score < MIN_SCORE:
            continue
        candidates = [artist.get("name", "")] + [
            alias.get("name", "") for alias in artist.get("aliases", [])
        ]
        if any(normalize_name(candidate) == wanted for candidate in candidates):
            return ArtistSort(
                name=artist["name"],
                sort_name=artist["sort-name"],
                type=artist.get("type"),
                mbid=artist["id"],
                score=score,
            )
    return None


class MusicBrainzClient:
    """Artist search only. No key is needed."""

    def __init__(
        self,
        user_agent: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        throttle: Throttle | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )
        self._throttle = throttle or Throttle(1.1)
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def search_artist(self, name: str) -> ArtistSort | None:
        """The best MusicBrainz match for ``name``, or ``None`` when there is no safe match."""
        query = strip_discogs_suffix(name)
        params = {"query": f'artist:"{_escape(query)}"', "fmt": "json", "limit": 5}
        for attempt in range(RETRIES + 1):
            self._throttle.wait()
            try:
                response = self._client.get("/artist/", params=params)
            except httpx.HTTPError as error:
                # A name this service cannot answer for must not stop a sync
                # that Discogs answered. The artist keeps its Discogs name for
                # this run and is asked for again tomorrow.
                logger.warning({"message": "musicbrainz unreachable", "error": str(error)})
                return None
            if response.status_code == 503 and attempt < RETRIES:
                delay = 3.0 * (attempt + 1)
                logger.warning({"message": "musicbrainz busy", "retry_after": delay})
                self._sleep(delay)
                continue
            if response.status_code != 200:
                logger.warning(
                    {"message": "musicbrainz lookup failed", "status": response.status_code}
                )
                return None
            return best_match(query, response.json().get("artists", []))
        return None
