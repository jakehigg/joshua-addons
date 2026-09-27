"""Tests for the prompt composition."""

from __future__ import annotations

from pathlib import Path

from conftest import make_brief
from joshua_developer_worker import prompt


def test_the_prompt_holds_the_base_the_persona_and_the_brief(tmp_path: Path) -> None:
    text = prompt.compose(make_brief(tmp_path), None)
    assert text.startswith("# Developer")
    assert "Work as a senior developer." in text
    assert "Add a greeting file named hello.txt." in text
    assert "- Branch: developer/task-0001 (checked out)" in text
    assert "- Base branch: main" in text
    assert "Repository notes" not in text


def test_each_persona_has_its_overlay(tmp_path: Path) -> None:
    for name, marker in (
        ("sonnet", "Do not refactor."),
        ("opus", "Work as a senior developer."),
        ("fable", "plan the"),
    ):
        brief = make_brief(tmp_path, persona={"name": name})
        assert marker in prompt.compose(brief, None)


def test_an_unknown_or_unsafe_persona_gets_the_default_overlay() -> None:
    default = prompt.persona_overlay("opus")
    assert prompt.persona_overlay("haiku") == default
    assert prompt.persona_overlay("../developer") == default


def test_claude_md_is_appended_as_text(tmp_path: Path) -> None:
    text = prompt.compose(make_brief(tmp_path), "Run `make test`.")
    assert "## Repository notes (CLAUDE.md, as text)" in text
    assert text.index("## The task") < text.index("## Repository notes")
    assert "Run `make test`." in text


def test_claude_md_is_cut_at_the_limit(tmp_path: Path) -> None:
    long = "a" * (prompt.MAX_CLAUDE_MD + 500)
    text = prompt.compose(make_brief(tmp_path), long)
    assert "a" * prompt.MAX_CLAUDE_MD in text
    assert "a" * (prompt.MAX_CLAUDE_MD + 1) not in text
    assert "CLAUDE.md is cut here" in text


def test_an_empty_claude_md_adds_nothing(tmp_path: Path) -> None:
    assert "Repository notes" not in prompt.compose(make_brief(tmp_path), "  \n")


def test_a_rework_brief_holds_the_feedback_the_note_and_the_answer(tmp_path: Path) -> None:
    brief = make_brief(
        tmp_path,
        task_type="rework",
        base_branch=None,
        feedback="Rename the file.",
        note="Stopped: the time limit ran out.",
        answer="Use port 8080.",
    )
    text = prompt.compose(brief, None)
    assert "- Task type: rework" in text
    assert "open pull request" in text
    assert "### Review feedback\n\nRename the file." in text
    assert "### Where the last worker stopped" in text
    assert "### The answer to the last question\n\nUse port 8080." in text
    assert "Base branch" not in text


def test_a_resumed_brief_gets_the_resume_block_before_the_brief(tmp_path: Path) -> None:
    brief = make_brief(
        tmp_path,
        resumed=True,
        note="Stopped: blocked on a question.",
        answer="Use port 8080.",
    )
    text = prompt.compose(brief, None)
    block = (
        "You are resuming a task. A previous worker stopped. "
        "Its note: Stopped: blocked on a question. "
        "The answer to its question: Use port 8080."
    )
    assert block in text
    assert text.index(block) < text.index("### Brief")
    assert "### Where the last worker stopped" not in text
    assert "### The answer to the last question" not in text
    bare = prompt.compose(make_brief(tmp_path, resumed=True), None)
    assert "Its note: none. The answer to its question: none." in bare


def test_the_prompt_tells_the_model_to_commit_and_not_push() -> None:
    base = " ".join(prompt.base_prompt().split())
    assert "You must not push." in base
    assert "call `ask` once with one clear question" in base
    assert "MR_URL" not in base
    assert "structured result" in base
