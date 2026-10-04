"""The git host adapters: one interface, three kinds.

A platform entry in ``developer.yaml`` names a host, its ``kind``, and the
environment variable that holds its token. ``for_task`` finds the entry for
the host of a repository and builds the adapter:

- ``github``: the GitHub REST API. ``https://api.github.com`` for
  ``github.com``, and ``https://<host>/api/v3`` for GitHub Enterprise.
- ``gitlab``: the GitLab REST API v4 at ``https://<host>/api/v4``.
- ``git``: no API. A task ends at the pushed branch.

A reply that is not 2xx raises ``PlatformError``. Its message never holds the
token or a URL with a credential.
"""

from __future__ import annotations

import os
import re
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from joshua_developer.config import DeveloperConfig, Settings
from joshua_developer.repos import repo_host

REQUEST_TIMEOUT_S = 30.0
MAX_ERROR_MESSAGE = 300
USER_AGENT = "joshua-developer"

CREDENTIAL_USERNAMES = {"github": "x-access-token", "gitlab": "oauth2", "git": "git"}
# The GitHub compare: the files of one page, the most files one reply can
# list, and the most pages the scan reads.
GITHUB_PAGE_SIZE = 100
GITHUB_COMPARE_FILE_CAP = 300
GITHUB_MAX_PAGES = 30

_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")


# --- errors ----------------------------------------------------------------


class PlatformConfigError(ValueError):
    """The repository has no usable platform entry. The message names no token."""

    reason = "platform_error"


class RepoNotConfigured(PlatformConfigError):
    reason = "repo_not_configured"


class TokenMissing(PlatformConfigError):
    reason = "token_missing"


class PlatformUnsupported(RuntimeError):
    """The kind of the host has no API for this call."""


