"""The tools, called over MCP the way the gateway calls them."""

from __future__ import annotations

from conftest import ALEX, MIA, error, make_settings, mcp_session, mcp_sessions, payload
from joshua_developer import server
from joshua_developer.server import build_app

REPO = "github.com/example-home/app"


def develop_args(**extra) -> dict:
    return {"repo": REPO, "brief": "Add a health check.", **extra}


async def test_the_tool_list_is_final(app) -> None:
    async with mcp_session(app) as session:
        tools = await session.list_tools()
    assert sorted(tool.name for tool in tools.tools) == [
        "answer",
        "develop",
        "get_settings",
        "list_personas",
        "list_tasks",
        "rework",
        "set_settings",
        "task_output",
        "task_status",
    ]


async def test_develop_runs_a_task_to_its_report(app) -> None:
    async with mcp_session(app) as session:
        started = payload(
            await session.call_tool(
                "develop", develop_args(repo="https://GitHub.com/example-home/app.git")
            )
        )
        status = payload(await session.call_tool("task_status", {"task_id": started["task_id"]}))
        output = payload(await session.call_tool("task_output", {"task_id": started["task_id"]}))
    assert started["status"] == "dispatched"
    assert started["persona"] == "sonnet"
    assert started["will_notify"] is False
    assert f"Poll task_status('{started['task_id']}')" in started["message"]
    assert status["repo"] == REPO
    assert status["status"] == "success"
    assert status["person"] == "alex"
    assert status["branch_name"] == f"joshua/add-a-health-check-{started['task_id'][:6]}"
    assert status["base_branch"] == "main"
    assert status["scope"] == f"branch:{status['branch_name']}"
    assert status["notify"] == "telegram:dm:alex"
    assert status["started_at"] and status["completed_at"]
    assert "brief" not in status
    assert output["report"]["summary"].startswith("The stub runtime")
    assert output["log"] == "stub runtime: no session ran"
    assert "log" not in output["report"]


async def test_develop_says_a_report_event_comes_when_channels_is_set(data_dir) -> None:
    settings = make_settings(
        data_dir, channels_url="http://channels:8000", channels_token="fleet-token"
    )
    app = build_app(settings, stub_delay_s=0)
    async with mcp_session(app) as session:
        started = payload(await session.call_tool("develop", develop_args(notify="voice:kitchen")))
        status = payload(await session.call_tool("task_status", {"task_id": started["task_id"]}))
    assert started["will_notify"] is True
    assert "arrives as an event" in started["message"]
    assert "fleet-token" not in started["message"]
    assert status["notify"] == "voice:kitchen"


async def test_a_repo_outside_the_list_is_refused_by_name(app) -> None:
    async with mcp_sessions(app, ALEX, MIA) as (alex, mia):
        other = payload(await alex.call_tool("develop", develop_args(repo="github.com/evil/app")))
        own = payload(
            await alex.call_tool("develop", develop_args(repo="github.com/alex-example/x"))
        )
        not_mia = payload(
            await mia.call_tool("develop", develop_args(repo="github.com/alex-example/x"))
        )
        listed = payload(await alex.call_tool("list_tasks", {}))
    assert other["status"] == "rejected"
    assert other["reason"] == "repo_not_allowed"
    assert "github.com/evil/app" in other["message"]
    assert "example-home" not in other["message"] and "alex-example" not in other["message"]
    assert own["status"] == "dispatched"
    assert not_mia["reason"] == "repo_not_allowed"
    assert len(listed["tasks"]) == 1


async def test_bad_arguments_are_rejected(app) -> None:
    async with mcp_session(app) as session:
        bad_repo = payload(await session.call_tool("develop", develop_args(repo="app")))
        no_brief = payload(await session.call_tool("develop", develop_args(brief="  ")))
        long_brief = payload(await session.call_tool("develop", develop_args(brief="x" * 100_001)))
        bad_branch = payload(await session.call_tool("develop", develop_args(branch="a..b")))
        bad_base = payload(await session.call_tool("develop", develop_args(base_branch="-x")))
        bad_pr = payload(
            await session.call_tool("rework", {"repo": REPO, "pr": "abc", "feedback": "x"})
        )
        no_feedback = payload(
            await session.call_tool("rework", {"repo": REPO, "pr": 3, "feedback": ""})
        )
        long_feedback = payload(
            await session.call_tool("rework", {"repo": REPO, "pr": 3, "feedback": "x" * 100_001})
        )
        tasks = payload(await session.call_tool("list_tasks", {}))
    for result in (
        bad_repo,
        no_brief,
        long_brief,
        bad_branch,
        bad_base,
        bad_pr,
        no_feedback,
        long_feedback,
    ):
        assert result["status"] == "rejected"
        assert result["reason"] == "invalid_arguments"
    assert tasks["tasks"] == []


