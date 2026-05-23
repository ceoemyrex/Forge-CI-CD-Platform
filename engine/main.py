# engine/main.py

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
import yaml
import logging
import os
import uuid
from typing import Dict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from engine.scheduler import DAGScheduler, JobStatus
from engine.runner import DockerJobRunner
from engine.parser import PipelineParser  # From Trojan (Person A gets from them)
from engine.logs import LogStreamer  # You'll implement this
from engine.slack import SlackAlertClient
import config  # Your config.yaml loader

logger = logging.getLogger(__name__)

app = FastAPI(title="Forge CI/CD Platform")

# Global state
run_storage: Dict[str, dict] = {}  # {run_id: {status, jobs, lockfile, logs}}
job_runner = None
log_streamer = LogStreamer(storage_root=config.STORAGE_ROOT)
slack_alerts = SlackAlertClient(config.SLACK_WEBHOOK_URL, config.SLACK_NOTIFY_TAGS)


def get_job_runner() -> DockerJobRunner:
    """Create the Docker runner only when a job actually starts."""
    global job_runner
    if job_runner is None:
        job_runner = DockerJobRunner(
            registry_url=config.REGISTRY_URL,
            storage_root=config.STORAGE_ROOT,
            max_job_duration_sec=config.MAX_JOB_DURATION_SEC,
        )
    return job_runner

class RunState:
    """Tracks state of a pipeline run"""
    def __init__(self, run_id: str, pipeline_config: dict):
        self.run_id = run_id
        self.pipeline_config = pipeline_config
        self.status = "queued"  # queued, running, succeeded, failed, etc.
        self.jobs = {}  # {job_name: {status, exit_code, logs_url}}
        self.lockfile = None
        self.created_at = datetime.utcnow().isoformat()
        self.started_at = None
        self.completed_at = None
        self.error = None  # For conflict/cycle errors
    
    def to_dict(self):
        return {
            "run_id": self.run_id,
            "status": self.status,
            "jobs": self.jobs,
            "lockfile_url": f"/runs/{self.run_id}/lockfile",
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }

@app.post("/runs")
async def submit_pipeline(
    pipeline: UploadFile = File(...),
    background_tasks: BackgroundTasks = None
):
    """
    POST /runs — submit a pipeline for execution.
    
    Returns: {run_id}
    """
    try:
        # Parse pipeline
        pipeline_yaml = await pipeline.read()
        pipeline_config = yaml.safe_load(pipeline_yaml)
        
        # Validate with Trojan's parser
        parser = PipelineParser()
        validated_config = parser.parse_and_validate(pipeline_config)
        
        # Create run
        run_id = str(uuid.uuid4())[:8]
        run_state = RunState(run_id, validated_config)
        run_storage[run_id] = run_state
        
        logger.info(f"Created run {run_id} for pipeline {validated_config.get('name')}")
        
        # Execute in background
        background_tasks.add_task(execute_pipeline, run_id)
        
        return {"run_id": run_id}
    
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")
    except Exception as e:
        logger.error(f"Error submitting pipeline: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/runs/{run_id}")
async def get_run_status(run_id: str):
    """
    GET /runs/{id} — get run status.
    
    Returns: {status, jobs, lockfile_url}
    """
    if run_id not in run_storage:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    
    run_state = run_storage[run_id]
    return run_state.to_dict()

@app.get("/runs/{run_id}/lockfile")
async def get_lockfile(run_id: str):
    """
    GET /runs/{id}/lockfile — get resolved lockfile.
    
    Returns: {lockfile JSON}
    """
    if run_id not in run_storage:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    
    run_state = run_storage[run_id]
    if run_state.lockfile is None:
        raise HTTPException(status_code=404, detail="Lockfile not available yet")
    
    return {"lockfile": run_state.lockfile}

