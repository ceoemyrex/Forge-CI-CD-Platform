import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional

logger = logging.getLogger(__name__)


def utc_now() -> str:
    """Return an ISO timestamp with UTC timezone information."""
    return datetime.now(timezone.utc).isoformat()


class LogStreamer:
    """
    Disk-backed log storage for pipeline runs.

    The log file is JSON Lines: one JSON object per output line. That makes it
    easy to stream one record at a time without loading the whole file.
    """

    def __init__(self, storage_root: str, poll_interval: float = 0.25):
        self.storage_root = Path(storage_root)
        self.log_dir = self.storage_root / "logs"
        self.poll_interval = poll_interval
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.jsonl"

    def write(self, run_id: str, line: str, job: str = "unknown", ts: Optional[str] = None) -> Dict[str, str]:
        """
        Append one log event to disk immediately.

        Args:
            run_id: Pipeline run ID.
            line: Text from stdout/stderr.
            job: Job name that produced the line.
            ts: Optional timestamp. If omitted, timestamp is created now.
        """
        event = {
            "ts": ts or utc_now(),
            "job": job,
            "line": line.rstrip("\n"),
        }
        # this is a critical path for performance, so we write directly to the file here instead of using a higher-level library that might buffer or add overhead. We also
        # flush after every write and call os.fsync() to ensure the data is on disk, so that clients can see it immediately even if the process crashes. 
        # We catch OSError just in case the disk is full or there's some other issue, 
        # but we don't want to raise an exception from here since that could crash the whole pipeline run.
        try:
            with self.path_for(run_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            logger.exception("Could not write log line for run %s", run_id)

        return event

    def read_events(self, run_id: str) -> Iterator[Dict[str, str]]:
        """
        Yield all existing log events line by line.

        This is safe for large logs because it never calls read() or readlines().
        """
        log_path = self.path_for(run_id)
        if not log_path.exists():
            return

        with log_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                event = self._parse_line(raw_line)
                if event:
                    yield event

    def follow_events(
        self,
        run_id: str,
        is_complete: Callable[[], bool],
    ) -> Iterator[Dict[str, str]]:
        """
        Yield backlog first, then keep yielding new events until the run is done.

        A client can connect mid-build and still receives every line already on
        disk before receiving new lines.
        """
        log_path = self.path_for(run_id)
        position = 0

        while not log_path.exists() and not is_complete():
            time.sleep(self.poll_interval)

        if log_path.exists():
            with log_path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    event = self._parse_line(raw_line)
                    if event:
                        yield event
                position = handle.tell()

        while not is_complete():
            if not log_path.exists():
                time.sleep(self.poll_interval)
                continue

            with log_path.open("r", encoding="utf-8") as handle:
                handle.seek(position)
                while True:
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    event = self._parse_line(raw_line)
                    if event:
                        yield event
                position = handle.tell()

            time.sleep(self.poll_interval)

        if log_path.exists():
            with log_path.open("r", encoding="utf-8") as handle:
                handle.seek(position)
                for raw_line in handle:
                    event = self._parse_line(raw_line)
                    if event:
                        yield event

    def sse_events(
        self,
        run_id: str,
        follow: bool,
        is_complete: Callable[[], bool],
    ) -> Iterator[str]:
        """Return events formatted for Server-Sent Events."""
        source = self.follow_events(run_id, is_complete) if follow else self.read_events(run_id)
        for event in source:
            yield f"data: {json.dumps(event)}\n\n"

    def text_events(
        self,
        run_id: str,
        follow: bool,
        is_complete: Callable[[], bool],
    ) -> Iterator[str]:
        """Return readable text log lines for non-SSE clients."""
        source = self.follow_events(run_id, is_complete) if follow else self.read_events(run_id)
        for event in source:
            yield f"[{event['ts']}] [{event['job']}] {event['line']}\n"

    def _parse_line(self, raw_line: str) -> Optional[Dict[str, str]]:
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed log line")
            return None

        if not {"ts", "job", "line"}.issubset(event):
            logger.warning("Skipping incomplete log line")
            return None

        return {
            "ts": str(event["ts"]),
            "job": str(event["job"]),
            "line": str(event["line"]),
        }
