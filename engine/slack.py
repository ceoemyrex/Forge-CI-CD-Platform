import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class SlackAlertClient:
    """Small Slack webhook client that sends Block Kit messages."""

    def __init__(self, webhook_url: str = "", notify_tags: str = ""):
        self.webhook_url = webhook_url or ""
        self.notify_tags = notify_tags or ""

    def pipeline_started(self, pipeline: str, run_id: str, user: str) -> None:
        self._send(
            title="Pipeline started",
            color=":large_blue_circle:",
            fields={
                "Pipeline": pipeline,
                "Run ID": run_id,
                "Submitted by": user,
            },
        )

    def pipeline_succeeded(self, pipeline: str, run_id: str, duration_seconds: float) -> None:
        self._send(
            title="Pipeline succeeded",
            color=":white_check_mark:",
            fields={
                "Pipeline": pipeline,
                "Run ID": run_id,
                "Duration": f"{duration_seconds}s",
            },
        )

    def pipeline_failed(
        self,
        pipeline: str,
        run_id: str,
        duration_seconds: float,
        failing_job: str,
    ) -> None:
        self._send(
            title="Pipeline failed",
            color=":x:",
            fields={
                "Pipeline": pipeline,
                "Run ID": run_id,
                "Duration": f"{duration_seconds}s",
                "Failing job": failing_job,
            },
        )

    def integrity_failure(
        self,
        artifact: str,
        expected_sha256: str,
        actual_sha256: str,
        run_id: str,
        notify_tags: Optional[str] = None,
    ) -> None:
        tags = notify_tags if notify_tags is not None else self.notify_tags
        self._send(
            title="Integrity failure",
            color=":rotating_light:",
            fields={
                "Artifact": artifact,
                "Expected SHA-256": expected_sha256,
                "Actual SHA-256": actual_sha256,
                "Run ID": run_id,
                "Notify": tags,
            },
        )

    def resolution_failure(self, pipeline: str, run_id: str, details: str) -> None:
        self._send(
            title="Resolution failure",
            color=":warning:",
            fields={
                "Pipeline": pipeline,
                "Run ID": run_id,
                "Details": details,
            },
        )

    def _send(self, title: str, color: str, fields: dict) -> None:
        if not self.webhook_url:
            logger.info("Slack webhook is not configured; skipping %s alert", title)
            return

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{color} {title}"},
            },
            {"type": "divider"},
        ]

        for label, value in fields.items():
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*{label}*"},
                        {"type": "mrkdwn", "text": str(value or "-")},
                    ],
                }
            )

        try:
            response = requests.post(self.webhook_url, json={"blocks": blocks}, timeout=5)
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Could not send Slack alert: %s", title)
