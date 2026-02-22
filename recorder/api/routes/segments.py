"""Segments endpoints — list, detail, patch (tags/important)."""

from __future__ import annotations

import datetime
import json
import logging

from flask import Blueprint, jsonify, request

from recorder.api.middleware import inject_request_id, require_api_key
from recorder.db.repository import SegmentRepository
from recorder.db.session import SessionLocal

logger = logging.getLogger(__name__)
bp = Blueprint("segments", __name__)


def _today_iso() -> str:
    return datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


@bp.get("/api/v1/segments")
@inject_request_id
@require_api_key
def list_segments():
    """
    Query params:
      start, end   — ISO datetime bounds (default: today)
      limit        — max results (default 200)
      offset       — pagination offset (default 0)
      q            — full-text search
      tag          — filter by tag
    """
    try:
        start = request.args.get("start", _today_iso())
        end = request.args.get("end")
        limit = min(int(request.args.get("limit", 200)), 500)
        offset = int(request.args.get("offset", 0))
        q = request.args.get("q") or None
        tag = request.args.get("tag") or None

        db = SessionLocal()
        try:
            repo = SegmentRepository(db)
            segs = repo.list(start=start, end=end, limit=limit, offset=offset, q=q, tag=tag)
            return jsonify({"ok": True, "segments": [s.to_dict() for s in segs]})
        finally:
            db.close()
    except Exception as exc:
        logger.error("segments.list_error", extra={"error": str(exc)})
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/api/v1/segments/<int:segment_id>")
@inject_request_id
@require_api_key
def get_segment(segment_id: int):
    db = SessionLocal()
    try:
        repo = SegmentRepository(db)
        seg = repo.get_by_id(segment_id)
        if not seg:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "segment": seg.to_dict()})
    finally:
        db.close()


@bp.patch("/api/v1/segments/<int:segment_id>")
@inject_request_id
@require_api_key
def patch_segment(segment_id: int):
    """
    Allowed patches:
      important: bool
      tags: list[str]
    """
    body = request.get_json(silent=True) or {}
    allowed = {"important", "tags"}
    updates = {k: v for k, v in body.items() if k in allowed}

    if "tags" in updates:
        tags = updates["tags"]
        if not isinstance(tags, list):
            return jsonify({"ok": False, "error": "tags must be an array"}), 400
        updates["tags"] = json.dumps([str(t) for t in tags])

    if not updates:
        return jsonify({"ok": False, "error": "no valid fields to update"}), 400

    db = SessionLocal()
    try:
        repo = SegmentRepository(db)
        seg = repo.update(segment_id, **updates)
        if not seg:
            return jsonify({"ok": False, "error": "not found"}), 404

        from recorder.api.sse import event_bus

        event_bus.publish("segment.updated", seg.to_dict())

        return jsonify({"ok": True, "segment": seg.to_dict()})
    finally:
        db.close()


@bp.delete("/api/v1/segments/<int:segment_id>")
@inject_request_id
@require_api_key
def delete_segment(segment_id: int):
    db = SessionLocal()
    try:
        repo = SegmentRepository(db)
        deleted = repo.soft_delete(segment_id)
        if not deleted:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True})
    finally:
        db.close()
