# engine/scheduler.py

from dataclasses import dataclass
from typing import Dict, List, Set, Optional
from enum import Enum

class JobStatus(Enum):
    """Job execution states"""
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass
class JobNode:
    """Represents a job in the DAG"""
    name: str
    config: dict  # From pipeline YAML
    needs: List[str]  # Job names this depends on
    dependents: List[str] = None  # Job names that depend on this (computed)
    status: JobStatus = JobStatus.QUEUED
    
    def __post_init__(self):
        if self.dependents is None:
            self.dependents = []

class DAGScheduler:
    """
    Builds a DAG from pipeline jobs, detects cycles, sorts topologically,
    and executes jobs respecting dependencies and concurrency limits.
    """
    
    def __init__(self, jobs_config: Dict[str, dict], concurrency_limit: int = 4):
        """
        Args:
            jobs_config: Dict of {job_name: job_config} from pipeline
            concurrency_limit: Max jobs to run in parallel
        """
        self.jobs_config = jobs_config
        self.concurrency_limit = concurrency_limit
        self.graph: Dict[str, JobNode] = {}
        self.sorted_jobs: List[str] = []
        self.job_statuses: Dict[str, JobStatus] = {}
        
        # Build graph and detect issues before running
        self._build_graph()
        self._compute_dependents()
        self._detect_cycles()
        self._topological_sort()
    
    def _build_graph(self):
        """Convert job configs into graph nodes"""
        for job_name, config in self.jobs_config.items():
            needs = config.get("needs", [])
            
            # Validate that all dependencies exist
            for dep in needs:
                if dep not in self.jobs_config:
                    raise ValueError(
                        f"Job '{job_name}' declares need '{dep}', "
                        f"but '{dep}' is not defined in pipeline"
                    )
            
            self.graph[job_name] = JobNode(
                name=job_name,
                config=config,
                needs=needs
            )
            self.job_statuses[job_name] = JobStatus.QUEUED
    
    def _detect_cycles(self):
        """Detect cycles by DFS over needs edges."""
        visiting: Set[str] = set()
        visited: Set[str] = set()
        cycles_found: List[str] = []

        def dfs(node: str, path: List[str]) -> None:
            if node in visiting:
                cycle_start = path.index(node)
                cycle = path[cycle_start:] + [node]
                cycles_found.append(" → ".join(cycle))
                return
            if node in visited:
                return

            visiting.add(node)
            path.append(node)
            for dep in self.graph[node].needs:
                dfs(dep, path)
            path.pop()
            visiting.discard(node)
            visited.add(node)

        for job in self.graph:
            if job not in visited:
                dfs(job, [])

        if cycles_found:
            raise ValueError(f"Dependency cycles detected: {', '.join(cycles_found)}")
    
    def _compute_dependents(self):
        """
        For each job, compute which jobs depend on it.
        This reverses the 'needs' relationship.
        """
        for job_name, node in self.graph.items():
            for dep in node.needs:
                self.graph[dep].dependents.append(job_name)
    
    def _topological_sort(self):
        """
        Sort jobs topologically using Kahn's algorithm.
        Result: self.sorted_jobs is a valid execution order.
        """
        # Count in-degrees (number of dependencies each job has)
        in_degree = {job: len(node.needs) for job, node in self.graph.items()}
        
        # Start with jobs that have no dependencies
        queue = [job for job in self.graph if in_degree[job] == 0]
        self.sorted_jobs = []
        
        while queue:
            # Process jobs in order (could randomize for fairness, but determinism is good)
            job = queue.pop(0)
            self.sorted_jobs.append(job)
            
            # Reduce in-degree for all dependents
            for dependent in self.graph[job].dependents:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        
        # If we didn't process all jobs, there's a cycle (should be caught earlier)
        if len(self.sorted_jobs) != len(self.graph):
            raise ValueError("Cycle detected in DAG (should have been caught earlier)")
    
    def get_execution_plan(self) -> List[List[str]]:
        """
        Return execution plan as list of "levels" — each level is jobs that can run in parallel.
        
        Example output:
            [
                ["job_a"],           # Level 0: no dependencies
                ["job_b", "job_c"],  # Level 1: both depend only on job_a
                ["job_d"]            # Level 2: depends on job_b and job_c
            ]
        """
        levels = []
        processed = set()
        
        while len(processed) < len(self.graph):
            current_level = []
            
            for job in self.sorted_jobs:
                if job in processed:
                    continue
                
                # Check if all dependencies of this job are processed
                node = self.graph[job]
                if all(dep in processed for dep in node.needs):
                    current_level.append(job)
            
            if not current_level:
                # No jobs can run (shouldn't happen if DAG is valid)
                raise RuntimeError("Deadlock in job execution (cyclic dependency?)")
            
            levels.append(current_level)
            processed.update(current_level)
        
        return levels
    
    def mark_job_failed(self, job_name: str):
        """
        When a job fails, mark it and all its dependents (recursively) as skipped.
        """
        self.job_statuses[job_name] = JobStatus.FAILED
        
        def skip_dependents(job: str):
            for dependent in self.graph[job].dependents:
                if self.job_statuses[dependent] != JobStatus.SUCCEEDED:
                    self.job_statuses[dependent] = JobStatus.SKIPPED
                    skip_dependents(dependent)
        
        skip_dependents(job_name)
    
    def update_job_status(self, job_name: str, status: JobStatus):
        """Update status of a job"""
        self.job_statuses[job_name] = status
    
    def get_job_status(self, job_name: str) -> JobStatus:
        """Get current status of a job"""
        return self.job_statuses[job_name]
    
    def get_all_statuses(self) -> Dict[str, str]:
        """Return all job statuses as dict for API response"""
        return {job: status.value for job, status in self.job_statuses.items()}
