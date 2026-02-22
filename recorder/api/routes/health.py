"""Health and metrics endpoints."""
from __future__ import annotations

from flask import Blueprint, Response, jsonify

from recorder.api.middleware import inject_request_id
from recorder.metrics import PROMETHEUS_AVAILABLE

bp = Blueprint("health", __name__)


@bp.get("/api/v1/health")
@inject_request_id
def health():
    from recorder.config import settings
    return jsonify({
        "ok": True,
        "version": "2.0.0",
        "auth_required": bool(settings.recorder_api_key),
    })


@bp.get("/metrics")
@inject_request_id
def prometheus_metrics():
    if not PROMETHEUS_AVAILABLE:
        return Response("# prometheus_client not installed\n", mimetype="text/plain")
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # type: ignore

    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
