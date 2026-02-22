"""Tests for all API endpoints — auth, pagination, SSE."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


# ---- Auth tests --------------------------------------------------------

def test_health_no_auth(test_client):
    """Health endpoint is exempt from auth."""
    resp = test_client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True


def test_segments_requires_auth():
    """Segments endpoint returns 401 without key when key is configured."""
    from recorder.api.app import create_app
    from recorder.pipeline.processor import RecorderPipeline

    # Temporarily set an API key via env
    with patch("recorder.config.settings") as mock_settings:
        mock_settings.recorder_api_key = "secret"
        mock_settings.use_litellm = False

        pipeline = MagicMock(spec=RecorderPipeline)
        pipeline.status.return_value = {"running": False, "queue_depth": 0, "live_transcript": ""}

        app = create_app(pipeline=pipeline)
        app.config["TESTING"] = True

        # Patch middleware to use the mocked settings
        with patch("recorder.api.middleware.settings", mock_settings):
            with app.test_client() as client:
                resp = client.get("/api/v1/segments")
                assert resp.status_code == 401


def test_segments_with_valid_key(test_client):
    resp = test_client.get("/api/v1/segments")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "segments" in data


def test_segments_with_bearer_token():
    """Bearer token auth should work."""
    from recorder.api.app import create_app
    from recorder.pipeline.processor import RecorderPipeline

    pipeline = MagicMock(spec=RecorderPipeline)
    pipeline.status.return_value = {"running": False, "queue_depth": 0, "live_transcript": ""}

    app = create_app(pipeline=pipeline)
    app.config["TESTING"] = True

    with patch("recorder.api.middleware.settings") as mock_settings:
        mock_settings.recorder_api_key = "mykey"

        with app.test_client() as client:
            resp = client.get(
                "/api/v1/segments",
                headers={"Authorization": "Bearer mykey"},
            )
            # May fail due to DB, but not 401
            assert resp.status_code != 401


# ---- Segment endpoints --------------------------------------------------

def test_get_segments_today(test_client):
    resp = test_client.get("/api/v1/segments")
    assert resp.status_code == 200
    assert isinstance(resp.get_json()["segments"], list)


def test_get_segments_with_limit(test_client):
    resp = test_client.get("/api/v1/segments?limit=10")
    assert resp.status_code == 200


def test_get_segment_not_found(test_client):
    resp = test_client.get("/api/v1/segments/99999")
    assert resp.status_code == 404


def test_patch_segment_not_found(test_client):
    resp = test_client.patch(
        "/api/v1/segments/99999",
        data=json.dumps({"important": True}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_patch_segment_invalid_tags(test_client):
    resp = test_client.patch(
        "/api/v1/segments/1",
        data=json.dumps({"tags": "not-a-list"}),
        content_type="application/json",
    )
    assert resp.status_code in (400, 404)  # 400 if validation, 404 if no seg


# ---- Recording control --------------------------------------------------

def test_start_recording(test_client):
    resp = test_client.post("/api/v1/recordings/start")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "running" in data


def test_stop_recording(test_client):
    resp = test_client.post("/api/v1/recordings/stop")
    assert resp.status_code == 200


def test_recording_status(test_client):
    resp = test_client.get("/api/v1/recordings/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "running" in data


def test_live_transcript(test_client):
    resp = test_client.get("/api/v1/recordings/live")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "transcript" in data


# ---- Summaries ----------------------------------------------------------

def test_daily_summary(test_client):
    with patch("recorder.api.routes.summaries.summarize_daily", return_value="Test summary"), \
         patch("recorder.api.routes.summaries.SegmentRepository") as mock_repo_cls, \
         patch("recorder.api.routes.summaries.DailyDigestRepository") as mock_daily_cls:
        mock_repo_cls.return_value.list_for_date.return_value = []
        mock_daily_cls.return_value.get_by_date.return_value = None
        mock_daily_cls.return_value.upsert.return_value = MagicMock(summary="Test summary", action_items="")
        resp = test_client.get("/api/v1/summaries/daily")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "summary" in data


def test_hourly_summaries(test_client):
    resp = test_client.get("/api/v1/summaries/hourly")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "digests" in data


# ---- Export -------------------------------------------------------------

def test_export_json(test_client):
    resp = test_client.get("/api/v1/export?format=json")
    assert resp.status_code == 200
    assert resp.content_type == "application/json"


def test_export_csv(test_client):
    resp = test_client.get("/api/v1/export?format=csv")
    assert resp.status_code == 200
    assert "csv" in resp.content_type


def test_export_markdown(test_client):
    resp = test_client.get("/api/v1/export?format=markdown")
    assert resp.status_code == 200


def test_export_unknown_format(test_client):
    resp = test_client.get("/api/v1/export?format=xml")
    assert resp.status_code == 400


# ---- Health / Metrics ---------------------------------------------------

def test_metrics_endpoint(test_client):
    resp = test_client.get("/metrics")
    assert resp.status_code == 200
