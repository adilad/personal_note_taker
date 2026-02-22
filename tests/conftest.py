"""Shared pytest fixtures."""

from __future__ import annotations

import os
import struct
import wave
from unittest.mock import MagicMock, patch

import pytest

# Point config to an in-memory/temp DB before any recorder imports
os.environ.setdefault("RECORDER_API_KEY", "test-key-12345")


@pytest.fixture(scope="session")
def tmp_db_path(tmp_path_factory):
    """A temp directory that persists for the whole test session."""
    return str(tmp_path_factory.mktemp("db") / "test.db")


@pytest.fixture
def db(tmp_db_path):
    """SQLAlchemy session backed by a temp SQLite DB."""
    from recorder.db.session import Base, make_engine, sessionmaker

    engine = make_engine(tmp_db_path)
    import recorder.db.models  # noqa: F401 — register models

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def sample_wav(tmp_path) -> str:
    """A 2-second 16 kHz mono WAV file with a 440 Hz sine wave."""
    import math

    path = str(tmp_path / "test.wav")
    sr = 16000
    freq = 440
    n_samples = sr * 2
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        for i in range(n_samples):
            val = int(32767 * math.sin(2 * math.pi * freq * i / sr))
            wf.writeframes(struct.pack("<h", val))
    return path


@pytest.fixture
def mock_llm():
    """Patch LiteLLM calls to return deterministic JSON."""
    deterministic_response = {
        "summary": "Test summary bullet",
        "speakers": "[Speaker 1] Hello world",
        "participants": ["Alice", "Bob"],
        "category": "meeting",
        "action_items": ["[ ] Alice: Follow up"],
        "open_questions": ["What is the deadline?"],
        "sentiment": "neutral",
        "keywords": ["test", "meeting"],
    }
    import json

    with (
        patch("recorder.llm.client.settings") as mock_settings,
        patch("recorder.llm.client._call_litellm", return_value=json.dumps(deterministic_response)),
    ):
        mock_settings.use_litellm = True
        mock_settings.model_max_tokens = 500
        yield deterministic_response


@pytest.fixture
def test_client(tmp_db_path, mock_llm):
    """Flask test client with API key pre-set."""
    from recorder.api.app import create_app
    from recorder.pipeline.processor import RecorderPipeline

    pipeline = MagicMock(spec=RecorderPipeline)
    pipeline.status.return_value = {"running": False, "queue_depth": 0, "live_transcript": ""}
    pipeline.start.return_value = True
    pipeline.stop.return_value = True
    pipeline.enqueue.return_value = True

    app = create_app(pipeline=pipeline)
    app.config["TESTING"] = True

    with app.test_client() as client:
        client.environ_base["HTTP_X_API_KEY"] = "test-key-12345"
        yield client
