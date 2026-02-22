"""
Thin entrypoint — replaces the top-level recorder.py block.

Usage:
    python main.py                     # forever-listen mode (no UI)
    python main.py --flask-ui          # Flask UI on http://127.0.0.1:5000
    python main.py --only-hourly       # run only the hourly digest worker
    python main.py --migrate           # run DB migrations then exit
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys

from recorder.config import settings
from recorder.logging_config import configure_logging

# Configure structured logging before anything else
configure_logging(settings.log_dir)
logger = logging.getLogger(__name__)


def _warn_if_no_api_key() -> None:
    if not settings.recorder_api_key:
        logger.warning(
            "RECORDER_API_KEY is not set — all API endpoints are open. "
            "Set RECORDER_API_KEY in .env to enable authentication."
        )


def _run_migrations() -> None:
    from alembic.config import Config
    from alembic import command

    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    cfg = Config()
    cfg.set_main_option("script_location", migrations_dir)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{settings.db_path}")
    command.upgrade(cfg, "head")
    logger.info("migrations.complete")


def _ensure_schema() -> None:
    """Create tables if they don't exist (quick path — no Alembic required)."""
    from recorder.db.session import Base, engine
    import recorder.db.models  # noqa: F401

    Base.metadata.create_all(engine)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enterprise voice recorder")
    parser.add_argument(
        "--only-hourly",
        action="store_true",
        help="Run only the hourly digest worker (no audio capture)",
    )
    parser.add_argument(
        "--flask-ui",
        action="store_true",
        help="Start Flask web UI (does not auto-start recording)",
    )
    parser.add_argument(
        "--ui-host",
        default=settings.recorder_ui_host,
        help=f"Flask UI host (default: {settings.recorder_ui_host})",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=settings.recorder_ui_port,
        help=f"Flask UI port (default: {settings.recorder_ui_port})",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Run Alembic migrations and exit",
    )
    args = parser.parse_args()

    _warn_if_no_api_key()

    if args.migrate:
        _run_migrations()
        return

    # Ensure schema exists (fast path for first run)
    _ensure_schema()

    if args.only_hourly:
        import threading
        stop_flag = threading.Event()
        logger.info("mode.hourly_only")
        from recorder.pipeline.hourly import hourly_worker
        try:
            hourly_worker(stop_flag)
        except KeyboardInterrupt:
            logger.info("mode.hourly_only.stopping")
            stop_flag.set()
        return

    if args.flask_ui:
        from recorder.api.app import create_app
        from recorder.pipeline.processor import RecorderPipeline

        pipeline = RecorderPipeline()

        # Also wire event_bus to pipeline so SSE gets events
        from recorder.api.sse import event_bus
        pipeline.event_bus = event_bus

        app = create_app(pipeline=pipeline)

        host = args.ui_host
        port = args.ui_port

        # Find a free port if requested one is busy
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sock.connect_ex((host, port)) == 0:
                logger.warning(
                    "port.in_use",
                    extra={"port": port, "trying": port + 1},
                )
                port += 1
        finally:
            sock.close()

        logger.info("flask.starting", extra={"host": host, "port": port})
        print(f"Open http://{host}:{port} in your browser.")
        app.run(host=host, port=port, debug=False, threaded=True)
        return

    # Default: start recording immediately (headless mode)
    from recorder.pipeline.processor import RecorderPipeline
    from recorder.api.sse import event_bus

    pipeline = RecorderPipeline()
    pipeline.event_bus = event_bus

    logger.info("mode.headless_start")
    print(
        f"[recorder] Starting. SAMPLE_RATE={settings.sample_rate}, "
        f"OFF_TIME_SEC={settings.off_time_sec}, "
        f"MAX_SEGMENT_SEC={settings.max_segment_sec}, "
        f"VAD={settings.vad_aggressiveness}"
    )
    pipeline.start()
    try:
        import time
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("mode.headless.stopping")
        pipeline.stop()


if __name__ == "__main__":
    main()
