# Audio Recorder

An intelligent audio recording application with automatic transcription, AI analysis, semantic search, and a modern web UI.

## System Design

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Browser (index.html)                           │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  ┌──────────────┐  │
│  │  Control UI  │  │  Segment     │  │  Date-Range   │  │  Audio       │  │
│  │  Start/Stop  │  │  Browser     │  │  Summary +    │  │  Player      │  │
│  │  Live text   │  │  Search/Tags │  │  Export       │  │  <audio>     │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  └──────┬───────┘  │
│         │   REST/SSE      │                  │                  │          │
└─────────┼─────────────────┼──────────────────┼──────────────────┼──────────┘
          │                 │                  │                  │
          ▼                 ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Flask API  (api/app.py)                             │
│                                                                             │
│  Middleware: inject_request_id → require_api_key (X-API-Key / Bearer)       │
│                                                                             │
│  ┌──────────────┐ ┌─────────────┐ ┌─────────────┐ ┌──────────────────────┐ │
│  │ /recordings  │ │ /segments   │ │ /summaries  │ │ /export  /stream     │ │
│  │ start stop   │ │ list search │ │ daily hourly│ │ json md csv  SSE     │ │
│  │ status live  │ │ patch delete│ │ range       │ │ /segments/<id>/audio │ │
│  └──────┬───────┘ └──────┬──────┘ └──────┬──────┘ └──────────────────────┘ │
└─────────┼────────────────┼───────────────┼─────────────────────────────────┘
          │                │               │
          ▼                ▼               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      RecorderPipeline  (pipeline/processor.py)              │
│                                                                             │
│   pipeline.start()   pipeline.stop()   pipeline.status()   pipeline.enqueue()│
│                                                                             │
│   stop_flag (threading.Event)   live_transcript (_LiveTranscriptHolder)     │
│   event_bus (EventBus → SSE)                                                │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │  spawns
        ┌──────────────────────┼────────────────────────────┐
        ▼                      ▼                            ▼
