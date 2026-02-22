"""Flask application factory — registers all blueprints."""
from __future__ import annotations

import logging

from flask import Flask, Response, render_template

from recorder.api.middleware import inject_request_id, require_api_key
from recorder.api.routes import audio, export, health, recordings, segments, summaries
from recorder.api.sse import event_bus, sse_stream

logger = logging.getLogger(__name__)


def create_app(pipeline=None) -> Flask:
    """
    Create and configure the Flask application.

    Args:
        pipeline: RecorderPipeline instance (injected for routes to use)
    """
    app = Flask(
        __name__,
        template_folder="../templates",
    )

    if pipeline is not None:
        app.config["pipeline"] = pipeline

    # Register blueprints
    for bp in (
        health.bp,
        segments.bp,
        recordings.bp,
        summaries.bp,
        audio.bp,
        export.bp,
    ):
        app.register_blueprint(bp)

    # SSE stream endpoint (Phase 8)
    @app.get("/api/v1/stream")
    @inject_request_id
    @require_api_key
    def sse_endpoint():
        q = event_bus.subscribe()
        return Response(
            sse_stream(q),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # Legacy API compatibility shims
    @app.get("/api/health")
    def legacy_health():
        from flask import jsonify
        pipeline_inst = app.config.get("pipeline")
        running = pipeline_inst.status()["running"] if pipeline_inst else False
        return jsonify({"ok": True, "running": running})

    @app.get("/api/live")
    def legacy_live():
        from flask import jsonify
        pipeline_inst = app.config.get("pipeline")
        if pipeline_inst:
            status = pipeline_inst.status()
            return jsonify({
                "ok": True,
                "transcript": status["live_transcript"],
                "running": status["running"],
            })
        return jsonify({"ok": True, "transcript": "", "running": False})

    @app.post("/api/start")
    def legacy_start():
        from flask import jsonify
        pipeline_inst = app.config.get("pipeline")
        if pipeline_inst:
            started = pipeline_inst.start()
            return jsonify({"ok": True, "running": pipeline_inst.status()["running"], "started": started})
        return jsonify({"ok": False, "error": "no pipeline"}), 500

    @app.post("/api/stop")
    def legacy_stop():
        from flask import jsonify
        pipeline_inst = app.config.get("pipeline")
        if pipeline_inst:
            stopped = pipeline_inst.stop()
            return jsonify({"ok": True, "running": pipeline_inst.status()["running"], "stopped": stopped})
        return jsonify({"ok": False, "error": "no pipeline"}), 500

    @app.get("/api/segments")
    def legacy_segments():
        import datetime
        from flask import jsonify
        from recorder.db.repository import SegmentRepository
        from recorder.db.session import SessionLocal
        db = SessionLocal()
        try:
            repo = SegmentRepository(db)
            start_of_day = datetime.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            segs = repo.list(start=start_of_day, limit=200)
            return jsonify({"ok": True, "segments": [s.to_dict() for s in segs]})
        finally:
            db.close()

    @app.get("/api/summary")
    def legacy_summary():
        from flask import jsonify
        import datetime
        from recorder.db.repository import SegmentRepository
        from recorder.db.session import SessionLocal
        from recorder.llm.client import summarize_daily
        db = SessionLocal()
        try:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            repo = SegmentRepository(db)
            segs = repo.list_for_date(date_str)
            texts = []
            for s in segs:
                txt = (s.transcript or s.summary or "").strip()
                if txt:
                    ts = datetime.datetime.fromisoformat(s.start_ts).strftime("%I:%M %p")
                    texts.append(f"[{ts}] {txt}")
            combined = "\n\n".join(texts)
            summary = summarize_daily(combined)
            return jsonify({"ok": True, "summary": summary, "count": len(texts)})
        finally:
            db.close()

    @app.post("/api/reprocess")
    def legacy_reprocess():
        from flask import jsonify
        import datetime, os
        from recorder.config import settings
        from recorder.db.repository import SegmentRepository
        from recorder.db.session import SessionLocal
        pipeline_inst = app.config.get("pipeline")
        if not pipeline_inst:
            return jsonify({"ok": False, "error": "no pipeline"}), 500
        db = SessionLocal()
        try:
            repo = SegmentRepository(db)
            queued = 0
            audio_dir = settings.audio_dir
            for fname in sorted(os.listdir(audio_dir)):
                if not fname.endswith(".wav"):
                    continue
                abs_path = str(audio_dir / fname)
                if repo.exists_by_audio_key(fname) or repo.exists_by_audio_key(abs_path):
                    continue
                try:
                    ts_part = fname.replace("seg_", "").replace(".wav", "")
                    date_part, time_part = ts_part.split("T")
                    ts = datetime.datetime.fromisoformat(f"{date_part}T{time_part.replace('-', ':')}")
                except Exception:
                    ts = datetime.datetime.now()
                duration = os.path.getsize(abs_path) / 2 / settings.sample_rate
                if pipeline_inst.enqueue(abs_path, ts.isoformat(), ts.isoformat(), duration):
                    queued += 1
            return jsonify({"ok": True, "queued": queued})
        finally:
            db.close()

    # UI (index.html extracted to templates/)
    @app.get("/")
    def index():
        return render_template("index.html")

    return app
