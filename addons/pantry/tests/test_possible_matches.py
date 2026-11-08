"""record_purchase returns possible_matches for each new item (issue #15).

The automatic match in ``resolve_item`` stays careful. A possible match is
only a suggestion for the agent.
"""

from __future__ import annotations

from joshua_pantry import server
from joshua_pantry.models import Item
from joshua_pantry.resolution import find_possible_matches, significant_words
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_EARLIER = "2026-08-01"
_DAY = "2026-08-15"


async def _buy(names: list[str], day: str = _EARLIER) -> dict:
    return await server.record_purchase(
        items=[server.PurchaseLine(name=n) for n in names], purchased_at=day
    )


async def test_little_yellow_potatoes_suggests_little_potatoes(bound_db) -> None:
    await _buy(["little potatoes", "kale"])

    result = await _buy(["little yellow potatoes"], day=_DAY)

    (line,) = result["items"]
    assert line["created"] is True
    assert line["possible_matches"] == [{"item": "Little Potatoes", "shared_words": ["potato"]}]
    assert result["created_items"] == ["Little Yellow Potatoes"]
    assert result["possible_matches"] == {"Little Yellow Potatoes": line["possible_matches"]}


async def test_almond_milk_is_a_new_item_and_not_merged_into_milk(bound_db, sessionmaker_) -> None:
    await _buy(["milk"])

    result = await _buy(["almond milk"], day=_DAY)

    (line,) = result["items"]
    assert line["created"] is True
    assert line["resolved_to"] == "Almond Milk"
    # It may appear as a possible match, but nothing merged.
    assert [m["item"] for m in line["possible_matches"]] == ["Milk"]
    async with sessionmaker_() as session:
        names = set((await session.execute(select(Item.name))).scalars().all())
    assert names == {"milk", "almond milk"}
    milk = await server.get_item_history("milk")
    assert milk["purchase_count"] == 1


async def test_filler_word_overlap_is_no_match(bound_db) -> None:
    await _buy(["organic large eggs", "fresh basil 2 oz"])

    result = await _buy(["organic small fresh tomatoes 16oz"], day=_DAY)

    assert result["items"][0]["possible_matches"] == []
    assert result["possible_matches"] == {}


async def test_matched_line_has_no_possible_matches(bound_db) -> None:
    await _buy(["spinach"])

    result = await _buy(["spinach"], day=_DAY)

    assert result["items"][0]["created"] is False
    assert result["items"][0]["possible_matches"] == []


async def test_new_lines_of_one_receipt_do_not_suggest_each_other(bound_db) -> None:
    result = await _buy(["red onions", "yellow onions"], day=_DAY)

    assert [line["possible_matches"] for line in result["items"]] == [[], []]


async def test_at_most_three_matches_most_shared_words_first(bound_db) -> None:
    await _buy(
        [
            "baby spinach",
            "frozen chopped spinach",
            "spinach dip",
            "spinach wraps",
            "frozen baby spinach",
        ]
    )

    result = await _buy(["organic frozen baby spinach leaves"], day=_DAY)

    matches = result["items"][0]["possible_matches"]
    assert len(matches) == 3
    assert matches[0] == {
        "item": "Frozen Baby Spinach",
        "shared_words": ["baby", "frozen", "spinach"],
    }
    assert [m["item"] for m in matches[1:]] == ["Baby Spinach", "Frozen Chopped Spinach"]


async def test_alias_counts_as_a_name_of_its_item(session: AsyncSession, bound_db) -> None:
    await _buy(["granola bars"])
    await server.add_alias("granola bars", "oat clusters")

    matches = await find_possible_matches(session, "honey oat clusters")

    assert matches == [{"item": "Granola Bars", "shared_words": ["cluster", "oat"]}]


async def test_untracked_item_is_not_suggested(session: AsyncSession, bound_db) -> None:
    session.add(Item(name="sweet potatoes", normalized="sweet potatoes", is_tracked=False))
    await session.commit()

    assert await find_possible_matches(session, "little potatoes") == []


def test_significant_words_drop_filler_size_and_plurals() -> None:
    assert significant_words("Organic Little Yellow Potatoes 24oz") == {"yellow", "potato"}
    assert significant_words("Fresh Berries 1.5 lb") == {"berry"}
    assert significant_words("large eggs 12 ct") == {"egg"}
    assert significant_words("swiss cheese") == {"swiss", "cheese"}
    assert significant_words("organic fresh") == set()
