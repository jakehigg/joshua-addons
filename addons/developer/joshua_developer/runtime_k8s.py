"""The Kubernetes runtime: one Job for each task, in the manager's namespace.

The manager calls the Kubernetes API with its ServiceAccount. The chart's
``rbac`` block gives it a Role in its own namespace: create, read, and delete
Jobs, read pods, and read pod logs.

A worker pod gets the same four variables as a Docker worker, and nothing
else:

    TASK_ID          the task id
    MANAGER_URL      http://<MANAGER_HOST>:8001
    GIT_PROXY_URL    http://task:<TASK_TOKEN>@<MANAGER_HOST>:8002
    TASK_TOKEN       the task token

On Kubernetes, ``MANAGER_HOST`` is the name of the manager's Service, which
is the Helm release name.

The pod runs as uid 1000 with no Linux capabilities, no privilege
escalation, and a read-only root file system. ``/work`` and ``/tmp`` are
emptyDir volumes. The pod gets no ServiceAccount token and no Service
variables. The chart's ``networkPolicy`` block limits where it can connect.

A supervisor reads the Job every ``poll_s`` seconds. When the Job ends and
the task has no report, it records a ``failed`` report with the end of the
pod log. When the deadline passes, it deletes the Job and, if no report came,
records ``timed_out``. The deadline is the same as on Docker: the persona
timeout, plus a grace time, plus the time the worker waited for answers
(``runtime.Deadline``). The Job's ``activeDeadlineSeconds`` is the hard cap,
``timeout_s + ask_wait_s + grace``, so Kubernetes stops the pod if the
manager cannot. When the supervisor ends, it deletes the Job: the log
tail is in the report, and a finished Job has no other use.
``ttlSecondsAfterFinished`` removes a Job that no supervisor deletes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from joshua_developer.config import Settings
from joshua_developer.log import get_logger
from joshua_developer.runtime import Deadline, Report, Reporter, worker_name
from joshua_developer.runtime_docker import (
    GRACE_S,
    LABEL,
    LABEL_VALUE,
    LOG_TAIL_CHARS,
    POLL_S,
    TASK_LABEL,
    worker_environment,
)
from joshua_developer.store import utcnow

logger = get_logger("joshua_developer.runtime_k8s")

NAME_LABEL = "app.kubernetes.io/name"
NAME_VALUE = "joshua-addon-developer-worker"
SELECTOR = f"{LABEL}={LABEL_VALUE},{NAME_LABEL}={NAME_VALUE}"
TTL_S = 3600
LOG_TAIL_LINES = 1000
WORKER_UID = 1000
SA_NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
DEFAULT_NAMESPACE = "default"
_MEMORY_UNITS = {"b": "", "k": "Ki", "m": "Mi", "g": "Gi"}


def memory_quantity(memory: str) -> str:
    """Change a Docker memory value (``2g``, ``512m``) to a Kubernetes quantity."""
    unit = memory[-1]
    if unit in _MEMORY_UNITS:
        return memory[:-1] + _MEMORY_UNITS[unit]
    return memory


def cpu_quantity(cpus: float) -> str:
    """Change a CPU count (``1.5``) to millicores (``1500m``)."""
    return f"{round(cpus * 1000)}m"


def pod_namespace(settings: Settings, namespace_file: Path = SA_NAMESPACE_FILE) -> str:
    """``POD_NAMESPACE``, else the namespace of the ServiceAccount, else ``default``."""
    if settings.pod_namespace:
        return settings.pod_namespace
    try:
        found = namespace_file.read_text(encoding="utf-8").strip()
    except OSError:
        found = ""
    return found or DEFAULT_NAMESPACE


def load_clients() -> tuple[Any, Any]:
    """The Batch and Core API clients: in-cluster config, else the kubeconfig."""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    return k8s_client.BatchV1Api(), k8s_client.CoreV1Api()


def job_manifest(
    settings: Settings, task: dict[str, Any], namespace: str, deadline_s: int
) -> dict[str, Any]:
    """The Job for the worker of ``task`` (the full row)."""
    labels = {NAME_LABEL: NAME_VALUE, LABEL: LABEL_VALUE, TASK_LABEL: task["task_id"]}
    limits = settings.config.worker
    quantities = {"memory": memory_quantity(limits.memory), "cpu": cpu_quantity(limits.cpus)}
    env = [{"name": k, "value": v} for k, v in worker_environment(settings, task).items()]
    pod: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": WORKER_UID,
            "fsGroup": WORKER_UID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "worker",
                "image": settings.worker_image,
                "imagePullPolicy": "IfNotPresent",
                "env": env,
                "resources": {"requests": dict(quantities), "limits": dict(quantities)},
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                },
                "volumeMounts": [
                    {"name": "work", "mountPath": "/work"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "work", "emptyDir": {}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    if settings.worker_image_pull_secret:
        pod["imagePullSecrets"] = [{"name": settings.worker_image_pull_secret}]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": worker_name(task), "namespace": namespace, "labels": labels},
        "spec": {
            # A worker that stops is reported failed, never started again: a
            # second pod would run the model twice on one task.
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline_s,
            "ttlSecondsAfterFinished": TTL_S,
            "template": {"metadata": {"labels": dict(labels)}, "spec": pod},
        },
    }


def _job_state(job: Any) -> str | None:
    """``complete``, ``failed``, ``deadline``, or None while the Job runs."""
    status = job.status
    for condition in getattr(status, "conditions", None) or []:
        if condition.status != "True":
            continue
        if condition.type == "Failed":
            return "deadline" if condition.reason == "DeadlineExceeded" else "failed"
        if condition.type == "Complete":
            return "complete"
    if getattr(status, "failed", None):
        return "failed"
    if getattr(status, "succeeded", None):
        return "complete"
    return None


class KubernetesRuntime:
    """Start one worker Job for each task, and watch it."""

    def __init__(
        self,
        reporter: Reporter,
        settings: Settings,
        batch: Any | None = None,
        core: Any | None = None,
        namespace: str | None = None,
        grace_s: float = GRACE_S,
        poll_s: float = POLL_S,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if not settings.worker_image:
            raise ValueError("WORKER_IMAGE is required for the kubernetes runtime")
        if batch is None or core is None:
            batch, core = load_clients()
        self.reporter = reporter
        self.settings = settings
        self.batch = batch
        self.core = core
        self.namespace = namespace or pod_namespace(settings)
        self.grace_s = grace_s
        self.poll_s = poll_s
        self.now = now
        self._supervisors: set[asyncio.Task[None]] = set()

    def _jobs(self) -> list[Any]:
        return list(
            self.batch.list_namespaced_job(namespace=self.namespace, label_selector=SELECTOR).items
        )

    def count_active(self) -> int:
        return sum(1 for job in self._jobs() if _job_state(job) is None)

    def remove_orphans(self) -> int:
        """Delete every worker Job whose task is not active. Returns the count.

        A manager start fails every task that was active, so a Job from an
        earlier run has no task left.
        """
        removed = 0
        for job in self._jobs():
            task_id = (job.metadata.labels or {}).get(TASK_LABEL, "")
            if task_id and self.reporter.is_active(task_id):
                continue
            if self._delete(job.metadata.name):
                removed += 1
        if removed:
            logger.info({"message": "removed old workers", "count": removed})
        return removed

    def _deadline(self, task: dict[str, Any]) -> Deadline:
        config = self.settings.config
        return Deadline(
            self.reporter,
            task["task_id"],
            config.persona_of(task["persona"]).timeout_s,
            config.ask_wait_of(task["persona"]),
            self.grace_s,
            now=self.now,
        )

    def _hard_cap_s(self, task: dict[str, Any]) -> int:
        """``timeout_s + ask_wait_s + grace``: the Job's ``activeDeadlineSeconds``."""
        config = self.settings.config
        persona = config.persona_of(task["persona"])
        return int(persona.timeout_s + config.ask_wait_of(task["persona"]) + self.grace_s)

    async def start(self, task: dict[str, Any]) -> str:
        manifest = job_manifest(self.settings, task, self.namespace, max(self._hard_cap_s(task), 1))
        name = manifest["metadata"]["name"]
        await asyncio.to_thread(
            self.batch.create_namespaced_job, namespace=self.namespace, body=manifest
        )
        self.reporter.mark_running(task["task_id"])
        logger.info({"message": "worker started", "task_id": task["task_id"], "job": name})
        job = asyncio.create_task(
            self._supervise(task["task_id"], task.get("worker_token"), name, self._deadline(task))
        )
        self._supervisors.add(job)
        job.add_done_callback(self._supervisors.discard)
        return name

    async def _supervise(
        self, task_id: str, token: str | None, name: str, deadline: Deadline
    ) -> None:
        state: str | None = None
        try:
            while True:
                try:
                    job = await asyncio.to_thread(
                        self.batch.read_namespaced_job, name=name, namespace=self.namespace
                    )
                except ApiException as exc:
                    if exc.status != 404:
                        raise
                    state = "gone"
                    break
                state = _job_state(job)
                if state is not None:
                    break
                if deadline.passed():
                    state = "deadline"
                    logger.warning({"message": "worker ran past its timeout", "task_id": task_id})
                    break
                await asyncio.sleep(self.poll_s)
            if self.reporter.is_active(task_id, token):
                tail = await asyncio.to_thread(self._log_tail, name)
                await self.reporter.record_report(task_id, _report(state, tail))
        except Exception as exc:
            logger.error(
                {
                    "message": "the supervisor of a worker failed",
                    "task_id": task_id,
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            await asyncio.to_thread(self._delete, name)

    def _log_tail(self, name: str) -> str:
        """The last characters of the pod log of the Job ``name``, or an empty string."""
        try:
            pods = self.core.list_namespaced_pod(
                namespace=self.namespace, label_selector=f"job-name={name}"
            ).items
            if not pods:
                return ""
            text = self.core.read_namespaced_pod_log(
                name=pods[-1].metadata.name, namespace=self.namespace, tail_lines=LOG_TAIL_LINES
            )
        except ApiException:
            return ""
        return str(text or "")[-LOG_TAIL_CHARS:]

    def _delete(self, name: str) -> bool:
        """Delete the Job and its pod. False when the API refuses."""
        try:
            self.batch.delete_namespaced_job(
                name=name, namespace=self.namespace, propagation_policy="Background"
            )
        except ApiException as exc:
            if exc.status == 404:
                return True
            logger.warning(
                {"message": "could not delete a worker job", "job": name, "status": exc.status}
            )
            return False
        return True


def _report(state: str | None, tail: str) -> Report:
    if state == "deadline":
        return Report(
            status="timed_out",
            error="the worker ran past the persona timeout and sent no report",
            log=tail,
        )
    if state == "complete":
        error = "the worker exited with code 0 and sent no report"
    elif state == "gone":
        error = "the worker job was deleted before the worker sent a report"
    else:
        error = "the worker failed and sent no report"
    return Report(status="failed", error=error, log=tail)