@app.get("/runs/{run_id}/logs")
async def stream_logs(run_id: str, follow: bool = False):
    """
    GET /runs/{id}/logs?follow=true — stream logs over SSE.
    
    Returns: Server-Sent Events stream
    """
    if run_id not in run_storage:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    
    def is_complete():
        return run_storage[run_id].status not in ["queued", "running"]

    if follow:
        return StreamingResponse(
            log_streamer.sse_events(run_id, follow=True, is_complete=is_complete),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        log_streamer.text_events(run_id, follow=False, is_complete=is_complete),
        media_type="text/plain",
    )

def execute_pipeline(run_id: str):
    """
    Background task: execute the pipeline end-to-end.
    
    Flow:
    1. Validate pipeline (Trojan's parser)
    2. Resolve dependencies (Trojan's resolver) → lockfile
    3. Build DAG (scheduler)
    4. Execute jobs in order with concurrency limit
    5. Auto-publish artifacts
    6. Update status
    """
    run_state = run_storage[run_id]
    pipeline_config = run_state.pipeline_config
    
    try:
        # Resolve dependencies (get from Trojan)
        from engine.resolver import DependencyResolver  # Not yet implemented, but you'll integrate it
        resolver = DependencyResolver(registry_url=config.REGISTRY_URL)
        
        try:
            lockfile = resolver.resolve(pipeline_config)
            run_state.lockfile = lockfile
        except Exception as e:
            run_state.status = "conflict_failure"
            run_state.error = str(e)
            slack_alerts.resolution_failure(
                pipeline=pipeline_config.get("name", "unknown"),
                run_id=run_id,
                details=str(e),
            )
            logger.error(f"[{run_id}] Resolution failed: {e}")
            return
        
        # Build DAG
        jobs_config = pipeline_config.get("jobs", {})
        try:
            scheduler = DAGScheduler(jobs_config, concurrency_limit=config.CONCURRENCY_LIMIT)
        except ValueError as e:
            run_state.status = "cycle_failure"
            run_state.error = str(e)
            slack_alerts.resolution_failure(
                pipeline=pipeline_config.get("name", "unknown"),
                run_id=run_id,
                details=str(e),
            )
            logger.error(f"[{run_id}] Cycle detected: {e}")
            return
        
        # Get execution plan
        execution_plan = scheduler.get_execution_plan()
        
        run_state.status = "running"
        run_state.started_at = datetime.utcnow().isoformat()
        slack_alerts.pipeline_started(
            pipeline=pipeline_config.get("name", "unknown"),
            run_id=run_id,
            user="api",
        )
        
        # Execute jobs level-by-level (respecting concurrency)
        for level, jobs_in_level in enumerate(execution_plan):
            logger.info(f"[{run_id}] Executing level {level}: {jobs_in_level}")
            
            # Execute jobs in parallel (up to concurrency limit)
            with ThreadPoolExecutor(max_workers=config.CONCURRENCY_LIMIT) as executor:
                futures = {}
                
                for job_name in jobs_in_level:
                    job_config = jobs_config[job_name]
                    
                    future = executor.submit(
                        execute_job,
                        run_id=run_id,
                        job_name=job_name,
                        job_config=job_config,
                        pipeline_config=pipeline_config,
                        lockfile=lockfile
                    )
                    futures[future] = job_name
                
                # Wait for all jobs in this level to complete
                for future in as_completed(futures):
                    job_name = futures[future]
                    exit_code, status, artifacts = future.result()
                    
                    run_state.jobs[job_name] = {
                        "status": status,
                        "exit_code": exit_code,
                        "artifacts": artifacts,
                    }
                    
                    scheduler.update_job_status(job_name, JobStatus(status))
                    
                    # If job failed, mark dependents as skipped
                    if status == "failed":
                        scheduler.mark_job_failed(job_name)
        
        # Determine final status
        all_statuses = [job["status"] for job in run_state.jobs.values()]
        
        if any(s == "integrity_failure" for s in all_statuses):
            run_state.status = "integrity_failure"
        elif any(s == "failed" for s in all_statuses):
            run_state.status = "failed"
        else:
            run_state.status = "succeeded"
        
        run_state.completed_at = datetime.utcnow().isoformat()
        duration = _duration_seconds(run_state.started_at, run_state.completed_at)
        failing_job = _first_failing_job(run_state.jobs)
        if run_state.status == "succeeded":
            slack_alerts.pipeline_succeeded(
                pipeline=pipeline_config.get("name", "unknown"),
                run_id=run_id,
                duration_seconds=duration,
            )
        else:
            slack_alerts.pipeline_failed(
                pipeline=pipeline_config.get("name", "unknown"),
                run_id=run_id,
                duration_seconds=duration,
                failing_job=failing_job,
            )
        logger.info(f"[{run_id}] Pipeline completed with status: {run_state.status}")
    
    except Exception as e:
        logger.error(f"[{run_id}] Unexpected error: {e}")
        run_state.status = "failed"
        run_state.error = str(e)