async def test_a_second_develop_on_the_same_branch_is_locked(holding_app) -> None:
    async with mcp_session(holding_app) as session:
        first = payload(await session.call_tool("develop", develop_args(branch="feature")))
        second = payload(await session.call_tool("develop", develop_args(branch="feature")))
        other_branch = payload(await session.call_tool("develop", develop_args(branch="other")))
        status = payload(await session.call_tool("task_status", {"task_id": first["task_id"]}))
    assert status["status"] == "running"
    assert second["status"] == "rejected"
    assert second["reason"] == "locked"
    assert second["lock_info"]["task_id"] == first["task_id"]
    assert first["task_id"] in second["message"]
    assert "hint" in second
    assert other_branch["status"] == "dispatched"


async def test_a_second_rework_on_the_same_pr_is_locked(holding_app) -> None:
    args = {"repo": REPO, "pr": "https://github.com/example-home/app/pull/5", "feedback": "fix"}
    async with mcp_session(holding_app) as session:
        first = payload(await session.call_tool("rework", args))
        second = payload(await session.call_tool("rework", {**args, "pr": 5}))
        status = payload(await session.call_tool("task_status", {"task_id": first["task_id"]}))
    assert first["status"] == "dispatched"
    assert second["reason"] == "locked"
    assert status["task_type"] == "rework"
    # A rework locks the source branch of its pull request.
    assert status["scope"] == "branch:pr-5"
    assert status["pr_number"] == 5
    assert status["pr_url"] == args["pr"]


async def test_a_rework_on_the_branch_of_a_running_develop_is_locked(holding_app) -> None:
    async with mcp_session(holding_app) as session:
        # The fake host gives pull request 5 the source branch pr-5.
        first = payload(await session.call_tool("develop", develop_args(branch="pr-5")))
        rework = payload(
            await session.call_tool("rework", {"repo": REPO, "pr": 5, "feedback": "fix"})
        )
        other = payload(
            await session.call_tool("rework", {"repo": REPO, "pr": 6, "feedback": "fix"})
        )
    assert first["status"] == "dispatched"
    assert rework["status"] == "rejected"
    assert rework["reason"] == "locked"
    assert rework["lock_info"]["task_id"] == first["task_id"]
    assert other["status"] == "dispatched"


async def test_a_develop_on_the_branch_of_a_running_rework_is_locked(holding_app) -> None:
    async with mcp_session(holding_app) as session:
        first = payload(
            await session.call_tool("rework", {"repo": REPO, "pr": 5, "feedback": "fix"})
        )
        second = payload(await session.call_tool("develop", develop_args(branch="pr-5")))
    assert first["status"] == "dispatched"
    assert second["reason"] == "locked"


async def test_max_workers_limits_the_running_tasks(data_dir) -> None:
    app = build_app(make_settings(data_dir, config={"max_workers": 1}), stub_delay_s=None)
    async with mcp_session(app) as session:
        first = payload(await session.call_tool("develop", develop_args()))
        second = payload(
            await session.call_tool("develop", develop_args(repo="github.com/example-home/other"))
        )
    assert first["status"] == "dispatched"
    assert second["status"] == "rejected"
    assert second["reason"] == "concurrency_limit"
    assert "limit 1" in second["message"]


async def test_the_persona_order_is_call_then_person_then_instance(app) -> None:
    async with mcp_sessions(app, ALEX, MIA) as (alex, mia):
        from_yaml = payload(await alex.call_tool("develop", develop_args()))
        from_call = payload(await alex.call_tool("develop", develop_args(persona="fable")))
        payload(await alex.call_tool("set_settings", {"default_persona": "opus"}))
        from_setting = payload(await alex.call_tool("develop", develop_args()))
        from_instance = payload(await mia.call_tool("develop", develop_args()))
        unknown = payload(await alex.call_tool("develop", develop_args(persona="haiku")))
        alex_personas = payload(await alex.call_tool("list_personas", {}))
    assert from_yaml["persona"] == "sonnet"
    assert from_call["persona"] == "fable"
    assert from_setting["persona"] == "opus"
    assert from_instance["persona"] == "opus"
    assert unknown["status"] == "rejected"
    assert unknown["reason"] == "unknown_persona"
    assert alex_personas["default"] == "opus"
    assert set(alex_personas["personas"]) == {"sonnet", "opus", "fable"}
    assert alex_personas["personas"]["opus"]["timeout_s"] == 2400


