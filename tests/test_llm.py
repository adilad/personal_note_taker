"""Tests for LLM client — analyze_transcript(), JSON parsing, fallback."""

from __future__ import annotations

import json
from unittest.mock import patch


def test_analyze_transcript_success(mock_llm):
    from recorder.llm.client import analyze_transcript

    result = analyze_transcript("Alice and Bob discussed the project deadline.")
    assert result.summary == "Test summary bullet"
    assert result.category == "meeting"
    assert result.sentiment == "neutral"
    assert "Alice" in result.participants
    assert isinstance(result.action_items, list)
    assert isinstance(result.keywords, list)


def test_analyze_transcript_empty():
    from recorder.llm.client import analyze_transcript

    result = analyze_transcript("")
    assert result.summary == ""
    assert result.category == ""


def test_analyze_transcript_json_fallback():
    """On JSON parse error, fallback to extractive summary."""
    with patch("recorder.llm.client._call_litellm", return_value="this is not json"):
        from recorder.llm.client import analyze_transcript

        result = analyze_transcript("Hello world. This is a test transcript.")
        assert result.summary != ""  # extractive fallback
        assert result.category == ""


def test_analyze_transcript_no_llm():
    """Without LLM configured, returns extractive fallback."""
    with patch("recorder.llm.client.settings") as mock_settings:
        mock_settings.use_litellm = False
        mock_settings.litellm_api_key = ""
        from recorder.llm.client import analyze_transcript

        with patch("recorder.llm.client._get_local_llm", return_value=None):
            result = analyze_transcript("Hello world. Testing the fallback.")
            assert isinstance(result.summary, str)


def test_analyze_parses_list_fields():
    """Ensure list fields are correctly parsed from JSON arrays."""
    response = json.dumps(
        {
            "summary": "Summary",
            "speakers": "[Alice] Hello",
            "participants": ["Alice", "Bob"],
            "category": "meeting",
            "action_items": ["[ ] Alice: Do thing"],
            "open_questions": ["When is the deadline?"],
            "sentiment": "positive",
            "keywords": ["test", "meeting", "deadline"],
        }
    )
    with (
        patch("recorder.llm.client.settings") as mock_settings,
        patch("recorder.llm.client._call_litellm", return_value=response),
    ):
        mock_settings.use_litellm = True
        mock_settings.model_max_tokens = 500
        from recorder.llm.client import analyze_transcript

        result = analyze_transcript("Alice and Bob met.")
        assert len(result.participants) == 2
        assert len(result.keywords) == 3
        assert result.sentiment == "positive"


def test_summarize_daily_empty():
    from recorder.llm.client import summarize_daily

    result = summarize_daily("")
    assert result == "(no transcripts today)"


def test_summarize_hourly_empty():
    from recorder.llm.client import summarize_hourly

    result = summarize_hourly("")
    assert result == ""
