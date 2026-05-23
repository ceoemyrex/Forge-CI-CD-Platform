"""Slack webhook alerts using Block Kit."""

from __future__ import annotations

import logging
from typing import List, Optional

import requests

import config

logger = logging.getLogger(__name__)


def _post(blocks: list, text: str) -> None:
    url = config.SLACK_WEBHOOK_URL
    if not url or url.startswith("SLACK"):
        return
    try:
        requests.post(
            url,
            json={"text": text, "blocks": blocks},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Slack alert failed: %s", exc)


def _mention_line() -> str:
    users = config.SLACK_NOTIFY_USERS or []
    return " ".join(users)


def pipeline_started(pipeline: str, run_id: str, user: str = "unknown") -> None:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Pipeline Started"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Pipeline:*\n{pipeline}"},
                {"type": "mrkdwn", "text": f"*Run ID:*\n{run_id}"},
                {"type": "mrkdwn", "text": f"*Submitted by:*\n{user}"},
            ],
        },
    ]
    _post(blocks, f"Pipeline {pipeline} started (run {run_id})")


def pipeline_succeeded(pipeline: str, run_id: str, duration_sec: float) -> None:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Pipeline Succeeded"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Pipeline:*\n{pipeline}"},
                {"type": "mrkdwn", "text": f"*Run ID:*\n{run_id}"},
                {"type": "mrkdwn", "text": f"*Duration:*\n{duration_sec:.1f}s"},
            ],
        },
    ]
    _post(blocks, f"Pipeline {pipeline} succeeded (run {run_id})")


def pipeline_failed(
    pipeline: str, run_id: str, duration_sec: float, failing_job: Optional[str]
) -> None:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Pipeline Failed"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Pipeline:*\n{pipeline}"},
                {"type": "mrkdwn", "text": f"*Run ID:*\n{run_id}"},
                {"type": "mrkdwn", "text": f"*Duration:*\n{duration_sec:.1f}s"},
                {
                    "type": "mrkdwn",
                    "text": f"*Failing job:*\n{failing_job or 'unknown'}",
                },
            ],
        },
    ]
    _post(blocks, f"Pipeline {pipeline} failed (run {run_id})")


def integrity_failure(
    name: str,
    version: str,
    expected_sha256: str,
    actual_sha256: str,
    run_id: str,
) -> None:
    mention = _mention_line()
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Integrity Failure"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{mention}\nArtifact checksum mismatch detected.",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Artifact:*\n{name}@{version}"},
                {"type": "mrkdwn", "text": f"*Run ID:*\n{run_id}"},
                {"type": "mrkdwn", "text": f"*Expected SHA-256:*\n`{expected_sha256}`"},
                {"type": "mrkdwn", "text": f"*Actual SHA-256:*\n`{actual_sha256}`"},
            ],
        },
    ]
    _post(blocks, f"Integrity failure: {name}@{version} (run {run_id})")


def resolution_failure(pipeline: str, run_id: str, details: str) -> None:
    mention = _mention_line()
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Resolution Failure"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{mention}\nDependency resolution failed for *{pipeline}*.",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Run ID:* `{run_id}`\n```{details[:2800]}```"},
        },
    ]
    _post(blocks, f"Resolution failure: {pipeline} (run {run_id})")
