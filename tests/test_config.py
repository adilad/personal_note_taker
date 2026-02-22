"""Tests for config loading and env var overrides."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_default_settings():
    from recorder.config import Settings

    s = Settings()
    assert s.sample_rate == 16000
    assert s.frame_ms == 30
    assert s.whisper_model == "small"
    assert s.off_time_sec == 3
    assert s.max_segment_sec == 120


def test_derived_paths():
    from recorder.config import Settings

    s = Settings()
    assert s.audio_dir is not None
    assert s.db_path is not None
    assert s.log_dir is not None
    assert str(s.audio_dir).endswith("audio")
    assert str(s.db_path).endswith(".db")


def test_use_litellm_property():
    from recorder.config import Settings

    s = Settings(litellm_api_key="sk-test")
    assert s.use_litellm is True

    s2 = Settings(litellm_api_key="")
    assert s2.use_litellm is False


def test_frame_len_property():
    from recorder.config import Settings

    s = Settings(sample_rate=16000, frame_ms=30)
    assert s.frame_len == 480  # 16000 * 30 / 1000


def test_env_override(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "base")
    monkeypatch.setenv("OFF_TIME_SEC", "5")
    from recorder.config import Settings

    s = Settings()
    assert s.whisper_model == "base"
    assert s.off_time_sec == 5
