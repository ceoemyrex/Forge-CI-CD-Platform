"""Forge CI engine — pipeline submission, scheduling, and execution."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tarfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, Optional

import requests
import yaml
from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

import config
from engine.logs import LogStreamer
from engine.parser import ParseError, PipelineParser
from engine.runner import DockerJobRunner
from engine.scheduler import DAGScheduler, JobStatus
from engine import slack
from registry.auth import require_auth

logger = logging.getLogger(__name__)

app = FastAPI(title="Forge CI/CD Platform")

log_streamer = LogStreamer(storage_root=config.STORAGE_ROOT)
job_runner = DockerJobRunner(
    registry_url=config.REGISTRY_URL,
    storage_root=config.STORAGE_ROOT,
    max_job_duration_sec=config.MAX_JOB_DURATION_SEC,
    network_name=config.JOB_NETWORK,
    data_volume_name=config.DATA_VOLUME_NAME,
)

run_storage: Dict[str, "RunState"] = {}


class IntegrityError(Exception):
    def __init__(self, name: str, version: str, expected: str, actual: str):
        self.name = name
        self.version = version
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"integrity failure for {name}@{version}: "
            f"expected {expected}, got {actual}"
        )


class RunState:
    def __init__(self, run_id: str, pipeline_config: dict, submitter: str = "unknown"):
        self.run_id = run_id
        self.pipeline_config = pipeline_config
        self.submitter = submitter
        self.status = "queued"
        self.jobs: dict = {}
        self.lockfile = None
        self.forge_token: Optional[str] = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.started_at = None
        self.completed_at = None
        self.error = None

    def to_dict(self):
        return {
            "status": self.status,
            "jobs": self.jobs,
            "lockfile_url": f"/runs/{self.run_id}/lockfile",
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }


def _auth(authorization: str = Header(None)) -> str:
    try:
        return require_auth(authorization)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/runs")
async def submit_pipeline(
    background_tasks: BackgroundTasks,
    pipeline: UploadFile = File(...),
    authorization: str = Header(None),
):
    submitter = _auth(authorization)
    try:
        pipeline_yaml = await pipeline.read()
        parser = PipelineParser()
        validated_config = parser.parse_and_validate(pipeline_yaml)

        run_id = str(uuid.uuid4())[:8]
        run_state = RunState(run_id, validated_config, submitter)
        run_state.forge_token = authorization.split(" ", 1)[1] if authorization else None
        run_storage[run_id] = run_state

        log_streamer.register_status_checker(run_id, lambda: run_storage[run_id].status)

        pipeline_name = validated_config.get("name", "unknown")
        slack.pipeline_started(pipeline_name, run_id, submitter)
        background_tasks.add_task(execute_pipeline, run_id)
        return {"run_id": run_id}

    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error submitting pipeline: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/runs/{run_id}")
async def get_run_status(run_id: str):
    if run_id not in run_storage:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run_storage[run_id].to_dict()


@app.get("/runs/{run_id}/lockfile")
async def get_lockfile(run_id: str):
    if run_id not in run_storage:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    run_state = run_storage[run_id]
    if run_state.lockfile is None:
        raise HTTPException(status_code=404, detail="Lockfile not available yet")
    return run_state.lockfile


@app.get("/runs/{run_id}/logs")
async def stream_logs(run_id: str, follow: bool = False):
    if run_id not in run_storage:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return StreamingResponse(
        log_streamer.stream_sse(run_id, follow=follow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def execute_pipeline(run_id: str) -> None:
    run_state = run_storage[run_id]
    pipeline_config = run_state.pipeline_config
    pipeline_name = pipeline_config.get("name", "unknown")
    start_ts = datetime.now(timezone.utc)

    try:
        from registry.resolver import (
            ConflictError,
            CycleError,
            DependencyResolver,
            ResolutionError,
        )

        resolver = DependencyResolver(registry_url=config.REGISTRY_URL)
        try:
            lockfile = resolver.resolve(pipeline_config)
            run_state.lockfile = lockfile
            lockfile_path = os.path.join(config.STORAGE_ROOT, "runs", run_id, "lockfile.json")
            os.makedirs(os.path.dirname(lockfile_path), exist_ok=True)
            with open(lockfile_path, "w", encoding="utf-8") as f:
                json.dump(lockfile, f, sort_keys=True, separators=(",", ":"))
        except CycleError as exc:
            run_state.status = "cycle_failure"
            run_state.error = str(exc)
            slack.resolution_failure(pipeline_name, run_id, str(exc))
            return
        except (ConflictError, ResolutionError) as exc:
            run_state.status = "conflict_failure"
            run_state.error = str(exc)
            slack.resolution_failure(pipeline_name, run_id, str(exc))
            return

        jobs_config = pipeline_config.get("jobs", {})
        try:
            scheduler = DAGScheduler(jobs_config, concurrency_limit=config.CONCURRENCY_LIMIT)
        except ValueError as exc:
            run_state.status = "cycle_failure"
            run_state.error = str(exc)
            slack.resolution_failure(pipeline_name, run_id, str(exc))
            return

        execution_plan = scheduler.get_execution_plan()
        run_state.status = "running"
        run_state.started_at = datetime.now(timezone.utc).isoformat()

        integrity_failed = False
        for jobs_in_level in execution_plan:
            if integrity_failed:
                for job_name in jobs_in_level:
                    if job_name not in run_state.jobs:
                        run_state.jobs[job_name] = {"status": "skipped", "exit_code": None}
                continue

            with ThreadPoolExecutor(max_workers=config.CONCURRENCY_LIMIT) as executor:
                futures = {}
                for job_name in jobs_in_level:
                    if scheduler.get_job_status(job_name) == JobStatus.SKIPPED:
                        run_state.jobs[job_name] = {"status": "skipped", "exit_code": None}
                        continue

                    job_config = jobs_config[job_name]
                    futures[
                        executor.submit(
                            execute_job,
                            run_id,
                            job_name,
                            job_config,
                            pipeline_config,
                            run_state.lockfile,
                            run_state.forge_token,
                        )
                    ] = job_name

                for future in as_completed(futures):
                    job_name = futures[future]
                    try:
                        exit_code, status, artifacts = future.result()
                    except IntegrityError as exc:
                        integrity_failed = True
                        msg = (
                            f"integrity failure pulling {exc.name}@{exc.version}: "
                            f"expected sha256:{exc.expected}, actual sha256:{exc.actual}"
                        )
                        log_streamer.write(run_id, job_name, msg)
                        slack.integrity_failure(
                            exc.name, exc.version, exc.expected, exc.actual, run_id
                        )
                        run_state.jobs[job_name] = {
                            "status": "integrity_failure",
                            "exit_code": 1,
                        }
                        scheduler.mark_job_failed(job_name)
                        continue

                    run_state.jobs[job_name] = {
                        "status": status,
                        "exit_code": exit_code,
                        "artifacts": artifacts,
                    }
                    scheduler.update_job_status(job_name, JobStatus(status))

                    if status == "failed":
                        scheduler.mark_job_failed(job_name)

                    for jname, jstatus in scheduler.get_all_statuses().items():
                        if jstatus == "skipped" and jname not in run_state.jobs:
                            run_state.jobs[jname] = {"status": "skipped", "exit_code": None}

        all_statuses = [j.get("status") for j in run_state.jobs.values()]
        if integrity_failed or "integrity_failure" in all_statuses:
            run_state.status = "integrity_failure"
        elif any(s == "failed" for s in all_statuses):
            run_state.status = "failed"
        else:
            run_state.status = "succeeded"

        run_state.completed_at = datetime.now(timezone.utc).isoformat()
        duration = (datetime.now(timezone.utc) - start_ts).total_seconds()

        if run_state.status == "succeeded":
            slack.pipeline_succeeded(pipeline_name, run_id, duration)
        elif run_state.status in ("failed", "integrity_failure"):
            failing = next(
                (n for n, j in run_state.jobs.items() if j.get("status") in ("failed", "integrity_failure")),
                None,
            )
            slack.pipeline_failed(pipeline_name, run_id, duration, failing)

    except Exception as exc:
        logger.error("[%s] Unexpected error: %s", run_id, exc)
        run_state.status = "failed"
        run_state.error = str(exc)
    finally:
        log_streamer.unregister_status_checker(run_id)


def _prepare_job_dir(path: str) -> None:
    """Job containers run as uid 1000; engine creates dirs as root."""
    os.makedirs(path, exist_ok=True)
    os.chown(path, 1000, 1000)
    os.chmod(path, 0o755)


def execute_job(
    run_id: str,
    job_name: str,
    job_config: dict,
    pipeline_config: dict,
    lockfile: dict,
    forge_token: Optional[str],
) -> tuple:
    workspace_path = os.path.join(config.STORAGE_ROOT, "runs", run_id, "workspace")
    deps_path = os.path.join(workspace_path, "deps")
    _prepare_job_dir(workspace_path)
    _prepare_job_dir(deps_path)

    log_streamer.write(run_id, job_name, f"Starting job {job_name}")

    pull_deps(lockfile, deps_path, run_id, job_name)

    def on_log(line: str) -> None:
        log_streamer.write(run_id, job_name, line)

    result = job_runner.run_job(
        job_name=job_name,
        job_config=job_config,
        workspace_path=workspace_path,
        deps_path=deps_path,
        forge_token=forge_token or "",
        log_callback=on_log,
    )

    published = []
    if result.exit_code == 0:
        for artifact in pipeline_config.get("artifacts", []):
            rel_path = artifact.get("path", "").lstrip("./")
            artifact_path = os.path.join(workspace_path, rel_path)
            if os.path.exists(artifact_path):
                pub = publish_artifact(
                    artifact_path,
                    artifact["name"],
                    artifact["version"],
                    forge_token,
                    pipeline_config.get("dependencies", []),
                )
                published.append(pub)
                log_streamer.write(
                    run_id, job_name, f"Published {artifact['name']}@{artifact['version']}"
                )

    return result.exit_code, result.status, published


def pull_deps(lockfile: dict, deps_path: str, run_id: str, job_name: str) -> None:
    from registry.resolver import lockfile_as_deps_dict

    deps = lockfile_as_deps_dict(lockfile)
    for dep_name, dep_info in deps.items():
        dep_version = dep_info["version"]
        expected_sha256 = dep_info["sha256"]

        url = f"{config.REGISTRY_URL}/artifacts/{dep_name}/{dep_version}"
        resp = requests.get(url, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(
                f"failed to fetch {dep_name}@{dep_version}: HTTP {resp.status_code}"
            )

        data = resp.content
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != expected_sha256:
            log_streamer.write(
                run_id,
                job_name,
                f"Checksum mismatch for {dep_name}@{dep_version}: "
                f"expected sha256:{expected_sha256}, actual sha256:{actual_sha256}",
            )
            raise IntegrityError(dep_name, dep_version, expected_sha256, actual_sha256)

        dest = os.path.join(deps_path, dep_name)
        os.makedirs(dest, exist_ok=True)
        _extract_artifact(data, dest)
        log_streamer.write(run_id, job_name, f"Pulled {dep_name}@{dep_version}")


def _extract_artifact(data: bytes, dest: str) -> None:
    if data[:2] == b"\x1f\x8b":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(dest)
        return
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(dest)
        return
    with open(os.path.join(dest, "artifact.bin"), "wb") as f:
        f.write(data)


def publish_artifact(
    path: str,
    name: str,
    version: str,
    token: Optional[str],
    declared_deps: list,
) -> dict:
    with open(path, "rb") as f:
        data = f.read()
    sha256 = hashlib.sha256(data).hexdigest()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.post(
        f"{config.REGISTRY_URL}/artifacts/{name}/{version}",
        headers=headers,
        files={"file": (os.path.basename(path), data)},
        data={"checksum": f"sha256:{sha256}", "deps": json.dumps(declared_deps)},
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"publish failed: {resp.status_code} {resp.text}")
    return {"name": name, "version": version, "sha256": sha256}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.ENGINE_HOST, port=config.ENGINE_PORT)
