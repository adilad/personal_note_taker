"""Central configuration — single source of truth for all env vars."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths ---
    app_dir: Path = Path(__file__).parent.parent
    audio_dir: Optional[Path] = None
    db_path: Optional[Path] = None
    log_dir: Optional[Path] = None

    # --- LiteLLM (cloud LLM) ---
    litellm_api_key: str = ""
    litellm_base_url: str = "https://litellm.marqeta.com"
    litellm_model_id: str = "openai/gpt-4o-mini"
    litellm_temperature: float = 0.3
    model_max_tokens: int = 2000

    # --- LiteLLM Transcription ---
    use_litellm_transcription: bool = False
    litellm_transcription_model: str = "whisper-1"

    # --- Local LLM (llama.cpp fallback) ---
    llm_model_path: str = "~/models/llama-3-8b-instruct.Q4_K_M.gguf"

    # --- Whisper ASR ---
    whisper_model: str = "small"
    whisper_beam_size: int = 5

    # --- VAD / Segmentation ---
    vad_aggressiveness: int = 1
    off_time_sec: int = 3
    max_segment_sec: int = 120

    # --- Speaker diarization ---
    use_diarization: bool = False
    hf_token: str = ""

    # --- Noise reduction ---
    use_noisereduce: bool = True

    # --- Flask UI ---
    recorder_ui_host: str = "127.0.0.1"
    recorder_ui_port: int = 5000

    # --- API key auth ---
    recorder_api_key: str = ""

    # --- Audio retention ---
    audio_retention_days: int = 90

    # --- Audio constants ---
    sample_rate: int = 16000
    frame_ms: int = 30

    @model_validator(mode="after")
    def _set_derived_paths(self) -> "Settings":
        if self.audio_dir is None:
            self.audio_dir = self.app_dir / "audio"
        if self.db_path is None:
            self.db_path = self.app_dir / "journal.db"
        if self.log_dir is None:
            self.log_dir = self.app_dir / "logs"
        return self

    @property
    def use_litellm(self) -> bool:
        return bool(self.litellm_api_key)

    @property
    def frame_len(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)


settings = Settings()
