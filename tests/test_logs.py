import json

from engine.logs import LogStreamer


def test_log_streamer_writes_json_lines_and_reads_text(tmp_path):
    streamer = LogStreamer(str(tmp_path))

    streamer.write("run-1", "hello", job="build")
    streamer.write("run-1", "done", job="test")

    text = "".join(streamer.text_events("run-1", follow=False, is_complete=lambda: True))

    assert "[build] hello" in text
    assert "[test] done" in text


def test_log_streamer_sse_events_have_required_shape(tmp_path):
    streamer = LogStreamer(str(tmp_path))
    streamer.write("run-1", "hello", job="build")

    raw_event = next(streamer.sse_events("run-1", follow=False, is_complete=lambda: True))
    payload = json.loads(raw_event.removeprefix("data: ").strip())

    assert set(payload) == {"ts", "job", "line"}
    assert payload["job"] == "build"
    assert payload["line"] == "hello"
