"""LiteLLM cloud transcription (Whisper API)."""

from __future__ import annotations

import logging

from recorder.config import settings

logger = logging.getLogger(__name__)


def transcribe_cloud(wav_path: str) -> str | None:
    """
    Attempt cloud transcription via LiteLLM's /audio/transcriptions endpoint.
    Returns transcript text on success, None on failure (caller should fall back).
    """
    if not (settings.use_litellm_transcription and settings.litellm_api_key):
        return None

    try:
        import requests  # type: ignore

        with open(wav_path, "rb") as f:
            resp = requests.post(
                f"{settings.litellm_base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
                files={"file": (wav_path.split("/")[-1], f, "audio/wav")},
                data={
                    "model": settings.litellm_transcription_model,
                    "language": "en",
                },
                timeout=60,
            )
        if resp.status_code == 200:
            txt = resp.json().get("text", "").strip()
            logger.info(
                "transcription.cloud_ok",
                extra={"chars": len(txt)},
            )
            return txt
        else:
            logger.warning(
                "transcription.cloud_failed",
                extra={"status": resp.status_code},
            )
    except Exception as exc:
        logger.warning("transcription.cloud_error", extra={"error": str(exc)})

    return None
