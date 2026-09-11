"""Shelf sections: the divider letter, or a named section after Z.

The letter is the first character of the artist sort-name, which MusicBrainz
supplies, so no name parser lives here. A release with a special trait
(various, soundtrack) goes to the first configured section that lists that
trait. Those sections sort after Z, in configured order. A sort-name that
starts with a digit or a symbol files under ``#``, before A.

``compilation`` and ``various`` are separate traits on purpose. A greatest-hits
record by one artist is a compilation, but it belongs beside that artist's
other records, so only ``various`` sends a release to the compilations
section. ``compilation`` stays true for display and filtering.
"""

from __future__ import annotations

import string
import unicodedata
from typing import Any

from joshua_vinyl.config import Section

NUMERIC_SECTION = "#"
LETTERS = tuple(string.ascii_uppercase)
COMPILATION = "compilation"
VARIOUS = "various"
SOUNDTRACK = "soundtrack"
VARIOUS_ARTISTS = frozenset({"various", "various artists"})
SOUNDTRACK_STYLES = frozenset({"soundtrack", "score"})


def letter_of(sort_name: str) -> str:
    """The divider letter for a sort-name, or ``#`` when it does not start with a letter."""
    text = unicodedata.normalize("NFKD", sort_name.strip())
    for ch in text:
        if unicodedata.combining(ch):
            continue
        if ch.isalpha() and ch.upper() in LETTERS:
            return ch.upper()
        return NUMERIC_SECTION
    return NUMERIC_SECTION


def traits_of(artist: str, styles: list[str], formats: list[dict[str, Any]]) -> set[str]:
    """The special traits of a release: ``compilation``, ``various``, ``soundtrack``.

    A Discogs compilation is credited to ``Various`` or carries the
    ``Compilation`` format description. ``various`` marks the first of those
    only, because a compilation by one artist files under that artist. A
    soundtrack carries the ``Soundtrack`` or ``Score`` style.
    """
    traits: set[str] = set()
    descriptions = {
        str(item).casefold() for fmt in formats for item in (fmt.get("descriptions") or [])
    }
    many_artists = artist.strip().casefold() in VARIOUS_ARTISTS
    if many_artists:
        traits.add(VARIOUS)
    if many_artists or "compilation" in descriptions:
        traits.add(COMPILATION)
    if any(style.casefold() in SOUNDTRACK_STYLES for style in styles):
        traits.add(SOUNDTRACK)
    return traits


def special_section(traits: set[str], sections: list[Section]) -> str | None:
    """The first configured section that matches a trait, or ``None``."""
    for section in sections:
        if any(trait in traits for trait in section.traits):
            return section.name
    return None


def section_for(sort_name: str, traits: set[str], sections: list[Section]) -> str:
    """The shelf section: a special section when a trait matches, else the letter."""
    return special_section(traits, sections) or letter_of(sort_name)


def section_order(sections: list[Section]) -> list[str]:
    """Every section in shelf order: ``#``, A to Z, then the special sections."""
    return [NUMERIC_SECTION, *LETTERS, *(section.name for section in sections)]
