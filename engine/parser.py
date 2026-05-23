"""
Pipeline YAML parser and strict schema validator.

Produces a typed internal dict consumed by the scheduler and resolver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Union

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

# Allowed top-level keys
TOP_LEVEL_KEYS = frozenset({"name", "version", "dependencies", "jobs", "artifacts"})
REQUIRED_TOP_LEVEL = frozenset({"name", "version", "jobs"})

# Job keys
JOB_KEYS = frozenset({"runtime", "resources", "steps", "needs"})
REQUIRED_JOB_KEYS = frozenset({"runtime", "resources", "steps"})

# Step keys
STEP_KEYS = frozenset({"name", "run"})
REQUIRED_STEP_KEYS = frozenset({"name", "run"})

# Dependency keys
DEP_KEYS = frozenset({"name", "version"})
REQUIRED_DEP_KEYS = frozenset({"name", "version"})

# Artifact keys
ARTIFACT_KEYS = frozenset({"name", "version", "path"})
REQUIRED_ARTIFACT_KEYS = frozenset({"name", "version", "path"})

# Resource keys
RESOURCE_KEYS = frozenset({"cpu", "memory"})

IMAGE_REF_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-/:@]*$")
MEMORY_PATTERN = re.compile(r"^\d+(\.\d+)?(Ki|Mi|Gi|K|M|G)?$")
PACKAGE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]*$")
SEMVER_VERSION_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
CONSTRAINT_PATTERN = re.compile(
    r"^(\^|~)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


class ParseError(Exception):
    """Raised when pipeline YAML fails validation."""

    def __init__(self, message: str, line: Optional[int] = None):
        self.line = line
        if line is not None:
            super().__init__(f"line {line}: {message}")
        else:
            super().__init__(message)


@dataclass
class PipelineStep:
    name: str
    run: str


@dataclass
class PipelineJob:
    runtime: str
    resources: Dict[str, Any]
    steps: List[PipelineStep]
    needs: List[str] = field(default_factory=list)


@dataclass
class PipelineDependency:
    name: str
    version: str


@dataclass
class PipelineArtifact:
    name: str
    version: str
    path: str


@dataclass
class Pipeline:
    name: str
    version: str
    dependencies: List[PipelineDependency]
    jobs: Dict[str, PipelineJob]
    artifacts: List[PipelineArtifact]

    def to_dict(self) -> dict:
        """Dict representation for scheduler/resolver integration."""
        return {
            "name": self.name,
            "version": self.version,
            "dependencies": [
                {"name": d.name, "version": d.version} for d in self.dependencies
            ],
            "jobs": {
                name: {
                    "runtime": job.runtime,
                    "resources": job.resources,
                    "steps": [{"name": s.name, "run": s.run} for s in job.steps],
                    **({"needs": job.needs} if job.needs else {}),
                }
                for name, job in self.jobs.items()
            },
            "artifacts": [
                {"name": a.name, "version": a.version, "path": a.path}
                for a in self.artifacts
            ],
        }


def _line(node: Any) -> Optional[int]:
    if hasattr(node, "start_mark") and node.start_mark is not None:
        return node.start_mark.line + 1
    return None


def _expect_mapping(node: Any, context: str) -> MappingNode:
    if not isinstance(node, MappingNode):
        ln = _line(node)
        raise ParseError(f"{context} must be a mapping", ln)
    return node


def _expect_sequence(node: Any, context: str) -> SequenceNode:
    if not isinstance(node, SequenceNode):
        ln = _line(node)
        raise ParseError(f"{context} must be a sequence", ln)
    return node


def _expect_scalar(node: Any, context: str) -> ScalarNode:
    if not isinstance(node, ScalarNode):
        ln = _line(node)
        raise ParseError(f"{context} must be a scalar value", ln)
    return node


def _scalar_value(node: ScalarNode) -> str:
    return str(node.value)


def _check_unknown_keys(
    node: MappingNode, allowed: Set[str], context: str
) -> None:
    for key_node, _ in node.value:
        key = _scalar_value(_expect_scalar(key_node, f"{context} key"))
        if key not in allowed:
            raise ParseError(
                f"unknown field '{key}' in {context}",
                _line(key_node),
            )


def _require_keys(
    data: Dict[str, Any],
    node: MappingNode,
    required: Set[str],
    context: str,
) -> None:
    key_lines: Dict[str, int] = {}
    for key_node, _ in node.value:
        key = _scalar_value(_expect_scalar(key_node, f"{context} key"))
        key_lines[key] = _line(key_node) or 1

    for req in required:
        if req not in data:
            raise ParseError(
                f"missing required field '{req}' in {context}",
                key_lines.get(req, _line(node)),
            )


def is_valid_semver_version(version: str) -> bool:
    return bool(SEMVER_VERSION_PATTERN.match(version))


def is_valid_semver_constraint(constraint: str) -> bool:
    if not constraint or not constraint.strip():
        return False
    constraint = constraint.strip()
    # Comparator range: one or more comparators
    if any(op in constraint for op in (">=", "<=", ">", "<", "!=", "=")):
        return _parse_comparator_tokens(constraint) is not None
    if constraint.startswith("^") or constraint.startswith("~"):
        return bool(CONSTRAINT_PATTERN.match(constraint))
    return is_valid_semver_version(constraint)


_COMPARATOR_OP = re.compile(r"^(>=|<=|!=|>|<|=)(.+)$")


def _parse_comparator_tokens(constraint: str) -> Optional[List[tuple]]:
    """Return list of (op, version) or None if invalid."""
    parts = constraint.split()
    if not parts:
        return None
    parsed: List[tuple] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if token in (">=", "<=", ">", "<", "!="):
            if i + 1 >= len(parts):
                return None
            ver = parts[i + 1]
            if not is_valid_semver_version(ver):
                return None
            parsed.append((token, ver))
            i += 2
            continue

        match = _COMPARATOR_OP.match(token)
        if not match:
            return None
        ver = match.group(2)
        if not is_valid_semver_version(ver):
            return None
        parsed.append((match.group(1), ver))
        i += 1
    return parsed if parsed else None


def _validate_relative_path(path: str, line: Optional[int]) -> None:
    if not path:
        raise ParseError("artifact path must not be empty", line)
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ParseError(
            f"artifact path must be relative, got '{path}'",
            line,
        )
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        raise ParseError(
            f"artifact path must not contain '..', got '{path}'",
            line,
        )


def _validate_image_ref(image: str, line: Optional[int]) -> None:
    if not image or not IMAGE_REF_PATTERN.match(image):
        raise ParseError(
            f"invalid container image reference '{image}'",
            line,
        )


def _validate_memory(memory: str, line: Optional[int]) -> None:
    if not MEMORY_PATTERN.match(memory):
        raise ParseError(
            f"invalid memory value '{memory}' (expected e.g. 512Mi, 1Gi)",
            line,
        )


def _validate_cpu(cpu: Any, line: Optional[int]) -> float:
    try:
        value = float(cpu)
    except (TypeError, ValueError):
        raise ParseError(f"invalid cpu value '{cpu}'", line)
    if value <= 0:
        raise ParseError(f"cpu must be positive, got {cpu}", line)
    return value


def _collect_step_output_hints(steps: List[PipelineStep]) -> Set[str]:
    """Extract filenames referenced in step commands for artifact validation."""
    hints: Set[str] = set()
    for step in steps:
        # Match common output patterns: > file, tar czf file, cp x file
        for match in re.finditer(
            r"(?:>|tar\s+(?:czf|xzf|cf)\s+|mv\s+\S+\s+|cp\s+\S+\s+)([\w./\-]+\.(?:tar\.gz|tgz|zip|jar|whl|out))",
            step.run,
        ):
            hints.add(match.group(1).lstrip("./"))
        # Also match explicit paths in quotes
        for match in re.finditer(r"['\"](\./[\w./\-]+)['\"]", step.run):
            hints.add(match.group(1).lstrip("./"))
    return hints


class PipelineParser:
    """Strict pipeline YAML parser with line-precise error messages."""

    def parse_and_validate(
        self, source: Union[str, bytes, dict]
    ) -> dict:
        """
        Parse and validate pipeline YAML.

        Accepts raw YAML text (preferred for line numbers) or a pre-parsed dict.
        Returns a dict for scheduler/resolver consumption.
        """
        if isinstance(source, dict):
            return self._validate_root_dict(source, root_line=1).to_dict()

        if isinstance(source, bytes):
            source = source.decode("utf-8")

        try:
            root_node = yaml.compose(source)
        except yaml.YAMLError as exc:
            raise ParseError(f"invalid YAML: {exc}") from exc

        if root_node is None:
            raise ParseError("pipeline document is empty", 1)

        if not isinstance(root_node, MappingNode):
            raise ParseError("pipeline root must be a mapping", _line(root_node))

        loader = yaml.SafeLoader(source)
        pipeline_data = loader.construct_document(root_node)

        pipeline = self._validate_root_dict(
            pipeline_data, root_node=root_node, root_line=_line(root_node) or 1
        )
        return pipeline.to_dict()

    def _validate_root_dict(
        self,
        data: Any,
        root_node: Optional[MappingNode] = None,
        root_line: int = 1,
    ) -> Pipeline:
        if not isinstance(data, dict):
            raise ParseError("pipeline root must be a mapping", root_line)

        if root_node is not None:
            _check_unknown_keys(root_node, TOP_LEVEL_KEYS, "pipeline")
            _require_keys(data, root_node, REQUIRED_TOP_LEVEL, "pipeline")

        for key in REQUIRED_TOP_LEVEL:
            if key not in data:
                raise ParseError(f"missing required field '{key}' in pipeline", root_line)

        for key in data:
            if key not in TOP_LEVEL_KEYS:
                raise ParseError(f"unknown field '{key}' in pipeline", root_line)

        name = data["name"]
        version = data["version"]
        if not isinstance(name, str) or not name.strip():
            raise ParseError("field 'name' must be a non-empty string", root_line)
        if not isinstance(version, str) or not is_valid_semver_version(version):
            raise ParseError(
                f"field 'version' must be a valid semver version, got '{version}'",
                root_line,
            )

        dependencies = self._parse_dependencies(
            data.get("dependencies", []), root_node, root_line
        )
        jobs = self._parse_jobs(data["jobs"], root_node, root_line)
        artifacts = self._parse_artifacts(
            data.get("artifacts", []), root_node, root_line
        )

        self._validate_artifact_paths(artifacts, jobs)

        return Pipeline(
            name=name.strip(),
            version=version.strip(),
            dependencies=dependencies,
            jobs=jobs,
            artifacts=artifacts,
        )

    def _parse_dependencies(
        self,
        deps_data: Any,
        root_node: Optional[MappingNode],
        default_line: int,
    ) -> List[PipelineDependency]:
        if deps_data is None:
            return []
        if not isinstance(deps_data, list):
            raise ParseError("field 'dependencies' must be a sequence", default_line)

        dep_nodes: List[Any] = []
        if root_node is not None:
            for key_node, val_node in root_node.value:
                if _scalar_value(_expect_scalar(key_node, "key")) == "dependencies":
                    dep_nodes = _expect_sequence(
                        val_node, "dependencies"
                    ).value
                    break

        dependencies: List[PipelineDependency] = []
        for idx, item in enumerate(deps_data):
            line = default_line
            item_node = dep_nodes[idx] if idx < len(dep_nodes) else None
            if item_node is not None:
                line = _line(item_node) or default_line

            if not isinstance(item, dict):
                raise ParseError("each dependency must be a mapping", line)

            if item_node is not None and isinstance(item_node, MappingNode):
                _check_unknown_keys(item_node, DEP_KEYS, "dependency")
                _require_keys(item, item_node, REQUIRED_DEP_KEYS, "dependency")

            for key in REQUIRED_DEP_KEYS:
                if key not in item:
                    raise ParseError(f"missing required field '{key}' in dependency", line)
            for key in item:
                if key not in DEP_KEYS:
                    raise ParseError(f"unknown field '{key}' in dependency", line)

            dep_name = item["name"]
            dep_version = item["version"]
            if not isinstance(dep_name, str) or not PACKAGE_NAME_PATTERN.match(dep_name):
                raise ParseError(
                    f"invalid dependency name '{dep_name}'",
                    line,
                )
            if not isinstance(dep_version, str) or not is_valid_semver_constraint(
                dep_version
            ):
                raise ParseError(
                    f"invalid semver constraint '{dep_version}'",
                    line,
                )

            dependencies.append(
                PipelineDependency(name=dep_name, version=dep_version.strip())
            )

        return dependencies

    def _parse_jobs(
        self,
        jobs_data: Any,
        root_node: Optional[MappingNode],
        default_line: int,
    ) -> Dict[str, PipelineJob]:
        if not isinstance(jobs_data, dict) or not jobs_data:
            raise ParseError("field 'jobs' must be a non-empty mapping", default_line)

        jobs_node: Optional[MappingNode] = None
        if root_node is not None:
            for key_node, val_node in root_node.value:
                if _scalar_value(_expect_scalar(key_node, "key")) == "jobs":
                    jobs_node = _expect_mapping(val_node, "jobs")
                    break

        jobs: Dict[str, PipelineJob] = {}
        job_items = list(jobs_data.items())
        job_nodes = jobs_node.value if jobs_node else []

        for idx, (job_name, job_data) in enumerate(job_items):
            line = default_line
            job_node = job_nodes[idx][1] if idx < len(job_nodes) else None
            if job_node is not None:
                line = _line(job_node) or default_line

            if not isinstance(job_name, str) or not job_name.strip():
                raise ParseError("job name must be a non-empty string", line)
            if not isinstance(job_data, dict):
                raise ParseError(f"job '{job_name}' must be a mapping", line)

            if job_node is not None and isinstance(job_node, MappingNode):
                _check_unknown_keys(job_node, JOB_KEYS, f"job '{job_name}'")
                _require_keys(
                    job_data, job_node, REQUIRED_JOB_KEYS, f"job '{job_name}'"
                )

            for key in REQUIRED_JOB_KEYS:
                if key not in job_data:
                    raise ParseError(
                        f"missing required field '{key}' in job '{job_name}'",
                        line,
                    )
            for key in job_data:
                if key not in JOB_KEYS:
                    raise ParseError(
                        f"unknown field '{key}' in job '{job_name}'",
                        line,
                    )

            runtime = job_data["runtime"]
            if not isinstance(runtime, str):
                raise ParseError(
                    f"job '{job_name}' runtime must be a string",
                    line,
                )
            _validate_image_ref(runtime, line)

            resources = job_data["resources"]
            if not isinstance(resources, dict):
                raise ParseError(
                    f"job '{job_name}' resources must be a mapping",
                    line,
                )

            resources_node = None
            if job_node is not None:
                for kn, vn in job_node.value:
                    if _scalar_value(_expect_scalar(kn, "key")) == "resources":
                        resources_node = vn
                        break
            if resources_node is not None and isinstance(resources_node, MappingNode):
                _check_unknown_keys(
                    resources_node, RESOURCE_KEYS, f"job '{job_name}' resources"
                )
                _require_keys(
                    resources,
                    resources_node,
                    RESOURCE_KEYS,
                    f"job '{job_name}' resources",
                )

            for rk in RESOURCE_KEYS:
                if rk not in resources:
                    raise ParseError(
                        f"missing required field '{rk}' in job '{job_name}' resources",
                        line,
                    )
            for rk in resources:
                if rk not in RESOURCE_KEYS:
                    raise ParseError(
                        f"unknown field '{rk}' in job '{job_name}' resources",
                        line,
                    )

            cpu = _validate_cpu(resources["cpu"], line)
            memory = resources["memory"]
            if not isinstance(memory, str):
                raise ParseError(
                    f"job '{job_name}' memory must be a string",
                    line,
                )
            _validate_memory(memory, line)

            steps = self._parse_steps(job_data["steps"], job_node, job_name, line)

            needs: List[str] = []
            if "needs" in job_data:
                needs_raw = job_data["needs"]
                if not isinstance(needs_raw, list):
                    raise ParseError(
                        f"job '{job_name}' needs must be a sequence",
                        line,
                    )
                for need in needs_raw:
                    if not isinstance(need, str) or not need.strip():
                        raise ParseError(
                            f"job '{job_name}' needs entries must be strings",
                            line,
                        )
                    needs.append(need.strip())

            jobs[job_name] = PipelineJob(
                runtime=runtime,
                resources={"cpu": cpu, "memory": memory},
                steps=steps,
                needs=needs,
            )

        # Validate needs references
        for job_name, job in jobs.items():
            for need in job.needs:
                if need not in jobs:
                    raise ParseError(
                        f"job '{job_name}' needs unknown job '{need}'",
                        default_line,
                    )
                if need == job_name:
                    raise ParseError(
                        f"job '{job_name}' cannot depend on itself",
                        default_line,
                    )

        return jobs

    def _parse_steps(
        self,
        steps_data: Any,
        job_node: Optional[MappingNode],
        job_name: str,
        default_line: int,
    ) -> List[PipelineStep]:
        if not isinstance(steps_data, list) or not steps_data:
            raise ParseError(
                f"job '{job_name}' steps must be a non-empty sequence",
                default_line,
            )

        steps_node: Optional[SequenceNode] = None
        if job_node is not None:
            for kn, vn in job_node.value:
                if _scalar_value(_expect_scalar(kn, "key")) == "steps":
                    steps_node = _expect_sequence(vn, "steps")
                    break

        steps: List[PipelineStep] = []
        step_names: Set[str] = set()

        for idx, step_data in enumerate(steps_data):
            line = default_line
            step_node = steps_node.value[idx] if steps_node and idx < len(steps_node.value) else None
            if step_node is not None:
                line = _line(step_node) or default_line

            if not isinstance(step_data, dict):
                raise ParseError(
                    f"job '{job_name}' step must be a mapping",
                    line,
                )

            if step_node is not None and isinstance(step_node, MappingNode):
                _check_unknown_keys(step_node, STEP_KEYS, f"step in job '{job_name}'")
                _require_keys(
                    step_data, step_node, REQUIRED_STEP_KEYS, f"step in job '{job_name}'"
                )

            for key in REQUIRED_STEP_KEYS:
                if key not in step_data:
                    raise ParseError(
                        f"missing required field '{key}' in step of job '{job_name}'",
                        line,
                    )
            for key in step_data:
                if key not in STEP_KEYS:
                    raise ParseError(
                        f"unknown field '{key}' in step of job '{job_name}'",
                        line,
                    )

            step_name = step_data["name"]
            step_run = step_data["run"]
            if not isinstance(step_name, str) or not step_name.strip():
                raise ParseError(
                    f"step name in job '{job_name}' must be a non-empty string",
                    line,
                )
            if step_name in step_names:
                raise ParseError(
                    f"duplicate step name '{step_name}' in job '{job_name}'",
                    line,
                )
            if not isinstance(step_run, str) or not step_run.strip():
                raise ParseError(
                    f"step run in job '{job_name}' must be a non-empty string",
                    line,
                )

            step_names.add(step_name)
            steps.append(PipelineStep(name=step_name.strip(), run=step_run))

        return steps

    def _parse_artifacts(
        self,
        artifacts_data: Any,
        root_node: Optional[MappingNode],
        default_line: int,
    ) -> List[PipelineArtifact]:
        if artifacts_data is None:
            return []
        if not isinstance(artifacts_data, list):
            raise ParseError("field 'artifacts' must be a sequence", default_line)

        artifact_nodes: List[Any] = []
        if root_node is not None:
            for key_node, val_node in root_node.value:
                if _scalar_value(_expect_scalar(key_node, "key")) == "artifacts":
                    artifact_nodes = _expect_sequence(val_node, "artifacts").value
                    break

        artifacts: List[PipelineArtifact] = []
        for idx, item in enumerate(artifacts_data):
            line = default_line
            item_node = artifact_nodes[idx] if idx < len(artifact_nodes) else None
            if item_node is not None:
                line = _line(item_node) or default_line

            if not isinstance(item, dict):
                raise ParseError("each artifact must be a mapping", line)

            if item_node is not None and isinstance(item_node, MappingNode):
                _check_unknown_keys(item_node, ARTIFACT_KEYS, "artifact")
                _require_keys(item, item_node, REQUIRED_ARTIFACT_KEYS, "artifact")

            for key in REQUIRED_ARTIFACT_KEYS:
                if key not in item:
                    raise ParseError(f"missing required field '{key}' in artifact", line)
            for key in item:
                if key not in ARTIFACT_KEYS:
                    raise ParseError(f"unknown field '{key}' in artifact", line)

            art_name = item["name"]
            art_version = item["version"]
            art_path = item["path"]

            if not isinstance(art_name, str) or not PACKAGE_NAME_PATTERN.match(art_name):
                raise ParseError(f"invalid artifact name '{art_name}'", line)
            if not isinstance(art_version, str) or not is_valid_semver_version(
                art_version
            ):
                raise ParseError(
                    f"artifact version must be valid semver, got '{art_version}'",
                    line,
                )
            if not isinstance(art_path, str):
                raise ParseError("artifact path must be a string", line)
            _validate_relative_path(art_path, line)

            artifacts.append(
                PipelineArtifact(
                    name=art_name,
                    version=art_version.strip(),
                    path=art_path.strip(),
                )
            )

        return artifacts

    def _validate_artifact_paths(
        self,
        artifacts: List[PipelineArtifact],
        jobs: Dict[str, PipelineJob],
    ) -> None:
        """Ensure artifact paths are plausibly produced by at least one job step."""
        if not artifacts:
            return

        all_hints: Set[str] = set()
        for job in jobs.values():
            all_hints.update(_collect_step_output_hints(job.steps))

        normalized_hints = {h.lstrip("./") for h in all_hints}
        normalized_hints.update(all_hints)

        for artifact in artifacts:
            norm_path = artifact.path.lstrip("./")
            basename = norm_path.split("/")[-1]

            # Accept if referenced in a step command or is a conventional output name
            produced = (
                norm_path in normalized_hints
                or basename in normalized_hints
                or any(basename in step.run for job in jobs.values() for step in job.steps)
            )
            if not produced:
                raise ParseError(
                    f"artifact path '{artifact.path}' is not produced by any job step",
                    None,
                )
