"""Docker-based job runner with filesystem, process, network, and resource isolation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import docker

logger = logging.getLogger(__name__)


@dataclass
class JobResult:
    job_name: str
    exit_code: int
    status: str
    log_path: str
    artifacts: List[str]
    duration_seconds: float


class DockerJobRunner:
    """Run pipeline jobs in isolated Docker containers."""

    def __init__(
        self,
        registry_url: str,
        storage_root: str,
        max_job_duration_sec: int = 1800,
        network_name: str = "forge_jobs",
    ):
        self.registry_url = registry_url
        self.storage_root = storage_root
        self.max_job_duration_sec = max_job_duration_sec
        self.network_name = network_name
        self.docker_client = docker.from_env()
        self._ensure_network_exists()

    def _ensure_network_exists(self) -> None:
        try:
            self.docker_client.networks.get(self.network_name)
        except docker.errors.NotFound:
            self.docker_client.networks.create(
                self.network_name,
                driver="bridge",
                internal=True,
                check_duplicate=True,
            )

    def run_job(
        self,
        job_name: str,
        job_config: dict,
        workspace_path: str,
        deps_path: str,
        forge_token: str,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> JobResult:
        start_time = time.time()
        job_deadline = start_time + self.max_job_duration_sec
        container = None
        log_path = f"{self.storage_root}/logs/{job_name}.log"

        try:
            image = job_config.get("runtime", "alpine:3.18")
            resources = job_config.get("resources", {})
            steps = job_config.get("steps", [])

            cpu_limit = float(resources.get("cpu", 1.0))
            memory_limit = resources.get("memory", "512Mi")
            memory_bytes = self._parse_memory(memory_limit)

            try:
                self.docker_client.images.get(image)
            except docker.errors.ImageNotFound:
                logger.info("[%s] Pulling image %s", job_name, image)
                self.docker_client.images.pull(image)

            container = self._create_container(
                image=image,
                job_name=job_name,
                workspace_path=workspace_path,
                deps_path=deps_path,
                cpu_limit=cpu_limit,
                memory_bytes=memory_bytes,
                forge_token=forge_token,
            )

            # Start once; keep alive with 'sleep infinity' as entrypoint
            container.start()

            exit_code = 0
            for step_idx, step in enumerate(steps):
                step_name = step.get("name", f"step_{step_idx}")
                step_cmd = step.get("run", "")

                remaining = job_deadline - time.time()
                if remaining <= 0:
                    msg = f"[{step_name}] Job timed out after {self.max_job_duration_sec}s"
                    logger.error("[%s] %s", job_name, msg)
                    if log_callback:
                        log_callback(msg)
                    return JobResult(
                        job_name=job_name,
                        exit_code=124,
                        status="failed",
                        log_path=log_path,
                        artifacts=[],
                        duration_seconds=time.time() - start_time,
                    )

                step_exit_code, oom_killed = self._exec_step(
                    container=container,
                    command=step_cmd,
                    step_name=step_name,
                    deadline=job_deadline,
                    log_callback=log_callback,
                )

                if oom_killed:
                    msg = f"[{step_name}] OOM killed — memory limit {memory_limit} exceeded"
                    logger.error("[%s] %s", job_name, msg)
                    if log_callback:
                        log_callback(msg)
                    return JobResult(
                        job_name=job_name,
                        exit_code=137,
                        status="failed",
                        log_path=log_path,
                        artifacts=[],
                        duration_seconds=time.time() - start_time,
                    )

                if step_exit_code != 0:
                    exit_code = step_exit_code
                    break

            status = "succeeded" if exit_code == 0 else "failed"
            return JobResult(
                job_name=job_name,
                exit_code=exit_code,
                status=status,
                log_path=log_path,
                artifacts=[],
                duration_seconds=time.time() - start_time,
            )

        except docker.errors.ContainerError as exc:
            logger.error("[%s] Container error: %s", job_name, exc)
            return JobResult(
                job_name=job_name,
                exit_code=1,
                status="failed",
                log_path=log_path,
                artifacts=[],
                duration_seconds=time.time() - start_time,
            )
        finally:
            if container:
                try:
                    container.stop(timeout=5)
                    container.remove(force=True)
                except Exception as exc:
                    logger.warning("[%s] Cleanup error: %s", job_name, exc)

    def _create_container(
        self,
        image: str,
        job_name: str,
        workspace_path: str,
        deps_path: str,
        cpu_limit: float,
        memory_bytes: int,
        forge_token: str,
    ):
        volumes = {
            workspace_path: {"bind": "/workspace", "mode": "rw"},
            deps_path: {"bind": "/workspace/deps", "mode": "ro"},
        }

        environment = {
            "FORGE_TOKEN": forge_token,
            "FORGE_URL": self.registry_url,
            "HOME": "/tmp",
        }

        # Use 'sleep infinity' to keep the container alive across all steps.
        # Each step is exec'd in; the container is stopped/removed after the job.
        return self.docker_client.containers.create(
            image=image,
            name=f"forge-job-{job_name}-{int(time.time())}",
            command=["sleep", "infinity"],
            volumes=volumes,
            working_dir="/workspace",
            user="1000:1000",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=256,
            read_only=True,
            tmpfs={"/tmp": "size=64m,mode=1777"},
            network=self.network_name,
            cpu_quota=int(cpu_limit * 100000),
            cpu_period=100000,
            mem_limit=memory_bytes,
            memswap_limit=memory_bytes,
            environment=environment,
            detach=True,
        )

    def _exec_step(
        self,
        container,
        command: str,
        step_name: str,
        deadline: float,
        log_callback: Optional[Callable[[str], None]],
    ) -> Tuple[int, bool]:
        """Execute one step inside the running container; returns (exit_code, oom_killed)."""
        exec_id = self.docker_client.api.exec_create(
            container.id,
            cmd=["sh", "-c", command],
            stdout=True,
            stderr=True,
            user="1000:1000",
            workdir="/workspace",
        )
        output = self.docker_client.api.exec_start(exec_id, stream=True, demux=True)

        for stdout_chunk, stderr_chunk in output:
            if time.time() > deadline:
                container.kill()
                if log_callback:
                    log_callback(f"[{step_name}] Job timed out")
                return 124, False

            for chunk in (stdout_chunk, stderr_chunk):
                if not chunk:
                    continue
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    entry = f"[{step_name}] {line}"
                    if log_callback:
                        log_callback(entry)

        inspect = self.docker_client.api.exec_inspect(exec_id)
        exit_code = inspect.get("ExitCode") or 0

        container.reload()
        state = container.attrs.get("State", {})
        oom_killed = bool(state.get("OOMKilled"))
        if oom_killed and log_callback:
            log_callback(f"[{step_name}] OOM killed — container exceeded memory limit")

        return exit_code, oom_killed

    def _parse_memory(self, memory_str: str) -> int:
        units = {
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "K": 1000,
            "M": 1000**2,
            "G": 1000**3,
        }
        for unit, multiplier in units.items():
            if memory_str.endswith(unit):
                return int(float(memory_str[: -len(unit)]) * multiplier)
        return int(memory_str)
