"""The wiki on the joshua-ai data volume: path rules, reads, and writes.

There are three sources, all inside ``<data>/wiki``:

- ``wiki``: the pages. This is every page outside ``journal/`` and ``knowledge/``.
- ``journal``: Joshua's journal, ``journal/YYYY/MM/DD/*.md``.
- ``knowledge``: the knowledge folder, ``knowledge/``. It can be missing.

A path is always relative to the wiki. A path that goes out of the wiki
(``..``, an absolute path, a symlink out) or into a dot directory (``.git``,
``.trash``) is refused before any file operation. An error message names the
path the caller sent, never an absolute path on the volume.

A write commits with ``joshua_shared.wikigit`` when the wiki is a git
repository. A commit failure is a WARNING from ``wikigit`` and never fails the
write.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from joshua_shared import layout, wikigit

from joshua_mcp.log import get_logger

logger = get_logger("joshua_mcp.wiki")

SOURCES = ("wiki", "journal", "knowledge")
JOURNAL = "journal"
KNOWLEDGE = "knowledge"
# The folders that write_page refuses. The journal has its own tool, the
# profiles and the documentation are Joshua's, and attachments hold binary
# files that the wiki repository ignores.
PROTECTED = ("journal", "people", "joshua-docs", "attachments")
# The folders that no tool lists or searches: binary files only.
SKIPPED = ("attachments",)
PROVENANCE = "joshua-mcp"
MAX_BYTES = 256 * 1024
MAX_DAYS = 31
COMMIT_PREFIX = "joshua-mcp: "
_MAX_MESSAGE = 200


class WikiError(Exception):
    """A request breaks a rule. The message is safe to return to the caller."""


@dataclass(frozen=True)
class Page:
    path: str
    frontmatter: dict[str, Any]
    body: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "frontmatter": self.frontmatter, "body": self.body}


# --- paths ---------------------------------------------------------------


def resolve(wiki: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``wiki``, or raise ``WikiError``."""
    raw = str(rel).strip().replace("\\", "/")
    if not raw:
        raise WikiError("path is empty")
    if raw.startswith("/"):
        raise WikiError(f"path must be relative to the wiki: {rel}")
    parts = PurePosixPath(raw).parts
    if ".." in parts:
        raise WikiError(f"path is outside the wiki: {rel}")
    if any(part.startswith(".") for part in parts):
        raise WikiError(f"path is in a hidden folder: {rel}")
    base = wiki.resolve()
    candidate = (wiki / raw).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise WikiError(f"path is outside the wiki: {rel}") from exc
    return candidate


def relative(wiki: Path, path: Path) -> str:
    return path.relative_to(wiki.resolve()).as_posix()


def inside(wiki: Path, path: Path) -> bool:
    """True when ``path``, with every symlink followed, is inside the wiki."""
    try:
        path.resolve().relative_to(wiki.resolve())
    except ValueError:
        return False
    return True


def source_of(rel: str) -> str:
    """The source a wiki-relative path belongs to."""
    top = PurePosixPath(rel).parts[0] if rel else ""
    if top == JOURNAL:
        return JOURNAL
    if top == KNOWLEDGE:
        return KNOWLEDGE
    return "wiki"


def check_source(source: str | None) -> str | None:
    if source is None or source == "":
        return None
    if source not in SOURCES:
        raise WikiError(f"source must be one of: {', '.join(SOURCES)}")
    return source


def iter_pages(wiki: Path, source: str | None = None):
    """Yield ``(rel, path)`` for every visible ``.md`` file, in path order."""
    base = wiki.resolve()
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        here = Path(dirpath)
        rel_dir = here.relative_to(base)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".") and not (rel_dir == Path(".") and d in SKIPPED)
        )
        for name in sorted(filenames):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            rel = (rel_dir / name).as_posix()
            if not inside(wiki, here / name):
                continue
            if source is None or source_of(rel) == source:
                yield rel, here / name


def knowledge_root(wiki: Path) -> Path:
    return wiki / KNOWLEDGE


