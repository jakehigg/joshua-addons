"""The Docker runtime, with a fake Docker client: no Docker runs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import make_settings
from docker.errors import APIError, ImageNotFound, NotFound
from joshua_developer import runtime_docker, server
from joshua_developer.runtime import Report
from joshua_developer.runtime_docker import DockerRuntime, make_client, worker_environment

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

    def remove(self, force: bool = False) -> None:
        self.removed = True

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
        self.images = self
        self.networks = self

    def pull(self, image: str) -> None:
        self.pulled.append(image)

    def get(self, name: str) -> FakeNetwork:
        self.network_names.append(name)
        return self.network


class Recorder:
    def __init__(self) -> None:
        self.running: list[str] = []
        self.reports: list[tuple[str, Report]] = []
        self.active = True

    def is_active(self, task_id: str) -> bool:
        return self.active

    def mark_running(self, task_id: str) -> None:
        self.running.append(task_id)

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
    assert kwargs["labels"] == {"joshua-addon": "developer", "task-id": TASK["task_id"]}
    assert kwargs["network"] == "developer_workers"
    assert kwargs["mem_limit"] == "1g"
    assert kwargs["nano_cpus"] == 1_500_000_000
    assert kwargs["auto_remove"] is False
    assert kwargs["cap_drop"] == ["ALL"]
    assert client.network_names == []
    assert client.containers.made[0].started
    assert recorder.running == [TASK["task_id"]]
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
        {"filters": {"label": "joshua-addon=developer", "status": "running"}}
    ]


async def test_an_exit_without_a_report_records_failed_with_the_log_tail(tmp_path) -> None:
    client = FakeClient()
    recorder = Recorder()
    client.containers.next_statuses = ["running", "exited"]
    runtime = DockerRuntime(recorder, settings_for(tmp_path), client=client, poll_s=0.01)
    await runtime.start(TASK)
    await wait_for(lambda: client.containers.made[0].removed)
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
    client.containers.next_statuses = ["running"] * 10_000
    runtime = DockerRuntime(
        recorder, settings_for(tmp_path), client=client, grace_s=-10_000, poll_s=0.01
    )
    await runtime.start({**TASK, "persona": "retired"})
    await wait_for(lambda: client.containers.made[0].removed)
    container = client.containers.made[0]
    assert container.killed
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

    def refuse(force: bool = False) -> None:
        raise APIError("busy")

    stuck.remove = refuse  # type: ignore[method-assign]
    old = FakeContainer("old", [])
    client.containers.running = [old, stuck]
    runtime = DockerRuntime(Recorder(), settings_for(tmp_path), client=client)
    assert runtime.remove_orphans() == 1
    assert old.removed
    assert client.containers.listed[-1] == {
        "all": True,
        "filters": {"label": "joshua-addon=developer"},
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


def test_server_start_builds_the_docker_runtime(tmp_path, monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(runtime_docker.docker, "from_env", lambda: client)
    manager = server.start(settings_for(tmp_path))
    assert isinstance(manager.runtime, DockerRuntime)
    assert server.worker_app is not None and server.git_proxy is not None
    assert client.containers.listed[-1]["all"] is True


def test_server_start_refuses_a_runtime_it_does_not_have(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="the podman runtime is not in this release"):
        server.start(make_settings(tmp_path, worker_runtime="podman"))
