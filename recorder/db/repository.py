"""Repository pattern — all SQL lives here, no raw SQL in routes or pipeline."""
import datetime
import logging
import struct

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from recorder.db.models import DailyDigest, FailedSegment, HourlyDigest, Segment

logger = logging.getLogger(__name__)


def _pack_f32(v: list[float]) -> bytes:
    """Pack a float list into little-endian float32 bytes for sqlite-vec."""
    return struct.pack(f"{len(v)}f", *v)


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

    def get_by_id(self, segment_id: int) -> Segment | None:
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
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
        offset: int = 0,
        q: str | None = None,
        tag: str | None = None,
    ) -> list[Segment]:
        query = self.db.query(Segment).filter(Segment.deleted_at.is_(None))

        if start:
            query = query.filter(Segment.start_ts >= start)
        if end:
            query = query.filter(Segment.start_ts < end)

        if q:
            # 1. Semantic search (requires sqlite-vec + sentence-transformers)
            _used_semantic = False
            try:
                from recorder.embeddings.client import generate_embedding

                query_emb = generate_embedding(q)
                if query_emb is not None:
                    emb_repo = SegmentEmbeddingRepository(self.db)
                    k = min(limit + offset + 50, 200)
                    hits = emb_repo.search(query_emb, k=k)
                    if hits:
                        ids_in_order = [h[0] for h in hits]
                        base = self.db.query(Segment).filter(
                            Segment.id.in_(ids_in_order),
                            Segment.deleted_at.is_(None),
                        )
                        if start:
                            base = base.filter(Segment.start_ts >= start)
                        if end:
                            base = base.filter(Segment.start_ts < end)
                        if tag:
                            base = base.filter(Segment.tags.like(f'%"{tag}"%'))
                        seg_map = {s.id: s for s in base.all()}  # type: ignore[misc]
                        ordered = [seg_map[i] for i in ids_in_order if i in seg_map]
                        return ordered[offset : offset + limit]
                    _used_semantic = True  # embedding worked but no results
            except Exception as exc:
                logger.debug("semantic_search.fallback", extra={"error": str(exc)})

            # 2. FTS5 lexical search
            try:
                fts_ids = self.db.execute(
                    text("SELECT rowid FROM segments_fts WHERE segments_fts MATCH :q"),
                    {"q": q},
                ).fetchall()
                ids = [r[0] for r in fts_ids]
                if ids:
                    query = query.filter(Segment.id.in_(ids))
                elif _used_semantic:
                    return []  # semantic found nothing, FTS also empty
                else:
                    return []
            except Exception:
                # 3. LIKE fallback
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
        seg.deleted_at = datetime.datetime.now()  # type: ignore[assignment]
        self.db.commit()
        logger.info("segment.soft_deleted", extra={"segment_id": segment_id})
        return True

    def update(self, segment_id: int, **kwargs) -> Segment | None:
        seg = self.get_by_id(segment_id)
        if not seg:
            return None
        for key, val in kwargs.items():
            if hasattr(seg, key):
                setattr(seg, key, val)
        seg.updated_at = datetime.datetime.now()  # type: ignore[assignment]
        self.db.commit()
        self.db.refresh(seg)
        return seg


class SegmentEmbeddingRepository:
    """
    Manages the segment_embeddings table.

    Embeddings are stored as raw float32 BLOBs and searched in-memory via
    NumPy cosine similarity. At the expected scale (< 100k segments / ~150 MB)
    a full linear scan completes in < 10 ms — no ANN index required.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def store(self, segment_id: int, embedding: list[float]) -> None:
        self.db.execute(
            text(
                "INSERT OR REPLACE INTO segment_embeddings(segment_id, embedding)"
                " VALUES (:id, :emb)"
            ),
            {"id": segment_id, "emb": _pack_f32(embedding)},
        )
        self.db.commit()

    def search(self, query_embedding: list[float], k: int = 30) -> list[tuple[int, float]]:
        """Return [(segment_id, cosine_distance), ...] ordered closest-first."""
        import numpy as np

        rows = self.db.execute(
            text("SELECT segment_id, embedding FROM segment_embeddings")
        ).fetchall()
        if not rows:
            return []

        ids = np.array([r[0] for r in rows], dtype=np.int64)
        raw = b"".join(r[1] for r in rows)
        # ascontiguousarray avoids numpy 2.x warnings on non-contiguous read-only buffers
        mat = np.ascontiguousarray(
            np.frombuffer(raw, dtype=np.float32).reshape(len(rows), -1)
        )
        query = np.ascontiguousarray(np.array(query_embedding, dtype=np.float32))
        # Both query and stored embeddings are L2-normalised, so dot == cosine sim.
        scores = np.dot(mat, query)  # (n,)
        top_idx = np.argsort(scores)[::-1][:k]
        return [(int(ids[i]), float(1.0 - scores[i])) for i in top_idx]

    def exists(self, segment_id: int) -> bool:
        row = self.db.execute(
            text("SELECT 1 FROM segment_embeddings WHERE segment_id = :id"),
            {"id": segment_id},
        ).fetchone()
        return row is not None


class HourlyDigestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_hour(self, hour_start: str) -> HourlyDigest | None:
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
            existing.hour_end = hour_end  # type: ignore[assignment]
            existing.summary = summary  # type: ignore[assignment]
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

    def get_by_date(self, date_str: str) -> DailyDigest | None:
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
            existing.summary = summary  # type: ignore[assignment]
            existing.action_items = action_items  # type: ignore[assignment]
            existing.updated_at = datetime.datetime.now()  # type: ignore[assignment]
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
            fs.attempts += 1  # type: ignore[assignment]
            fs.updated_at = datetime.datetime.now()  # type: ignore[assignment]
            self.db.commit()
