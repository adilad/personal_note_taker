"""Audio capture loop — wraps sounddevice RawInputStream."""

from __future__ import annotations

import logging
import time

import sounddevice as sd  # type: ignore

from recorder.config import settings

logger = logging.getLogger(__name__)


def audio_callback(audio_q, indata, frames, time_info, status):
    """sounddevice callback — runs in a C thread, must be minimal."""
    if status:
        logger.debug("audio_callback.status", extra={"status": str(status)})
    audio_q.put(bytes(indata))


def record_loop(audio_q, stop_flag) -> None:
    """Stream mic audio into audio_q until stop_flag is set."""
    logger.info("audio_capture.started")
    try:
        with sd.RawInputStream(
            samplerate=settings.sample_rate,
            blocksize=settings.frame_len,
            dtype="int16",
            channels=1,
            callback=lambda indata, frames, ti, st: audio_callback(audio_q, indata, frames, ti, st),
        ):
            while not stop_flag.is_set():
                time.sleep(0.05)
    except Exception as exc:
        logger.error("audio_capture.error", extra={"error": str(exc)})
    finally:
        logger.info("audio_capture.stopped")
