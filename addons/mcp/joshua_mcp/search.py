"""Lexical search over the wiki: BM25 on words, in memory.

The index holds one entry for each page. Each search refreshes it first: a
page with a new size or modification time is read again, and a page that is
gone is dropped. The first search reads the whole wiki, and each later search
reads only the pages that changed. Joshua's vector index stays in core; this
index is only for a caller outside the cluster.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from joshua_shared import layout

from joshua_mcp import wiki as wikifs

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_K1 = 1.5
_B = 0.75
_SNIPPET = 200
MAX_LIMIT = 50


def tokenize(text: str) -> list[str]:
    return [word.lower() for word in _WORD_RE.findall(text)]


@dataclass
class _Doc:
    stamp: tuple[int, int]
    source: str
    title: str
    date: str | None
    lines: list[str]
    terms: Counter[str]
    length: int


class Index:
    """A BM25 index over the pages of one wiki. Safe to share between threads."""

    def __init__(self, wiki: Path) -> None:
        self.wiki = wiki
        self._docs: dict[str, _Doc] = {}
        self._lock = threading.Lock()

    def refresh(self) -> None:
        seen: set[str] = set()
        for rel, path in wikifs.iter_pages(self.wiki):
            seen.add(rel)
            try:
                stat = path.stat()
            except OSError:
                continue
            stamp = (stat.st_mtime_ns, stat.st_size)
            current = self._docs.get(rel)
            if current is not None and current.stamp == stamp:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._docs.pop(rel, None)
                continue
            self._docs[rel] = _build(self.wiki, rel, path, stamp, text)
        for rel in set(self._docs) - seen:
            del self._docs[rel]

    def search(self, query: str, source: str | None = None, limit: int = 10) -> list[dict]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            raise wikifs.WikiError("the query has no words")
        limit = max(1, min(int(limit), MAX_LIMIT))
        with self._lock:
            self.refresh()
            docs = {
                rel: doc
                for rel, doc in self._docs.items()
                if source is None or doc.source == source
            }
            return _rank(docs, terms, limit)

    def __len__(self) -> int:
        return len(self._docs)


def _build(wiki: Path, rel: str, path: Path, stamp: tuple[int, int], text: str) -> _Doc:
    frontmatter, body = wikifs.split_frontmatter_loose(text)
    lines = body.splitlines()
    title = _title(frontmatter, lines, rel)
    source = wikifs.source_of(rel)
    day = None
    if source == wikifs.JOURNAL:
        found = layout.journal_day_from_path(path, root=wiki.parent)
        day = found.isoformat() if found else _str_or_none(frontmatter.get("date"))
    terms = Counter(tokenize(title + "\n" + body))
    return _Doc(
        stamp=stamp,
        source=source,
        title=title,
        date=day,
        lines=lines,
        terms=terms,
        length=sum(terms.values()),
    )


def _title(frontmatter: dict[str, Any], lines: list[str], rel: str) -> str:
    title = frontmatter.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    return PurePosixPath(rel).stem


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _rank(docs: dict[str, _Doc], terms: list[str], limit: int) -> list[dict]:
    if not docs:
        return []
    count = len(docs)
    average = sum(doc.length for doc in docs.values()) / count or 1.0
    frequency = {term: sum(1 for doc in docs.values() if term in doc.terms) for term in terms}
    idf = {term: math.log(1 + (count - n + 0.5) / (n + 0.5)) for term, n in frequency.items() if n}

    scored = []
    for rel, doc in docs.items():
        score = 0.0
        for term, weight in idf.items():
            tf = doc.terms.get(term, 0)
            if tf:
                norm = tf + _K1 * (1 - _B + _B * doc.length / average)
                score += weight * tf * (_K1 + 1) / norm
        if score > 0:
            scored.append((score, rel, doc))
    scored.sort(key=lambda item: (-item[0], item[1]))

    results = []
    for score, rel, doc in scored[:limit]:
        hit: dict[str, Any] = {
            "path": rel,
            "source": doc.source,
            "title": doc.title,
            "snippet": _snippet(doc.lines, terms),
            "score": round(score, 4),
        }
        if doc.date is not None:
            hit["date"] = doc.date
        results.append(hit)
    return results


def _snippet(lines: list[str], terms: list[str]) -> str:
    wanted = set(terms)
    for line in lines:
        if wanted.intersection(tokenize(line)):
            return line.strip()[:_SNIPPET]
    for line in lines:
        if line.strip():
            return line.strip()[:_SNIPPET]
    return ""
