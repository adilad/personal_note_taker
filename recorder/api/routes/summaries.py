"""Summary endpoints — daily and hourly digests."""
from __future__ import annotations

import datetime
import logging

from flask import Blueprint, jsonify, request

from recorder.api.middleware import inject_request_id, require_api_key
from recorder.db.repository import DailyDigestRepository, HourlyDigestRepository, SegmentRepository
from recorder.db.session import SessionLocal
from recorder.llm.client import summarize_daily

logger = logging.getLogger(__name__)
bp = Blueprint("summaries", __name__)


@bp.get("/api/v1/summaries/daily")
@inject_request_id
@require_api_key
def get_daily_summary():
    """Return cached daily digest (generate on the fly if not cached)."""
    date_str = request.args.get(
        "date", datetime.datetime.now().strftime("%Y-%m-%d")
    )
    db = SessionLocal()
    try:
        daily_repo = DailyDigestRepository(db)
        existing = daily_repo.get_by_date(date_str)
        if existing and existing.summary:
            return jsonify({
                "ok": True,
                "date": date_str,
                "summary": existing.summary,
                "action_items": existing.action_items or "",
                "cached": True,
            })

        # Generate on the fly
        seg_repo = SegmentRepository(db)
        segs = seg_repo.list_for_date(date_str)
        texts = []
        for s in segs:
            txt = (s.transcript or s.summary or "").strip()
            if txt:
                ts = datetime.datetime.fromisoformat(s.start_ts).strftime("%I:%M %p")
                texts.append(f"[{ts}] {txt}")

        combined = "\n\n".join(texts)
        summary = summarize_daily(combined)

        all_actions = []
        for s in segs:
            if s.action_items:
                all_actions.extend(
                    [x.strip() for x in s.action_items.split("\n") if x.strip()]
                )
        action_text = "\n".join(all_actions)

        daily_repo.upsert(date_str, summary, action_text)

        return jsonify({
            "ok": True,
            "date": date_str,
            "summary": summary,
            "action_items": action_text,
            "cached": False,
        })
    except Exception as exc:
        logger.error("summaries.daily_error", extra={"error": str(exc)})
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()


@bp.post("/api/v1/summaries/daily")
@inject_request_id
@require_api_key
def generate_daily_summary():
    """Force-regenerate the daily digest for a given date."""
    body = request.get_json(silent=True) or {}
    date_str = body.get("date", datetime.datetime.now().strftime("%Y-%m-%d"))
    db = SessionLocal()
    try:
        from recorder.pipeline.hourly import run_daily_digest
        run_daily_digest(db, date_str)
        daily_repo = DailyDigestRepository(db)
        digest = daily_repo.get_by_date(date_str)
        return jsonify({
            "ok": True,
            "date": date_str,
            "summary": digest.summary if digest else "",
        })
    except Exception as exc:
        logger.error("summaries.generate_error", extra={"error": str(exc)})
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()


@bp.get("/api/v1/summaries/range")
@inject_request_id
@require_api_key
def get_range_summary():
    """
    Summarise all transcripts between two dates (inclusive).

    Query params:
      start  — YYYY-MM-DD  (required)
      end    — YYYY-MM-DD  (required)
    """
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    if not start_date or not end_date:
        return jsonify({"ok": False, "error": "start and end dates are required"}), 400

    try:
        start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.datetime.strptime(end_date, "%Y-%m-%d") + datetime.timedelta(days=1)
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()
    except ValueError:
        return jsonify({"ok": False, "error": "Dates must be YYYY-MM-DD"}), 400

    db = SessionLocal()
    try:
        seg_repo = SegmentRepository(db)
        segs = seg_repo.list(start=start_iso, end=end_iso, limit=5000)

        if not segs:
            return jsonify({
                "ok": True,
                "start": start_date,
                "end": end_date,
                "segment_count": 0,
                "summary": "(no transcripts found in this date range)",
                "action_items": "",
            })

        texts = []
        for s in segs:
            txt = (s.transcript or s.summary or "").strip()
            if txt:
                ts = datetime.datetime.fromisoformat(s.start_ts).strftime("%b %d %I:%M %p")
                texts.append(f"[{ts}] {txt}")

        combined = "\n\n".join(reversed(texts))  # chronological order
        summary = summarize_daily(combined)

        all_actions = []
        for s in segs:
            if s.action_items:
                all_actions.extend(
                    [x.strip() for x in s.action_items.split("\n") if x.strip()]
                )
        action_text = "\n".join(all_actions)

        return jsonify({
            "ok": True,
            "start": start_date,
            "end": end_date,
            "segment_count": len(segs),
            "summary": summary,
            "action_items": action_text,
        })
    except Exception as exc:
        logger.error("summaries.range_error", extra={"error": str(exc)})
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()


@bp.get("/api/v1/summaries/hourly")
@inject_request_id
@require_api_key
def get_hourly_summaries():
    """Return recent hourly digests."""
    limit = int(request.args.get("limit", 24))
    db = SessionLocal()
    try:
        repo = HourlyDigestRepository(db)
        digests = repo.list_recent(limit=limit)
        return jsonify({
            "ok": True,
            "digests": [
                {
                    "hour_start": d.hour_start,
                    "hour_end": d.hour_end,
                    "summary": d.summary or "",
                }
                for d in digests
            ],
        })
    finally:
        db.close()
