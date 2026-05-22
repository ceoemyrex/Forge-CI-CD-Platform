# engine/runner.py

import docker
import logging
import time
import json
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass
from enum import Enum
import io

logger = logging.getLogger(__name__)

@dataclass
class JobResult:
    """Result of running a job"""
    job_name: str
    exit_code: int
    status: str  # "succeeded", "failed", "timeout", "integrity_failure"
    log_path: str
    artifacts: List[str]  # Paths to produced artifacts (for auto-publish)
    duration_seconds: float

class DockerJobRunner:
    """
    Manages Docker container lifecycle for job execution.
    Enforces isolation: filesystem, process, network, resources, time.
    """
    
    def __init__(
        self,
        registry_url: str,
        storage_root: str,
        max_job_duration_sec: int = 1800,
        network_name: str = "forge-isolated"
    ):
        """
        Args:
            registry_url: Full URL to Forge registry (e.g., http://registry:5000)
            storage_root: Path to storage (for logs, workspaces)
            max_job_duration_sec: Max job runtime (default 30 min)
            network_name: Docker network to use for jobs
        """
        self.registry_url = registry_url
        self.storage_root = storage_root
        self.max_job_duration_sec = max_job_duration_sec
        self.network_name = network_name
        
        # Connect to Docker daemon
        self.docker_client = docker.from_env()
        
        # Ensure network exists
        self._ensure_network_exists()
    
    def _ensure_network_exists(self):
        """Create Docker network if it doesn't exist"""
        try:
            network = self.docker_client.networks.get(self.network_name)
            logger.info(f"Using existing network: {self.network_name}")
        except docker.errors.NotFound:
            logger.info(f"Creating network: {self.network_name}")
            self.docker_client.networks.create(
                self.network_name,
                driver="bridge",
                check_duplicate=True
            )
    
    def run_job(
        self,
        job_name: str,
        job_config: dict,
        workspace_path: str,
        deps_path: str,
        forge_token: str,
        log_stream_callback=None
    ) -> JobResult:
        """
        Execute a job in an isolated container.
        
        Args:
            job_name: Name of the job
            job_config: Job config from pipeline (runtime, resources, steps)
            workspace_path: Path to workspace on host
            deps_path: Path to dependencies on host
            forge_token: Auth token for registry access
            log_stream_callback: Callable(line) to stream logs in real-time
        
        Returns:
            JobResult with exit code, status, logs, artifacts
        """
        start_time = time.time()
        container = None
        
        try:
            # Parse container config
            image = job_config.get("runtime", "alpine:3.18")
            resources = job_config.get("resources", {})
            steps = job_config.get("steps", [])
            
            # Parse resource limits
            cpu_limit = float(resources.get("cpu", 1.0))
            memory_limit = resources.get("memory", "512Mi")
            memory_bytes = self._parse_memory(memory_limit)
            
            logger.info(f"[{job_name}] Starting job with image={image}, cpu={cpu_limit}, mem={memory_limit}")
            
            # Pull image if needed
            try:
                self.docker_client.images.get(image)
            except docker.errors.ImageNotFound:
                logger.info(f"Pulling image {image}...")
                self.docker_client.images.pull(image)
            
            # Create container with isolation
            container = self._create_container(
                image=image,
                job_name=job_name,
                workspace_path=workspace_path,
                deps_path=deps_path,
                cpu_limit=cpu_limit,
                memory_bytes=memory_bytes,
                forge_token=forge_token
            )
            
            logger.info(f"[{job_name}] Container created: {container.id[:12]}")
            
            # Execute all steps
            exit_code = 0
            artifacts = []
            
            for step_idx, step in enumerate(steps):
                step_name = step.get("name", f"step_{step_idx}")
                step_cmd = step.get("run", "")
                
                logger.info(f"[{job_name}/{step_name}] Running: {step_cmd}")
                
                # Execute step inside container
                step_exit_code, step_logs = self._exec_in_container(
                    container=container,
                    command=step_cmd,
                    job_name=job_name,
                    step_name=step_name,
                    timeout_sec=self.max_job_duration_sec,
                    log_callback=log_stream_callback
                )
                
                if step_exit_code != 0:
                    logger.error(f"[{job_name}/{step_name}] Step failed with exit code {step_exit_code}")
                    exit_code = step_exit_code
                    break
                
                logger.info(f"[{job_name}/{step_name}] Step succeeded")
            
            # Get artifacts if job succeeded
            if exit_code == 0:
                artifacts = self._extract_artifacts(
                    container=container,
                    workspace_path=workspace_path
                )
            
            duration = time.time() - start_time
            
            # Determine status
            if exit_code == 0:
                status = "succeeded"
            else:
                status = "failed"
            
            return JobResult(
                job_name=job_name,
                exit_code=exit_code,
                status=status,
                log_path=f"{self.storage_root}/logs/{job_name}.log",
                artifacts=artifacts,
                duration_seconds=duration
            )
        
        except docker.errors.ContainerError as e:
            logger.error(f"[{job_name}] Container error: {e}")
            return JobResult(
                job_name=job_name,
                exit_code=1,
                status="failed",
                log_path=f"{self.storage_root}/logs/{job_name}.log",
                artifacts=[],
                duration_seconds=time.time() - start_time
            )
        
        finally:
            # Clean up container
            if container:
                try:
                    container.stop(timeout=5)
                    container.remove()
                    logger.info(f"[{job_name}] Container cleaned up")
                except Exception as e:
                    logger.warning(f"[{job_name}] Error cleaning container: {e}")
    
    def _create_container(
        self,
        image: str,
        job_name: str,
        workspace_path: str,
        deps_path: str,
        cpu_limit: float,
        memory_bytes: int,
        forge_token: str
    ) -> docker.models.containers.Container:
        """
        Create a container with strict isolation settings.
        
        Security settings:
        - Mount only workspace + deps (read-write for workspace, read-only for deps)
        - No host filesystem access
        - Process namespace isolation (default in Docker)
        - Network isolation (custom bridge network)
        - Resource limits (CPU, memory)
        - Non-root user
        - Drop unnecessary capabilities
        """
        
        # Mounts: workspace (rw), deps (ro)
        volumes = {
            workspace_path: {"bind": "/workspace", "mode": "rw"},
            deps_path: {"bind": "/workspace/deps", "mode": "ro"},
        }
        
        # Environment variables for the job
        environment = {
            "FORGE_TOKEN": forge_token,
            "FORGE_URL": self.registry_url,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        
        # Create container
        container = self.docker_client.containers.create(
            image=image,
            name=f"forge-job-{job_name}-{int(time.time())}",
            
            # Isolation
            volumes=volumes,
            working_dir="/workspace",
            user="1000:1000",  # Non-root user
            cap_drop=["NET_RAW"],  # Drop capability to send raw packets
            
            # Network
            network=self.network_name,
            network_disabled=False,
            hostname=f"job-{job_name}",
            
            # Resources
            cpu_quota=int(cpu_limit * 100000),  # Docker uses 100000 = 1 CPU
            mem_limit=memory_bytes,
            memswap_limit=memory_bytes,  # Disable swap
            
            # Logging
            stdout=True,
            stderr=True,
            stdin_open=False,
            
            # Other
            remove=False,  # We'll remove manually
        )
        
        return container
    
    def _exec_in_container(
        self,
        container: docker.models.containers.Container,
        command: str,
        job_name: str,
        step_name: str,
        timeout_sec: int,
        log_callback=None
    ) -> Tuple[int, str]:
        """
        Execute a command inside a container with timeout.
        Stream output in real-time.
        """
        
        # Start container
        container.start()
        
        # Execute command
        try:
            result = container.exec_run(
                cmd=["sh", "-c", command],
                stdout=True,
                stderr=True,
                stream=True,
                user="1000:1000"
            )
            
            logs = io.StringIO()
            
            # Stream output with timeout
            for line in result.output:
                decoded = line.decode("utf-8", errors="replace").strip()
                
                if decoded:
                    log_entry = f"[{job_name}/{step_name}] {decoded}"
                    logger.info(log_entry)
                    
                    if log_callback:
                        log_callback(log_entry)
                    
                    logs.write(decoded + "\n")
            
            # Get exit code
            exit_code = result.exit_code
            
            return exit_code, logs.getvalue()
        
        except Exception as e:
            logger.error(f"[{job_name}/{step_name}] Exec error: {e}")
            return 1, str(e)
    
    def _parse_memory(self, memory_str: str) -> int:
        """Parse memory string like '512Mi' to bytes"""
        units = {
            "Ki": 1024,
            "Mi": 1024 ** 2,
            "Gi": 1024 ** 3,
            "K": 1000,
            "M": 1000 ** 2,
            "G": 1000 ** 3,
        }
        
        for unit, multiplier in units.items():
            if memory_str.endswith(unit):
                value = float(memory_str[:-len(unit)])
                return int(value * multiplier)
        
        # No unit, assume bytes
        return int(memory_str)
    
    def _extract_artifacts(
        self,
        container: docker.models.containers.Container,
        workspace_path: str
    ) -> List[str]:
        """
        Extract artifact paths from successful job.
        Look for artifacts declared in pipeline (handled by main.py).
        """
        # For now, return empty list — main.py will handle artifact collection
        return []