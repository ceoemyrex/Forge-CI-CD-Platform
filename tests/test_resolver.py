"""Unit tests for registry/resolver.py"""

import pytest

from registry.resolver import (
    ConflictError,
    ConstraintSet,
    CycleError,
    DependencyResolver,
    InMemoryRegistry,
    PackageMeta,
    SemVer,
    compare_versions,
    expand_constraint,
    serialize_lockfile,
)


def _pkg(name, version, sha256="abc123", deps=None):
    return PackageMeta(
        name=name,
        version=version,
        sha256=sha256,
        size=100,
        deps=deps or [],
    )


def _registry(packages):
    return InMemoryRegistry(packages)


class TestSemVer:
    def test_parse_and_compare(self):
        assert compare_versions(SemVer.parse("1.0.0"), SemVer.parse("1.0.1")) < 0
        assert compare_versions(SemVer.parse("2.0.0"), SemVer.parse("1.9.9")) > 0
        assert compare_versions(SemVer.parse("1.0.0"), SemVer.parse("1.0.0")) == 0

    def test_prerelease_ordering(self):
        assert compare_versions(SemVer.parse("1.0.0"), SemVer.parse("1.0.0-alpha")) > 0
        assert (
            compare_versions(SemVer.parse("1.0.0-alpha"), SemVer.parse("1.0.0-beta"))
            < 0
        )


class TestConstraintExpansion:
    def test_caret_major(self):
        cs = ConstraintSet.from_string("^1.2.3")
        assert cs.satisfies(SemVer.parse("1.9.9"))
        assert cs.satisfies(SemVer.parse("1.2.3"))
        assert not cs.satisfies(SemVer.parse("2.0.0"))
        assert not cs.satisfies(SemVer.parse("0.9.9"))

    def test_caret_zero_minor(self):
        cs = ConstraintSet.from_string("^0.2.3")
        assert cs.satisfies(SemVer.parse("0.2.5"))
        assert not cs.satisfies(SemVer.parse("0.3.0"))

    def test_caret_zero_zero(self):
        cs = ConstraintSet.from_string("^0.0.3")
        assert cs.satisfies(SemVer.parse("0.0.3"))
        assert not cs.satisfies(SemVer.parse("0.0.4"))

    def test_tilde(self):
        cs = ConstraintSet.from_string("~1.2.3")
        assert cs.satisfies(SemVer.parse("1.2.9"))
        assert not cs.satisfies(SemVer.parse("1.3.0"))

    def test_comparator_range(self):
        cs = ConstraintSet.from_string(">=1.0.0 <2.0.0")
        assert cs.satisfies(SemVer.parse("1.5.0"))
        assert not cs.satisfies(SemVer.parse("2.0.0"))
        assert not cs.satisfies(SemVer.parse("0.9.0"))

    def test_expand_constraint(self):
        lower, upper = expand_constraint("^1.0.0")
        assert lower == "1.0.0"
        assert upper == "2.0.0"


