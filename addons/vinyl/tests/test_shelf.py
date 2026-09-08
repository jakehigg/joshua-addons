"""Shelf sections: the letter from the sort-name, and the special sections after Z."""

from __future__ import annotations

import pytest
from joshua_vinyl import shelf
from joshua_vinyl.config import Section, ShelfConfig


@pytest.mark.parametrize(
    ("sort_name", "letter"),
    [
        ("Dylan, Bob", "D"),
        ("Beatles, The", "B"),
        ("Rolling Stones, The", "R"),
        ("Sly & the Family Stone", "S"),
        ("Morricone, Ennio", "M"),
        ("  lowercase", "L"),
        ("Élysée", "E"),
        ("Ólafur Arnalds", "O"),
        ("2Pac", "#"),
        ("!!!", "#"),
        ("", "#"),
        ("東京事変", "#"),
    ],
)
def test_letter_of(sort_name: str, letter: str) -> None:
    assert shelf.letter_of(sort_name) == letter


def test_various_is_a_compilation() -> None:
    assert shelf.traits_of("Various", [], [{"name": "Vinyl", "descriptions": ["LP"]}]) == {
        shelf.COMPILATION
    }


def test_compilation_format_description_is_a_compilation() -> None:
    formats = [{"name": "Vinyl", "descriptions": ["LP", "Compilation"]}]
    assert shelf.traits_of("Ennio Morricone", [], formats) == {shelf.COMPILATION}


@pytest.mark.parametrize("style", ["Soundtrack", "Score", "soundtrack"])
def test_soundtrack_style_is_a_soundtrack(style: str) -> None:
    assert shelf.traits_of("Ennio Morricone", [style], []) == {shelf.SOUNDTRACK}


def test_a_plain_album_has_no_traits() -> None:
    formats = [{"name": "Vinyl", "descriptions": ["LP", "Album"]}]
    assert shelf.traits_of("Bob Dylan", ["Folk Rock"], formats) == set()


def test_formats_without_descriptions_do_not_fail() -> None:
    assert shelf.traits_of("Bob Dylan", [], [{"name": "Vinyl"}]) == set()


def test_soundtrack_by_one_composer_is_never_filed_under_the_composer() -> None:
    sections = ShelfConfig().sections
    section = shelf.section_for("Morricone, Ennio", {shelf.SOUNDTRACK}, sections)
    assert section == "Compilations & Soundtracks"


def test_compilation_goes_after_z() -> None:
    sections = ShelfConfig().sections
    assert shelf.section_for("Various Artists", {shelf.COMPILATION}, sections) == (
        "Compilations & Soundtracks"
    )


def test_no_trait_means_the_letter() -> None:
    assert shelf.section_for("Dylan, Bob", set(), ShelfConfig().sections) == "D"


def test_split_sections_take_the_first_match_in_order() -> None:
    sections = [
        Section(name="Soundtracks", traits=["soundtrack"]),
        Section(name="Compilations", traits=["compilation"]),
    ]
    both = {shelf.COMPILATION, shelf.SOUNDTRACK}
    assert shelf.section_for("Morricone, Ennio", both, sections) == "Soundtracks"
    assert shelf.section_for("Various Artists", {shelf.COMPILATION}, sections) == "Compilations"


def test_a_trait_no_section_lists_falls_back_to_the_letter() -> None:
    sections = [Section(name="Compilations", traits=["compilation"])]
    assert shelf.section_for("Morricone, Ennio", {shelf.SOUNDTRACK}, sections) == "M"


def test_section_order_is_numeric_then_letters_then_specials() -> None:
    sections = [Section(name="Soundtracks", traits=["soundtrack"]), Section(name="X", traits=["y"])]
    order = shelf.section_order(sections)
    assert order[0] == "#"
    assert order[1:27] == list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert order[27:] == ["Soundtracks", "X"]
