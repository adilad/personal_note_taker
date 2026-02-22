"""Audio file streaming endpoint (Phase 7)."""
from __future__ import annotations

import logging
import mimetypes

from flask import Blueprint, send_file

from recorder.api.middleware import inject_request_id, require_api_key
from recorder.db.repository import SegmentRepository
from recorder.db.session import SessionLocal
from recorder.storage.audio_store import key_to_path

logger = logging.getLogger(__name__)
bp = Blueprint("audio", __name__)


@bp.get("/api/v1/segments/<int:segment_id>/audio")
@inject_request_id
@require_api_key
def stream_audio(segment_id: int):
    """Stream the audio file for a segment."""
    db = SessionLocal()
    try:
        repo = SegmentRepository(db)
        seg = repo.get_by_id(segment_id)
        if not seg:
            from flask import jsonify
            return jsonify({"ok": False, "error": "not found"}), 404

        # Try audio_key first, fall back to audio_path
        key = seg.audio_key or ""
        path = key_to_path(key) if key else None

        if path is None or not path.exists():
            # Fall back to absolute audio_path
            import pathlib
            fallback = pathlib.Path(seg.audio_path) if seg.audio_path else None
            if fallback and fallback.exists():
                path = fallback
            else:
                from flask import jsonify
                return jsonify({"ok": False, "error": "audio file not found"}), 404

        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type:
            mime_type = "audio/wav"

        return send_file(str(path), mimetype=mime_type, as_attachment=False)
    finally:
        db.close()