┌───────────────┐   ┌──────────────────┐         ┌──────────────────┐
│recorder-hourly│   │  recorder-audio  │         │ recorder-metrics │
│ hourly.py     │   │  capture.py      │         │ (every 5 s)      │
│               │   │  sd.RawInputStream         │ queue_depth.set()│
│ every 5 min:  │   │  audio_callback()│         └──────────────────┘
│ • hourly digest    └────────┬─────────┘
│ • daily digest              │ raw PCM frames (int16, 480 samples/frame)
│   (midnight)                ▼
│ • retention cleanup  ┌──────────────────┐
│   (audio files)      │   audio_q        │  unbounded queue.Queue
└──────────┬───────────┘   (queue.Queue)  │
           │               └──────┬───────┘
           │                      │
           │               ┌──────▼───────────────────────────────────────────┐
           │               │        recorder-segmenter  (segmenter.py)        │
           │               │                                                  │
           │               │  WebRTC VAD (aggressiveness=1, 30ms frames)      │
           │               │                                                  │
           │               │  Speech?──Yes──► accumulate frames               │
           │               │     │                                            │
           │               │  Silence > off_time_sec (3s)?                   │
           │               │  OR buffer > max_segment_sec (120s)?            │
           │               │     │                                            │
           │               │     ▼  flush_segment()                           │
           │               │  Save WAV → audio/seg_YYYY-MM-DDTHH-MM-SS.wav   │
           │               │                                                  │
           │               │  ┌── live transcription (every 2s) ─────────┐   │
           │               │  │  transcribe_local(buffer) → text          │   │
           │               │  │  live_transcript.set(text)                │   │
           │               │  └───────────────────────────────────────────┘   │
           │               └──────────────────────┬───────────────────────────┘
           │                                      │ (wav_path, start_ts, end_ts, duration)
           │                                      ▼
           │                              ┌───────────────┐
           │                              │    proc_q     │  bounded queue.Queue
           │                              │  maxsize=50   │  backpressure applied
           │                              └───────┬───────┘  warn at depth ≥ 40
           │                                      │
           │                               ┌──────▼────────────────────────────────┐
           │                               │    recorder-processor  (processor.py)  │
           │                               │                                        │
           │                               │  1. denoise_audio()  (noisereduce)     │
           │                               │                                        │
           │                               │  2. transcribe_cloud()  ──fail──►      │
           │                               │     LiteLLM whisper-1                  │
           │                               │         │ success                      │
           │                               │         ▼                              │
           │                               │     transcribe_local()  (fallback)     │
           │                               │     faster-whisper (beam=5)            │
           │                               │     → (text, segments_list)            │
           │                               │                                        │
           │                               │  3. Quality gates ──► DROP if:         │
           │                               │     • duration  < 2.0 s                │
           │                               │     • word count < 3                   │
           │                               │     • _is_hallucination() (40 phrases) │
           │                               │                                        │
           │                               │  4. _diarize()  (optional)             │
           │                               │     pyannote/speaker-diarization-3.1   │
           │                               │     → "[SPEAKER_00] text\n..."         │
           │                               │                                        │
           │                               │  5. analyze_transcript()  (1 LLM call) │
           │                               │     LiteLLM gpt-4o-mini  ──fail──►     │
           │                               │     llama.cpp (local)    ──fail──►     │
           │                               │     _simple_summarize()  (extractive)  │
           │                               │     → AnalysisResult {                 │
           │                               │         summary, speakers,             │
           │                               │         participants, category,        │
           │                               │         action_items, open_questions,  │
           │                               │         sentiment, keywords }          │
           │                               │                                        │
           │                               │  6. _extract_keywords()  (YAKE)        │
           │                               │     (fallback if LLM keywords empty)   │
           │                               │                                        │
           │                               │  7. SegmentRepository.create()         │
           │                               │     → INSERT segments + FTS5 trigger   │
           │                               │                                        │
           │                               │  8. generate_embedding()               │
           │                               │     all-MiniLM-L6-v2 (384-dim, local)  │
           │                               │     → SegmentEmbeddingRepository.store()│
           │                               │     → segment_embeddings BLOB table    │
           │                               │                                        │
           │                               │  Retry:  3 attempts, 2^n s backoff     │
           │                               │  DLQ:    failed_segments after 3 fails │
           │                               └──────────────────┬────────────────────┘
           │                                                  │
           ▼                                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        SQLite  (journal.db)                                  │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  segments                                                            │    │
│  │  id · start_ts · end_ts · duration_sec                              │    │
│  │  audio_path · audio_key (UNIQUE)                                    │    │
│  │  transcript · summary · keywords · speakers · participants          │    │
│  │  category · action_items · questions · sentiment                    │    │
│  │  important · tags (JSON) · word_count · char_count                  │    │
│  │  created_at · updated_at · deleted_at (soft delete)                 │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────┐  ┌────────────────────────────────────────┐   │
│  │  segments_fts (FTS5)     │  │  hourly_digests / daily_digests        │   │
│  │  content=segments        │  │  hour_start · summary (hourly)         │   │
│  │  auto-synced by triggers │  │  date · summary · action_items (daily) │   │
│  │  MATCH queries           │  └────────────────────────────────────────┘   │
│  └──────────────────────────┘  ┌────────────────────────────────────────┐   │
│  ┌────────────────────────────┐ │  failed_segments (DLQ)                 │   │
│  │  segment_embeddings        │ │  audio_path · error · attempts         │   │
│  │  segment_id · embedding    │ └────────────────────────────────────────┘   │
│  │  (float32 BLOB, 384-dim)   │                                              │
│  │  NumPy cosine similarity   │                                              │
│  └────────────────────────────┘                                              │
│                                                                              │
│  WAL mode · busy_timeout=30s · synchronous=NORMAL                           │
│  Alembic migrations: 001_initial → 002_fts5 → 003_failed_segments           │
│                      → 004_embeddings                                        │
└──────────────────────────────────────────────────────────────────────────────┘

                              ┌──────────────────────────────┐
                              │  audio/  (filesystem)        │
                              │  YYYY/MM/DD/seg_*.wav        │
                              │  retention cleanup hourly    │
                              │  audio_store.py              │
                              └──────────────────────────────┘