class PlatformError(RuntimeError):
    """The git host refused a call, or did not answer (``status`` 0)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove each secret and each URL credential from ``text``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return _URL_USERINFO_RE.sub(r"\1[redacted]@", text)


# --- data ------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequest:
    """A pull request, or a GitLab merge request. ``number`` is the GitLab ``iid``."""

    number: int
    url: str
    html_url: str
    source_branch: str | None = None
    base_branch: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class DiffFile:
    path: str
    patch: str


@dataclass(frozen=True)
class Diff:
    """The text the scan reads.

    ``unscanned`` names the files the host sent no patch text for: a binary
    file, or a file too large for the API. ``truncated`` is True when the
    host did not list every file of the diff. ``unavailable`` is True when
    the host has no API for a diff.
    """

    files: list[DiffFile] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    unscanned: list[str] = field(default_factory=list)
    truncated: bool = False
    unavailable: bool = False


def count_lines(patch: str) -> tuple[int, int]:
    """The added and the removed lines of a unified diff."""
    added = removed = 0
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


# --- the interface ---------------------------------------------------------


class Platform(Protocol):
    kind: str

    async def open_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...

    async def find_pr(self, repo: str, head: str) -> PullRequest | None: ...

    async def pr(self, repo: str, number: int) -> PullRequest: ...

    async def compare(self, repo: str, base: str, head: str) -> Diff: ...

    async def delete_branch(self, repo: str, branch: str) -> None: ...

    def credential_username(self) -> str: ...

    async def aclose(self) -> None: ...


def split_repo(repo: str) -> tuple[str, str]:
    """``host/owner/name`` to ``(host, owner/name)``. The path can have subgroups."""
    host, _, path = repo.partition("/")
    return host, path


class _HttpPlatform:
    """The shared HTTP part of the adapters with an API."""

    kind = ""

    def __init__(
        self,
        host: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        verify: str | bool = True,
    ) -> None:
        self.host = host
        self._token = token
        self._owns_client = client is None
        self._client = client or make_client(verify)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(host={self.host!r})"

    @property
    def base_url(self) -> str:  # pragma: no cover - each kind sets it
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:  # pragma: no cover - each kind sets it
        raise NotImplementedError

    def credential_username(self) -> str:
        return CREDENTIAL_USERNAMES[self.kind]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
    ) -> Any:
        """Send one call. Returns the parsed JSON body, or None for an empty body."""
        try:
            response = await self._client.request(
                method,
                self.base_url + path,
                params=params,
                json=json,
                headers={**self._headers(), "User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise PlatformError(
                0, f"the git host {self.host} did not answer ({type(exc).__name__})"
            ) from None
        if not 200 <= response.status_code < 300:
            raise PlatformError(response.status_code, self._error_message(response))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise PlatformError(
                response.status_code, f"the git host {self.host} sent a reply that is not JSON"
            ) from None

    def _error_message(self, response: httpx.Response) -> str:
        text = ""
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ("message", "error_description", "error"):
                value = data.get(key)
                if value:
                    text = value if isinstance(value, str) else str(value)
                    break
        text = text or response.reason_phrase or "no message"
        text = scrub(" ".join(text.split()), (self._token,))
        return text[:MAX_ERROR_MESSAGE]


# --- github ----------------------------------------------------------------


class GitHubPlatform(_HttpPlatform):
    kind = "github"

    @property
    def base_url(self) -> str:
        if self.host == "github.com":
            return "https://api.github.com"
        return f"https://{self.host}/api/v3"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _repo_path(repo: str) -> str:
        _, path = split_repo(repo)
        return f"/repos/{quote(path, safe='/')}"

    @staticmethod
    def _parse(data: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=int(data["number"]),
            url=str(data.get("url") or ""),
            html_url=str(data.get("html_url") or ""),
            source_branch=(data.get("head") or {}).get("ref"),
            base_branch=(data.get("base") or {}).get("ref"),
            state=data.get("state"),
        )

    async def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> PullRequest:
        data = await self._request(
            "POST",
            f"{self._repo_path(repo)}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        return self._parse(data)

    async def find_pr(self, repo: str, head: str) -> PullRequest | None:
        _, path = split_repo(repo)
        owner = path.split("/", 1)[0]
        data = await self._request(
            "GET",
            f"{self._repo_path(repo)}/pulls",
            params={"head": f"{owner}:{head}", "state": "open"},
        )
        return self._parse(data[0]) if data else None

    async def pr(self, repo: str, number: int) -> PullRequest:
        return self._parse(await self._request("GET", f"{self._repo_path(repo)}/pulls/{number}"))

    async def compare(self, repo: str, base: str, head: str) -> Diff:
        """The diff from ``base`` to ``head``, page by page.

        The pages continue while a page has ``GITHUB_PAGE_SIZE`` files or
        more. One reply lists at most ``GITHUB_COMPARE_FILE_CAP`` files. When
        a page reaches that cap and the next page adds no file, or the pages
        stop at ``GITHUB_MAX_PAGES``, some files can be missing, so the diff
        is ``truncated`` and the scan is ``partial``.
        """
        spec = f"{quote(base, safe='/')}...{quote(head, safe='/')}"
        files: list[DiffFile] = []
        unscanned: list[str] = []
        seen: set[str] = set()
        additions = deletions = 0
        truncated = False
        for page in range(1, GITHUB_MAX_PAGES + 1):
            data = await self._request(
                "GET",
                f"{self._repo_path(repo)}/compare/{spec}",
                params={"per_page": str(GITHUB_PAGE_SIZE), "page": str(page)},
            )
            items = (data or {}).get("files") or []
            new = 0
            for item in items:
                path = str(item.get("filename") or "")
                if path in seen:
                    continue
                seen.add(path)
                new += 1
                additions += int(item.get("additions") or 0)
                deletions += int(item.get("deletions") or 0)
                patch = item.get("patch")
                if patch:
                    files.append(DiffFile(path=path, patch=patch))
                elif item.get("status") != "removed" and int(item.get("additions") or 0) > 0:
                    unscanned.append(path)
            if page > 1 and new == 0 and len(seen) >= GITHUB_COMPARE_FILE_CAP:
                # The host sent the same capped list again: it cannot page the files.
                truncated = True
                break
            if len(items) < GITHUB_PAGE_SIZE:
                break
        else:
            truncated = True
        return Diff(
            files=files,
            additions=additions,
            deletions=deletions,
            unscanned=unscanned,
            truncated=truncated,
        )

    async def delete_branch(self, repo: str, branch: str) -> None:
        await self._request(
            "DELETE", f"{self._repo_path(repo)}/git/refs/heads/{quote(branch, safe='/')}"
        )


# --- gitlab ----------------------------------------------------------------


class GitLabPlatform(_HttpPlatform):
    kind = "gitlab"

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/api/v4"

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token, "Accept": "application/json"}

    @staticmethod
    def project_id(repo: str) -> str:
        """The URL-encoded project path: everything after the host."""
        _, path = split_repo(repo)
        return quote(path, safe="")

    def _project(self, repo: str) -> str:
        return f"/projects/{self.project_id(repo)}"

    @staticmethod
    def _parse(data: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=int(data["iid"]),
            url=str(data.get("web_url") or ""),
            html_url=str(data.get("web_url") or ""),
            source_branch=data.get("source_branch"),
            base_branch=data.get("target_branch"),
            state=data.get("state"),
        )

    async def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> PullRequest:
        data = await self._request(
            "POST",
            f"{self._project(repo)}/merge_requests",
            json={
                "source_branch": head,
                "target_branch": base,
                "title": title,
                "description": body,
            },
        )
        return self._parse(data)

    async def find_pr(self, repo: str, head: str) -> PullRequest | None:
        """The open merge request from ``head`` of this project into this project.

        A merge request from a fork with a branch of the same name has
        another ``source_project_id``, and is not taken.
        """
        data = await self._request(
            "GET",
            f"{self._project(repo)}/merge_requests",
            params={"source_branch": head, "state": "opened"},
        )
        for item in data or []:
            target = item.get("target_project_id", item.get("project_id"))
            source = item.get("source_project_id")
            if source is not None and source == target:
                return self._parse(item)
        return None

    async def pr(self, repo: str, number: int) -> PullRequest:
        return self._parse(
            await self._request("GET", f"{self._project(repo)}/merge_requests/{number}")
        )

    async def compare(self, repo: str, base: str, head: str) -> Diff:
        data = await self._request(
            "GET",
            f"{self._project(repo)}/repository/compare",
            params={"from": base, "to": head},
        )
        files: list[DiffFile] = []
        unscanned: list[str] = []
        additions = deletions = 0
        for item in (data or {}).get("diffs") or []:
            path = str(item.get("new_path") or item.get("old_path") or "")
            patch = item.get("diff") or ""
            if patch:
                files.append(DiffFile(path=path, patch=patch))
                added, removed = count_lines(patch)
                additions += added
                deletions += removed
            elif not item.get("deleted_file"):
                unscanned.append(path)
        return Diff(files=files, additions=additions, deletions=deletions, unscanned=unscanned)

    async def delete_branch(self, repo: str, branch: str) -> None:
        await self._request(
            "DELETE", f"{self._project(repo)}/repository/branches/{quote(branch, safe='')}"
        )


# --- git -------------------------------------------------------------------


class GitPlatform:
    """A plain git host. It has no API, so a task ends at the pushed branch."""

    kind = "git"

    def __init__(self, host: str) -> None:
        self.host = host

    def _refuse(self, what: str) -> PlatformUnsupported:
        return PlatformUnsupported(
            f"the git host {self.host} has no API for {what}; the task ends at the pushed branch"
        )

    async def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> PullRequest:
        raise self._refuse("pull requests")

    async def find_pr(self, repo: str, head: str) -> PullRequest | None:
        raise self._refuse("pull requests")

    async def pr(self, repo: str, number: int) -> PullRequest:
        raise self._refuse("pull requests")

    async def compare(self, repo: str, base: str, head: str) -> Diff:
        return Diff(unavailable=True)

    async def delete_branch(self, repo: str, branch: str) -> None:
        raise self._refuse("branch removal")

    def credential_username(self) -> str:
        return CREDENTIAL_USERNAMES["git"]

    async def aclose(self) -> None:
        return None


# --- resolution ------------------------------------------------------------


def make_client(verify: str | bool = True) -> httpx.AsyncClient:
    """The HTTP client of an adapter. ``verify`` is True or the path of a CA bundle."""
    context: ssl.SSLContext | bool = (
        ssl.create_default_context(cafile=verify) if isinstance(verify, str) else verify
    )
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, verify=context)


@dataclass(frozen=True)
class Resolved:
    """The platform entry of a task's host, and its token."""

    host: str
    kind: str
    token_env: str
    token: str = field(repr=False)

    @property
    def credential_username(self) -> str:
        return CREDENTIAL_USERNAMES[self.kind]