async def test_settings_take_the_file_values_and_the_changes(app) -> None:
    async with mcp_sessions(app, ALEX, MIA) as (alex, mia):
        before = payload(await alex.call_tool("get_settings", {}))
        changed = payload(
            await alex.call_tool(
                "set_settings", {"git_name": "A. Example", "notify": "telegram:group"}
            )
        )
        cleared = payload(await alex.call_tool("set_settings", {"git_name": ""}))
        bad_email = payload(await alex.call_tool("set_settings", {"git_email": "nope"}))
        bad_persona = payload(await alex.call_tool("set_settings", {"default_persona": "haiku"}))
        too_long = payload(await alex.call_tool("set_settings", {"notify": "x" * 321}))
        email = payload(await alex.call_tool("set_settings", {"git_email": "a@example.org"}))
        mia_settings = payload(await mia.call_tool("get_settings", {}))
    assert before == {
        "person": "alex",
        "git_name": "Alex Example",
        "git_email": "alex@users.noreply.github.com",
        "default_persona": "sonnet",
        "notify": "telegram:dm:alex",
    }
    assert changed["git_name"] == "A. Example"
    assert changed["notify"] == "telegram:group"
    assert cleared["git_name"] == "Alex Example"
    assert bad_email["reason"] == "invalid_arguments"
    assert bad_persona["reason"] == "unknown_persona"
    assert too_long["reason"] == "invalid_arguments"
    assert email["git_email"] == "a@example.org"
    assert mia_settings == {
        "person": "mia",
        "git_name": None,
        "git_email": None,
        "default_persona": "opus",
        "notify": None,
    }


async def test_a_person_cannot_see_another_persons_task(app) -> None:
    async with mcp_sessions(app, ALEX, MIA) as (alex, mia):
        task_id = payload(await alex.call_tool("develop", develop_args()))["task_id"]
        status = await mia.call_tool("task_status", {"task_id": task_id})
        output = await mia.call_tool("task_output", {"task_id": task_id})
        answer = await mia.call_tool("answer", {"task_id": task_id, "text": "yes"})
        tasks = payload(await mia.call_tool("list_tasks", {}))
        missing = await alex.call_tool("task_status", {"task_id": "no-such-task"})
    for result in (status, output, answer):
        assert error(result).endswith(f"404 not found: no task {task_id}")
    assert tasks["tasks"] == []
    assert error(missing).endswith("404 not found: no task no-such-task")


async def test_answer_needs_a_waiting_task(holding_app) -> None:
    async with mcp_session(holding_app) as session:
        task_id = payload(await session.call_tool("develop", develop_args()))["task_id"]
        not_waiting = payload(
            await session.call_tool("answer", {"task_id": task_id, "text": "use sqlite"})
        )
        server._get_manager().store.update_task(
            task_id, open_question="Which database?", asked_at="2026-10-04T12:00:00+00:00"
        )
        empty = payload(await session.call_tool("answer", {"task_id": task_id, "text": " "}))
        too_long = payload(
            await session.call_tool("answer", {"task_id": task_id, "text": "x" * 100_001})
        )
        answered = payload(
            await session.call_tool("answer", {"task_id": task_id, "text": "use sqlite"})
        )
        again = payload(await session.call_tool("answer", {"task_id": task_id, "text": "no"}))
        status = payload(await session.call_tool("task_status", {"task_id": task_id}))
    assert not_waiting["status"] == "rejected"
    assert not_waiting["reason"] == "not_waiting"
    assert "no open question" in not_waiting["message"]
    assert empty["reason"] == "invalid_arguments"
    assert too_long["reason"] == "invalid_arguments"
    assert answered["status"] == "answered"
    assert again["reason"] == "not_waiting"
    assert status["open_question"] == "Which database?"
    full = server._get_manager().store.get_task_full(task_id)
    assert full is not None and full["answer"] == "use sqlite"


async def test_a_late_answer_is_refused_when_the_worker_stopped_waiting(holding_app) -> None:
    async with mcp_session(holding_app) as session:
        task_id = payload(await session.call_tool("develop", develop_args()))["task_id"]
        # The worker asked, then stopped waiting: the question is open, the wait is not.
        server._get_manager().store.update_task(task_id, open_question="Which database?")
        late = payload(await session.call_tool("answer", {"task_id": task_id, "text": "sqlite"}))
    assert late["status"] == "rejected"
    assert late["reason"] == "not_waiting"
    assert "no longer waits" in late["message"]
    assert "blocked or success" in late["message"]
    assert "answer again to resume" in late["message"]
    full = server._get_manager().store.get_task_full(task_id)
    assert full is not None and full["answer"] is None


async def test_list_tasks_limit(app) -> None:
    async with mcp_session(app) as session:
        for _ in range(3):
            payload(await session.call_tool("develop", develop_args()))
        two = payload(await session.call_tool("list_tasks", {"limit": 2}))
        clamp = payload(await session.call_tool("list_tasks", {"limit": 0}))
    assert len(two["tasks"]) == 2
    assert len(clamp["tasks"]) == 1


async def test_a_restart_fails_the_running_tasks(settings) -> None:
    first_app = build_app(settings, stub_delay_s=None)
    async with mcp_session(first_app) as session:
        task_id = payload(await session.call_tool("develop", develop_args(branch="feature")))[
            "task_id"
        ]

    second_app = build_app(settings, stub_delay_s=None)
    assert server._get_manager().recovered == [task_id]
    async with mcp_session(second_app) as session:
        status = payload(await session.call_tool("task_status", {"task_id": task_id}))
        again = payload(await session.call_tool("develop", develop_args(branch="feature")))
    assert status["status"] == "failed"
    assert status["error"] == "manager restarted"
    assert again["status"] == "dispatched"
