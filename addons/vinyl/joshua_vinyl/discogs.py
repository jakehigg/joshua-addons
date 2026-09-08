"""A read-only Discogs client: the collection, one release, and an image.

Every request carries a descriptive ``User-Agent`` (Discogs refuses a request
without one) and waits on a throttle, because Discogs allows 60 requests each
minute. A 429 waits for ``Retry-After`` and tries again. A 5xx tries again
with backoff. Nothing here writes to Discogs.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from joshua_vinyl.log import get_logger
from joshua_vinyl.throttle import Throttle

logger = get_logger("vinyl.discogs")

BASE_URL = "https://api.discogs.com"
RATELIMIT_REMAINING = "X-Discogs-Ratelimit-Remaining"
RETRIES = 3


class DiscogsError(RuntimeError):
    """A Discogs request failed after every retry."""


class DiscogsClient:
    """The subset of the Discogs API the sync reads.

    ``transport`` lets a test supply ``httpx.MockTransport``. ``sleep`` is the
    backoff sleep, also injectable.
    """

    def __init__(
        self,
        token: str,
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
            headers={"User-Agent": user_agent, "Authorization": f"Discogs token={token}"},
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
        )
        self._throttle = throttle or Throttle(1.05)
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        for attempt in range(RETRIES + 1):
            self._throttle.wait()
            response = self._client.get(url, params=params)
            remaining = response.headers.get(RATELIMIT_REMAINING)
            if response.status_code == 429:
                delay = float(response.headers.get("Retry-After") or 60)
                logger.warning({"message": "discogs rate limit", "retry_after": delay})
                self._sleep(delay)
                continue
            if response.status_code >= 500 and attempt < RETRIES:
                self._sleep(2.0 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise DiscogsError(f"discogs {url} returned {response.status_code}")
            logger.debug({"message": "discogs request", "path": url, "remaining": remaining})
            return response
        raise DiscogsError(f"discogs {url} failed after {RETRIES} retries")

    def identity(self) -> dict[str, Any]:
        """The account the token belongs to. Read this to learn the username."""
        return self._get("/oauth/identity").json()

    def collection(self, username: str, *, per_page: int = 100) -> Iterator[dict[str, Any]]:
        """Every item in the ``All`` folder (folder 0), page by page."""
        page = 1
        while True:
            data = self._get(
                f"/users/{username}/collection/folders/0/releases",
                {"per_page": per_page, "page": page, "sort": "added", "sort_order": "desc"},
            ).json()
            yield from data.get("releases", [])
            pages = int(data.get("pagination", {}).get("pages", 1))
            if page >= pages:
                return
            page += 1

    def release(self, release_id: int) -> dict[str, Any]:
        """The full release: tracklist, country, images, and the marketplace summary."""
        return self._get(f"/releases/{release_id}").json()

    def download(self, url: str) -> bytes:
        """The bytes of an image URL from a Discogs response."""
        return self._get(url).content
