"""Repository pattern — all SQL lives here, no raw SQL in routes or pipeline."""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from recorder.db.models import DailyDigest, FailedSegment, HourlyDigest, Segment

logger = logging.getLogger(__name__)


class SegmentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, **kwargs) -> Segment:
        transcript = kwargs.get("transcript", "")
        kwargs.setdefault("word_count", len(transcript.split()) if transcript else 0)
        kwargs.setdefault("char_count", len(transcript) if transcript else 0)
        seg = Segment(**kwargs)
        self.db.add(seg)
        self.db.commit()
        self.db.refresh(seg)
        logger.info("segment.created", extra={"segment_id": seg.id})
        return seg

    def get_by_id(self, segment_id: int) -> Optional[Segment]:
        return (
            self.db.query(Segment)
            .filter(Segment.id == segment_id, Segment.deleted_at.is_(None))
            .first()
        )

    def exists_by_audio_key(self, audio_key: str) -> bool:
        """Deduplication check — avoid reprocessing the same file."""
        return (
            self.db.query(Segment.id)
            .filter(
                or_(Segment.audio_key == audio_key, Segment.audio_path == audio_key)
            )
            .first()
            is not None
        )

    def list(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
        q: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[Segment]:
        query = self.db.query(Segment).filter(Segment.deleted_at.is_(None))

        if start:
            query = query.filter(Segment.start_ts >= start)
        if end:
            query = query.filter(Segment.start_ts < end)

        if q:
            try:
                fts_ids = self.db.execute(
                    text("SELECT rowid FROM segments_fts WHERE segments_fts MATCH :q"),
                    {"q": q},
                ).fetchall()
                ids = [r[0] for r in fts_ids]
                if ids:
                    query = query.filter(Segment.id.in_(ids))
                else:
                    return []
            except Exception:
                like = f"%{q}%"
                query = query.filter(
                    or_(
                        Segment.transcript.ilike(like),
                        Segment.summary.ilike(like),
                    )
                )

        if tag:
            query = query.filter(Segment.tags.like(f'%"{tag}"%'))

        return (
            query.order_by(Segment.start_ts.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    def list_for_date(self, date_str: str) -> list[Segment]:
        start = f"{date_str}T00:00:00"
        end = f"{date_str}T23:59:59"
        return (
            self.db.query(Segment)
            .filter(
                Segment.deleted_at.is_(None),
                Segment.start_ts >= start,
                Segment.start_ts <= end,
            )
            .order_by(Segment.start_ts.asc())
            .all()
        )

    def list_for_hour(self, hour_start: datetime.datetime) -> list[Segment]:
        hour_end = hour_start + datetime.timedelta(hours=1)
        return (
            self.db.query(Segment)
            .filter(
                Segment.deleted_at.is_(None),
                Segment.start_ts >= hour_start.isoformat(),
                Segment.start_ts < hour_end.isoformat(),
            )
            .order_by(Segment.start_ts.asc())
            .all()
        )

    def list_older_than(self, days: int) -> list[Segment]:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        return (
            self.db.query(Segment)
            .filter(
                Segment.deleted_at.is_(None),
                Segment.start_ts < cutoff.isoformat(),
                Segment.audio_key.isnot(None),
            )
            .all()
        )

    def soft_delete(self, segment_id: int) -> bool:
        seg = self.get_by_id(segment_id)
        if not seg:
            return False
        seg.deleted_at = datetime.datetime.now()
        self.db.commit()
        logger.info("segment.soft_deleted", extra={"segment_id": segment_id})
        return True

    def update(self, segment_id: int, **kwargs) -> Optional[Segment]:
        seg = self.get_by_id(segment_id)
        if not seg:
            return None
        for key, val in kwargs.items():
            if hasattr(seg, key):
                setattr(seg, key, val)
        seg.updated_at = datetime.datetime.now()
        self.db.commit()
        self.db.refresh(seg)
        return seg


class HourlyDigestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_hour(self, hour_start: str) -> Optional[HourlyDigest]:
        return (
            self.db.query(HourlyDigest)
            .filter(HourlyDigest.hour_start == hour_start)
            .first()
        )

    def upsert(
        self, hour_start: str, hour_end: str, summary: str
    ) -> HourlyDigest:
        existing = self.get_by_hour(hour_start)
        if existing:
            existing.hour_end = hour_end
            existing.summary = summary
        else:
            existing = HourlyDigest(
                hour_start=hour_start, hour_end=hour_end, summary=summary
            )
            self.db.add(existing)
        self.db.commit()
        self.db.refresh(existing)
        return existing

    def list_recent(self, limit: int = 24) -> list[HourlyDigest]:
        return (
            self.db.query(HourlyDigest)
            .order_by(HourlyDigest.hour_start.desc())
            .limit(limit)
            .all()
        )


class DailyDigestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_date(self, date_str: str) -> Optional[DailyDigest]:
        return (
            self.db.query(DailyDigest)
            .filter(DailyDigest.date == date_str)
            .first()
        )

    def upsert(
        self, date_str: str, summary: str, action_items: str = ""
    ) -> DailyDigest:
        existing = self.get_by_date(date_str)
        if existing:
            existing.summary = summary
            existing.action_items = action_items
            existing.updated_at = datetime.datetime.now()
        else:
            existing = DailyDigest(
                date=date_str, summary=summary, action_items=action_items
            )
            self.db.add(existing)
        self.db.commit()
        self.db.refresh(existing)
        return existing

    def list_recent(self, limit: int = 30) -> list[DailyDigest]:
        return (
            self.db.query(DailyDigest)
            .order_by(DailyDigest.date.desc())
            .limit(limit)
            .all()
        )


class FailedSegmentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, audio_path: str, error: str) -> FailedSegment:
        fs = FailedSegment(audio_path=audio_path, error=error)
        self.db.add(fs)
        self.db.commit()
        self.db.refresh(fs)
        return fs

    def increment_attempts(self, failed_id: int) -> None:
        fs = (
            self.db.query(FailedSegment)
            .filter(FailedSegment.id == failed_id)
            .first()
        )
        if fs:
            fs.attempts += 1
            fs.updated_at = datetime.datetime.now()
            self.db.commit()
