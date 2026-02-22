"""Export endpoint — JSON, Markdown, CSV (Phase 10)."""
from __future__ import annotations

import csv
import datetime
import io
import json
import logging

from flask import Blueprint, Response, jsonify, request

from recorder.api.middleware import inject_request_id, require_api_key
from recorder.db.repository import SegmentRepository
from recorder.db.session import SessionLocal

logger = logging.getLogger(__name__)
bp = Blueprint("export", __name__)


def _default_start() -> str:
    return datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


@bp.get("/api/v1/export")
@inject_request_id
@require_api_key
def export_segments():
    """
    Query params:
      format  — json | markdown | csv  (default: json)
      start   — ISO datetime (default: today 00:00)
      end     — ISO datetime (default: none)
    """
    fmt = request.args.get("format", "json").lower()
    start = request.args.get("start", _default_start())
    end = request.args.get("end")

    db = SessionLocal()
    try:
        repo = SegmentRepository(db)
        segs = repo.list(start=start, end=end, limit=5000)

        if fmt == "json":
            data = json.dumps(
                {"segments": [s.to_dict() for s in segs], "exported_at": datetime.datetime.now().isoformat()},
                indent=2,
            )
            return Response(
                data,
                mimetype="application/json",
                headers={"Content-Disposition": "attachment; filename=recordings.json"},
            )

        elif fmt == "markdown":
            lines = ["# Voice Recordings Export", ""]
            for s in reversed(segs):
                ts = datetime.datetime.fromisoformat(s.start_ts).strftime("%H:%M")
                cat = f" · {s.category}" if s.category else ""
                lines.append(f"## [{ts}]{cat}")
                if s.transcript:
                    lines.append(f"\n**Transcript:**\n{s.transcript}")
                if s.summary:
                    lines.append(f"\n**Summary:**\n{s.summary}")
                if s.action_items:
                    lines.append(f"\n**Action Items:**\n{s.action_items}")
                lines.append("")
            md = "\n".join(lines)
            return Response(
                md,
                mimetype="text/markdown",
                headers={"Content-Disposition": "attachment; filename=recordings.md"},
            )

        elif fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "id", "start_ts", "end_ts", "duration_sec",
                "transcript", "summary", "keywords",
                "category", "sentiment", "participants",
                "action_items", "questions",
            ])
            for s in segs:
                writer.writerow([
                    s.id, s.start_ts, s.end_ts, s.duration_sec,
                    s.transcript, s.summary, s.keywords,
                    s.category, s.sentiment, s.participants,
                    s.action_items, s.questions,
                ])
            return Response(
                output.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment; filename=recordings.csv"},
            )

        else:
            return jsonify({"ok": False, "error": f"Unknown format: {fmt}"}), 400

    except Exception as exc:
        logger.error("export.error", extra={"error": str(exc)})
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()
