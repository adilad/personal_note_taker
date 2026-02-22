"""Audio file store — save, load, delete, and measure disk usage."""
from __future__ import annotations

import logging
from pathlib import Path

from recorder.config import settings
from recorder.metrics import audio_disk_bytes, audio_files_total

logger = logging.getLogger(__name__)


def _audio_root() -> Path:
    root = settings.audio_dir
    assert root is not None  # always set by Settings._set_derived_paths
    root.mkdir(parents=True, exist_ok=True)
    return root


def wav_path_to_key(abs_path: str) -> str:
    """
    Convert an absolute WAV path to a relative audio_key.
    e.g. /…/audio/seg_2026-01-01T09-00-00.wav → seg_2026-01-01T09-00-00.wav
    """
    try:
        return str(Path(abs_path).relative_to(_audio_root()))
    except ValueError:
        return Path(abs_path).name


def key_to_path(audio_key: str) -> Path:
    return _audio_root() / audio_key


def save_segment(raw_bytes: bytes, filename: str) -> str:
    """
    Save raw PCM/WAV bytes under the audio root.
    Returns the relative audio_key (e.g. 'seg_2026-01-01T09-00-00.wav').
    """
    dest = _audio_root() / filename
    dest.write_bytes(raw_bytes)
    _update_metrics()
    logger.debug("audio_store.saved", extra={"key": filename, "bytes": len(raw_bytes)})
    return filename


def load_segment(audio_key: str) -> bytes:
    """Return raw bytes for an audio file. Raises FileNotFoundError if missing."""
    path = key_to_path(audio_key)
    if not path.exists():
        raise FileNotFoundError(f"Audio not found: {audio_key}")
    return path.read_bytes()


def delete_segment(audio_key: str) -> bool:
    """Delete an audio file from disk. Returns True if deleted, False if not found."""
    path = key_to_path(audio_key)
    if path.exists():
        path.unlink()
        _update_metrics()
        logger.info("audio_store.deleted", extra={"key": audio_key})
        return True
    return False


def get_disk_usage() -> tuple[int, int]:
    """Return (file_count, total_bytes) for all audio files."""
    root = _audio_root()
    count = 0
    total = 0
    for f in root.rglob("*.wav"):
        count += 1
        total += f.stat().st_size
    for f in root.rglob("*.flac"):
        count += 1
        total += f.stat().st_size
    return count, total


def _update_metrics() -> None:
    try:
        count, total = get_disk_usage()
        audio_files_total.set(count)
        audio_disk_bytes.set(total)
    except Exception:
        pass
