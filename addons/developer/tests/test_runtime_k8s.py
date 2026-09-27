"""The Kubernetes runtime, with fake API clients: no cluster runs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import make_settings
from joshua_developer import runtime_k8s, server
from joshua_developer.runtime import Report
from joshua_developer.runtime_k8s import (
    KubernetesRuntime,
    cpu_quantity,
    job_manifest,
    load_clients,
    memory_quantity,
    pod_namespace,
)
from kubernetes.client.exceptions import ApiException

TASK = {
    "task_id": "12345678-aaaa-bbbb-cccc-dddddddddddd",
    "persona": "sonnet",
    "worker_token": "tok-value",
}
LABELS = {
    "app.kubernetes.io/name": "joshua-addon-developer-worker",
    "joshua-addon": "developer",
    "task-id": TASK["task_id"],
}
SELECTOR = "joshua-addon=developer,app.kubernetes.io/name=joshua-addon-developer-worker"


def running() -> Any:
    return SimpleNamespace(conditions=None, succeeded=None, failed=None)


def condition(kind: str, reason: str = "", status: str = "True") -> Any:
    return SimpleNamespace(conditions=[SimpleNamespace(type=kind, status=status, reason=reason)])


def job(name: str, status: Any, task_id: str | None = None) -> Any:
    labels = {"task-id": task_id} if task_id else None
    return SimpleNamespace(metadata=SimpleNamespace(name=name, labels=labels), status=status)


class FakeBatch:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.listed: list[dict] = []
        self.jobs: list[Any] = []
        # The status each read returns, in order. The last one repeats.
        self.statuses: list[Any] = [running()]
        self.read_error: ApiException | None = None
        self.delete_error: ApiException | None = None

    def create_namespaced_job(self, namespace: str, body: dict) -> None:
        self.created.append({"namespace": namespace, "body": body})

    def read_namespaced_job(self, name: str, namespace: str) -> Any:
        if self.read_error is not None:
            raise self.read_error
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return job(name, status)

    def list_namespaced_job(self, namespace: str, label_selector: str) -> Any:
        self.listed.append({"namespace": namespace, "label_selector": label_selector})
        return SimpleNamespace(items=list(self.jobs))

    def delete_namespaced_job(self, name: str, namespace: str, propagation_policy: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append((name, namespace, propagation_policy))


class FakeCore:
    def __init__(self) -> None:
        self.pods = [SimpleNamespace(metadata=SimpleNamespace(name="dev-worker-12345678-abcde"))]
        self.log = "x" * 5000 + "the end"
        self.log_error: ApiException | None = None
        self.log_calls: list[dict] = []
        self.pod_selectors: list[str] = []

    def list_namespaced_pod(self, namespace: str, label_selector: str) -> Any:
        self.pod_selectors.append(label_selector)
        return SimpleNamespace(items=list(self.pods))

    def read_namespaced_pod_log(self, **kwargs: Any) -> str:
        self.log_calls.append(kwargs)
        if self.log_error is not None:
            raise self.log_error
        return self.log


class Recorder:
    def __init__(self) -> None:
        self.running: list[str] = []
        self.reports: list[tuple[str, Report]] = []
        self.active = True
        self.active_ids: set[str] | None = None

    def is_active(self, task_id: str, worker_token: str | None = None) -> bool:
        if self.active_ids is not None:
            return task_id in self.active_ids
        return self.active

    def mark_running(self, task_id: str) -> None:
        self.running.append(task_id)

    async def record_report(self, task_id: str, report: Report) -> dict:
        self.reports.append((task_id, report))
        self.active = False
        return {}


def settings_for(tmp_path, **overrides) -> Any:
    config = overrides.pop("config", {})
    return make_settings(
        tmp_path,
        worker_image="example/worker:1",
        worker_runtime="kubernetes",
        manager_host="developer",
        config=config,
        **overrides,
    )


def make_runtime(
    tmp_path, recorder=None, **kwargs
) -> tuple[KubernetesRuntime, FakeBatch, FakeCore]:
    batch, core = FakeBatch(), FakeCore()
    settings = kwargs.pop("settings", None) or settings_for(tmp_path)
    runtime = KubernetesRuntime(
        recorder or Recorder(),
        settings,
        batch=batch,
        core=core,
        namespace="joshua",
        poll_s=kwargs.pop("poll_s", 0.01),
        **kwargs,
    )
    return runtime, batch, core


async def wait_for(check, seconds: float = 2.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition did not come true")


def test_the_job_manifest_has_every_security_field(tmp_path) -> None:
    settings = settings_for(tmp_path, config={"worker": {"memory": "1g", "cpus": 1.5}})
    manifest = job_manifest(settings, TASK, "joshua", 1320)
    assert manifest["apiVersion"] == "batch/v1" and manifest["kind"] == "Job"
    assert manifest["metadata"] == {
        "name": "dev-worker-12345678",
        "namespace": "joshua",
        "labels": LABELS,
    }
    spec = manifest["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 1320
    assert spec["ttlSecondsAfterFinished"] == 3600
    assert spec["template"]["metadata"]["labels"] == LABELS
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert "imagePullSecrets" not in pod
    assert "serviceAccountName" not in pod
    [container] = pod["containers"]
    assert container["image"] == "example/worker:1"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert container["env"] == [
        {"name": "TASK_ID", "value": TASK["task_id"]},
        {"name": "MANAGER_URL", "value": "http://developer:8001"},
        {"name": "GIT_PROXY_URL", "value": "http://task:tok-value@developer:8002"},
        {"name": "TASK_TOKEN", "value": "tok-value"},
    ]
    assert "envFrom" not in container
    assert container["resources"] == {
        "requests": {"memory": "1Gi", "cpu": "1500m"},
        "limits": {"memory": "1Gi", "cpu": "1500m"},
    }
    assert container["volumeMounts"] == [
        {"name": "work", "mountPath": "/work"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    assert pod["volumes"] == [
        {"name": "work", "emptyDir": {}},
        {"name": "tmp", "emptyDir": {}},
    ]


def test_the_pull_secret_goes_on_the_pod_when_it_is_set(tmp_path) -> None:
    settings = settings_for(tmp_path, worker_image_pull_secret="ghcr-pull")
    pod = job_manifest(settings, TASK, "joshua", 60)["spec"]["template"]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "ghcr-pull"}]


@pytest.mark.parametrize(
    ("memory", "quantity"),
    [("2g", "2Gi"), ("512m", "512Mi"), ("1024k", "1024Ki"), ("100b", "100"), ("4096", "4096")],
)
def test_memory_quantity(memory: str, quantity: str) -> None:
    assert memory_quantity(memory) == quantity


def test_cpu_quantity() -> None:
    assert cpu_quantity(2.0) == "2000m"
    assert cpu_quantity(0.25) == "250m"


def test_the_namespace_comes_from_pod_namespace_then_the_service_account(tmp_path) -> None:
    file = tmp_path / "namespace"
    assert pod_namespace(settings_for(tmp_path, pod_namespace="joshua"), file) == "joshua"
    assert pod_namespace(settings_for(tmp_path), file) == "default"
    file.write_text("home\n")
    assert pod_namespace(settings_for(tmp_path), file) == "home"


async def test_start_creates_the_job_and_returns_its_name(tmp_path) -> None:
    recorder = Recorder()
    runtime, batch, _ = make_runtime(tmp_path, recorder)
    name = await runtime.start(TASK)
    assert name == "dev-worker-12345678"
    [created] = batch.created
    assert created["namespace"] == "joshua"
    assert created["body"]["metadata"]["name"] == name
    # The sonnet persona has timeout_s 1200; the grace time is 120 s.
    assert created["body"]["spec"]["activeDeadlineSeconds"] == 1320
    assert recorder.running == [TASK["task_id"]]
    for supervisor in list(runtime._supervisors):
        supervisor.cancel()


async def test_count_active_counts_the_jobs_that_have_not_ended(tmp_path) -> None:
    runtime, batch, _ = make_runtime(tmp_path)
    batch.jobs = [
        job("a", running()),
        job("b", condition("Complete")),
        job("c", condition("Failed", "BackoffLimitExceeded")),
        job("d", condition("Failed", status="False")),
        job("e", SimpleNamespace(conditions=None, succeeded=1, failed=None)),
        job("f", SimpleNamespace(conditions=None, succeeded=None, failed=1)),
    ]
    assert runtime.count_active() == 2
    assert batch.listed == [{"namespace": "joshua", "label_selector": SELECTOR}]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (condition("Failed", "BackoffLimitExceeded"), "the worker failed and sent no report"),
        (condition("Complete"), "the worker exited with code 0 and sent no report"),
    ],
)
async def test_an_end_without_a_report_records_failed_with_the_log_tail(
    tmp_path, status: Any, error: str
) -> None:
    recorder = Recorder()
    runtime, batch, core = make_runtime(tmp_path, recorder)
    batch.statuses = [running(), status]
    await runtime.start(TASK)
    await wait_for(lambda: batch.deleted)
    [(task_id, report)] = recorder.reports
    assert task_id == TASK["task_id"]
    assert report.status == "failed"
    assert report.error == error
    assert len(report.log) == 4000 and report.log.endswith("the end")
    assert "tok-value" not in (report.error or "")
    assert core.pod_selectors == ["job-name=dev-worker-12345678"]
    assert core.log_calls == [
        {"name": "dev-worker-12345678-abcde", "namespace": "joshua", "tail_lines": 1000}
    ]
    assert batch.deleted == [("dev-worker-12345678", "joshua", "Background")]


async def test_an_end_after_a_report_records_nothing_and_deletes_the_job(tmp_path) -> None:
    recorder = Recorder()
    recorder.active = False
    runtime, batch, core = make_runtime(tmp_path, recorder)
    batch.statuses = [condition("Complete")]
    await runtime.start(TASK)
    await wait_for(lambda: batch.deleted)
    assert recorder.reports == []
    assert core.log_calls == []


async def test_a_worker_past_its_timeout_is_deleted_and_timed_out(tmp_path) -> None:
    recorder = Recorder()
    runtime, batch, _ = make_runtime(tmp_path, recorder, grace_s=-10_000)
    await runtime.start({**TASK, "persona": "retired"})
    await wait_for(lambda: batch.deleted)
    [(_, report)] = recorder.reports
    assert report.status == "timed_out"
    assert report.log.endswith("the end")
    assert batch.deleted == [("dev-worker-12345678", "joshua", "Background")]
    # The Job deadline is never below one second.
    assert batch.created[0]["body"]["spec"]["activeDeadlineSeconds"] == 1


async def test_the_job_deadline_of_kubernetes_is_timed_out(tmp_path) -> None:
    recorder = Recorder()
    runtime, batch, _ = make_runtime(tmp_path, recorder)
    batch.statuses = [condition("Failed", "DeadlineExceeded")]
    await runtime.start(TASK)
    await wait_for(lambda: batch.deleted)
    [(_, report)] = recorder.reports
    assert report.status == "timed_out"


async def test_a_job_that_is_gone_and_a_broken_log(tmp_path) -> None:
    recorder = Recorder()
    runtime, batch, core = make_runtime(tmp_path, recorder)
    batch.read_error = ApiException(status=404)
    core.log_error = ApiException(status=403)
    await runtime.start(TASK)
    await wait_for(lambda: batch.deleted)
    [(_, report)] = recorder.reports
    assert report.status == "failed"
    assert report.error == "the worker job was deleted before the worker sent a report"
    assert report.log == ""


async def test_no_pod_gives_an_empty_log(tmp_path) -> None:
    recorder = Recorder()
    runtime, batch, core = make_runtime(tmp_path, recorder)
    core.pods = []
    batch.statuses = [condition("Failed", "BackoffLimitExceeded")]
    await runtime.start(TASK)
    await wait_for(lambda: batch.deleted)
    [(_, report)] = recorder.reports
    assert report.log == ""


async def test_a_supervisor_error_is_logged_and_the_job_deleted(tmp_path, caplog) -> None:
    runtime, batch, _ = make_runtime(tmp_path)
    batch.read_error = ApiException(status=500)
    await runtime.start(TASK)
    await wait_for(lambda: batch.deleted)
    assert "the supervisor of a worker failed" in caplog.text


def test_orphans_are_deleted_and_an_active_task_keeps_its_job(tmp_path) -> None:
    recorder = Recorder()
    recorder.active_ids = {"live-task"}
    runtime, batch, _ = make_runtime(tmp_path, recorder)
    batch.jobs = [
        job("old", condition("Complete"), task_id="old-task"),
        job("live", running(), task_id="live-task"),
        job("unlabelled", running()),
    ]
    assert runtime.remove_orphans() == 2
    assert [name for name, _, _ in batch.deleted] == ["old", "unlabelled"]
    assert batch.listed[-1] == {"namespace": "joshua", "label_selector": SELECTOR}
    batch.jobs = []
    assert runtime.remove_orphans() == 0


def test_a_refused_delete_is_not_counted(tmp_path, caplog) -> None:
    recorder = Recorder()
    recorder.active = False
    runtime, batch, _ = make_runtime(tmp_path, recorder)
    batch.jobs = [job("stuck", running(), task_id="t")]
    batch.delete_error = ApiException(status=403)
    assert runtime.remove_orphans() == 0
    assert "could not delete a worker job" in caplog.text
    batch.delete_error = ApiException(status=404)
    assert runtime.remove_orphans() == 1


def test_the_runtime_needs_an_image(tmp_path) -> None:
    with pytest.raises(ValueError, match="WORKER_IMAGE"):
        KubernetesRuntime(Recorder(), make_settings(tmp_path), batch=FakeBatch(), core=FakeCore())


def test_load_clients_falls_back_to_the_kubeconfig(monkeypatch) -> None:
    calls: list[str] = []

    def no_cluster() -> None:
        calls.append("incluster")
        raise runtime_k8s.k8s_config.ConfigException("not in a cluster")

    monkeypatch.setattr(runtime_k8s.k8s_config, "load_incluster_config", no_cluster)
    monkeypatch.setattr(
        runtime_k8s.k8s_config, "load_kube_config", lambda: calls.append("kubeconfig")
    )
    monkeypatch.setattr(runtime_k8s.k8s_client, "BatchV1Api", lambda: "batch")
    monkeypatch.setattr(runtime_k8s.k8s_client, "CoreV1Api", lambda: "core")
    assert load_clients() == ("batch", "core")
    assert calls == ["incluster", "kubeconfig"]


def test_server_start_builds_the_kubernetes_runtime(tmp_path, monkeypatch) -> None:
    batch, core = FakeBatch(), FakeCore()
    monkeypatch.setattr(runtime_k8s, "load_clients", lambda: (batch, core))
    manager = server.start(settings_for(tmp_path, pod_namespace="joshua"))
    assert isinstance(manager.runtime, KubernetesRuntime)
    assert manager.runtime.namespace == "joshua"
    assert batch.listed == [{"namespace": "joshua", "label_selector": SELECTOR}]