class TestDependencyResolver:
    def test_resolves_single_dependency(self):
        registry = _registry(
            {
                "lib-core": {
                    "1.0.0": _pkg("lib-core", "1.0.0", sha256="sha-core-1"),
                    "1.1.0": _pkg("lib-core", "1.1.0", sha256="sha-core-2"),
                }
            }
        )
        resolver = DependencyResolver(registry=registry)
        pipeline = {
            "dependencies": [{"name": "lib-core", "version": "^1.0.0"}],
        }
        lockfile = resolver.resolve(pipeline)
        assert lockfile == {
            "packages": [
                {"name": "lib-core", "version": "1.1.0", "sha256": "sha-core-2"},
            ]
        }

    def test_transitive_dependencies(self):
        registry = _registry(
            {
                "lib-core": {
                    "1.0.0": _pkg("lib-core", "1.0.0", sha256="sha-core"),
                },
                "lib-http": {
                    "1.0.0": _pkg(
                        "lib-http",
                        "1.0.0",
                        sha256="sha-http",
                        deps=[{"name": "lib-core", "version": "^1.0.0"}],
                    ),
                },
            }
        )
        resolver = DependencyResolver(registry=registry)
        pipeline = {
            "dependencies": [{"name": "lib-http", "version": "1.0.0"}],
        }
        lockfile = resolver.resolve(pipeline)
        names = [p["name"] for p in lockfile["packages"]]
        assert names == ["lib-core", "lib-http"]

    def test_conflict_detection(self):
        registry = _registry(
            {
                "lib-a": {
                    "1.0.0": _pkg(
                        "lib-a",
                        "1.0.0",
                        deps=[{"name": "lib-shared", "version": "^1.0.0"}],
                    ),
                },
                "lib-b": {
                    "1.0.0": _pkg(
                        "lib-b",
                        "1.0.0",
                        deps=[{"name": "lib-shared", "version": "^2.0.0"}],
                    ),
                },
                "lib-shared": {
                    "1.5.0": _pkg("lib-shared", "1.5.0"),
                    "2.1.0": _pkg("lib-shared", "2.1.0"),
                },
            }
        )
        resolver = DependencyResolver(registry=registry)
        pipeline = {
            "dependencies": [
                {"name": "lib-a", "version": "1.0.0"},
                {"name": "lib-b", "version": "1.0.0"},
            ],
        }
        with pytest.raises(ConflictError) as exc:
            resolver.resolve(pipeline)
        assert "version conflict" in str(exc.value).lower()
        assert "lib-shared" in str(exc.value)

    def test_cycle_detection(self):
        registry = _registry(
            {
                "pkg-a": {
                    "1.0.0": _pkg(
                        "pkg-a",
                        "1.0.0",
                        deps=[{"name": "pkg-b", "version": "1.0.0"}],
                    ),
                },
                "pkg-b": {
                    "1.0.0": _pkg(
                        "pkg-b",
                        "1.0.0",
                        deps=[{"name": "pkg-a", "version": "1.0.0"}],
                    ),
                },
            }
        )
        resolver = DependencyResolver(registry=registry)
        pipeline = {
            "dependencies": [{"name": "pkg-a", "version": "1.0.0"}],
        }
        with pytest.raises(CycleError) as exc:
            resolver.resolve(pipeline)
        assert "pkg-a" in exc.value.cycle
        assert "pkg-b" in exc.value.cycle

    def test_deterministic_lockfile(self):
        registry = _registry(
            {
                "lib-z": {"1.0.0": _pkg("lib-z", "1.0.0", sha256="z")},
                "lib-a": {"2.0.0": _pkg("lib-a", "2.0.0", sha256="a")},
                "lib-m": {
                    "1.0.0": _pkg(
                        "lib-m",
                        "1.0.0",
                        sha256="m",
                        deps=[
                            {"name": "lib-a", "version": "^2.0.0"},
                            {"name": "lib-z", "version": "1.0.0"},
                        ],
                    ),
                },
            }
        )
        resolver = DependencyResolver(registry=registry)
        pipeline = {
            "dependencies": [{"name": "lib-m", "version": "1.0.0"}],
        }
        lock1 = serialize_lockfile(resolver.resolve(pipeline))
        lock2 = serialize_lockfile(resolver.resolve(pipeline))
        assert lock1 == lock2
        assert lock1.index("lib-a") < lock1.index("lib-m") < lock1.index("lib-z")

    def test_exact_version_constraint(self):
        registry = _registry(
            {
                "lib-core": {
                    "1.0.0": _pkg("lib-core", "1.0.0", sha256="v1"),
                    "1.0.1": _pkg("lib-core", "1.0.1", sha256="v2"),
                }
            }
        )
        resolver = DependencyResolver(registry=registry)
        pipeline = {
            "dependencies": [{"name": "lib-core", "version": "1.0.0"}],
        }
        lockfile = resolver.resolve(pipeline)
        assert lockfile["packages"][0]["version"] == "1.0.0"

    def test_resolve_to_json(self):
        registry = _registry(
            {"lib-core": {"1.0.0": _pkg("lib-core", "1.0.0", sha256="x")}}
        )
        resolver = DependencyResolver(registry=registry)
        pipeline = {"dependencies": [{"name": "lib-core", "version": "1.0.0"}]}
        json1 = resolver.resolve_to_json(pipeline)
        json2 = resolver.resolve_to_json(pipeline)
        assert json1 == json2
        assert json1.endswith("\n")