def resolve(
    cfg: DeveloperConfig, person: str, repo: str, env: Mapping[str, str] | None = None
) -> Resolved:
    """Find the platform entry and the token for ``repo``.

    The person's entry for the host comes first, then the instance entry.
    Raises RepoNotConfigured when neither exists, and TokenMissing when the
    variable is empty or not set.
    """
    env = os.environ if env is None else env
    host = repo_host(repo)
    person_entry = cfg.people.get(person)
    own = person_entry.platforms.get(host) if person_entry else None
    instance = cfg.platforms.get(host)
    if own is None and instance is None:
        raise RepoNotConfigured(
            f"no platform is configured for the host {host}; add it to platforms in developer.yaml"
        )
    kind = (own.kind if own else None) or (instance.kind if instance else None)
    token_env = own.token_env if own else instance.token_env  # type: ignore[union-attr]
    assert kind is not None
    token = (env.get(token_env) or "").strip()
    if not token:
        raise TokenMissing(f"the variable {token_env} that token_env names for {host} is not set")
    return Resolved(host=host, kind=kind, token_env=token_env, token=token)


def build(
    resolved: Resolved,
    client: httpx.AsyncClient | None = None,
    verify: str | bool = True,
) -> Platform:
    """The adapter for a resolved entry."""
    if resolved.kind == "github":
        return GitHubPlatform(resolved.host, resolved.token, client=client, verify=verify)
    if resolved.kind == "gitlab":
        return GitLabPlatform(resolved.host, resolved.token, client=client, verify=verify)
    return GitPlatform(resolved.host)


def for_task(
    settings: Settings,
    cfg: DeveloperConfig,
    person: str,
    repo: str,
    *,
    client: httpx.AsyncClient | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Platform, str]:
    """The adapter and the token for a task on ``repo``.

    ``GIT_CA_BUNDLE`` (``settings.git_ca_bundle``) is the CA bundle the client
    trusts. Close the adapter with ``aclose`` after use.
    """
    resolved = resolve(cfg, person, repo, env)
    verify: str | bool = settings.git_ca_bundle or True
    return build(resolved, client=client, verify=verify), resolved.token
