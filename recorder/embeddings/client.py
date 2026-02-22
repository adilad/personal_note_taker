"""Embedding generation — local sentence-transformers with graceful degradation."""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore

            from recorder.config import settings

            logger.info("embeddings.loading_model", extra={"model": settings.embedding_model})
            _model = SentenceTransformer(settings.embedding_model)
            logger.info("embeddings.model_loaded")
    return _model


def generate_embedding(text: str) -> list[float] | None:
    """Return a normalized float32 embedding, or None on any failure."""
    from recorder.config import settings

    if not settings.use_embeddings or not text.strip():
        return None
    try:
        return _get_model().encode(text, normalize_embeddings=True).tolist()
    except ImportError:
        logger.debug("embeddings.not_available")
        return None
    except Exception as exc:
        logger.warning("embeddings.error", extra={"error": str(exc)})
        return None


def is_available() -> bool:
    """True if sentence-transformers is importable and embeddings are enabled."""
    from recorder.config import settings

    if not settings.use_embeddings:
        return False
    try:
        import sentence_transformers  # noqa: F401  # type: ignore

        return True
    except ImportError:
        return False
