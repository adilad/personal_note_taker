"""Hourly digest worker — runs in background, generates hour summaries."""

from __future__ import annotations

import datetime
import logging
import time

from recorder.config import settings
from recorder.db.repository import (
    DailyDigestRepository,
    HourlyDigestRepository,
    SegmentRepository,
)
from recorder.db.session import SessionLocal
from recorder.llm.client import summarize_daily, summarize_hourly
from recorder.storage.audio_store import delete_segment

logger = logging.getLogger(__name__)


def _hour_floor(dt: datetime.datetime) -> datetime.datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def run_hourly_digest(db, hour_start: datetime.datetime) -> None:
    seg_repo = SegmentRepository(db)
    hr_repo = HourlyDigestRepository(db)

    existing = hr_repo.get_by_hour(hour_start.isoformat())
    if existing:
        return

    segs = seg_repo.list_for_hour(hour_start)
    hour_end = hour_start + datetime.timedelta(hours=1)
    texts = [
        (s.transcript or s.summary or "").strip()
        for s in segs
        if (s.transcript or s.summary or "").strip()
    ]

    if not texts:
        hr_repo.upsert(hour_start.isoformat(), hour_end.isoformat(), "")
        logger.info("hourly_digest.no_content", extra={"hour": str(hour_start)})
        return

    combined = "\n".join(texts)
    summary = summarize_hourly(combined)
    hr_repo.upsert(hour_start.isoformat(), hour_end.isoformat(), summary)
    preview = (summary[:140] + "…") if len(summary) > 140 else summary
    logger.info(
        "hourly_digest.done",
        extra={"hour": str(hour_start), "preview": preview},
    )


def run_daily_digest(db, date_str: str) -> None:
    """Generate and persist a daily digest."""
    seg_repo = SegmentRepository(db)
    daily_repo = DailyDigestRepository(db)

    segs = seg_repo.list_for_date(date_str)
    texts = []
    for s in segs:
        txt = (s.transcript or s.summary or "").strip()
        if txt:
            ts = datetime.datetime.fromisoformat(s.start_ts).strftime("%I:%M %p")
            texts.append(f"[{ts}] {txt}")

    combined = "\n\n".join(texts)
    summary = summarize_daily(combined)

    # Compile action items from all segments
    all_actions = []
    for s in segs:
        if s.action_items:
            items = s.action_items if isinstance(s.action_items, list) else [s.action_items]
            all_actions.extend(items)
    action_text = "\n".join(all_actions)

    daily_repo.upsert(date_str, summary, action_text)
    logger.info("daily_digest.done", extra={"date": date_str})


def run_retention_cleanup(db) -> None:
    """Delete audio files older than AUDIO_RETENTION_DAYS."""
    seg_repo = SegmentRepository(db)
    old_segs = seg_repo.list_older_than(settings.audio_retention_days)
    deleted = 0
    for seg in old_segs:
        key = seg.audio_key
        if key and delete_segment(key):
            seg_repo.soft_delete(seg.id)
            deleted += 1
    if deleted:
        logger.info("retention.cleanup", extra={"deleted": deleted})


def hourly_worker(stop_flag) -> None:
    """
    Background thread: every ~5 minutes, check if any recent hours
    need a digest; also runs daily digest at midnight.
    """
    logger.info("hourly_worker.started")
    last_daily_date = ""

    while not stop_flag.is_set():
        db = SessionLocal()
        try:
            now_aligned = _hour_floor(datetime.datetime.now())

            for i in range(6, 0, -1):
                h0 = now_aligned - datetime.timedelta(hours=i)
                try:
                    run_hourly_digest(db, h0)
                except Exception as exc:
                    logger.error(
                        "hourly_worker.digest_error",
                        extra={"hour": str(h0), "error": str(exc)},
                    )

            # Daily digest at midnight (once per day)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            if today != last_daily_date and datetime.datetime.now().hour == 0:
                yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime(
                    "%Y-%m-%d"
                )
                try:
                    run_daily_digest(db, yesterday)
                    last_daily_date = today
                except Exception as exc:
                    logger.error(
                        "hourly_worker.daily_error",
                        extra={"date": yesterday, "error": str(exc)},
                    )

            # Retention cleanup (once per hour-ish)
            try:
                run_retention_cleanup(db)
            except Exception as exc:
                logger.error("hourly_worker.retention_error", extra={"error": str(exc)})

        finally:
            db.close()

        # Sleep in small increments so stop_flag is checked promptly
        for _ in range(60):
            if stop_flag.is_set():
                break
            time.sleep(5)

    logger.info("hourly_worker.stopped")
