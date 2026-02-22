"""Request middleware: API key auth and request ID injection (Phase 2 & 3)."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from functools import wraps

from flask import jsonify, request

from recorder.config import settings
from recorder.logging_config import set_request_id

logger = logging.getLogger(__name__)

# Endpoints exempt from API key check
_EXEMPT_PATHS = {"/", "/api/v1/health", "/metrics"}


def inject_request_id(f: Callable) -> Callable:
    """Attach a UUID request_id to every request for log correlation."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        rid = request.headers.get("X-Request-Id", str(uuid.uuid4()))
        set_request_id(rid)
        return f(*args, **kwargs)

    return wrapper


def require_api_key(f: Callable) -> Callable:
    """
    Reject requests that lack a valid API key.

    Reads from X-API-Key header or Authorization: Bearer <key>.
    Exempt paths bypass the check.
    """

    @wraps(f)
    def wrapper(*args, **kwargs):
        # No key configured → open access (dev mode)
        if not settings.recorder_api_key:
            return f(*args, **kwargs)

        if request.path in _EXEMPT_PATHS:
            return f(*args, **kwargs)

        provided = request.headers.get("X-API-Key", "")
        if not provided:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                provided = auth[7:]
        if not provided:
            # Support ?api_key query param for SSE (EventSource can't set headers)
            provided = request.args.get("api_key", "")

        if provided != settings.recorder_api_key:
            logger.warning(
                "auth.rejected",
                extra={"path": request.path, "ip": request.remote_addr},
            )
            return (
                jsonify(
                    {
                        "error": {
                            "code": "unauthorized",
                            "message": "Invalid or missing API key. "
                            "Provide X-API-Key header or Authorization: Bearer <key>.",
                        }
                    }
                ),
                401,
            )
        return f(*args, **kwargs)

    return wrapper
