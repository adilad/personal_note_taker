"""Local Whisper ASR via faster-whisper."""

from __future__ import annotations

import logging

import numpy as np

from recorder.config import settings

logger = logging.getLogger(__name__)

_model = None


def get_model():
    """Lazily load the Whisper model (singleton)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # type: ignore

        logger.info(
            "whisper.loading",
            extra={"model_size": settings.whisper_model},
        )
        _model = WhisperModel(settings.whisper_model, device="auto", compute_type="int8")
        logger.info("whisper.ready")
    return _model


def transcribe_local(
    audio_array: np.ndarray,
    beam_size: int | None = None,
) -> tuple[str, list]:
    """Transcribe a numpy float32 audio array. Returns (text, segments_list)."""
    model = get_model()
    beam = beam_size if beam_size is not None else settings.whisper_beam_size
    segments_list, _ = model.transcribe(
        audio_array, beam_size=beam, vad_filter=False, language="en"
    )
    segments_list = list(segments_list)
    txt = " ".join(s.text.strip() for s in segments_list).strip()
    return txt, segments_list
