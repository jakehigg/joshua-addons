"""The Docker runtime: one container for each task, through the Docker API.

The manager reaches Docker at ``DOCKER_HOST``. On compose, that is a socket
proxy that allows the container, image, and network calls only.

A worker container gets four variables and nothing else:

    TASK_ID          the task id
    MANAGER_URL      http://<MANAGER_HOST>:8001
    GIT_PROXY_URL    http://task:<TASK_TOKEN>@<MANAGER_HOST>:8002
    TASK_TOKEN       the task token

The one exception is a test aid: when ``WORKER_FAKE_SESSION`` is set, the
worker also gets ``JOSHUA_WORKER_FAKE_SESSION`` with the same value, and runs
a scripted session instead of Claude Code.

It joins ``WORKER_NETWORK``, where the manager is the only other member. With
``network: on`` in developer.yaml it also joins ``PUBLIC_NETWORK``. The
container has the ``worker`` memory and CPU limits, at most ``PIDS_LIMIT``
processes, no Linux capabilities, ``no-new-privileges``, and a read-only
root file system. ``/work`` and ``/tmp`` are anonymous volumes that the
removal of the container deletes. Docker has no disk limit for them. With
``worker.disk_docker_storage_opt: true``, the container instead gets
``storage_opt`` ``size`` = ``worker.disk`` and keeps ``/work`` and ``/tmp`` in
its writable layer, so its root file system is not read-only; the storage
driver must support ``storage_opt`` (overlay2 on xfs with ``pquota``).

Each worker container has the label ``joshua-developer-instance`` with the
value ``MANAGER_HOST``. The orphan removal and ``count_active`` touch only the
containers of this instance, so two installs on one Docker daemon never
touch each other's workers.

The manager pulls the worker image when it starts. A task pulls the image
only when it is still missing.

A supervisor watches each container. When the container exits and the task
has no report, it records a ``failed`` report with the end of the container
log. When the deadline passes, it kills the container and then, if no report
came, records ``timed_out``. Then it removes the container.

The deadline is the time the worker read its brief, plus the persona timeout,
plus a grace time, plus the time the worker waited for answers
(``runtime.Deadline``). The supervisor reads it from the task row on each
poll. ``timeout_s + ask_wait_s + grace`` after that is a hard cap. A worker
that has not read its brief ``WORKER_START_GRACE_S`` seconds after its start
is killed, and its task fails.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import Mount

from joshua_developer.config import GIT_PROXY_PORT, WORKER_API_PORT, Settings
from joshua_developer.log import get_logger
from joshua_developer.runtime import Deadline, Report, Reporter, no_start_report, worker_name
from joshua_developer.store import utcnow

logger = get_logger("joshua_developer.runtime_docker")

LABEL = "joshua-addon"
LABEL_VALUE = "developer"
TASK_LABEL = "task-id"
INSTANCE_LABEL = "joshua-developer-instance"
PIDS_LIMIT = 512
# The folders a worker writes to: the checkout and $HOME, and the temp files.
WRITABLE_PATHS = ("/work", "/tmp")
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
    env = {
        "TASK_ID": task["task_id"],
        "MANAGER_URL": f"http://{host}:{WORKER_API_PORT}",
        "GIT_PROXY_URL": f"http://task:{token}@{host}:{GIT_PROXY_PORT}",
        "TASK_TOKEN": token,
    }
    if settings.worker_fake_session:
        env["JOSHUA_WORKER_FAKE_SESSION"] = settings.worker_fake_session
    return env


class DockerRuntime:
    """Start one worker container for each task, and watch it."""

    def __init__(
        self,
        reporter: Reporter,
        settings: Settings,
        client: docker.DockerClient | None = None,
        grace_s: float = GRACE_S,
        poll_s: float = POLL_S,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if not settings.worker_image:
            raise ValueError("WORKER_IMAGE is required for the docker runtime")
        if settings.worker_fake_session:
            logger.warning(
                {
                    "message": "WORKER_FAKE_SESSION is set: workers run a scripted session, "
                    "not Claude Code. This is a test aid.",
                    "mode": settings.worker_fake_session,
                }
            )
        self.reporter = reporter
        self.settings = settings
        self.client = client if client is not None else make_client(settings)
        self.grace_s = grace_s
        self.poll_s = poll_s
        self.now = now
        self._supervisors: set[asyncio.Task[None]] = set()

    @property
    def labels(self) -> dict[str, str]:
        """The labels every worker container of this instance has."""
        return {LABEL: LABEL_VALUE, INSTANCE_LABEL: self.settings.manager_host}

    def _label_filter(self) -> list[str]:
        return [f"{key}={value}" for key, value in self.labels.items()]

    def count_active(self) -> int:
        running = self.client.containers.list(
            filters={"label": self._label_filter(), "status": "running"}
        )
        return len(running)

    def pull_image(self) -> bool:
        """Pull the worker image. Returns False when the pull failed.

        A failure is not fatal: the first task pulls the image when it is
        still missing.
        """
        image = self.settings.worker_image
        logger.info({"message": "pulling the worker image", "image": image})
        try:
            self.client.images.pull(image)
        except DockerException as exc:
            logger.warning(
                {
                    "message": "the worker image was not pulled; a task pulls it if missing",
                    "image": image,
                    "error_type": type(exc).__name__,
                }
            )
            return False
        logger.info({"message": "pulled the worker image", "image": image})
        return True

    def remove_orphans(self) -> int:
        """Remove every worker container of this instance from an earlier run.

        Returns the count. A manager start fails every task that was active,
        so no old worker has a task left. A container of another instance
        on the same daemon has another ``joshua-developer-instance`` label
        and stays.
        """
        removed = 0
        for container in self.client.containers.list(
            all=True, filters={"label": self._label_filter()}
        ):
            try:
                container.remove(force=True, v=True)
                removed += 1
            except DockerException as exc:
                logger.warning(
                    {"message": "could not remove an old worker", "error_type": type(exc).__name__}
                )
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
            self.settings.worker_start_grace_s,
            now=self.now,
        )

    def _create_kwargs(self, task: dict[str, Any]) -> dict[str, Any]:
        limits = self.settings.config.worker
        kwargs: dict[str, Any] = {
            "name": worker_name(task),
            "environment": worker_environment(self.settings, task),
            "labels": {**self.labels, TASK_LABEL: task["task_id"]},
            "network": self.settings.worker_network,
            "mem_limit": limits.memory,
            "nano_cpus": int(limits.cpus * 1_000_000_000),
            "pids_limit": PIDS_LIMIT,
            "auto_remove": False,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
        }
        if limits.disk_docker_storage_opt:
            # The size limit covers the writable layer only, so the worker's
            # folders stay in it, and the root file system stays writable.
            kwargs["storage_opt"] = {"size": limits.disk}
        else:
            kwargs["read_only"] = True
            kwargs["mounts"] = [
                Mount(target=path, source="", type="volume", labels=dict(self.labels))
                for path in WRITABLE_PATHS
            ]
        return kwargs

    def _create(self, task: dict[str, Any]) -> Any:
        kwargs = self._create_kwargs(task)
        image = self.settings.worker_image
        try:
            container = self.client.containers.create(image, **kwargs)
        except ImageNotFound:
            logger.info({"message": "pulling the missing worker image", "image": image})
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
        logger.info(
            {"message": "worker started", "task_id": task["task_id"], "container": container.name}
        )
        supervise = self._supervise(
            task["task_id"], task.get("worker_token"), container, self._deadline(task)
        )
        job = asyncio.create_task(supervise)
        self._supervisors.add(job)
        job.add_done_callback(self._supervisors.discard)
        return str(container.name)

    async def _supervise(
        self, task_id: str, token: str | None, container: Any, deadline: Deadline
    ) -> None:
        stopped: str | None = None
        try:
            while True:
                try:
                    await asyncio.to_thread(container.reload)
                except NotFound:
                    break
                if container.status in ("exited", "dead"):
                    break
                stopped = deadline.check()
                if stopped is not None:
                    logger.warning(
                        {
                            "message": "worker ran past its timeout"
                            if stopped == "deadline"
                            else "worker did not start in time",
                            "task_id": task_id,
                        }
                    )
                    # The kill comes before the report, so the worker cannot
                    # push after the status is final.
                    await asyncio.to_thread(_kill, container)
                    break
                await asyncio.sleep(self.poll_s)
            if self.reporter.is_active(task_id, token):
                await self.reporter.record_report(task_id, self._report(container, stopped))
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

    def _report(self, container: Any, stopped: str | None) -> Report:
        tail = _log_tail(container)
        if stopped == "no_start":
            return no_start_report(self.settings.worker_start_grace_s, tail)
        if stopped == "deadline":
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
    """Remove the container and its anonymous volumes."""
    try:
        container.remove(force=True, v=True)
    except DockerException:
        pass
