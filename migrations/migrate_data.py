"""
Data migration script — reads existing journal.db and migrates to new schema.

Usage:
    python migrations/migrate_data.py [--db-path /path/to/journal.db]

Features:
- Idempotent: skips segments that already exist by start_ts
- Converts audio_path (absolute) → audio_key (relative filename)
- Runs Alembic migrations first
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow running as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def run_alembic_migrations(db_path: str) -> None:
    from alembic.config import Config
    from alembic import command

    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..","migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    print("[alembic] Migrations complete.")


def migrate(db_path: str) -> None:
    import sqlite3

    from recorder.config import settings
    from recorder.db.repository import SegmentRepository
    from recorder.db.session import SessionLocal, make_engine
    from recorder.db.models import Base

    # Run schema migrations first
    run_alembic_migrations(db_path)

    # Connect to old schema (same file, post-migration)
    old_conn = sqlite3.connect(db_path)
    old_conn.row_factory = sqlite3.Row
    cur = old_conn.cursor()

    # Fetch all old segments
    try:
        rows = cur.execute(
            "SELECT id, start_ts, end_ts, duration_sec, audio_path, transcript, "
            "summary, keywords, important, speakers, participants, category, "
            "action_items, questions, sentiment FROM segments ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[migrate] Cannot read segments: {e}")
        old_conn.close()
        return

    old_conn.close()

    db = SessionLocal()
    repo = SegmentRepository(db)
    migrated = 0
    skipped = 0

    for row in rows:
        start_ts = row["start_ts"]

        # Check if already migrated (by audio_path or start_ts uniqueness)
        audio_path = row["audio_path"] or ""
        audio_key = os.path.basename(audio_path) if audio_path else ""

        if audio_key and repo.exists_by_audio_key(audio_key):
            skipped += 1
            continue

        # Check by start_ts collision
        existing = db.execute(
            __import__("sqlalchemy").text("SELECT id FROM segments WHERE start_ts=:ts"),
            {"ts": start_ts},
        ).fetchone()
        if existing:
            skipped += 1
            continue

        try:
            repo.create(
                start_ts=start_ts,
                end_ts=row["end_ts"] or start_ts,
                duration_sec=row["duration_sec"] or 0.0,
                audio_path=audio_path,
                audio_key=audio_key,
                transcript=row["transcript"] or "",
                summary=row["summary"] or "",
                keywords=row["keywords"] or "",
                important=bool(row["important"]),
                speakers=row["speakers"] or "",
                participants=row["participants"] or "",
                category=row["category"] or "",
                action_items=row["action_items"] or "",
                questions=row["questions"] or "",
                sentiment=row["sentiment"] or "",
            )
            migrated += 1
        except Exception as exc:
            print(f"[migrate] Error on row {row['id']}: {exc}")

    db.close()
    print(f"[migrate] Done: {migrated} migrated, {skipped} skipped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate journal.db to new schema")
    parser.add_argument(
        "--db-path",
        default=str(__import__("recorder.config", fromlist=["settings"]).settings.db_path),
        help="Path to journal.db",
    )
    args = parser.parse_args()
    print(f"[migrate] Using DB: {args.db_path}")
    migrate(args.db_path)


if __name__ == "__main__":
    main()
