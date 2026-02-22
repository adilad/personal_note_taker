"""VAD-based audio segmenter with live transcription."""

from __future__ import annotations

import datetime
import logging
import queue
import time
import wave

import numpy as np
import webrtcvad  # type: ignore

from recorder.config import settings

logger = logging.getLogger(__name__)


def segmenter_loop(
    audio_q: queue.Queue,
    proc_q: queue.Queue,
    stop_flag,
    asr_model,
    live_transcript_holder,
) -> None:
    """
    Consume raw PCM frames from audio_q, detect speech via WebRTC VAD,
    flush complete segments to proc_q, and stream live transcripts.
    """
    vad = webrtcvad.Vad(settings.vad_aggressiveness)
    frame_len = settings.frame_len

    buffer = b""
    active = False
    last_voice_ts = time.time()
    seg_start_ts = datetime.datetime.now()
    seg_end_ts = seg_start_ts
    last_live_transcribe: float = 0.0

    def flush_segment() -> None:
        nonlocal buffer, active, seg_start_ts, seg_end_ts
        live_transcript_holder.set("")
        if not buffer:
            seg_start_ts = datetime.datetime.now()
            return

        assert settings.audio_dir is not None  # always set by Settings._set_derived_paths
        ts_str = seg_start_ts.isoformat(timespec="seconds").replace(":", "-")
        wav_path = str(settings.audio_dir / f"seg_{ts_str}.wav")
        settings.audio_dir.mkdir(parents=True, exist_ok=True)

        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(settings.sample_rate)
            wf.writeframes(buffer)

        duration = len(buffer) / 2 / settings.sample_rate
        logger.info(
            "segment.saved",
            extra={"wav_path": wav_path, "duration_sec": round(duration, 2)},
        )

        try:
            proc_q.put(
                (wav_path, seg_start_ts.isoformat(), seg_end_ts.isoformat(), duration),
                block=True,
                timeout=5,
            )
        except queue.Full:
            logger.warning("proc_queue.full — dropping segment", extra={"wav_path": wav_path})

        buffer = b""
        active = False
        seg_start_ts = datetime.datetime.now()

    def do_live_transcribe() -> None:
        nonlocal last_live_transcribe
        if not buffer or len(buffer) < settings.sample_rate * 2:
            return
        now = time.time()
        if now - last_live_transcribe < 2:
            return
        last_live_transcribe = now
        try:
            audio_np = np.frombuffer(buffer, dtype=np.int16).astype(np.float32) / 32768.0
            segs, _ = asr_model.transcribe(audio_np, beam_size=1, vad_filter=False, language="en")
            txt = " ".join(s.text.strip() for s in segs).strip()
            live_transcript_holder.set(txt)
        except Exception as exc:
            logger.debug("live_transcribe.error", extra={"error": str(exc)})

    logger.info("segmenter.started")
    while not stop_flag.is_set():
        try:
            chunk = audio_q.get(timeout=0.2)
        except queue.Empty:
            chunk = None

        now = time.time()
        if not seg_end_ts:
            seg_end_ts = seg_start_ts

        if chunk is not None:
            for i in range(0, len(chunk), frame_len * 2):
                frame = chunk[i : i + frame_len * 2]
                if len(frame) < frame_len * 2:
                    continue
                is_speech = vad.is_speech(frame, settings.sample_rate)
                if is_speech:
                    if not active:
                        logger.info("vad.speech_detected")
                    last_voice_ts = now
                    active = True
                    seg_end_ts = datetime.datetime.now()
                if active:
                    buffer += frame

        if active:
            do_live_transcribe()

        window_elapsed = (datetime.datetime.now() - seg_start_ts).total_seconds()
        if window_elapsed >= settings.max_segment_sec:
            logger.info(
                "segment.rollover",
                extra={"elapsed_sec": round(window_elapsed, 1)},
            )
            flush_segment()

        if active and (now - last_voice_ts) >= settings.off_time_sec:
            logger.info(
                "vad.silence_flush",
                extra={"silence_sec": round(now - last_voice_ts, 1)},
            )
            flush_segment()
            seg_start_ts = datetime.datetime.now()

    if buffer:
        logger.info("segmenter.final_flush")
        seg_start_ts = datetime.datetime.now()
        flush_segment()

    logger.info("segmenter.stopped")
