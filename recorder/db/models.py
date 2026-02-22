"""SQLAlchemy ORM models."""
from __future__ import annotations

import json

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)

from recorder.db.session import Base


class Segment(Base):
    __tablename__ = "segments"

    id = Column(Integer, primary_key=True)
    start_ts = Column(String, nullable=False)
    end_ts = Column(String, nullable=False)
    duration_sec = Column(Float, default=0.0)

    # audio_path kept for backward compat; audio_key is the canonical relative path
    audio_path = Column(String)
    audio_key = Column(String)

    transcript = Column(Text, default="")
    summary = Column(Text, default="")
    keywords = Column(Text, default="")
    speakers = Column(Text, default="")
    participants = Column(Text, default="")
    category = Column(String, default="")
    action_items = Column(Text, default="")
    questions = Column(Text, default="")
    sentiment = Column(String, default="")
    important = Column(Boolean, default=False)

    # Phase 4 additions
    tags = Column(Text, default="[]")
    word_count = Column(Integer, default=0)
    char_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_segments_start", "start_ts"),
        Index("idx_segments_end", "end_ts"),
        Index("idx_segments_important", "important"),
        Index("idx_segments_deleted", "deleted_at"),
    )

    def get_tags(self) -> list[str]:
        try:
            return json.loads(self.tags or "[]")  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError):
            return []

    def set_tags(self, tags: list[str]) -> None:
        self.tags = json.dumps(tags)  # type: ignore[assignment]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_sec": self.duration_sec or 0.0,
            "audio_key": self.audio_key or "",
            "audio_path": self.audio_path or "",
            "transcript": self.transcript or "",
            "summary": self.summary or "",
            "keywords": self.keywords or "",
            "speakers": self.speakers or "",
            "participants": self.participants or "",
            "category": self.category or "",
            "action_items": self.action_items or "",
            "questions": self.questions or "",
            "sentiment": self.sentiment or "",
            "important": bool(self.important),
            "tags": self.get_tags(),
            "word_count": self.word_count or 0,
            "char_count": self.char_count or 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class HourlyDigest(Base):
    __tablename__ = "hourly_digests"

    id = Column(Integer, primary_key=True)
    hour_start = Column(String, unique=True, nullable=False)
    hour_end = Column(String, nullable=False)
    summary = Column(Text, default="")
    created_at = Column(DateTime, default=func.now())


class DailyDigest(Base):
    __tablename__ = "daily_digests"

    id = Column(Integer, primary_key=True)
    date = Column(String, unique=True, nullable=False)
    summary = Column(Text, default="")
    action_items = Column(Text, default="")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class FailedSegment(Base):
    """Dead-letter queue for segments that fail all processing retries."""

    __tablename__ = "failed_segments"

    id = Column(Integer, primary_key=True)
    audio_path = Column(String, nullable=False)
    error = Column(Text, nullable=False)
    attempts = Column(Integer, default=1)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