def require_knowledge(wiki: Path) -> None:
    if not knowledge_root(wiki).is_dir():
        raise WikiError("no knowledge folder")


# --- frontmatter ---------------------------------------------------------


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a leading ``---`` YAML block from ``text``.

    A file that opens with ``---`` and has no closing fence is a horizontal
    rule, not frontmatter. A block that is not a YAML mapping raises
    ``WikiError``.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                data = yaml.safe_load("".join(lines[1:index]))
            except yaml.YAMLError as exc:
                raise WikiError("invalid YAML frontmatter") from exc
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise WikiError("frontmatter must be a YAML mapping")
            return data, "".join(lines[index + 1 :]).lstrip("\n")
    return {}, text


def join_frontmatter(frontmatter: dict[str, Any], body: str) -> str:
    block = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{block}\n---\n\n{body}"


def split_frontmatter_loose(text: str) -> tuple[dict[str, Any], str]:
    """``split_frontmatter`` for a read: a broken block stays in the body."""
    try:
        return split_frontmatter(text)
    except WikiError:
        return {}, text


# --- reads ---------------------------------------------------------------


def read_page(wiki: Path, rel: str) -> Page:
    path = resolve(wiki, rel)
    if path.suffix != ".md":
        raise WikiError(f"not a page (a page ends in .md): {rel}")
    if not path.is_file():
        raise WikiError(f"no such page: {rel}")
    text = _read_text(path, rel)
    frontmatter, body = split_frontmatter_loose(text)
    return Page(path=relative(wiki, path), frontmatter=frontmatter, body=body)


def list_folder(wiki: Path, rel: str | None = None, source: str | None = None) -> dict[str, Any]:
    """List one folder: its subfolders and its files, as the viewer shows them."""
    source = check_source(source)
    if rel:
        folder = resolve(wiki, rel)
    elif source in (JOURNAL, KNOWLEDGE):
        folder = wiki.resolve() / source
        if source == KNOWLEDGE and not folder.is_dir():
            raise WikiError("no knowledge folder")
    else:
        folder = wiki.resolve()
    if not folder.is_dir():
        raise WikiError(f"no such folder: {rel or source or '.'}")

    at_root = folder == wiki.resolve()
    entries = []
    for item in sorted(folder.iterdir(), key=lambda p: p.name):
        if item.name.startswith("."):
            continue
        if at_root and item.name in SKIPPED:
            continue
        if at_root and source == "wiki" and item.name in (JOURNAL, KNOWLEDGE):
            continue
        if not inside(wiki, item):
            continue
        rel_item = relative(wiki, item.resolve())
        if item.is_dir():
            entries.append({"name": item.name, "path": rel_item, "type": "folder"})
        elif item.is_file():
            kind = "page" if item.suffix == ".md" else "file"
            entries.append(
                {"name": item.name, "path": rel_item, "type": kind, "bytes": item.stat().st_size}
            )
    folder_rel = "" if at_root else relative(wiki, folder)
    return {"path": folder_rel, "entries": entries}