def execute_job(
    run_id: str,
    job_name: str,
    job_config: dict,
    pipeline_config: dict,
    lockfile: dict
) -> tuple:
    """
    Execute a single job in a container.
    
    Returns: (exit_code, status, artifacts)
    """
    workspace_path = f"{config.STORAGE_ROOT}/runs/{run_id}/workspace"
    deps_path = f"{workspace_path}/deps"
    
    # Create workspace
    os.makedirs(workspace_path, exist_ok=True)
    os.makedirs(deps_path, exist_ok=True)
    
    logger.info(f"[{run_id}/{job_name}] Executing...")
    
    try:
        # Pull dependencies from lockfile
        pull_deps(lockfile, deps_path, run_id, job_name)
        
        # Get FORGE_TOKEN (todo: implement token creation)
        forge_token = "placeholder-token"
        
        # Run job
        result = get_job_runner().run_job(
            job_name=job_name,
            job_config=job_config,
            workspace_path=workspace_path,
            deps_path=deps_path,
            forge_token=forge_token,
            log_stream_callback=lambda line: log_streamer.write(run_id, line, job=job_name)
        )
        
        # Auto-publish artifacts
        artifacts = []
        artifacts_config = pipeline_config.get("artifacts", [])
        if result.exit_code == 0:
            for artifact in artifacts_config:
                artifact_path = f"{workspace_path}/{artifact.get('path')}"
                if os.path.exists(artifact_path):
                    # Publish to registry (todo: implement)
                    logger.info(f"[{run_id}/{job_name}] Publishing {artifact['name']}:{artifact['version']}")
                    artifacts.append({
                        "name": artifact["name"],
                        "version": artifact["version"],
                        "path": artifact_path,
                    })
        
        return result.exit_code, result.status, artifacts
    
    except Exception as e:
        logger.error(f"[{run_id}/{job_name}] Error: {e}")
        return 1, "failed", []

def pull_deps(lockfile: dict, deps_path: str, run_id: str, job_name: str):
    """
    Pull dependencies from registry into workspace.
    Verify SHA-256 against lockfile.
    """
    deps = lockfile.get("dependencies", {})
    
    for dep_name, dep_info in deps.items():
        dep_version = dep_info["version"]
        expected_sha256 = dep_info["sha256"]
        
        logger.info(f"[{run_id}/{job_name}] Pulling {dep_name}:{dep_version}")
        
        # Fetch from registry (todo: implement)
        # Verify checksum
        # Extract to {deps_path}/{dep_name}/
        
        pass


def _duration_seconds(started_at: str, completed_at: str) -> float:
    if not started_at or not completed_at:
        return 0.0
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(completed_at)
    return round((end - start).total_seconds(), 2)


def _first_failing_job(jobs: dict) -> str:
    for job_name, job in jobs.items():
        if job.get("status") not in ["succeeded", "skipped"]:
            return job_name
    return "unknown"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
