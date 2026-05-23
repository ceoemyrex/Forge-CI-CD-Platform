"""
Dependency resolver with hand-rolled semver parsing and constraint matching.

Fetches package metadata from the Forge registry, walks the transitive graph,
detects cycles and version conflicts, and emits a deterministic lockfile.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Tuple

import requests

# ---------------------------------------------------------------------------
# Semver core (no external semver libraries)
# ---------------------------------------------------------------------------

PRERELEASE_PATTERN = re.compile(
    r"^(0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*$"
)


@dataclass(frozen=True, order=False)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: Tuple[str, ...] = ()
    build: Tuple[str, ...] = ()

    @classmethod
    def parse(cls, version: str) -> "SemVer":
        version = version.strip()
        if version.startswith("^") or version.startswith("~"):
            raise ValueError(f"not a bare version: {version}")

        build_part = ""
        if "+" in version:
            version, build_part = version.split("+", 1)

        prerelease_part = ""
        if "-" in version:
            version, prerelease_part = version.split("-", 1)

        parts = version.split(".")
        if len(parts) != 3:
            raise ValueError(f"invalid semver: {version}")

        try:
            major, minor, patch = (int(p) for p in parts)
        except ValueError as exc:
            raise ValueError(f"invalid semver numeric parts: {version}") from exc

        prerelease: Tuple[str, ...] = ()
        if prerelease_part:
            prerelease = tuple(prerelease_part.split("."))
            for ident in prerelease:
                if not PRERELEASE_PATTERN.match(ident):
                    raise ValueError(f"invalid prerelease identifier: {ident}")

        build: Tuple[str, ...] = ()
        if build_part:
            build = tuple(build_part.split("."))

        return cls(major=major, minor=minor, patch=patch, prerelease=prerelease, build=build)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(self.prerelease)
        if self.build:
            base += "+" + ".".join(self.build)
        return base

    def without_prerelease(self) -> "SemVer":
        return SemVer(self.major, self.minor, self.patch, (), self.build)


def _compare_identifiers(a: str, b: str) -> int:
    """Compare prerelease identifiers per semver spec."""
    a_num = a.isdigit()
    b_num = b.isdigit()
    if a_num and b_num:
        return (int(a) > int(b)) - (int(a) < int(b))
    if a_num and not b_num:
        return -1
    if not a_num and b_num:
        return 1
    return (a > b) - (a < b)


def compare_versions(a: SemVer, b: SemVer) -> int:
    """
    Compare two semver versions.
    Returns -1 if a < b, 0 if equal, 1 if a > b.
    Build metadata is ignored.
    """
    for av, bv in ((a.major, b.major), (a.minor, b.minor), (a.patch, b.patch)):
        if av != bv:
            return 1 if av > bv else -1

    if not a.prerelease and not b.prerelease:
        return 0
    if not a.prerelease and b.prerelease:
        return 1
    if a.prerelease and not b.prerelease:
        return -1

    for i in range(max(len(a.prerelease), len(b.prerelease))):
        if i >= len(a.prerelease):
            return -1
        if i >= len(b.prerelease):
            return 1
        cmp = _compare_identifiers(a.prerelease[i], b.prerelease[i])
        if cmp != 0:
            return cmp
    return 0


def version_key(version: str) -> Tuple:
    """Sort key for deterministic ordering (highest version first when reversed)."""
    v = SemVer.parse(version)
    return (v.major, v.minor, v.patch, v.prerelease, version)


# ---------------------------------------------------------------------------
# Constraint parsing and matching
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Comparator:
    op: str
    version: SemVer

    def satisfies(self, candidate: SemVer) -> bool:
        cmp = compare_versions(candidate, self.version)
        if self.op == ">=":
            return cmp >= 0
        if self.op == "<=":
            return cmp <= 0
        if self.op == ">":
            return cmp > 0
        if self.op == "<":
            return cmp < 0
        if self.op in ("=", "=="):
            return cmp == 0
        if self.op == "!=":
            return cmp != 0
        raise ValueError(f"unknown comparator: {self.op}")


@dataclass(frozen=True)
class ConstraintSet:
    comparators: Tuple[Comparator, ...]

    def satisfies(self, candidate: SemVer) -> bool:
        return all(c.satisfies(candidate) for c in self.comparators)

    @classmethod
    def from_string(cls, constraint: str) -> "ConstraintSet":
        constraint = constraint.strip()
        if not constraint:
            raise ValueError("empty constraint")

        # Caret
        if constraint.startswith("^"):
            base = SemVer.parse(constraint[1:])
            upper = _caret_upper_bound(base)
            return cls(
                (
                    Comparator(">=", base),
                    Comparator("<", upper),
                )
            )

        # Tilde
        if constraint.startswith("~"):
            base = SemVer.parse(constraint[1:])
            upper = _tilde_upper_bound(base)
            return cls(
                (
                    Comparator(">=", base),
                    Comparator("<", upper),
                )
            )

        # Comparator range
        if any(op in constraint for op in (">=", "<=", ">", "<", "!=")):
            return cls(tuple(_parse_comparator_range(constraint)))

        # Exact
        exact = SemVer.parse(constraint)
        return cls((Comparator("=", exact),))


def _caret_upper_bound(base: SemVer) -> SemVer:
    if base.major > 0:
        return SemVer(base.major + 1, 0, 0)
    if base.minor > 0:
        return SemVer(0, base.minor + 1, 0)
    return SemVer(0, 0, base.patch + 1)


def _tilde_upper_bound(base: SemVer) -> SemVer:
    if base.major > 0:
        return SemVer(base.major, base.minor + 1, 0)
    if base.minor > 0:
        return SemVer(0, base.minor + 1, 0)
    return SemVer(0, 0, base.patch + 1)


_COMPARATOR_OP = re.compile(r"^(>=|<=|!=|>|<|=)(.+)$")


def _parse_comparator_range(constraint: str) -> List[Comparator]:
    """Parse comparator ranges like '>=1.0.0 <2.0.0' or '>= 1.0.0 < 2.0.0'."""
    parts = constraint.split()
    comparators: List[Comparator] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if token in (">=", "<=", ">", "<", "!="):
            if i + 1 >= len(parts):
                raise ValueError(f"incomplete comparator range: {constraint}")
            comparators.append(Comparator(token, SemVer.parse(parts[i + 1])))
            i += 2
            continue

        match = _COMPARATOR_OP.match(token)
        if not match:
            raise ValueError(f"invalid comparator token: {token}")
        comparators.append(Comparator(match.group(1), SemVer.parse(match.group(2))))
        i += 1
    return comparators


def expand_constraint(constraint: str) -> Tuple[str, str]:
    """
    Expand a constraint to human-readable lower/upper bounds for error messages.
    Returns (lower_inclusive, upper_exclusive) as strings.
    """
    cs = ConstraintSet.from_string(constraint)
    lower = ""
    upper = ""
    for c in cs.comparators:
        if c.op == ">=":
            lower = str(c.version)
        elif c.op == "<":
            upper = str(c.version)
    return lower, upper


# ---------------------------------------------------------------------------
# Resolution errors
# ---------------------------------------------------------------------------

class ResolutionError(Exception):
    """Base class for dependency resolution failures."""


class CycleError(ResolutionError):
    def __init__(self, cycle: List[str]):
        self.cycle = cycle
        path = " → ".join(cycle + [cycle[0]]) if cycle else ""
        super().__init__(f"dependency cycle detected: {path}")


class ConflictError(ResolutionError):
    def __init__(self, message: str):
        super().__init__(message)


class PackageNotFoundError(ResolutionError):
    pass


# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

@dataclass
class PackageMeta:
    name: str
    version: str
    sha256: str
    size: int
    deps: List[Dict[str, str]]
    published_at: str = ""


class RegistryClient(Protocol):
    def list_versions(self, name: str) -> List[str]:
        ...

    def get_metadata(self, name: str, version: str) -> PackageMeta:
        ...


class HttpRegistryClient:
    """Fetch package metadata from the Forge registry HTTP API."""

    def __init__(self, registry_url: str, timeout: float = 30.0):
        self.registry_url = registry_url.rstrip("/")
        self.timeout = timeout

    def list_versions(self, name: str) -> List[str]:
        url = f"{self.registry_url}/artifacts/{name}"
        resp = requests.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        versions = data.get("versions", [])
        out: List[str] = []
        for v in versions:
            out.append(v["version"] if isinstance(v, dict) else v)
        return sorted(out, key=version_key)

    def get_metadata(self, name: str, version: str) -> PackageMeta:
        url = f"{self.registry_url}/artifacts/{name}/{version}/meta"
        resp = requests.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            raise PackageNotFoundError(f"package not found: {name}@{version}")
        resp.raise_for_status()
        data = resp.json()
        return PackageMeta(
            name=data["name"],
            version=data["version"],
            sha256=data["sha256"],
            size=data.get("size", 0),
            deps=data.get("deps", []),
            published_at=data.get("published_at", ""),
        )


class InMemoryRegistry:
    """In-memory registry for unit tests."""

    def __init__(self, packages: Dict[str, Dict[str, PackageMeta]]):
        self.packages = packages

    def list_versions(self, name: str) -> List[str]:
        if name not in self.packages:
            return []
        return sorted(self.packages[name].keys(), key=version_key)

    def get_metadata(self, name: str, version: str) -> PackageMeta:
        if name not in self.packages or version not in self.packages[name]:
            raise PackageNotFoundError(f"package not found: {name}@{version}")
        return self.packages[name][version]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

@dataclass
class _Requirement:
    name: str
    constraint: str
    constraint_set: ConstraintSet
    via: Optional[str] = None  # parent package that introduced this requirement


@dataclass
class _ResolvedPackage:
    name: str
    version: str
    sha256: str
    deps: List[Dict[str, str]] = field(default_factory=list)


class DependencyResolver:
    """
    Resolves pipeline dependencies to a deterministic lockfile.

    Lockfile schema:
        {"packages": [{"name", "version", "sha256"}, ...]}
    """

    def __init__(
        self,
        registry_url: Optional[str] = None,
        registry: Optional[RegistryClient] = None,
    ):
        if registry is not None:
            self.registry = registry
        elif registry_url is not None:
            self.registry = HttpRegistryClient(registry_url)
        else:
            raise ValueError("registry_url or registry client required")

    def resolve(self, pipeline_config: dict) -> dict:
        """
        Resolve all dependencies for a validated pipeline config.

        Raises CycleError, ConflictError, or PackageNotFoundError on failure.
        """
        root_deps = pipeline_config.get("dependencies", [])
        requirements: Dict[str, List[_Requirement]] = {}

        for dep in sorted(root_deps, key=lambda d: d["name"]):
            self._add_requirement(
                requirements,
                _Requirement(
                    name=dep["name"],
                    constraint=dep["version"],
                    constraint_set=ConstraintSet.from_string(dep["version"]),
                    via="pipeline",
                ),
            )

        resolved: Dict[str, _ResolvedPackage] = {}
        self._resolve_graph(requirements, resolved)
        return self._build_lockfile(resolved)

    def resolve_to_json(self, pipeline_config: dict) -> str:
        """Return lockfile as deterministic JSON string (byte-for-byte stable)."""
        lockfile = self.resolve(pipeline_config)
        return serialize_lockfile(lockfile)

    def _add_requirement(
        self,
        requirements: Dict[str, List[_Requirement]],
        req: _Requirement,
    ) -> None:
        requirements.setdefault(req.name, []).append(req)

    def _select_version(
        self, name: str, reqs: List[_Requirement]
    ) -> str:
        available = self.registry.list_versions(name)
        if not available:
            constraints = ", ".join(
                f"'{r.constraint}' (via {r.via})" for r in reqs
            )
            raise PackageNotFoundError(
                f"no published versions for '{name}' satisfying [{constraints}]"
            )

        candidates: List[str] = []
        for ver in available:
            try:
                parsed = SemVer.parse(ver)
            except ValueError:
                continue
            if all(r.constraint_set.satisfies(parsed) for r in reqs):
                candidates.append(ver)

        if not candidates:
            self._raise_conflict(name, reqs, available)

        # Highest version wins; tie-break by version string sort (descending)
        candidates.sort(key=version_key, reverse=True)
        return candidates[0]

    def _raise_conflict(
        self,
        name: str,
        reqs: List[_Requirement],
        available: List[str],
    ) -> None:
        paths = []
        for r in reqs:
            lower, upper = expand_constraint(r.constraint)
            bound = f">={lower}" if lower else ""
            if upper:
                bound = f"{bound} <{upper}".strip()
            paths.append(f"  - via {r.via}: requires {name}@{r.constraint} ({bound})")

        avail_str = ", ".join(available[:10])
        if len(available) > 10:
            avail_str += ", ..."

        raise ConflictError(
            f"version conflict for '{name}': no single version satisfies all constraints.\n"
            + "\n".join(paths)
            + f"\n  available versions: [{avail_str}]"
        )

    def _resolve_graph(
        self,
        requirements: Dict[str, List[_Requirement]],
        resolved: Dict[str, _ResolvedPackage],
    ) -> None:
        """
        Iteratively resolve packages in deterministic order (alphabetical by name).
        """
        visiting: set = set()
        path_stack: List[str] = []

        def resolve_name(name: str) -> None:
            if name in visiting:
                cycle_start = path_stack.index(name)
                cycle = path_stack[cycle_start:]
                raise CycleError(cycle)

            if name in resolved:
                reqs = requirements.get(name, [])
                if reqs:
                    pkg = resolved[name]
                    selected = SemVer.parse(pkg.version)
                    if not all(r.constraint_set.satisfies(selected) for r in reqs):
                        self._raise_conflict(
                            name, reqs, self.registry.list_versions(name)
                        )
                return

            visiting.add(name)
            path_stack.append(name)

            try:
                reqs = requirements.get(name, [])
                if not reqs:
                    raise PackageNotFoundError(
                        f"package '{name}' is required but has no constraints"
                    )

                version = self._select_version(name, reqs)
                meta = self.registry.get_metadata(name, version)

                for dep in sorted(meta.deps, key=lambda d: d["name"]):
                    dep_name = dep["name"]
                    dep_constraint = dep["version"]
                    self._add_requirement(
                        requirements,
                        _Requirement(
                            name=dep_name,
                            constraint=dep_constraint,
                            constraint_set=ConstraintSet.from_string(dep_constraint),
                            via=f"{name}@{version}",
                        ),
                    )
                    resolve_name(dep_name)

                resolved[name] = _ResolvedPackage(
                    name=name,
                    version=version,
                    sha256=meta.sha256,
                    deps=list(meta.deps),
                )
            finally:
                path_stack.pop()
                visiting.discard(name)

        for name in sorted(requirements.keys()):
            if name not in resolved:
                resolve_name(name)

    def _build_lockfile(self, resolved: Dict[str, _ResolvedPackage]) -> dict:
        packages = [
            {
                "name": pkg.name,
                "version": pkg.version,
                "sha256": pkg.sha256,
            }
            for name in sorted(resolved.keys())
            for pkg in [resolved[name]]
        ]
        return {"packages": packages}


def serialize_lockfile(lockfile: dict) -> str:
    """Serialize lockfile deterministically for byte-for-byte comparison."""
    return json.dumps(lockfile, sort_keys=True, separators=(",", ":")) + "\n"


def lockfile_as_deps_dict(lockfile: dict) -> Dict[str, dict]:
    """
    Convert lockfile packages list to {name: {version, sha256}} for runner pull.
    """
    return {
        pkg["name"]: {"version": pkg["version"], "sha256": pkg["sha256"]}
        for pkg in lockfile.get("packages", [])
    }
