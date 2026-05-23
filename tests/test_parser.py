"""Unit tests for engine/parser.py"""

import pytest

from engine.parser import ParseError, PipelineParser, is_valid_semver_constraint


VALID_PIPELINE = """
name: build-lib-http
version: 1.0.0
dependencies:
  - name: lib-core
    version: "^1.0.0"
jobs:
  build:
    runtime: alpine:3.18
    resources:
      cpu: 1.0
      memory: 512Mi
    steps:
      - name: test
        run: "sh ./test.sh"
      - name: package
        run: "tar czf out.tar.gz src/"
artifacts:
  - name: lib-http
    version: 1.0.0
    path: ./out.tar.gz
"""


class TestPipelineParser:
    def setup_method(self):
        self.parser = PipelineParser()

    def test_valid_pipeline(self):
        result = self.parser.parse_and_validate(VALID_PIPELINE)
        assert result["name"] == "build-lib-http"
        assert result["version"] == "1.0.0"
        assert len(result["dependencies"]) == 1
        assert "build" in result["jobs"]
        assert len(result["artifacts"]) == 1

    def test_unknown_top_level_field_reports_line(self):
        yaml_text = VALID_PIPELINE.replace(
            "name: build-lib-http",
            "name: build-lib-http\nextra_field: true",
        )
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert "unknown field 'extra_field'" in str(exc.value)
        assert exc.value.line is not None

    def test_missing_required_field(self):
        data = self.parser.parse_and_validate(VALID_PIPELINE)
        del data["version"]
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(data)
        assert "missing required field 'version'" in str(exc.value)

    def test_unknown_job_field(self):
        yaml_text = VALID_PIPELINE.replace(
            "    runtime: alpine:3.18",
            "    runtime: alpine:3.18\n    invalid: true",
        )
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert "unknown field 'invalid'" in str(exc.value)

    def test_invalid_semver_version(self):
        yaml_text = VALID_PIPELINE.replace("version: 1.0.0", "version: not-semver")
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert "semver" in str(exc.value).lower()

    def test_invalid_dependency_constraint(self):
        yaml_text = VALID_PIPELINE.replace('version: "^1.0.0"', 'version: "latest"')
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert "invalid semver constraint" in str(exc.value)

    def test_job_needs_unknown_job(self):
        yaml_text = VALID_PIPELINE.replace(
            "    runtime: alpine:3.18",
            "    needs: [missing]\n    runtime: alpine:3.18",
        )
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert "needs unknown job" in str(exc.value)

    def test_artifact_path_not_produced(self):
        yaml_text = VALID_PIPELINE.replace("path: ./out.tar.gz", "path: ./missing.bin")
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert "not produced by any job step" in str(exc.value)

    def test_artifact_path_traversal_rejected(self):
        yaml_text = VALID_PIPELINE.replace("path: ./out.tar.gz", "path: ../etc/passwd")
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert ".." in str(exc.value)

    def test_invalid_memory(self):
        yaml_text = VALID_PIPELINE.replace("memory: 512Mi", "memory: lots")
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert "memory" in str(exc.value).lower()

    def test_step_missing_run(self):
        yaml_text = VALID_PIPELINE.replace(
            '        run: "sh ./test.sh"',
            "",
        )
        with pytest.raises(ParseError) as exc:
            self.parser.parse_and_validate(yaml_text)
        assert "missing required field 'run'" in str(exc.value)


class TestSemverConstraintValidation:
    @pytest.mark.parametrize(
        "constraint",
        ["1.0.0", "^1.0.0", "~1.2.3", ">=1.0.0 <2.0.0", ">1.0.0 <=2.0.0"],
    )
    def test_valid_constraints(self, constraint):
        assert is_valid_semver_constraint(constraint)

    @pytest.mark.parametrize("constraint", ["latest", "*", "^", "1.x"])
    def test_invalid_constraints(self, constraint):
        assert not is_valid_semver_constraint(constraint)
