"""The Discogs client: the collection, one release, an image, search, and two writes.

Every request carries a descriptive ``User-Agent`` (Discogs refuses a request
without one) and waits on a throttle, because Discogs allows 60 requests each
minute. A 429 waits for ``Retry-After`` and tries again. A 5xx tries again
with backoff.

Two methods write: add_release and set_field. A write is never retried,
because a repeated add puts a second copy of the record in the collection.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from joshua_vinyl.log import get_logger
from joshua_vinyl.throttle import Throttle

logger = get_logger("vinyl.discogs")

BASE_URL = "https://api.discogs.com"
RATELIMIT_REMAINING = "X-Discogs-Ratelimit-Remaining"
# A collection path names the account, and an error message reaches the log
# and the status route, so the name is taken out of it.
USER_PATH = re.compile(r"/users/[^/]+")
RETRIES = 3


def safe_path(url: str) -> str:
    """A request path with the account name taken out."""
    return USER_PATH.sub("/users/…", url)


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
                raise DiscogsError(f"discogs {safe_path(url)} returned {response.status_code}")
            logger.debug({"message": "discogs request", "path": url, "remaining": remaining})
            return response
        raise DiscogsError(f"discogs {safe_path(url)} failed after {RETRIES} retries")

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

    def release(self, release_id: int, *, currency: str | None = None) -> dict[str, Any]:
        """The full release: tracklist, country, images, and the marketplace summary.

        ``currency`` is the ISO code the ``lowest_price`` is quoted in.
        """
        params = {"curr_abbr": currency} if currency else None
        return self._get(f"/releases/{release_id}", params).json()

    def search(self, **params: Any) -> list[dict[str, Any]]:
        """Search the database for releases. Every value is a Discogs search field."""
        query = {key: value for key, value in params.items() if value}
        query.update({"type": "release", "per_page": 25})
        return self._get("/database/search", query).json().get("results", [])

    def master_versions(self, master_id: int, *, per_page: int = 50) -> list[dict[str, Any]]:
        """Every pressing of one master release."""
        return (
            self._get(f"/masters/{master_id}/versions", {"per_page": per_page})
            .json()
            .get("versions", [])
        )

    def folders(self, username: str) -> list[dict[str, Any]]:
        """The collection folders. Folder 0 is ``All`` and cannot take an add."""
        return self._get(f"/users/{username}/collection/folders").json().get("folders", [])

    def fields(self, username: str) -> list[dict[str, Any]]:
        """The collection fields. The note is one of them, usually field 3."""
        return self._get(f"/users/{username}/collection/fields").json().get("fields", [])

    def _post(self, url: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """One write. A write is never retried, because a retry can add a second copy."""
        self._throttle.wait()
        response = self._client.post(url, json=body)
        if response.status_code >= 400:
            raise DiscogsError(
                f"discogs {url} returned {response.status_code}: {response.text[:200]}"
            )
        return response.json() if response.content else None

    def add_release(self, username: str, folder_id: int, release_id: int) -> dict[str, Any]:
        """Put one release in the collection. Answers with the new instance."""
        if folder_id < 1:
            raise DiscogsError("folder 0 is All and cannot take an add; use folder 1")
        result = self._post(
            f"/users/{username}/collection/folders/{folder_id}/releases/{release_id}"
        )
        return result or {}

    def instances(self, username: str, release_id: int) -> list[dict[str, Any]]:
        """Every copy of one release in the collection, with its folder and instance.

        A delete needs both ids, and the folder a copy sits in is not the
        folder an add put it in, so it is read here and never assumed.
        """
        data = self._get(f"/users/{username}/collection/releases/{release_id}").json()
        return data.get("releases", [])

    def remove_instance(
        self, username: str, folder_id: int, release_id: int, instance_id: int
    ) -> None:
        """Take one copy out of the collection. Not retried, like every write."""
        self._throttle.wait()
        url = (
            f"/users/{username}/collection/folders/{folder_id}"
            f"/releases/{release_id}/instances/{instance_id}"
        )
        response = self._client.delete(url)
        if response.status_code >= 400:
            raise DiscogsError(f"discogs {url} returned {response.status_code}")

    def set_field(
        self,
        username: str,
        folder_id: int,
        release_id: int,
        instance_id: int,
        field_id: int,
        value: str,
    ) -> None:
        """Write one collection field on one instance.

        The value goes in the request body. As a query parameter Discogs
        answers 422, and it does so after the record is already added.
        """
        self._post(
            f"/users/{username}/collection/folders/{folder_id}/releases/{release_id}"
            f"/instances/{instance_id}/fields/{field_id}",
            {"value": value},
        )

    def download(self, url: str) -> bytes:
        """The bytes of an image URL from a Discogs response."""
        return self._get(url).content
