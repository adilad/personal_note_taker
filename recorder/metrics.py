"""Prometheus metrics definitions (no-op stubs if prometheus_client not installed)."""
from __future__ import annotations


class _Noop:
    def inc(self, *a, **kw):
        pass

    def observe(self, *a, **kw):
        pass

    def set(self, *a, **kw):
        pass

    def labels(self, **kw):
        return self


try:
    from prometheus_client import Counter, Gauge, Histogram  # type: ignore

    segments_total: Counter | _Noop = Counter(
        "recorder_segments_total",
        "Total number of segments processed",
    )
    transcription_duration: Histogram | _Noop = Histogram(
        "recorder_transcription_duration_seconds",
        "Time spent transcribing audio",
        buckets=[1, 5, 10, 30, 60, 120, 300],
    )
    llm_duration: Histogram | _Noop = Histogram(
        "recorder_llm_duration_seconds",
        "Time spent on LLM calls",
        labelnames=["provider"],
        buckets=[1, 5, 10, 30, 60, 120, 300],
    )
    llm_errors_total: Counter | _Noop = Counter(
        "recorder_llm_errors_total",
        "Total LLM errors",
        labelnames=["provider"],
    )
    queue_depth: Gauge | _Noop = Gauge(
        "recorder_queue_depth",
        "Current depth of the processing queue",
    )
    audio_files_total: Gauge | _Noop = Gauge(
        "recorder_audio_files_total",
        "Total number of audio files on disk",
    )
    audio_disk_bytes: Gauge | _Noop = Gauge(
        "recorder_audio_disk_bytes",
        "Total bytes used by audio files",
    )
    PROMETHEUS_AVAILABLE = True

except ImportError:
    segments_total = _Noop()
    transcription_duration = _Noop()
    llm_duration = _Noop()
    llm_errors_total = _Noop()
    queue_depth = _Noop()
    audio_files_total = _Noop()
    audio_disk_bytes = _Noop()
    PROMETHEUS_AVAILABLE = False
