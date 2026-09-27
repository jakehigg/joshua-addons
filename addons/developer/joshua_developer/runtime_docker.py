"""The Docker runtime: one container for each task, through the Docker API.

The manager reaches Docker at ``DOCKER_HOST``. On compose, that is a socket
proxy that allows the container, image, and network calls only.

A worker container gets four variables and nothing else:

    TASK_ID          the task id
    MANAGER_URL      http://<MANAGER_HOST>:8001
    GIT_PROXY_URL    http://task:<TASK_TOKEN>@<MANAGER_HOST>:8002
    TASK_TOKEN       the task token

It joins ``WORKER_NETWORK``, where the manager is the only other member. With
``network: on`` in developer.yaml it also joins ``PUBLIC_NETWORK``. The
container has the ``worker`` memory and CPU limits, no Linux capabilities,
and ``no-new-privileges``.

A supervisor watches each container. When the container exits and the task
has no report, it records a ``failed`` report with the end of the container
log. When the persona timeout plus a grace time passes, it kills the
container and, if no report came, records ``timed_out``. Then it removes the
container.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from joshua_developer.config import GIT_PROXY_PORT, WORKER_API_PORT, Settings
from joshua_developer.log import get_logger
from joshua_developer.runtime import Report, Reporter

logger = get_logger("joshua_developer.runtime_docker")

LABEL = "joshua-addon"
LABEL_VALUE = "developer"
TASK_LABEL = "task-id"
GRACE_S = 120
POLL_S = 5.0
LOG_TAIL_CHARS = 4000


def make_client(settings: Settings) -> docker.DockerClient:
    """A Docker client for ``DOCKER_HOST``, or for the SDK default when it is empty."""
    if settings.docker_host:
        return docker.DockerClient(base_url=settings.docker_host)
    return docker.from_env()


def worker_environment(settings: Settings, task: dict[str, Any]) -> dict[str, str]:
    """The environment of the worker for ``task`` (the full row)."""
    token = task["worker_token"]
    host = settings.manager_host
    return {
        "TASK_ID": task["task_id"],
        "MANAGER_URL": f"http://{host}:{WORKER_API_PORT}",
        "GIT_PROXY_URL": f"http://task:{token}@{host}:{GIT_PROXY_PORT}",
        "TASK_TOKEN": token,
    }


class DockerRuntime:
    """Start one worker container for each task, and watch it."""

    def __init__(
        self,
        reporter: Reporter,
        settings: Settings,
        client: docker.DockerClient | None = None,
        grace_s: float = GRACE_S,
        poll_s: float = POLL_S,
    ) -> None:
        if not settings.worker_image:
            raise ValueError("WORKER_IMAGE is required for the docker runtime")
        self.reporter = reporter
        self.settings = settings
        self.client = client if client is not None else make_client(settings)
        self.grace_s = grace_s
        self.poll_s = poll_s
        self._supervisors: set[asyncio.Task[None]] = set()

    def count_active(self) -> int:
        running = self.client.containers.list(
            filters={"label": f"{LABEL}={LABEL_VALUE}", "status": "running"}
        )
        return len(running)

    def remove_orphans(self) -> int:
        """Remove every worker container from an earlier run. Returns the count.

        A manager start fails every task that was active, so no old worker
        has a task left.
        """
        removed = 0
        for container in self.client.containers.list(
            all=True, filters={"label": f"{LABEL}={LABEL_VALUE}"}
        ):
            try:
                container.remove(force=True)
                removed += 1
            except DockerException as exc:
                logger.warning(
                    {"message": "could not remove an old worker", "error_type": type(exc).__name__}
                )
        if removed:
            logger.info({"message": "removed old workers", "count": removed})
        return removed

    def _timeout_s(self, task: dict[str, Any]) -> float:
        personas = self.settings.config.personas
        persona = personas.get(task["persona"]) or personas[self.settings.config.default_persona]
        return persona.timeout_s + self.grace_s

    def _create(self, task: dict[str, Any]) -> Any:
        limits = self.settings.config.worker
        kwargs: dict[str, Any] = {
            "name": f"dev-worker-{task['task_id'][:8]}",
            "environment": worker_environment(self.settings, task),
            "labels": {LABEL: LABEL_VALUE, TASK_LABEL: task["task_id"]},
            "network": self.settings.worker_network,
            "mem_limit": limits.memory,
            "nano_cpus": int(limits.cpus * 1_000_000_000),
            "auto_remove": False,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
        }
        image = self.settings.worker_image
        try:
            container = self.client.containers.create(image, **kwargs)
        except ImageNotFound:
            logger.info({"message": "pulling the worker image", "image": image})
            self.client.images.pull(image)
            container = self.client.containers.create(image, **kwargs)
        try:
            if self.settings.config.network == "on":
                self.client.networks.get(self.settings.public_network).connect(container)
            container.start()
        except BaseException:
            _remove(container)
            raise
        return container

    async def start(self, task: dict[str, Any]) -> str:
        container = await asyncio.to_thread(self._create, task)
        self.reporter.mark_running(task["task_id"])
        logger.info(
            {"message": "worker started", "task_id": task["task_id"], "container": container.name}
        )
        supervise = self._supervise(task["task_id"], container, self._timeout_s(task))
        job = asyncio.create_task(supervise)
        self._supervisors.add(job)
        job.add_done_callback(self._supervisors.discard)
        return str(container.name)

    async def _supervise(self, task_id: str, container: Any, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        timed_out = False
        try:
            while True:
                try:
                    await asyncio.to_thread(container.reload)
                except NotFound:
                    break
                if container.status in ("exited", "dead"):
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    logger.warning({"message": "worker ran past its timeout", "task_id": task_id})
                    await asyncio.to_thread(_kill, container)
                    break
                await asyncio.sleep(self.poll_s)
            if self.reporter.is_active(task_id):
                await self.reporter.record_report(task_id, self._report(container, timed_out))
        except Exception as exc:
            logger.error(
                {
                    "message": "the supervisor of a worker failed",
                    "task_id": task_id,
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            await asyncio.to_thread(_remove, container)

    def _report(self, container: Any, timed_out: bool) -> Report:
        tail = _log_tail(container)
        if timed_out:
            return Report(
                status="timed_out",
                error="the worker ran past the persona timeout and sent no report",
                log=tail,
            )
        code = container.attrs.get("State", {}).get("ExitCode")
        return Report(
            status="failed",
            error=f"the worker exited with code {code} and sent no report",
            log=tail,
        )


def _log_tail(container: Any) -> str:
    try:
        raw = container.logs(stdout=True, stderr=True, tail=1000)
    except DockerException:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return text[-LOG_TAIL_CHARS:]


def _kill(container: Any) -> None:
    try:
        container.kill()
    except DockerException:
        pass


def _remove(container: Any) -> None:
    try:
        container.remove(force=True)
    except DockerException:
        pass
