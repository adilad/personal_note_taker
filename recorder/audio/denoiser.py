"""Audio loading and noise reduction."""

from __future__ import annotations

import logging

import numpy as np

from recorder.config import settings

logger = logging.getLogger(__name__)

# Optional: noisereduce
_nr = None
if settings.use_noisereduce:
    try:
        import noisereduce as _nr_mod  # type: ignore

        _nr = _nr_mod
        logger.info("noise_reduction.enabled")
    except ImportError:
        logger.warning("noisereduce not installed; noise reduction disabled")


def denoise_audio(wav_path: str) -> np.ndarray:
    """Load a WAV file and optionally apply noise reduction."""
    import scipy.io.wavfile as wavfile  # type: ignore

    sr, audio = wavfile.read(wav_path)
    audio = audio.astype(np.float32) / 32768.0

    if _nr is not None:
        logger.debug("noise_reduction.applying", extra={"wav_path": wav_path})
        audio = _nr.reduce_noise(y=audio, sr=sr, prop_decrease=0.8)

    return audio
