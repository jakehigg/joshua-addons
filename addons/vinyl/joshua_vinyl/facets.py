"""Facets: the top-level filter list, derived from Discogs genres and styles.

A genre is the header, and choosing one is a complete filter. A style is the
optional refinement inside it. A promoted style becomes a facet of its own,
and the genre it left is dropped from that release. A relabel renames a
facet for display. ``primary_facet`` is the one bucket a release sorts into.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from joshua_vinyl.config import FacetRules


def facets_of(genres: list[str], styles: list[str], rules: FacetRules) -> list[str]:
    """The facets a release shows under, in order: promoted styles first, then genres."""
    promoted = [style for style in styles if style in rules.promote]
    left = {rules.promote[style] for style in promoted}
    result: list[str] = []
    for style in promoted:
        if style not in result:
            result.append(style)
    for genre in genres:
        if genre in left:
            continue
        label = rules.relabel.get(genre, genre)
        if label not in result:
            result.append(label)
    return result


def primary_facet(genres: list[str], styles: list[str], rules: FacetRules) -> str | None:
    """The one facet a release sorts into: the first promoted style, else the first genre."""
    facets = facets_of(genres, styles, rules)
    return facets[0] if facets else None


def facet_counts(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The facet list for the bundle, each with its count and its scoped styles.

    Each ``record`` needs ``facets`` and ``styles``. Facets order by count,
    then name. Styles inside a facet order the same way, and a promoted style
    never lists itself as its own refinement.
    """
    counts: Counter[str] = Counter()
    styles: dict[str, Counter[str]] = {}
    for record in records:
        for facet in record.get("facets") or []:
            counts[facet] += 1
            bucket = styles.setdefault(facet, Counter())
            for style in record.get("styles") or []:
                if style != facet:
                    bucket[style] += 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        {
            "name": name,
            "count": count,
            "styles": [
                {"name": style, "count": n}
                for style, n in sorted(styles[name].items(), key=lambda item: (-item[1], item[0]))
            ],
        }
        for name, count in ordered
    ]
