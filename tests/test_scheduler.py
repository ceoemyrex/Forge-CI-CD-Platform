import pytest

from engine.scheduler import DAGScheduler, JobStatus


def test_scheduler_detects_cycle_with_path():
    jobs = {
        "a": {"needs": ["b"]},
        "b": {"needs": ["a"]},
    }

    with pytest.raises(ValueError, match="Dependency cycles detected"):
        DAGScheduler(jobs)


def test_scheduler_marks_dependents_skipped():
    jobs = {
        "build": {},
        "package": {"needs": ["build"]},
        "deploy": {"needs": ["package"]},
    }

    scheduler = DAGScheduler(jobs)
    scheduler.mark_job_failed("build")

    assert scheduler.get_job_status("build") == JobStatus.FAILED
    assert scheduler.get_job_status("package") == JobStatus.SKIPPED
    assert scheduler.get_job_status("deploy") == JobStatus.SKIPPED
