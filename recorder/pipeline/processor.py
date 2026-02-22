"""
RecorderPipeline — encapsulates all shared state and worker threads.

Phase 5 features:
- Bounded proc_q (maxsize=50) with backpressure
- Dead letter queue (FailedSegment) after 3 retries
- Graceful shutdown: drain proc_q up to 60s
- Segment deduplication by audio_key
- Pipeline class with start() / stop() / status()
"""
from __future__ import annotations

import datetime
import logging
import queue
import threading
import time

from recorder.audio.capture import record_loop
from recorder.audio.segmenter import segmenter_loop
from recorder.config import settings
from recorder.db.repository import FailedSegmentRepository, SegmentRepository
from recorder.db.session import SessionLocal
from recorder.llm.client import analyze_transcript
from recorder.metrics import queue_depth, segments_total, transcription_duration
from recorder.pipeline.hourly import hourly_worker
from recorder.storage.audio_store import wav_path_to_key
from recorder.transcription.cloud import transcribe_cloud
from recorder.transcription.whisper_asr import get_model, transcribe_local

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
SHUTDOWN_DRAIN_TIMEOUT = 60  # seconds

# Minimum real speech to bother persisting
MIN_SEGMENT_DURATION_SEC = 2.0   # shorter segments are almost certainly noise
MIN_WORD_COUNT = 3                # "Thank you." = 2 words → dropped

# Whisper reliably hallucinates these phrases on silent/noisy audio.
# Normalised to lowercase, stripped of punctuation for comparison.
_HALLUCINATIONS: frozenset[str] = frozenset({
    "thank you",
    "thanks",
    "thank you so much",
    "thank you very much",
    "thanks for watching",
    "thank you for watching",
    "thanks for listening",
    "thank you for listening",
    "you",
    "bye",
    "goodbye",
    "see you next time",
    "see you later",
    "take care",
    "have a good day",
    "have a great day",
    "good night",
    "good morning",
    "good afternoon",
    "good evening",
    "hello",
    "hi",
    "hey",
    "okay",
    "ok",
    "yes",
    "no",
    "um",
    "uh",
    "hmm",
    "music",
    "applause",
    "laughter",
    "silence",
    "inaudible",
    "foreign",
    "subtitles by",
    "subscribe",
    "like and subscribe",
})


def _is_hallucination(text: str) -> bool:
    """Return True if the transcript looks like a Whisper hallucination."""
    import re
    normalised = re.sub(r"[^\w\s]", "", text.lower()).strip()
    # Exact match
    if normalised in _HALLUCINATIONS:
        return True
    # Contained entirely within a hallucination (e.g. "[Music]" → "music")
    if normalised.strip("[]()") in _HALLUCINATIONS:
        return True
    return False


