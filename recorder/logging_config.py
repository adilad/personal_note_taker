"""Structured JSON logging configuration."""
from __future__ import annotations

import logging
import logging.handlers
import threading
from pathlib import Path

_request_local = threading.local()


def get_request_id() -> str:
    return getattr(_request_local, "request_id", "-")


def set_request_id(rid: str) -> None:
    _request_local.request_id = rid


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()  # type: ignore[attr-defined]
        return True


def configure_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """Call once at startup to configure the root logger."""
    log_dir.mkdir(parents=True, exist_ok=True)

    try:
        from pythonjsonlogger import jsonlogger  # type: ignore

        fmt = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(request_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    except ImportError:
        fmt = logging.Formatter(  # type: ignore[assignment]
            "%(asctime)s [%(levelname)s] %(name)s rid=%(request_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    rid_filter = _RequestIdFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "recorder.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(rid_filter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(rid_filter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
