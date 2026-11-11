"""A manager restart does not end a task whose worker is alive (#71).

At start the manager finds the worker of each active task again. A task fails
only when its worker is gone. A worker that ended while the manager was away
gives its outcome: the report it wrote to its log, or the state of its Job.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import make_settings
from docker.errors import NotFound
from joshua_developer.locks import LockManager
from joshua_developer.manager import WORKER_GONE_ERROR, Manager
from joshua_developer.runtime import UNSENT_REPORT, Report, StubRuntime, unsent_report
from joshua_developer.runtime_docker import DockerRuntime
from joshua_developer.store import TaskStore
from kubernetes.client.exceptions import ApiException
from test_runtime_docker import FakeClient, FakeContainer
from test_runtime_docker import Recorder as DockerRecorder
from test_runtime_k8s import TASK, FakeBatch, Recorder, condition, make_runtime, running, wait_for

REPO = "github.com/example-home/app"


def unsent_line(task_id: str, **report: Any) -> str:
    """The last log line of a worker that could not send its report."""
    body = {"status": "success", "summary": "Done while the manager was away.", **report}
    return json.dumps({"message": UNSENT_REPORT, "task_id": task_id, "report": body})


# --- the manager -------------------------------------------------------------


class FakeRuntime:
    """A runtime whose workers in ``alive`` are still there after the restart."""

    def __init__(self, alive: set[str], fail: bool = False) -> None:
        self.alive = alive
        self.fail = fail
        self.reattached: list[str] = []
        self.orphan_calls = 0

    def count_active(self) -> int:
        return len(self.reattached)

    async def start(self, task: dict) -> str:
        return "w"

    async def reattach(self, task: dict) -> bool:
        if self.fail:
            raise RuntimeError("api down")
        if task["task_id"] in self.alive:
            self.reattached.append(task["task_id"])
            return True
        return False

    def remove_orphans(self) -> int:
        self.orphan_calls += 1
        return 0


@pytest.fixture
def store(settings):
    store = TaskStore(settings.db_path)
    yield store
    store.close()


async def two_running_tasks(settings, store) -> tuple[str, str]:
    first = Manager(settings, store, LockManager())
    first.runtime = StubRuntime(first, delay_s=None)
    alive = (await first.develop("alex", REPO, "Alive.", branch="alive"))["task_id"]
    gone = (await first.develop("alex", REPO, "Gone.", branch="gone"))["task_id"]
    return alive, gone


async def test_a_task_whose_worker_runs_stays_running_after_a_restart(settings, store) -> None:
    alive, gone = await two_running_tasks(settings, store)
    manager = Manager(settings, store, LockManager())
    runtime = FakeRuntime({alive})
    manager.runtime = runtime  # type: ignore[assignment]
    assert sorted(manager.recover()) == sorted([alive, gone])
    assert await manager.reattach_workers() == [gone]

    kept = store.get_task_full(alive)
    assert kept is not None and kept["status"] == "running" and kept["error"] is None
    assert kept["worker_token"], "the worker keeps its token, so its calls still work"
    # The lock stays with the task, so no second task takes the branch.
    assert manager.locks.get(REPO, "branch:alive") is not None
    refused = await manager.develop("alex", REPO, "Again.", branch="alive")
    assert refused["reason"] == "locked"

    failed = store.get_task_full(gone)
    assert failed is not None and failed["status"] == "failed"
    assert failed["error"] == WORKER_GONE_ERROR
    assert manager.locks.get(REPO, "branch:gone") is None
    assert manager.recovered == [gone]
    assert runtime.orphan_calls == 1


async def test_no_task_says_manager_restarted(settings, store) -> None:
    alive, _ = await two_running_tasks(settings, store)
    manager = Manager(settings, store, LockManager())
    manager.runtime = FakeRuntime({alive})  # type: ignore[assignment]
    manager.recover()
    await manager.reattach_workers()
    errors = {store.get_task(t)["error"] for t in (alive,)}  # type: ignore[index]
    assert errors == {None}


async def test_a_runtime_that_cannot_look_fails_the_task(settings, store) -> None:
    alive, gone = await two_running_tasks(settings, store)
    manager = Manager(settings, store, LockManager())
    manager.runtime = FakeRuntime(set(), fail=True)  # type: ignore[assignment]
    manager.recover()
    assert sorted(await manager.reattach_workers()) == sorted([alive, gone])


async def test_a_failed_orphan_removal_is_not_fatal(settings, store) -> None:
    await two_running_tasks(settings, store)
    manager = Manager(settings, store, LockManager())
    runtime = FakeRuntime(set())

    def broken() -> int:
        raise RuntimeError("api down")

    runtime.remove_orphans = broken  # type: ignore[method-assign]
    manager.runtime = runtime  # type: ignore[assignment]
    manager.recover()
    assert len(await manager.reattach_workers()) == 2


async def test_a_reattached_worker_reports_to_the_new_manager(settings, store) -> None:
    alive, _ = await two_running_tasks(settings, store)
    manager = Manager(settings, store, LockManager())
    manager.runtime = FakeRuntime({alive})  # type: ignore[assignment]
    manager.recover()
    await manager.reattach_workers()
    await manager.record_report(alive, Report(status="blocked", open_question="Which port?"))
    row = store.get_task(alive)
    assert row is not None and row["status"] == "blocked"
    assert manager.locks.get(REPO, "branch:alive") is None


# --- the unsent report -------------------------------------------------------


def test_the_unsent_report_is_the_last_line_of_the_log() -> None:
    log = "\n".join(
        [
            "not json",
            '{"message": "step", "step": "push"}',
            "{broken",
            unsent_line("t1", pushed=True, head="feature"),
            "",
        ]
    )
    report = unsent_report(log, "t1")
    assert report is not None and report.status == "success" and report.pushed is True


def test_an_unsent_report_of_another_task_or_with_a_bad_field_is_ignored() -> None:
    assert unsent_report(unsent_line("other"), "t1") is None
    bad = json.dumps({"message": UNSENT_REPORT, "task_id": "t1", "report": {"status": "maybe"}})
    assert unsent_report(bad, "t1") is None
    extra = json.dumps(
        {"message": UNSENT_REPORT, "task_id": "t1", "report": {"status": "success", "pr": 1}}
    )
    assert unsent_report(extra, "t1") is None
    assert unsent_report("", "t1") is None
    assert unsent_report("[1, 2]", "t1") is None


# --- Kubernetes ----------------------------------------------------------------


class LabelledBatch(FakeBatch):
    """A Job read that has the task label, as a Job the manager made has."""

    def __init__(self, task_id: str | None) -> None:
        super().__init__()
        self.task_id = task_id
        self.missing = False

    def read_namespaced_job(self, name: str, namespace: str) -> Any:
        if self.missing:
            raise ApiException(status=404)
        found = super().read_namespaced_job(name, namespace)
        found.metadata.labels = {"task-id": self.task_id} if self.task_id else None
        return found


def k8s_runtime(tmp_path, batch: FakeBatch, recorder: Recorder | None = None):
    runtime, _, core = make_runtime(tmp_path, recorder)
    runtime.batch = batch
    batch.core = core
    return runtime, core


async def test_a_running_job_is_watched_again_and_not_failed(tmp_path) -> None:
    batch = LabelledBatch(TASK["task_id"])
    batch.statuses = [running()]
    recorder = Recorder()
    runtime, _ = k8s_runtime(tmp_path, batch, recorder)
    assert await runtime.reattach(TASK) is True
    assert "dev-worker-12345678" in runtime._watched
    await wait_for(lambda: len(batch.statuses) == 1)
    assert recorder.reports == []
    assert batch.deleted == []
    # The supervisor is the one remove_orphans leaves alone.
    batch.jobs = [
        SimpleNamespace(
            metadata=SimpleNamespace(
                name="dev-worker-12345678", labels={"task-id": TASK["task_id"]}
            )
        )
    ]
    assert runtime.remove_orphans() == 0
    for job in list(runtime._supervisors):
        job.cancel()


async def test_a_job_that_is_gone_is_not_reattached(tmp_path) -> None:
    batch = LabelledBatch(TASK["task_id"])
    batch.missing = True
    runtime, _ = k8s_runtime(tmp_path, batch)
    assert await runtime.reattach(TASK) is False
    other = LabelledBatch("another-task")
    runtime, _ = k8s_runtime(tmp_path, other)
    assert await runtime.reattach(TASK) is False


async def test_an_api_error_on_reattach_is_raised(tmp_path) -> None:
    batch = LabelledBatch(TASK["task_id"])
    batch.read_error = ApiException(status=500)
    runtime, _ = k8s_runtime(tmp_path, batch)
    with pytest.raises(ApiException):
        await runtime.reattach(TASK)


async def test_a_job_that_finished_while_the_manager_was_away_gives_its_report(
    tmp_path,
) -> None:
    batch = LabelledBatch(TASK["task_id"])
    batch.statuses = [condition("Failed", "BackoffLimitExceeded")]
    recorder = Recorder()
    runtime, core = k8s_runtime(tmp_path, batch, recorder)
    core.log = "x\n" + unsent_line(TASK["task_id"], pushed=True, head="feature") + "\n"
    assert await runtime.reattach(TASK) is True
    await wait_for(lambda: recorder.reports)
    [(task_id, report)] = recorder.reports
    assert task_id == TASK["task_id"]
    assert report.status == "success" and report.pushed is True
    assert report.summary == "Done while the manager was away."


async def test_a_finished_job_with_no_report_takes_the_job_state(tmp_path) -> None:
    batch = LabelledBatch(TASK["task_id"])
    batch.statuses = [condition("Failed", "BackoffLimitExceeded")]
    recorder = Recorder()
    runtime, core = k8s_runtime(tmp_path, batch, recorder)
    core.log_error = ApiException(status=500)
    assert await runtime.reattach(TASK) is True
    await wait_for(lambda: recorder.reports)
    [(_, report)] = recorder.reports
    assert report.status == "failed"
    assert report.error == "the worker failed and sent no report"


async def test_an_unsent_report_without_pods_is_none(tmp_path) -> None:
    runtime, core = k8s_runtime(tmp_path, LabelledBatch(TASK["task_id"]))
    core.pods = []
    assert runtime._unsent_report("dev-worker-12345678", TASK["task_id"]) is None


# --- Docker --------------------------------------------------------------------


def labelled(name: str, task_id: str, instance: str = "developer") -> FakeContainer:
    container = FakeContainer(name, [])
    container.labels = {  # type: ignore[attr-defined]
        "joshua-addon": "developer",
        "joshua-developer-instance": instance,
        "task-id": task_id,
    }
    return container


class Containers:
    def __init__(self, client: FakeClient, by_name: dict[str, FakeContainer]) -> None:
        self.by_name = by_name
        client.containers.get = self.get  # type: ignore[attr-defined]

    def get(self, name: str) -> FakeContainer:
        if name not in self.by_name:
            raise NotFound("no such container")
        return self.by_name[name]


def docker_runtime(tmp_path, client: FakeClient, recorder=None) -> DockerRuntime:
    settings = make_settings(tmp_path, worker_image="example/worker:1", worker_runtime="docker")
    return DockerRuntime(recorder or DockerRecorder(), settings, client=client, poll_s=0.01)


async def test_a_running_container_is_watched_again_and_kept(tmp_path) -> None:
    client = FakeClient()
    live = labelled("dev-worker-12345678", TASK["task_id"])
    live.statuses = ["running"] * 1000
    old = labelled("dev-worker-old", "ended-task")
    Containers(client, {live.name: live})
    client.containers.running = [live, old]
    recorder = DockerRecorder()
    runtime = docker_runtime(tmp_path, client, recorder)
    assert await runtime.reattach(TASK) is True
    assert runtime.remove_orphans() == 1
    assert old.removed and not live.removed
    assert recorder.reports == []
    for job in list(runtime._supervisors):
        job.cancel()


async def test_a_missing_or_foreign_container_is_not_reattached(tmp_path) -> None:
    client = FakeClient()
    Containers(client, {})
    assert await docker_runtime(tmp_path, client).reattach(TASK) is False
    client = FakeClient()
    other = labelled("dev-worker-12345678", TASK["task_id"], instance="another-install")
    Containers(client, {other.name: other})
    assert await docker_runtime(tmp_path, client).reattach(TASK) is False


async def test_a_container_that_exited_while_the_manager_was_away_gives_its_report(
    tmp_path,
) -> None:
    client = FakeClient()
    done = labelled("dev-worker-12345678", TASK["task_id"])
    done.statuses = ["exited"]
    done.logs_text = (unsent_line(TASK["task_id"], status="blocked") + "\n").encode()
    Containers(client, {done.name: done})
    recorder = DockerRecorder()
    runtime = docker_runtime(tmp_path, client, recorder)
    assert await runtime.reattach(TASK) is True
    await wait_for(lambda: recorder.reports)
    [(_, report)] = recorder.reports
    assert report.status == "blocked"
    assert done.removed