def read_journal(
    wiki: Path,
    end: date,
    days: int = 1,
    people: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The day pages and the entries from ``end - days + 1`` to ``end``, oldest first.

    ``people`` keeps only the entries that name one of them. A day page is
    always included.
    """
    if days < 1 or days > MAX_DAYS:
        raise WikiError(f"days must be from 1 to {MAX_DAYS}")
    wanted = set(people or [])
    items: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        day = end - timedelta(days=offset)
        folder = layout.journal_day_dir(day, root=wiki.parent)
        if not folder.is_dir():
            continue
        day_page = f"{day.isoformat()}.md"
        files = sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix == ".md"),
            key=lambda p: (p.name != day_page, p.name),
        )
        for path in files:
            if not inside(wiki, path):
                continue
            kind = "day" if path.name == day_page else "entry"
            text = _read_text(path, path.name)
            frontmatter, body = split_frontmatter_loose(text)
            if kind == "entry" and wanted:
                named = frontmatter.get("people") or []
                if not isinstance(named, list) or not wanted.intersection(map(str, named)):
                    continue
            items.append(
                {
                    "path": relative(wiki, path.resolve()),
                    "date": day.isoformat(),
                    "kind": kind,
                    "frontmatter": frontmatter,
                    "body": body,
                }
            )
    return items


def _read_text(path: Path, rel: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise WikiError(f"not a UTF-8 text file: {rel}") from exc


# --- writes --------------------------------------------------------------


def check_page_path(wiki: Path, rel: str) -> Path:
    """Resolve a path that ``write_page`` can write, or raise ``WikiError``."""
    path = resolve(wiki, rel)
    rel_path = relative(wiki, path)
    top = PurePosixPath(rel_path).parts[0]
    if top in PROTECTED:
        if top == JOURNAL:
            raise WikiError(f"{top}/ is written with write_journal_entry, not write_page")
        raise WikiError(f"{top}/ is protected: the joshua-mcp addon does not write there")
    if path.suffix != ".md":
        raise WikiError(f"a page ends in .md: {rel}")
    if path.is_dir():
        raise WikiError(f"a folder has this name: {rel}")
    return path


def write_page(wiki: Path, rel: str, markdown: str, author: str, message: str | None) -> dict:
    """Create or replace one page, stamped with the addon's provenance."""
    path = check_page_path(wiki, rel)
    frontmatter, body = split_frontmatter(markdown)
    frontmatter["source"] = PROVENANCE
    frontmatter["author"] = author
    data = join_frontmatter(frontmatter, body).encode()
    _check_size(data)

    rel_path = relative(wiki, path)
    overwritten = path.exists()
    _atomic_write(path, data)
    committed = _commit(wiki, path, message or f"write_page {rel_path}")
    logger.info({"message": "page written", "path": rel_path, "author": author, "bytes": len(data)})
    return {
        "path": rel_path,
        "bytes": len(data),
        "overwritten": overwritten,
        "committed": committed,
    }


def write_journal_entry(
    wiki: Path,
    slug: str,
    markdown: str,
    author: str,
    day: date,
    people: list[str] | None = None,
) -> dict:
    """Write one entry at ``journal/YYYY/MM/DD/<slug>.md``, never the day page."""
    if people is None:
        people = []
    if not isinstance(people, list) or not all(isinstance(p, str) for p in people):
        raise WikiError("people must be a list of person ids")
    try:
        path = layout.journal_entry_path(day, slug, root=wiki.parent)
    except ValueError as exc:
        raise WikiError(str(exc)) from exc
    # The journal folder can be a symlink; the entry must still land in the wiki.
    if not inside(wiki, path.parent):
        raise WikiError("the journal folder is outside the wiki")

    _, body = split_frontmatter(markdown)
    frontmatter = {
        "date": day.isoformat(),
        "people": people,
        "source": PROVENANCE,
        "author": author,
    }
    data = join_frontmatter(frontmatter, body).encode()
    _check_size(data)

    overwritten = path.exists()
    _atomic_write(path, data)
    rel_path = relative(wiki, path.resolve())
    committed = _commit(wiki, path, f"write_journal_entry {rel_path}")
    logger.info(
        {"message": "journal entry written", "path": rel_path, "author": author, "bytes": len(data)}
    )
    return {
        "path": rel_path,
        "bytes": len(data),
        "overwritten": overwritten,
        "committed": committed,
    }


def commit_message(message: str) -> str:
    """The commit subject: the addon prefix, and one line of at most 200 characters."""
    line = " ".join(str(message).split())[:_MAX_MESSAGE]
    return f"{COMMIT_PREFIX}{line}"


def _check_size(data: bytes) -> None:
    if len(data) > MAX_BYTES:
        raise WikiError(f"the page is larger than {MAX_BYTES // 1024} KB")


def _commit(wiki: Path, path: Path, message: str) -> bool:
    if not (wiki / ".git").exists():
        return False
    return wikigit.commit(wiki, [path], commit_message(message))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".joshua-mcp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
