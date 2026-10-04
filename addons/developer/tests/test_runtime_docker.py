"""The Docker runtime, with a fake Docker client: no Docker runs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from conftest import make_settings
from docker.errors import APIError, ImageNotFound, NotFound
from joshua_developer import runtime_docker, server
from joshua_developer.runtime import Report, TaskClock
from joshua_developer.runtime_docker import DockerRuntime, make_client, worker_environment
from joshua_developer.store import utcnow

TASK = {
    "task_id": "12345678-aaaa-bbbb-cccc-dddddddddddd",
    "persona": "sonnet",
    "worker_token": "tok-value",
}


class FakeContainer:
    def __init__(self, name: str, statuses: list[str], exit_code: int = 1) -> None:
        self.name = name
        self.statuses = list(statuses)
        self.status = "created"
        self.attrs = {"State": {"ExitCode": exit_code}}
        self.started = False
        self.killed = False
        self.removed = False
        self.removed_volumes = False
        self.logs_text = b"x" * 5000 + b"the end"
        self.gone = False

    def start(self) -> None:
        self.started = True
        self.status = "running"

    def reload(self) -> None:
        if self.gone:
            raise NotFound("gone")
        if self.statuses:
            self.status = self.statuses.pop(0)

    def kill(self) -> None:
        self.killed = True
        self.status = "exited"

    def remove(self, force: bool = False, v: bool = False) -> None:
        self.removed = True
        self.removed_volumes = v

    def logs(self, **kwargs: Any) -> bytes:
        return self.logs_text


class FakeContainers:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.listed: list[dict] = []
        self.running: list[FakeContainer] = []
        self.next_statuses: list[str] = ["running"]
        self.missing_image = False
        self.made: list[FakeContainer] = []

    def create(self, image: str, **kwargs: Any) -> FakeContainer:
        if self.missing_image:
            self.missing_image = False
            raise ImageNotFound("no image")
        self.created.append((image, kwargs))
        container = FakeContainer(kwargs["name"], self.next_statuses)
        self.made.append(container)
        return container

    def list(self, **kwargs: Any) -> list[FakeContainer]:
        self.listed.append(kwargs)
        return list(self.running)


class FakeNetwork:
    def __init__(self) -> None:
        self.connected: list[FakeContainer] = []

    def connect(self, container: FakeContainer) -> None:
        self.connected.append(container)


class FakeClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.network = FakeNetwork()
        self.network_names: list[str] = []
        self.pulled: list[str] = []
        self.pull_error: Exception | None = None
        self.images = self
        self.networks = self

    def pull(self, image: str) -> None:
        if self.pull_error is not None:
            raise self.pull_error
        self.pulled.append(image)

    def get(self, name: str) -> FakeNetwork:
        self.network_names.append(name)
        return self.network


class Recorder:
    def __init__(self) -> None:
        self.running: list[str] = []
        self.reports: list[tuple[str, Report]] = []
        self.active = True
        # The worker read its brief when the recorder was made.
        self.clock: TaskClock | None = TaskClock(started_at=utcnow())

    def is_active(self, task_id: str, worker_token: str | None = None) -> bool:
        return self.active

    def mark_running(self, task_id: str) -> None:
        self.running.append(task_id)

    def task_clock(self, task_id: str) -> TaskClock | None:
        return self.clock

    async def record_report(self, task_id: str, report: Report) -> dict:
        self.reports.append((task_id, report))
        self.active = False
        return {}


def settings_for(tmp_path, **config) -> Any:
    return make_settings(
        tmp_path,
        worker_image="example/worker:1",
        worker_runtime="docker",
        manager_host="developer",
        config=config,
    )


async def wait_for(check, seconds: float = 2.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition did not come true")


async def test_start_creates_the_container_with_the_limits(tmp_path) -> None:
    client = FakeClient()
    recorder = Recorder()
    settings = settings_for(tmp_path, worker={"memory": "1g", "cpus": 1.5})
    runtime = DockerRuntime(recorder, settings, client=client, poll_s=0.01)
    client.containers.next_statuses = ["running"] * 1000
    name = await runtime.start(TASK)
    assert name == "dev-worker-12345678"
    [(image, kwargs)] = client.containers.created
    assert image == "example/worker:1"
    assert kwargs["environment"] == {
        "TASK_ID": TASK["task_id"],
        "MANAGER_URL": "http://developer:8001",
        "GIT_PROXY_URL": "http://task:tok-value@developer:8002",
        "TASK_TOKEN": "tok-value",
    }
    assert kwargs["labels"] == {
        "joshua-addon": "developer",
        "joshua-developer-instance": "developer",
        "task-id": TASK["task_id"],
    }
    assert kwargs["network"] == "developer_workers"
    assert kwargs["mem_limit"] == "1g"
    assert kwargs["nano_cpus"] == 1_500_000_000
    assert kwargs["pids_limit"] == 512
    assert kwargs["auto_remove"] is False
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["read_only"] is True
    # The worker writes to /work and /tmp only: anonymous volumes, removed with it.
    assert [(m["Target"], m["Type"], m["Source"]) for m in kwargs["mounts"]] == [
        ("/work", "volume", ""),
        ("/tmp", "volume", ""),
    ]
    assert kwargs["mounts"][0]["VolumeOptions"]["Labels"] == {
        "joshua-addon": "developer",
        "joshua-developer-instance": "developer",
    }
    assert "storage_opt" not in kwargs
    assert client.network_names == []
    assert client.containers.made[0].started
    # The task starts running when the worker reads its brief, not here.
    assert recorder.running == []
    for job in list(runtime._supervisors):
        job.cancel()


async def test_network_on_joins_the_public_network_and_a_missing_image_is_pulled(
    tmp_path,
) -> None:
    client = FakeClient()
    client.containers.missing_image = True
    runtime = DockerRuntime(Recorder(), settings_for(tmp_path, network="on"), client=client)
    client.containers.next_statuses = ["exited"]
    await runtime.start(TASK)
    assert client.pulled == ["example/worker:1"]
    assert client.network_names == ["bridge"]
    assert client.network.connected == client.containers.made


async def test_a_failed_start_removes_the_container(tmp_path) -> None:
    client = FakeClient()

    def refuse(name: str) -> FakeNetwork:
        raise APIError("no such network")

    client.get = refuse  # type: ignore[method-assign]
    runtime = DockerRuntime(Recorder(), settings_for(tmp_path, network="on"), client=client)
    with pytest.raises(APIError):
        await runtime.start(TASK)
    assert client.containers.made[0].removed
    assert not client.containers.made[0].started


async def test_count_active_counts_the_running_labelled_containers(tmp_path) -> None:
    client = FakeClient()
    client.containers.running = [FakeContainer("a", []), FakeContainer("b", [])]
    runtime = DockerRuntime(Recorder(), settings_for(tmp_path), client=client)
    assert runtime.count_active() == 2
    assert client.containers.listed == [
        {
            "filters": {
                "label": ["joshua-addon=developer", "joshua-developer-instance=developer"],
                "status": "running",
            }
        }
    ]


async def test_an_exit_without_a_report_records_failed_with_the_log_tail(tmp_path) -> None:
    client = FakeClient()
    recorder = Recorder()
    client.containers.next_statuses = ["running", "exited"]
    runtime = DockerRuntime(recorder, settings_for(tmp_path), client=client, poll_s=0.01)
    await runtime.start(TASK)
    await wait_for(lambda: client.containers.made[0].removed)
    assert client.containers.made[0].removed_volumes
    [(task_id, report)] = recorder.reports
    assert task_id == TASK["task_id"]
    assert report.status == "failed"
    assert report.error == "the worker exited with code 1 and sent no report"
    assert len(report.log) == 4000 and report.log.endswith("the end")
    assert "tok-value" not in (report.error or "")


async def test_an_exit_after_a_report_records_nothing(tmp_path) -> None:
    client = FakeClient()
    recorder = Recorder()
    recorder.active = False
    client.containers.next_statuses = ["exited"]
    runtime = DockerRuntime(recorder, settings_for(tmp_path), client=client, poll_s=0.01)
    await runtime.start(TASK)
    await wait_for(lambda: client.containers.made[0].removed)
    assert recorder.reports == []


async def test_a_worker_past_its_timeout_is_killed_and_timed_out(tmp_path) -> None:
    client = FakeClient()
    recorder = Recorder()
    killed_at_report: list[bool] = []
    record = recorder.record_report

    async def check_order(task_id: str, report: Report) -> dict:
        killed_at_report.append(client.containers.made[0].killed)
        return await record(task_id, report)

    recorder.record_report = check_order  # type: ignore[method-assign]
    client.containers.next_statuses = ["running"] * 10_000
    runtime = DockerRuntime(
        recorder, settings_for(tmp_path), client=client, grace_s=-10_000, poll_s=0.01
    )
    await runtime.start({**TASK, "persona": "retired"})
    await wait_for(lambda: client.containers.made[0].removed)
    container = client.containers.made[0]
    assert container.killed
    # The kill came before the timed_out status was recorded.
    assert killed_at_report == [True]
    [(_, report)] = recorder.reports
    assert report.status == "timed_out"


async def test_a_worker_that_never_reads_its_brief_fails_after_the_start_grace(tmp_path) -> None:
    from datetime import timedelta

    t0 = utcnow()
    now = [t0]
    client = FakeClient()
    recorder = Recorder()
    recorder.clock = TaskClock(started_at=None)
    client.containers.next_statuses = ["running"] * 10_000
    settings = settings_for(tmp_path)
    runtime = DockerRuntime(recorder, settings, client=client, poll_s=0.01, now=lambda: now[0])
    await runtime.start(TASK)
    container = client.containers.made[0]
    # Far past the persona timeout, but the clock has not started.
    now[0] = t0 + timedelta(seconds=settings.worker_start_grace_s - 1)
    await asyncio.sleep(0.05)
    assert not container.killed
    now[0] = t0 + timedelta(seconds=settings.worker_start_grace_s)
    await wait_for(lambda: container.removed)
    assert container.killed
    [(_, report)] = recorder.reports
    assert report.status == "failed"
    assert "did not start in 600 seconds" in (report.error or "")
    assert report.log.endswith("the end")


async def test_the_clock_starts_at_the_brief_and_not_at_the_container(tmp_path) -> None:
    from datetime import timedelta

    t0 = utcnow()
    now = [t0]
    client = FakeClient()
    recorder = Recorder()
    recorder.clock = TaskClock(started_at=None)
    client.containers.next_statuses = ["running"] * 10_000
    runtime = DockerRuntime(
        recorder, settings_for(tmp_path), client=client, poll_s=0.01, now=lambda: now[0]
    )
    await runtime.start(TASK)
    container = client.containers.made[0]
    # The image pull takes 500 s; then the worker reads its brief.
    recorder.clock = TaskClock(started_at=t0 + timedelta(seconds=500))
    # The sonnet persona: 1200 s, plus 120 s of grace, from the brief.
    now[0] = t0 + timedelta(seconds=500 + 1319)
    await asyncio.sleep(0.05)
    assert not container.killed
    now[0] = t0 + timedelta(seconds=500 + 1320)
    await wait_for(lambda: container.removed)
    [(_, report)] = recorder.reports
    assert report.status == "timed_out"


async def test_a_container_that_is_gone_and_a_broken_log(tmp_path) -> None:
    client = FakeClient()
    recorder = Recorder()
    runtime = DockerRuntime(recorder, settings_for(tmp_path), client=client, poll_s=0.01)
    client.containers.next_statuses = ["running"] * 1000
    await runtime.start(TASK)
    container = client.containers.made[0]

    def broken_logs(**kwargs: Any) -> bytes:
        raise APIError("logs refused")

    container.logs = broken_logs  # type: ignore[method-assign]
    container.gone = True
    await wait_for(lambda: container.removed)
    [(_, report)] = recorder.reports
    assert report.status == "failed"
    assert report.log == ""


async def test_a_supervisor_error_is_logged_and_the_container_removed(tmp_path, caplog) -> None:
    client = FakeClient()
    recorder = Recorder()

    async def broken(task_id: str, report: Report) -> dict:
        raise RuntimeError("store closed")

    recorder.record_report = broken  # type: ignore[method-assign]
    client.containers.next_statuses = ["exited"]
    runtime = DockerRuntime(recorder, settings_for(tmp_path), client=client, poll_s=0.01)
    await runtime.start(TASK)
    await wait_for(lambda: client.containers.made[0].removed)
    assert "the supervisor of a worker failed" in caplog.text


def test_orphans_are_removed(tmp_path) -> None:
    client = FakeClient()
    stuck = FakeContainer("stuck", [])

    def refuse(force: bool = False, v: bool = False) -> None:
        raise APIError("busy")

    stuck.remove = refuse  # type: ignore[method-assign]
    old = FakeContainer("old", [])
    client.containers.running = [old, stuck]
    runtime = DockerRuntime(Recorder(), settings_for(tmp_path), client=client)
    assert runtime.remove_orphans() == 1
    assert old.removed and old.removed_volumes
    # Only the workers of this instance: another install on the daemon has
    # another MANAGER_HOST, so another label.
    assert client.containers.listed[-1] == {
        "all": True,
        "filters": {"label": ["joshua-addon=developer", "joshua-developer-instance=developer"]},
    }
    client.containers.running = []
    assert runtime.remove_orphans() == 0


def test_the_runtime_needs_an_image(tmp_path) -> None:
    with pytest.raises(ValueError, match="WORKER_IMAGE"):
        DockerRuntime(Recorder(), make_settings(tmp_path), client=FakeClient())


def test_make_client_uses_docker_host(tmp_path, monkeypatch) -> None:
    made: list[Any] = []
    monkeypatch.setattr(
        runtime_docker.docker, "DockerClient", lambda base_url: made.append(base_url) or "c1"
    )
    monkeypatch.setattr(runtime_docker.docker, "from_env", lambda: "c2")
    with_host = make_settings(tmp_path, docker_host="tcp://docker-socket-proxy:2375")
    assert make_client(with_host) == "c1"
    assert made == ["tcp://docker-socket-proxy:2375"]
    assert make_client(make_settings(tmp_path)) == "c2"


def test_worker_environment_uses_the_manager_host(tmp_path) -> None:
    settings = make_settings(tmp_path, manager_host="mgr")
    env = worker_environment(settings, TASK)
    assert env["MANAGER_URL"] == "http://mgr:8001"
    assert env["GIT_PROXY_URL"] == "http://task:tok-value@mgr:8002"
    # No test aid reaches a worker unless WORKER_FAKE_SESSION is set.
    assert set(env) == {"TASK_ID", "MANAGER_URL", "GIT_PROXY_URL", "TASK_TOKEN"}


@pytest.mark.parametrize("mode", ["1", "ask"])
def test_worker_environment_forwards_the_fake_session_switch(tmp_path, mode: str) -> None:
    settings = make_settings(tmp_path, worker_fake_session=mode)
    env = worker_environment(settings, TASK)
    assert env["JOSHUA_WORKER_FAKE_SESSION"] == mode


def test_the_runtime_warns_when_the_fake_session_switch_is_set(tmp_path, caplog) -> None:
    settings = make_settings(
        tmp_path, worker_runtime="docker", worker_image="example/worker:1", worker_fake_session="1"
    )
    with caplog.at_level(logging.WARNING):
        DockerRuntime(Recorder(), settings, client=FakeClient())
    text = " ".join(str(record.msg) for record in caplog.records)
    assert "WORKER_FAKE_SESSION is set" in text


def test_server_start_builds_the_docker_runtime(tmp_path, monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(runtime_docker.docker, "from_env", lambda: client)
    manager = server.start(settings_for(tmp_path))
    assert isinstance(manager.runtime, DockerRuntime)
    assert server.worker_app is not None and server.git_proxy is not None
    assert client.containers.listed[-1]["all"] is True
    # The manager pulls the worker image at start, not on the first task.
    assert client.pulled == ["example/worker:1"]


def test_server_start_refuses_a_runtime_it_does_not_have(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="the podman runtime is not in this release"):
        server.start(make_settings(tmp_path, worker_runtime="podman"))


async def test_the_deadline_moves_while_the_worker_waits_for_an_answer(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    now = [t0 + timedelta(seconds=1400)]
    client = FakeClient()
    recorder = Recorder()
    # The sonnet persona: timeout_s 1200, and the grace time 120 s. With no
    # wait, the deadline is 1320 s after the start. 200 s of waits that ended
    # move it to 1520 s.
    recorder.clock = TaskClock(started_at=t0, paused_s=200)
    client.containers.next_statuses = ["running"] * 10_000
    runtime = DockerRuntime(
        recorder, settings_for(tmp_path), client=client, poll_s=0.01, now=lambda: now[0]
    )
    await runtime.start(TASK)
    container = client.containers.made[0]
    await asyncio.sleep(0.05)
    assert not container.killed
    # An open wait, started at 1000 s, moves it further while it lasts.
    recorder.clock = TaskClock(started_at=t0, paused_s=0, asked_at=t0 + timedelta(seconds=1000))
    now[0] = t0 + timedelta(seconds=5000)
    await asyncio.sleep(0.05)
    assert not container.killed
    # The answer comes: the wait of 4000 s is in paused_s, and 5000 s is before 5320 s.
    recorder.clock = TaskClock(started_at=t0, paused_s=4000)
    await asyncio.sleep(0.05)
    assert not container.killed
    now[0] = t0 + timedelta(seconds=5320)
    await wait_for(lambda: container.removed)
    assert container.killed
    [(_, report)] = recorder.reports
    assert report.status == "timed_out"


def test_a_failed_pull_at_start_is_logged_and_not_fatal(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    client = FakeClient()
    client.pull_error = APIError("registry down")
    runtime = DockerRuntime(Recorder(), settings_for(tmp_path), client=client)
    assert runtime.pull_image() is False
    assert "a task pulls it if missing" in caplog.text
    client.pull_error = None
    assert runtime.pull_image() is True
    assert "pulled the worker image" in caplog.text


async def test_two_instances_label_their_workers_apart(tmp_path) -> None:
    client = FakeClient()
    client.containers.next_statuses = ["running"] * 1000
    other = make_settings(
        tmp_path, worker_image="example/worker:1", worker_runtime="docker", manager_host="dev2"
    )
    runtime = DockerRuntime(Recorder(), other, client=client, poll_s=0.01)
    await runtime.start(TASK)
    [(_, kwargs)] = client.containers.created
    assert kwargs["labels"]["joshua-developer-instance"] == "dev2"
    runtime.remove_orphans()
    assert client.containers.listed[-1]["filters"]["label"] == [
        "joshua-addon=developer",
        "joshua-developer-instance=dev2",
    ]
    for job in list(runtime._supervisors):
        job.cancel()


async def test_storage_opt_sets_the_disk_limit_when_it_is_on(tmp_path) -> None:
    client = FakeClient()
    client.containers.next_statuses = ["running"] * 1000
    settings = settings_for(tmp_path, worker={"disk": "8Gi", "disk_docker_storage_opt": True})
    runtime = DockerRuntime(Recorder(), settings, client=client, poll_s=0.01)
    await runtime.start(TASK)
    [(_, kwargs)] = client.containers.created
    assert kwargs["storage_opt"] == {"size": "8Gi"}
    # The size limit covers the writable layer, so the folders stay in it.
    assert "mounts" not in kwargs
    assert "read_only" not in kwargs
    assert kwargs["pids_limit"] == 512
    for job in list(runtime._supervisors):
        job.cancel()
