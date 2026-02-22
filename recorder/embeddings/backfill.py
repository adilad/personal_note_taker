"""One-time backfill — generates embeddings for existing segments that lack them."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def backfill_embeddings(db) -> None:
    from recorder.db.repository import SegmentEmbeddingRepository, SegmentRepository
    from recorder.embeddings.client import generate_embedding

    seg_repo = SegmentRepository(db)
    emb_repo = SegmentEmbeddingRepository(db)

    all_segs = seg_repo.list(limit=10_000)
    missing = [s for s in all_segs if not emb_repo.exists(s.id)]  # type: ignore[arg-type]

    logger.info("backfill.start", extra={"total": len(missing)})
    done = 0
    for i, seg in enumerate(missing):
        text = (seg.transcript or seg.summary or "").strip()
        if not text:
            continue
        emb = generate_embedding(text)
        if emb is not None:
            emb_repo.store(seg.id, emb)  # type: ignore[arg-type]
            done += 1
        if i % 50 == 0 and i > 0:
            logger.info(
                "backfill.progress", extra={"processed": i, "embedded": done, "total": len(missing)}
            )

    logger.info("backfill.done", extra={"embedded": done, "skipped": len(missing) - done})
