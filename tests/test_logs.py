import json

from engine.logs import LogStreamer


def test_log_streamer_writes_json_lines(tmp_path):
    streamer = LogStreamer(str(tmp_path))

    streamer.write("run-1", "build", "hello")
    streamer.write("run-1", "test", "done")

    combined = (tmp_path / "logs" / "run-1" / "combined.jsonl").read_text()
    events = [json.loads(line) for line in combined.splitlines()]

    assert events[0]["job"] == "build"
    assert events[0]["line"] == "hello"
    assert events[1]["job"] == "test"
    assert events[1]["line"] == "done"


def test_log_streamer_sse_events_have_required_shape(tmp_path):
    streamer = LogStreamer(str(tmp_path))
    streamer.write("run-1", "build", "hello")

    events = list(streamer.stream_sse("run-1", follow=False))
    assert len(events) == 1

    raw = events[0]
    assert raw.startswith("data: ")
    payload = json.loads(raw.removeprefix("data: ").strip())

    assert set(payload) == {"ts", "job", "line"}
    assert payload["job"] == "build"
    assert payload["line"] == "hello"
