"""Recording control endpoints and reprocess."""
from __future__ import annotations

import datetime
import logging
import os

from flask import Blueprint, jsonify, current_app

from recorder.api.middleware import inject_request_id, require_api_key
from recorder.config import settings

logger = logging.getLogger(__name__)
bp = Blueprint("recordings", __name__)


def _get_pipeline():
    return current_app.config["pipeline"]


@bp.post("/api/v1/recordings/start")
@inject_request_id
@require_api_key
def start_recording():
    pipeline = _get_pipeline()
    started = pipeline.start()
    status = pipeline.status()

    from recorder.api.sse import event_bus
    event_bus.publish("recording.started", {"running": True})

    logger.info("recording.started", extra={"started": started})
    return jsonify({"ok": True, "running": status["running"], "started": started})


@bp.post("/api/v1/recordings/stop")
@inject_request_id
@require_api_key
def stop_recording():
    pipeline = _get_pipeline()
    stopped = pipeline.stop()
    status = pipeline.status()

    from recorder.api.sse import event_bus
    event_bus.publish("recording.stopped", {"running": False})

    logger.info("recording.stopped", extra={"stopped": stopped})
    return jsonify({"ok": True, "running": status["running"], "stopped": stopped})


@bp.get("/api/v1/recordings/status")
@inject_request_id
@require_api_key
def recording_status():
    pipeline = _get_pipeline()
    return jsonify({"ok": True, **pipeline.status()})


@bp.get("/api/v1/recordings/live")
@inject_request_id
@require_api_key
def live_transcript():
    pipeline = _get_pipeline()
    status = pipeline.status()
    return jsonify({
        "ok": True,
        "transcript": status["live_transcript"],
        "running": status["running"],
    })


@bp.post("/api/v1/recordings/reprocess")
@inject_request_id
@require_api_key
def reprocess():
    """Find WAV files in the audio dir that have no DB entry and queue them."""
    from recorder.db.repository import SegmentRepository
    from recorder.db.session import SessionLocal

    pipeline = _get_pipeline()
    audio_dir = settings.audio_dir

    db = SessionLocal()
    try:
        repo = SegmentRepository(db)
        queued = 0
        for fname in sorted(os.listdir(audio_dir)):
            if not fname.endswith(".wav"):
                continue
            abs_path = str(audio_dir / fname)
            if repo.exists_by_audio_key(fname) or repo.exists_by_audio_key(abs_path):
                continue
            try:
                ts_part = fname.replace("seg_", "").replace(".wav", "")
                date_part, time_part = ts_part.split("T")
                ts = datetime.datetime.fromisoformat(
                    f"{date_part}T{time_part.replace('-', ':')}"
                )
            except Exception:
                ts = datetime.datetime.now()
            duration = os.path.getsize(abs_path) / 2 / settings.sample_rate
            if pipeline.enqueue(abs_path, ts.isoformat(), ts.isoformat(), duration):
                queued += 1
        return jsonify({"ok": True, "queued": queued})
    except Exception as exc:
        logger.error("reprocess.error", extra={"error": str(exc)})
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()