```

---

### SSE Real-Time Event Flow

```
RecorderPipeline / API routes
         │
         │  event_bus.publish(event_type, data)
         ▼
┌─────────────────────────────────────────────┐
│  EventBus  (api/sse.py)                     │
│                                             │
│  _subscribers: list[Queue]  (max 10)        │
│  _lock: threading.Lock                      │
│                                             │
│  publish() → fan-out to all subscriber Qs  │
│  subscribe() → new Queue (caller's stream) │
│  unsubscribe() → remove on disconnect      │
└─────────────┬───────────────────────────────┘
              │  one Queue per open browser tab
    ┌─────────┼─────────┐
    ▼         ▼         ▼
 tab-1     tab-2     tab-3
 Queue     Queue     Queue
    │
    ▼
 GET /api/v1/stream  (Flask generator)
 Content-Type: text/event-stream
    │
    │  event: segment.created
    │  data: {"audio_key": "seg_2026-02-21T14-30-00.wav"}
    │
    │  event: live_transcript
    │  data: {"text": "and so the meeting agenda for today..."}
    │
    │  event: recording.started
    │  data: {"running": true}
    │
    │  : keepalive  (every 30 s)
    │
    ▼
 Browser EventSource listener
 → update segment list, live text, status badge
```

**Events published by:**

| Event | Published by | Trigger |
|---|---|---|
| `segment.created` | `processor.py` | After DB insert + embedding stored |
| `segment.updated` | `routes/segments.py` | After PATCH |
| `recording.started` | `routes/recordings.py` | On `pipeline.start()` |
| `recording.stopped` | `routes/recordings.py` | On `pipeline.stop()` |
| `live_transcript` | `segmenter.py` | Every ~2 s while speaking |

---

### Thread Model

```
main thread  (Flask WSGI)
│
├── recorder-audio     capture.py      sd.RawInputStream → audio_q
│                                      C audio callback (real-time)
│
├── recorder-segmenter segmenter.py    audio_q → VAD → proc_q
│                                      + live transcript every 2 s
│
├── recorder-processor processor.py    proc_q → denoise → transcribe
│                                      → diarize → LLM → DB → embed → SSE
│
├── recorder-hourly    hourly.py       sleep loop, fires every ~5 min
│                                      hourly digest / daily / retention
│
└── recorder-metrics   processor.py    queue_depth gauge every 5 s

All threads: daemon=True, share stop_flag (threading.Event)
Shutdown: stop_flag.set() → drain proc_q (≤60 s) → join all (3 s each)
```

---

### Data Flow Summary

```
Microphone
    │ PCM int16 @ 16 kHz
    ▼
audio_q  (unbounded)
    │ 480-sample frames (30 ms)
    ▼
WebRTC VAD  →  accumulate speech
    │ silence > 3 s  OR  buffer > 120 s
    ▼
Save WAV  →  audio/seg_*.wav
    │ (wav_path, start_ts, end_ts, duration)
    ▼
proc_q  (maxsize=50, backpressure)
    │
    ▼  Quality gates (drop if fails)
Denoise  →  Transcribe  →  Diarize  →  LLM Analysis
    │
    ▼  persist
SQLite segments table  +  FTS5 index
    │
    ▼  embed (async, ~50ms, local model)
segment_embeddings BLOB table  (384-dim float32)
    │
    ▼  fan-out
SSE EventBus  →  Browser tabs

Search query  ──►  generate_embedding(q)
                       │ cosine similarity (NumPy, full scan)
                       ▼
                   top-k segment IDs  →  filter dates/tags  →  return
                   falls back to FTS5 MATCH  →  LIKE if no embeddings
```

---

## Features

### Core Recording
- **Continuous Audio Capture** — Records audio with WebRTC voice activity detection (VAD)
- **Automatic Transcription** — Faster Whisper local ASR with optional cloud fallback via LiteLLM
- **Smart Segmentation** — Splits recordings on silence; configurable silence timeout and max length
- **Hallucination Filtering** — Drops known Whisper phantom phrases ("Thank you.", "Music", etc.), short segments (<2s), and low word-count transcripts (<3 words)
- **Audio Denoising** — Optional `noisereduce`-based denoising before transcription

### AI Intelligence
- **Single LLM call per segment** — One structured JSON call produces summary, speakers, participants, category, action items, open questions, sentiment, and keywords simultaneously
- **Concise summaries** — Summaries are capped to main topic + 2-3 bullets; no padding or introductory sentences
- **Speaker Diarization** — Optional `pyannote.audio` integration for multi-speaker transcripts
- **Keyword Extraction** — YAKE fallback when LLM keywords are absent
- **Hourly Digests** — Automatic summaries every hour via background worker
- **Daily Summaries** — On-demand or auto-generated at midnight; chunked for long transcripts
- **Date-Range Summaries** — Summarise any span of days with export

### Semantic Search
- **Vector embeddings** — Every segment is embedded with `all-MiniLM-L6-v2` (384-dim, local, free, ~90MB)
- **Semantic search** — `?q=budget discussion` finds "Q3 projections", "cut costs", "financial planning" — not just literal keyword matches
- **NumPy cosine similarity** — Full linear scan over BLOB-stored float32 vectors; <10ms for thousands of segments, no external index required
- **Graceful degradation** — Falls back to FTS5 full-text search if embeddings unavailable, then LIKE as last resort
- **Backfill** — `--backfill-embeddings` embeds all existing segments in one pass

### Modern Web UI
- **Real-time updates** — Server-Sent Events (SSE) replace polling; segments appear instantly
- **Live transcript** — See in-progress speech in the status bar as you speak
- **Segment browser** — Semantic + full-text search, tag filtering, click to expand details
- **Date-range summary panel** — Pick start/end dates, generate summary, export (JSON / Markdown / CSV)
- **Audio playback** — In-browser `<audio>` player per segment
- **Statistics** — Segment count, total duration, word count for today
- **Export** — JSON, Markdown, CSV for any date range
- **Dark mode** — Automatic via `prefers-color-scheme`

### Production-Grade Architecture
- **Python package** (`recorder/`) — No global state; all shared state in `RecorderPipeline` class
- **Pydantic settings** — Single `config.py` for all env vars; no scattered `os.getenv()` calls
- **SQLAlchemy ORM** + **Alembic migrations** — Schema versioned; safe upgrades
- **FTS5 full-text search** — SQLite virtual table with auto-sync triggers
- **Bounded processing queue** — `maxsize=50` with backpressure; dead letter queue after 3 retries
- **Graceful shutdown** — Drains queue up to 60 s before exiting
- **Structured JSON logging** — `python-json-logger` with rotating file handler + `request_id` correlation
- **Prometheus metrics** — `/metrics` endpoint (no-op stubs when `prometheus_client` not installed)
- **API key authentication** — `X-API-Key` / `Authorization: Bearer` / `?api_key=` (for SSE)

---

## Quick Start

### Installation

```bash
git clone <your-repo-url>
cd recorder

python3 -m venv venv
source venv/bin/activate

pip install -e .
# with semantic search (recommended):
pip install -e ".[embeddings]"
# with all optional extras:
pip install -e ".[embeddings,noise,keywords,diarization]"
```

### Configuration

Copy `.env.example` to `.env` and edit:

```bash
cp .env.example .env
```

Minimum required settings:

```bash
# Generate a random API key (optional — omit to run without auth)
RECORDER_API_KEY=your-secret-key-here

# Whisper model size
WHISPER_MODEL=small          # tiny | base | small | medium | large-v2

# LLM for analysis (optional)
LITELLM_API_KEY=sk-...
LITELLM_MODEL_ID=openai/gpt-4o-mini
LITELLM_BASE_URL=https://api.openai.com
```

### Running

```bash
# Run database migrations (first time or after upgrade)
python main.py --migrate

# Embed existing segments for semantic search (first time only)
python main.py --backfill-embeddings

# Web UI (recommended)
python main.py --flask-ui

# Headless recording (no UI, logs to console)
python main.py

# Hourly digest worker only
python main.py --only-hourly
```

Open http://127.0.0.1:5000 in your browser.

If `RECORDER_API_KEY` is set, you will be prompted for the key on first load (stored in `localStorage`).

---

## Configuration Reference

All settings live in `recorder/config.py` and are read from environment variables or `.env`.

| Variable | Default | Description |
|---|---|---|
| `RECORDER_API_KEY` | *(empty — no auth)* | Static API key for all endpoints |
| `WHISPER_MODEL` | `small` | Faster Whisper model size |
| `WHISPER_BEAM_SIZE` | `5` | Beam search width for Whisper |
| `VAD_AGGRESSIVENESS` | `1` | WebRTC VAD sensitivity 0–3 |
| `OFF_TIME_SEC` | `3` | Silence duration (s) to close a segment |
| `MAX_SEGMENT_SEC` | `120` | Maximum segment length (s) |
| `SAMPLE_RATE` | `16000` | Audio sample rate (Hz) |
| `FRAME_MS` | `30` | VAD frame size (10, 20, or 30 ms) |
| `LITELLM_API_KEY` | *(empty)* | Enables LiteLLM for analysis/summaries when set |
| `LITELLM_MODEL_ID` | `openai/gpt-4o-mini` | LiteLLM model string |
| `LITELLM_BASE_URL` | `https://litellm.marqeta.com` | LiteLLM proxy base URL |
| `LITELLM_TEMPERATURE` | `0.3` | LLM temperature |
| `MODEL_MAX_TOKENS` | `500` | Max tokens for per-segment LLM analysis |
| `USE_LITELLM_TRANSCRIPTION` | `false` | Use LiteLLM Whisper for transcription |
| `LITELLM_TRANSCRIPTION_MODEL` | `whisper-1` | Model for cloud transcription |
| `USE_EMBEDDINGS` | `true` | Enable semantic search via local embeddings |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model name |
| `EMBEDDING_DIM` | `384` | Embedding dimensions (must match model) |
| `USE_DIARIZATION` | `false` | Enable pyannote speaker diarization |
| `HF_TOKEN` | *(empty)* | HuggingFace token for pyannote |
| `USE_NOISEREDUCE` | `true` | Enable noisereduce denoising |
| `AUDIO_RETENTION_DAYS` | `90` | Days to keep audio files (0 = keep forever) |
| `DB_PATH` | `journal.db` | SQLite database path |
| `AUDIO_DIR` | `audio/` | Root directory for audio segments |
| `LOG_DIR` | `logs/` | Log file directory |
| `RECORDER_UI_HOST` | `127.0.0.1` | Flask bind host |
| `RECORDER_UI_PORT` | `5000` | Flask bind port |

---

## API Reference

All endpoints (except `/`, `/api/v1/health`, `/metrics`) require authentication when `RECORDER_API_KEY` is set.

Send the key as:
- Header: `X-API-Key: <key>`
- Header: `Authorization: Bearer <key>`
- Query param: `?api_key=<key>` (for SSE EventSource)

### Health

```
GET /api/v1/health
```
```json
{ "ok": true, "version": "2.0.0", "auth_required": false }
```

### Segments

```
GET /api/v1/segments
```

Query params: `start` (ISO datetime), `end`, `limit` (default 100), `offset`, `q` (semantic + full-text search), `tag`

The `q` parameter first attempts semantic cosine similarity search (requires `sentence-transformers`), falls back to FTS5 full-text, then LIKE.

```
GET /api/v1/segments/<id>
PATCH /api/v1/segments/<id>    body: { "important": true, "tags": ["meeting", "project-x"] }
DELETE /api/v1/segments/<id>   (soft delete)
```

### Recordings

```
POST /api/v1/recordings/start
POST /api/v1/recordings/stop
GET  /api/v1/recordings/status
GET  /api/v1/recordings/live       returns { "transcript": "..." }
POST /api/v1/recordings/reprocess  body: { "wav_path": "...", "start_ts": "...", "end_ts": "...", "duration": 0.0 }
```

### Summaries

```
GET  /api/v1/summaries/daily?date=YYYY-MM-DD
POST /api/v1/summaries/daily       body: { "date": "YYYY-MM-DD" }  (force regenerate)
GET  /api/v1/summaries/hourly?limit=24
GET  /api/v1/summaries/range?start=YYYY-MM-DD&end=YYYY-MM-DD
```

Range response:
```json
{
  "ok": true,
  "start": "2026-02-01",
  "end": "2026-02-07",
  "segment_count": 42,
  "summary": "...",
  "action_items": "..."
}
```

### Export

```
GET /api/v1/export?format=json&start=YYYY-MM-DD&end=YYYY-MM-DD
GET /api/v1/export?format=markdown&start=YYYY-MM-DD&end=YYYY-MM-DD
GET /api/v1/export?format=csv&start=YYYY-MM-DD&end=YYYY-MM-DD
```

### Audio Playback

```
GET /api/v1/segments/<id>/audio
```
Streams the WAV/FLAC file for in-browser playback.

### Real-time Stream (SSE)

```
GET /api/v1/stream
```
Events: `segment.created`, `live_transcript`, `recording.started`, `recording.stopped`, `health`

### Prometheus Metrics

```
GET /metrics
```
Metrics: `recorder_segments_total`, `recorder_transcription_duration_seconds`, `recorder_queue_depth`, `recorder_llm_duration_seconds`, `recorder_llm_errors_total`, `recorder_audio_files_total`, `recorder_audio_disk_bytes`

---

## Database Schema

### segments

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `start_ts` | TEXT | ISO 8601 |
| `end_ts` | TEXT | ISO 8601 |
| `duration_sec` | REAL | |
| `audio_path` | TEXT | Absolute path (legacy) |
| `audio_key` | TEXT UNIQUE | Relative: `YYYY/MM/DD/filename.wav` |
| `transcript` | TEXT | |
| `summary` | TEXT | |
| `keywords` | TEXT | Comma-separated |
| `speakers` | TEXT | Diarized dialogue |
| `participants` | TEXT | Comma-separated names |
| `category` | TEXT | |
| `action_items` | TEXT | Newline-separated |
| `questions` | TEXT | Newline-separated |
| `sentiment` | TEXT | |
| `important` | INTEGER | 0/1 flag |
| `tags` | TEXT | JSON array |
| `word_count` | INTEGER | Auto-computed on insert |
| `char_count` | INTEGER | Auto-computed on insert |
| `deleted_at` | TEXT | Soft delete timestamp |
| `created_at` | TEXT | |
| `updated_at` | TEXT | |

### segment_embeddings

| Column | Type | Notes |
|---|---|---|
| `segment_id` | INTEGER PK | FK → segments(id) ON DELETE CASCADE |
| `embedding` | BLOB | float32 little-endian, 384 values |

Searched via NumPy cosine similarity (full linear scan, <10ms at scale).

### hourly_digests

`id`, `hour_start`, `hour_end`, `summary`, `created_at`

### daily_digests

`id`, `date` (YYYY-MM-DD), `summary`, `action_items`, `created_at`

### failed_segments (dead letter queue)

`id`, `audio_path`, `error`, `attempts`, `created_at`

### segments_fts (FTS5 virtual table)

Automatically synced to `segments` via triggers. Used for `?q=` full-text search (fallback when embeddings unavailable).

---

## Project Structure

```
recorder/
├── recorder/                  # Main package
│   ├── config.py              # Pydantic BaseSettings
│   ├── logging_config.py      # Structured JSON logging
│   ├── metrics.py             # Prometheus counters/histograms
│   ├── audio/
│   │   ├── capture.py         # record_loop(), audio_callback()
│   │   ├── segmenter.py       # segmenter_loop(), flush_segment()
│   │   └── denoiser.py        # denoise_audio()
│   ├── transcription/
│   │   ├── whisper_asr.py     # Local Whisper ASR
│   │   └── cloud.py           # LiteLLM cloud transcription
│   ├── llm/
│   │   ├── client.py          # analyze_transcript(), summarize_daily()
│   │   └── prompts.py         # Prompt templates (concise, bullet-focused)
│   ├── embeddings/
│   │   ├── client.py          # generate_embedding() — all-MiniLM-L6-v2 singleton
│   │   └── backfill.py        # One-time backfill for existing segments
│   ├── pipeline/
│   │   ├── processor.py       # RecorderPipeline class
│   │   └── hourly.py          # Hourly/daily/retention worker
│   ├── db/
│   │   ├── models.py          # SQLAlchemy models
│   │   ├── repository.py      # Repository pattern (all SQL + semantic search here)
│   │   └── session.py         # Engine + SessionLocal
│   ├── api/
│   │   ├── app.py             # Flask app factory
│   │   ├── middleware.py      # Auth + request-id injection
│   │   ├── sse.py             # SSE EventBus
│   │   └── routes/
│   │       ├── health.py
│   │       ├── segments.py
│   │       ├── recordings.py
│   │       ├── summaries.py
│   │       ├── audio.py
│   │       └── export.py
│   ├── storage/
│   │   └── audio_store.py     # Audio file save/load/delete/retention
│   └── templates/
│       └── index.html         # Single-page UI
├── migrations/                # Alembic
│   ├── env.py
│   └── versions/
│       ├── 001_initial.py
│       ├── 002_fts5.py
│       ├── 003_failed_segments.py
│       └── 004_embeddings.py  # segment_embeddings BLOB table
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_repository.py
│   ├── test_llm.py
│   ├── test_api.py
│   ├── test_pipeline.py
│   └── test_transcription.py
├── main.py                    # Entrypoint
├── pyproject.toml
├── alembic.ini
├── .env                       # Local config (not committed)
└── .env.example
```

---

## Database Migrations

Migrations are managed with Alembic. Run them before first use and after any upgrade:

```bash
python main.py --migrate
# or directly:
alembic upgrade head
```

To create a new migration after changing models:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

---

## Testing

```bash
pip install -e ".[dev]"
pytest                          # all tests
pytest --cov=recorder           # with coverage
pytest tests/test_api.py -v     # single file
```

---

## Troubleshooting

### No audio detected
- Check microphone permissions in System Settings
- Lower `VAD_AGGRESSIVENESS` (0 = most sensitive)
- Verify default input device with `python -c "import sounddevice; print(sounddevice.query_devices())"`

### "Thank you." appearing on silence
The hallucination filter handles this automatically. If you see it, ensure your `WHISPER_MODEL` is at least `small` (tiny is more prone to hallucinations). The filter drops: segments under 2 s, transcripts under 3 words, and ~40 known Whisper phantom phrases.

### Semantic search not working
Ensure `sentence-transformers` is installed:
```bash
pip install -e ".[embeddings]"
python main.py --backfill-embeddings   # embed existing segments
```
To disable: set `USE_EMBEDDINGS=false` in `.env`. Search will fall back to FTS5.

### Database locked errors
The app uses WAL mode. If errors persist:
```bash
rm journal.db-wal journal.db-shm
```

### Port already in use
Set `RECORDER_UI_PORT=5001` (or any free port) in `.env`.

---

## Acknowledgments

- [Faster Whisper](https://github.com/guillaumekln/faster-whisper) — Fast local ASR
- [WebRTC VAD](https://github.com/wiseman/py-webrtcvad) — Voice activity detection
- [sentence-transformers](https://www.sbert.net/) — Local semantic embeddings
- [LiteLLM](https://github.com/BerriAI/litellm) — Unified LLM API
- [SQLAlchemy](https://www.sqlalchemy.org/) — ORM
- [Alembic](https://alembic.sqlalchemy.org/) — Database migrations
- [Flask](https://flask.palletsprojects.com/) — Web framework
- [YAKE](https://github.com/LIAAD/yake) — Keyword extraction
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) — Speaker diarization
