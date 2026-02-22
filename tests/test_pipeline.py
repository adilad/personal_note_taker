"""Tests for pipeline — bounded queue backpressure, deduplication."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch


def test_pipeline_start_stop():
    """Pipeline can start and stop cleanly."""
    from recorder.pipeline.processor import RecorderPipeline

    with (
        patch("recorder.pipeline.processor.record_loop"),
        patch("recorder.pipeline.processor.segmenter_loop"),
        patch("recorder.pipeline.processor.hourly_worker"),
        patch("recorder.pipeline.processor.get_model", return_value=MagicMock()),
    ):
        p = RecorderPipeline()
        assert p.start() is True
        assert p.start() is False  # already running
        status = p.status()
        assert status["running"] is True
        assert p.stop() is True
        assert p.stop() is False  # already stopped


def test_enqueue_returns_false_when_full():
    """enqueue() returns False when proc_q is at capacity."""
    from recorder.pipeline.processor import RecorderPipeline

    p = RecorderPipeline()
    # Fill the queue to capacity (maxsize=50)
    for i in range(50):
        p.proc_q.put_nowait(("path", "ts", "ts", 1.0))

    result = p.enqueue("new.wav", "ts", "ts", 1.0)
    assert result is False


def test_enqueue_returns_true_when_space():
    from recorder.pipeline.processor import RecorderPipeline

    p = RecorderPipeline()
    result = p.enqueue("test.wav", "2026-01-01T00:00:00", "2026-01-01T00:00:30", 30.0)
    assert result is True


def test_live_transcript_holder():
    from recorder.pipeline.processor import _LiveTranscriptHolder

    holder = _LiveTranscriptHolder()
    assert holder.get() == ""
    holder.set("Hello world")
    assert holder.get() == "Hello world"
    holder.set("")
    assert holder.get() == ""


def test_live_transcript_thread_safety():
    """Concurrent reads and writes should not raise."""
    from recorder.pipeline.processor import _LiveTranscriptHolder

    holder = _LiveTranscriptHolder()
    errors = []

    def writer():
        for i in range(100):
            try:
                holder.set(f"text {i}")
            except Exception as e:
                errors.append(e)

    def reader():
        for _ in range(100):
            try:
                _ = holder.get()
            except Exception as e:
                errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