class _LiveTranscriptHolder:
    """Thread-safe live transcript state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text = ""

    def set(self, text: str) -> None:
        with self._lock:
            self._text = text

    def get(self) -> str:
        with self._lock:
            return self._text


class RecorderPipeline:
    """
    Manages the audio → segment → transcription → analysis → DB pipeline.

    All state (queues, threads, flags) lives here — no globals.
    """

    def __init__(self) -> None:
        self.audio_q: queue.Queue = queue.Queue()
        self.proc_q: queue.Queue = queue.Queue(maxsize=50)
        self.stop_flag = threading.Event()
        self.live_transcript = _LiveTranscriptHolder()
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._running = False

        # SSE event bus — set by app.py after construction
        self.event_bus = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self.stop_flag.clear()

            # Eagerly load Whisper model before threads start
            get_model()

            t_rec = threading.Thread(
                target=record_loop,
                args=(self.audio_q, self.stop_flag),
                daemon=True,
                name="recorder-audio",
            )
            t_seg = threading.Thread(
                target=segmenter_loop,
                args=(
                    self.audio_q,
                    self.proc_q,
                    self.stop_flag,
                    get_model(),
                    self.live_transcript,
                ),
                daemon=True,
                name="recorder-segmenter",
            )
            t_proc = threading.Thread(
                target=self._process_worker,
                daemon=True,
                name="recorder-processor",
            )
            t_hourly = threading.Thread(
                target=hourly_worker,
                args=(self.stop_flag,),
                daemon=True,
                name="recorder-hourly",
            )
            t_metrics = threading.Thread(
                target=self._metrics_sampler,
                daemon=True,
                name="recorder-metrics",
            )

            for t in (t_rec, t_seg, t_proc, t_hourly, t_metrics):
                t.start()

            self._threads = {
                "rec": t_rec,
                "seg": t_seg,
                "proc": t_proc,
                "hourly": t_hourly,
                "metrics": t_metrics,
            }
            self._running = True
            logger.info("pipeline.started")
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self._running:
                return False
            logger.info("pipeline.stopping")
            self.stop_flag.set()

            # Drain the processing queue gracefully
            deadline = time.monotonic() + SHUTDOWN_DRAIN_TIMEOUT
            while not self.proc_q.empty() and time.monotonic() < deadline:
                time.sleep(0.5)

            for t in self._threads.values():
                t.join(timeout=3)

            self._threads = {}
            self._running = False
            logger.info("pipeline.stopped")
            return True

    def status(self) -> dict:
        return {
            "running": self._running,
            "queue_depth": self.proc_q.qsize(),
            "live_transcript": self.live_transcript.get(),
        }

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def _metrics_sampler(self) -> None:
        """Sample queue depth every 5s for Prometheus."""
        while not self.stop_flag.is_set():
            queue_depth.set(self.proc_q.qsize())
            time.sleep(5)

    def _process_worker(self) -> None:
        """Main worker: transcribe, analyze, persist each segment."""
        logger.info("processor.started")
        db = SessionLocal()
        try:
            while not self.stop_flag.is_set() or not self.proc_q.empty():
                try:
                    item = self.proc_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                self._process_item(db, item)
                self.proc_q.task_done()
        finally:
            db.close()
            logger.info("processor.stopped")

    def _process_item(self, db, item: tuple, attempt: int = 1) -> None:
        wav_path, seg_start_ts_iso, seg_end_ts_iso, duration = item

        seg_repo = SegmentRepository(db)
        audio_key = wav_path_to_key(wav_path)

        # Deduplication
        if seg_repo.exists_by_audio_key(audio_key):
            logger.info("processor.duplicate_skipped", extra={"audio_key": audio_key})
            return

        try:
            logger.info("processor.transcribing", extra={"wav_path": wav_path})

            # 1. Load and denoise audio
            from recorder.audio.denoiser import denoise_audio

            audio = denoise_audio(wav_path)

            # 2. Transcribe (cloud first, local fallback)
            t0 = time.monotonic()
            txt = transcribe_cloud(wav_path)
            if txt is None:
                txt, segments_list = transcribe_local(audio)
            else:
                segments_list = []
            transcription_duration.observe(time.monotonic() - t0)

            if not txt:
                logger.info("processor.empty_transcript", extra={"wav_path": wav_path})
                return

            # --- Quality gates ---
            if duration < MIN_SEGMENT_DURATION_SEC:
                logger.info(
                    "processor.segment_too_short",
                    extra={"duration": round(duration, 2), "wav_path": wav_path},
                )
                return

            word_count = len(txt.split())
            if word_count < MIN_WORD_COUNT:
                logger.info(
                    "processor.too_few_words",
                    extra={"words": word_count, "text": txt, "wav_path": wav_path},
                )
                return

            if _is_hallucination(txt):
                logger.info(
                    "processor.hallucination_dropped",
                    extra={"text": txt, "wav_path": wav_path},
                )
                return

            logger.info(
                "processor.transcript",
                extra={"chars": len(txt), "preview": txt[:80]},
            )

            # 3. Diarization (optional)
            diarized_txt = self._diarize(wav_path, segments_list)

            # 4. Single LLM call — all analysis at once (Phase 6)
            result = analyze_transcript(txt, diarized_txt)

            # 5. Keyword extraction fallback
            keywords_str = self._extract_keywords(txt)
            if result.keywords:
                keywords_str = ",".join(result.keywords)

            # 6. Persist
            seg_repo.create(
                start_ts=seg_start_ts_iso,
                end_ts=seg_end_ts_iso,
                duration_sec=duration,
                audio_path=wav_path,
                audio_key=audio_key,
                transcript=txt,
                summary=result.summary,
                keywords=keywords_str,
                speakers=result.speakers,
                participants=",".join(result.participants),
                category=result.category,
                action_items="\n".join(result.action_items),
                questions="\n".join(result.open_questions),
                sentiment=result.sentiment,
            )
            segments_total.inc()

            # Publish SSE event
            if self.event_bus:
                seg = seg_repo.exists_by_audio_key(audio_key)  # re-fetch for id
                self.event_bus.publish("segment.created", {"audio_key": audio_key})

        except Exception as exc:
            logger.error(
                "processor.error",
                extra={"wav_path": wav_path, "attempt": attempt, "error": str(exc)},
            )
            if attempt >= MAX_RETRIES:
                self._send_to_dlq(db, wav_path, str(exc))
            else:
                # Retry
                logger.info("processor.retrying", extra={"attempt": attempt + 1})
                time.sleep(2**attempt)
                self._process_item(db, item, attempt=attempt + 1)

    def _diarize(self, wav_path: str, segments_list) -> str:
        if not settings.use_diarization:
            return ""
        try:
            from pyannote.audio import Pipeline as DiarizationPipeline  # type: ignore

            pipeline = DiarizationPipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=settings.hf_token,
            )
            diarization = pipeline(wav_path)
            speaker_turns = [
                (turn.start, turn.end, spk)
                for turn, _, spk in diarization.itertracks(yield_label=True)
            ]
            result = []
            for seg in segments_list:
                seg_mid = (seg.start + seg.end) / 2
                speaker = "?"
                for start, end, spk in speaker_turns:
                    if start <= seg_mid <= end:
                        speaker = spk
                        break
                result.append(f"[{speaker}] {seg.text.strip()}")
            return "\n".join(result)
        except Exception as exc:
            logger.debug("diarization.error", extra={"error": str(exc)})
            return ""

    def _extract_keywords(self, text: str) -> str:
        try:
            import yake  # type: ignore

            kw_extractor = yake.KeywordExtractor(lan="en", n=1, top=10)
            return ",".join(k for k, _ in kw_extractor.extract_keywords(text))
        except Exception:
            return ""

    def _send_to_dlq(self, db, wav_path: str, error: str) -> None:
        try:
            FailedSegmentRepository(db).create(audio_path=wav_path, error=error)
            logger.error("processor.dlq", extra={"wav_path": wav_path})
        except Exception as exc:
            logger.error("processor.dlq_write_error", extra={"error": str(exc)})

    # ------------------------------------------------------------------
    # Queue management (for /api/v1/recordings/reprocess)
    # ------------------------------------------------------------------

    def enqueue(self, wav_path: str, start_ts: str, end_ts: str, duration: float) -> bool:
        """Non-blocking enqueue. Returns False if queue is full."""
        try:
            # Warn at 80% capacity
            if self.proc_q.qsize() >= 40:
                logger.warning("proc_queue.high_watermark", extra={"depth": self.proc_q.qsize()})
            self.proc_q.put_nowait((wav_path, start_ts, end_ts, duration))
            return True
        except queue.Full:
            logger.warning("proc_queue.full", extra={"wav_path": wav_path})
            return False
