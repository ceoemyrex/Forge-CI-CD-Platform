"""Real-time log persistence and SSE streaming."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Generator, Iterable, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LogStreamer:
    """
    Persists timestamped log events to disk and streams them without buffering.

    Layout:
      {log_dir}/{run_id}/{job_name}.log   — one JSON object per line
      {log_dir}/{run_id}/combined.jsonl   — merged stream for SSE tailing
    """

    def __init__(self, storage_root: str):
        self.log_dir = os.path.join(storage_root, "logs")
        self._running: dict[str, Callable[[], str]] = {}

    def register_status_checker(self, run_id: str, checker: Callable[[], str]) -> None:
        self._running[run_id] = checker

    def unregister_status_checker(self, run_id: str) -> None:
        self._running.pop(run_id, None)

    def _run_dir(self, run_id: str) -> Path:
        path = Path(self.log_dir) / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, run_id: str, job: str, line: str) -> dict:
        """Append one log event; return the event dict."""
        ts = _utcnow_iso()
        event = {"ts": ts, "job": job, "line": line}
        payload = json.dumps(event, separators=(",", ":")) + "\n"

        run_dir = self._run_dir(run_id)
        job_path = run_dir / f"{job}.log"
        combined_path = run_dir / "combined.jsonl"

        with open(job_path, "a", encoding="utf-8") as jf:
            jf.write(payload)
        with open(combined_path, "a", encoding="utf-8") as cf:
            cf.write(payload)

        return event

    def _iter_file_from(self, path: Path, offset: int) -> tuple[Iterable[str], int]:
        if not path.exists():
            return [], offset
        lines: list[str] = []
        with open(path, "r", encoding="utf-8") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
        if chunk:
            lines = chunk.splitlines(keepends=True)
        return lines, new_offset

    def stream_sse(
        self,
        run_id: str,
        follow: bool = False,
        poll_interval: float = 0.1,
    ) -> Generator[str, None, None]:
        """
        Yield SSE-formatted events from combined.jsonl.
        Sends backlog first, then tails new lines when follow=True.
        """
        combined = self._run_dir(run_id) / "combined.jsonl"
        offset = 0

        while True:
            lines, offset = self._iter_file_from(combined, offset)
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    event = {"ts": _utcnow_iso(), "job": "system", "line": raw}
                yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"

            if not follow:
                break

            checker = self._running.get(run_id)
            status = checker() if checker else "unknown"
            if status not in ("queued", "running"):
                # Drain any final lines written at completion
                lines, offset = self._iter_file_from(combined, offset)
                for raw in lines:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        event = {"ts": _utcnow_iso(), "job": "system", "line": raw}
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                break

            time.sleep(poll_interval)
