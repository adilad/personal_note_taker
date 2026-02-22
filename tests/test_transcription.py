"""Tests for transcription — local Whisper mock, cloud fallback."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_transcribe_cloud_disabled():
    """When USE_LITELLM_TRANSCRIPTION is False, cloud returns None."""
    with patch("recorder.transcription.cloud.settings") as mock_s:
        mock_s.use_litellm_transcription = False
        mock_s.litellm_api_key = ""

        from recorder.transcription.cloud import transcribe_cloud

        result = transcribe_cloud("test.wav")
        assert result is None


def test_transcribe_cloud_success(tmp_path, sample_wav):
    """Cloud transcription returns text on 200."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"text": "Hello from cloud"}

    # requests is imported lazily inside the function, so patch at the requests module level
    with patch("recorder.transcription.cloud.settings") as mock_s, \
         patch("requests.post", return_value=mock_response):
        mock_s.use_litellm_transcription = True
        mock_s.litellm_api_key = "sk-test"
        mock_s.litellm_base_url = "https://api.test"
        mock_s.litellm_transcription_model = "whisper-1"

        from recorder.transcription.cloud import transcribe_cloud

        result = transcribe_cloud(sample_wav)
        assert result == "Hello from cloud"


def test_transcribe_cloud_failure_returns_none(sample_wav):
    """Cloud transcription returns None on non-200."""
    mock_response = MagicMock()
    mock_response.status_code = 503

    with patch("recorder.transcription.cloud.settings") as mock_s, \
         patch("requests.post", return_value=mock_response):
        mock_s.use_litellm_transcription = True
        mock_s.litellm_api_key = "sk-test"
        mock_s.litellm_base_url = "https://api.test"
        mock_s.litellm_transcription_model = "whisper-1"

        from recorder.transcription.cloud import transcribe_cloud

        result = transcribe_cloud(sample_wav)
        assert result is None


def test_transcribe_local_returns_tuple(sample_wav):
    """Local whisper returns (text, segments_list)."""
    mock_model = MagicMock()
    mock_seg = MagicMock()
    mock_seg.text = " hello "
    mock_model.transcribe.return_value = ([mock_seg], MagicMock())

    with patch("recorder.transcription.whisper_asr._model", mock_model), \
         patch("recorder.transcription.whisper_asr.settings") as mock_s:
        mock_s.whisper_beam_size = 5

        import numpy as np

        from recorder.transcription.whisper_asr import transcribe_local

        audio = np.zeros(16000, dtype=np.float32)
        txt, segs = transcribe_local(audio)
        assert txt == "hello"
        assert len(segs) == 1
