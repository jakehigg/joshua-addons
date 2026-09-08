"""Facets: promotion, relabeling, the primary facet, and the scoped style counts."""

from __future__ import annotations

from conftest import load_fixture
from joshua_vinyl import facets
from joshua_vinyl.config import FacetRules

DEFAULT = FacetRules()


def test_country_is_promoted_and_its_genre_is_dropped() -> None:
    cash = load_fixture("discogs_release_cash_folsom.json")
    assert cash["genres"] == ["Folk, World, & Country"]
    assert facets.facets_of(cash["genres"], cash["styles"], DEFAULT) == ["Country"]
    assert facets.primary_facet(cash["genres"], cash["styles"], DEFAULT) == "Country"


def test_folk_without_country_is_relabeled() -> None:
    result = facets.facets_of(["Folk, World, & Country"], ["Folk"], DEFAULT)
    assert result == ["Folk & World"]


def test_a_release_with_no_styles_still_has_a_facet() -> None:
    color = load_fixture("discogs_collection_page1.json")["releases"][0]["basic_information"]
    assert color["styles"] == []
    assert facets.facets_of(color["genres"], color["styles"], DEFAULT) == ["Rock"]
    assert facets.primary_facet(color["genres"], color["styles"], DEFAULT) == "Rock"


def test_every_genre_is_a_facet_and_the_first_is_primary() -> None:
    morricone = load_fixture("discogs_release_morricone_good_bad_ugly.json")
    result = facets.facets_of(morricone["genres"], morricone["styles"], DEFAULT)
    assert result == ["Classical", "Stage & Screen"]
    assert facets.primary_facet(morricone["genres"], morricone["styles"], DEFAULT) == "Classical"


def test_a_promoted_style_wins_the_primary_facet_over_a_genre() -> None:
    rules = FacetRules(promote={"Prog Rock": "Rock"}, relabel={})
    assert facets.primary_facet(["Rock"], ["Prog Rock", "Alternative Rock"], rules) == "Prog Rock"
    assert facets.facets_of(["Rock"], ["Prog Rock"], rules) == ["Prog Rock"]


def test_a_promoted_style_keeps_a_genre_it_did_not_leave() -> None:
    rules = FacetRules(promote={"Country": "Folk, World, & Country"}, relabel={})
    result = facets.facets_of(["Rock", "Folk, World, & Country"], ["Country"], rules)
    assert result == ["Country", "Rock"]


def test_no_genres_means_no_facet() -> None:
    assert facets.facets_of([], [], DEFAULT) == []
    assert facets.primary_facet([], [], DEFAULT) is None


def test_facet_counts_order_by_count_then_name_and_scope_styles() -> None:
    records = [
        {"facets": ["Rock"], "styles": ["Prog Rock"]},
        {"facets": ["Rock"], "styles": ["Prog Rock", "Alternative Rock"]},
        {"facets": ["Rock"], "styles": []},
        {"facets": ["Classical", "Stage & Screen"], "styles": ["Soundtrack"]},
        {"facets": ["Classical"], "styles": ["Baroque"]},
        {"facets": ["Country"], "styles": ["Country"]},
    ]
    result = facets.facet_counts(records)
    assert [(f["name"], f["count"]) for f in result] == [
        ("Rock", 3),
        ("Classical", 2),
        ("Country", 1),
        ("Stage & Screen", 1),
    ]
    rock = result[0]
    assert rock["styles"] == [
        {"name": "Prog Rock", "count": 2},
        {"name": "Alternative Rock", "count": 1},
    ]
    country = result[2]
    assert country["styles"] == [], "a promoted style is not its own refinement"


def test_facet_counts_tolerates_records_with_no_style_field() -> None:
    result = facets.facet_counts([{"facets": ["Rock"]}, {"id": 1}])
    assert result == [{"name": "Rock", "count": 1, "styles": []}]
