import os, queue, sys, time, wave, json, datetime, sqlite3, threading, argparse, socket
import numpy as np
import sounddevice as sd
import webrtcvad

# --- Flask UI imports (simple web UI) ---
try:
    from flask import Flask, jsonify, request, render_template_string
except Exception:
    Flask = None
    print("[flask] Flask not available. Install with: pip install flask", file=sys.stderr)

# --- Optional noise reduction ---
USE_NOISEREDUCE = os.getenv("USE_NOISEREDUCE", "1").lower() in ("1", "true", "yes", "on")
nr = None
try:
    import noisereduce as nr
    print("[nr] Noise reduction enabled")
except ImportError:
    USE_NOISEREDUCE = False
    print("[nr] noisereduce not installed; skipping. Install with: pip install noisereduce")

# --- Optional speaker diarization ---
USE_DIARIZATION = os.getenv("USE_DIARIZATION", "0").lower() in ("1", "true", "yes", "on")
diarization_pipeline = None
if USE_DIARIZATION:
    try:
        from pyannote.audio import Pipeline
        HF_TOKEN = os.getenv("HF_TOKEN", "")
        if HF_TOKEN:
            diarization_pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=HF_TOKEN)
            print("[diarization] Speaker diarization enabled")
        else:
            USE_DIARIZATION = False
            print("[diarization] HF_TOKEN not set; diarization disabled. Get token from huggingface.co")
    except ImportError:
        USE_DIARIZATION = False
        print("[diarization] pyannote-audio not installed. Install with: pip install pyannote-audio")

# --- Optional local LLM setup ---
# Automatically enable if model exists or USE_LLM env var = 1/true
USE_LLM = os.getenv("USE_LLM", "1").lower() in ("1", "true", "yes", "on")
LLM_MODEL_PATH = os.path.expanduser(os.getenv("LLM_MODEL_PATH", "~/models/llama-3-8b-instruct.Q4_K_M.gguf"))
LLM_N_CTX = int(os.getenv("LLM_N_CTX", "4096"))
LLM_N_THREADS = int(os.getenv("LLM_N_THREADS", "8"))

llm = None
try:
    from llama_cpp import Llama  # pip install llama-cpp-python
    if USE_LLM and os.path.exists(LLM_MODEL_PATH):
        llm = Llama(model_path=LLM_MODEL_PATH, n_ctx=LLM_N_CTX, n_threads=LLM_N_THREADS)
        print(f"[llm] Loaded local model: {LLM_MODEL_PATH}")
    else:
        USE_LLM = False
        print(f"[llm] Model not found or disabled; using fallback summarizer.")
except Exception as e:
    USE_LLM = False
    llm = None
    print(f"[llm] Failed to initialize llama.cpp: {e}")

# ASR
from faster_whisper import WhisperModel

APP_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(APP_DIR, "audio")
DB_PATH = os.path.join(APP_DIR, "journal.db")
MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")  # "tiny","base","small","medium","large-v2"
BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "5"))  # higher = more accurate, slower
VAD_AGGRESSIVENESS = int(os.getenv("VAD_AGGRESSIVENESS", "1"))  # 0-3 (lower=more sensitive, catches more speech)
SAMPLE_RATE = 16000
FRAME_MS = 30  # 10, 20, or 30 ms for webrtcvad
OFF_TIME_SEC = 3  # silence to close segment (seconds)

OFF_TIME_SEC = int(os.getenv("OFF_TIME_SEC", str(OFF_TIME_SEC)))
MAX_SEGMENT_SEC = int(os.getenv("MAX_SEGMENT_SEC", "120"))  # max segment length (seconds)

os.makedirs(AUDIO_DIR, exist_ok=True)

# --- DB setup (refactored) ---
def get_db_connection(check_same_thread=False):
    conn = sqlite3.connect(DB_PATH, check_same_thread=check_same_thread, timeout=30.0, isolation_level='DEFERRED')
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=30000;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn

def init_schema(conn):
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS segments (
          id INTEGER PRIMARY KEY,
          start_ts TEXT,
          end_ts TEXT,
          duration_sec REAL,
          audio_path TEXT,
          transcript TEXT,
          summary TEXT,
          keywords TEXT,
          important INTEGER DEFAULT 0,
          speakers TEXT
        )
        """
    )
    # Migration: add speakers column if missing
    cur.execute("PRAGMA table_info(segments)")
    cols = [r[1] for r in cur.fetchall()]
    if "speakers" not in cols:
        cur.execute("ALTER TABLE segments ADD COLUMN speakers TEXT")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_digests (
          id INTEGER PRIMARY KEY,
          date TEXT UNIQUE,
          summary TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS hourly_digests (
          id INTEGER PRIMARY KEY,
          hour_start TEXT UNIQUE,
          hour_end   TEXT,
          summary    TEXT,
          created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_segments_start ON segments(start_ts);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_segments_end   ON segments(end_ts);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_segments_imp   ON segments(important);")
    conn.commit()

conn = get_db_connection(check_same_thread=False)
init_schema(conn)
cur = conn.cursor()

# --- ASR model (local) ---
asr_model = WhisperModel(MODEL_SIZE, device="auto", compute_type="int8")  # CPU-friendly

# --- Optional local LLM (ensure llm is defined if USE_LLM toggled above) ---
if USE_LLM and llm is None and os.path.exists(LLM_MODEL_PATH):
    try:
        llm = Llama(model_path=LLM_MODEL_PATH, n_ctx=4096, n_threads=8)
    except Exception:
        llm = None
        USE_LLM = False

# --- Simple extractive summarizer (fallback) ---
def simple_summarize(text, max_sentences=4):
    sents = [s.strip() for s in text.replace("\n"," ").split(".") if s.strip()]
    if not sents:
        return ""
    scores = [(len(s), i, s) for i,s in enumerate(sents)]
    scores.sort(reverse=True)
    chosen = []
    for _, i, s in scores:
        if len(chosen) >= max_sentences: break
        if any(s.lower()[:40] in c.lower() or c.lower()[:40] in s.lower() for c in chosen):
            continue
        chosen.append(s)
    return ". ".join(chosen) + ("." if chosen else "")

def llm_summarize(text):
    if not text.strip():
        return ""
    if not llm:
        return simple_summarize(text)
    prompt = f"""You are a meticulous note-taker.
Summarize the following transcript into concise bullet points with timestamps, action items (who/what/when), decisions, and key takeaways.
Keep it faithful to the text, no speculation.

Transcript:
{text}"""
    out = llm(prompt, max_tokens=512, temperature=0.1, stop=["</s>"])
    return out["choices"][0]["text"].strip()

# --- Hourly summarization helpers ---
def _hour_floor(iso_ts: str) -> datetime.datetime:
    t = datetime.datetime.fromisoformat(iso_ts)
    return t.replace(minute=0, second=0, microsecond=0)

def _fetch_texts_for_hour(db_conn: sqlite3.Connection, hour_start: datetime.datetime):
    hour_end = hour_start + datetime.timedelta(hours=1)
    c = db_conn.cursor()
    rows = c.execute(
        """
        SELECT COALESCE(NULLIF(transcript,''), NULLIF(summary,'')) AS txt
        FROM segments
        WHERE start_ts >= ? AND start_ts < ?
        ORDER BY start_ts ASC
        """,
        (hour_start.isoformat(), hour_end.isoformat()),
    ).fetchall()
    texts = [(r[0] or "").strip() for r in rows]
    texts = [t for t in texts if len(t) >= 10]
    return texts, hour_end

def _compose_hour_summary(text: str) -> str:
    return llm_summarize(text) if USE_LLM else simple_summarize(text)

def _upsert_hourly(db_conn: sqlite3.Connection, hour_start: datetime.datetime, hour_end: datetime.datetime, combined_text: str) -> str:
    summary = _compose_hour_summary(combined_text)
    c = db_conn.cursor()
    c.execute(
        """
        INSERT INTO hourly_digests(hour_start, hour_end, summary)
        VALUES (?, ?, ?)
        ON CONFLICT(hour_start) DO UPDATE SET
          hour_end=excluded.hour_end,
          summary=excluded.summary
        """,
        (hour_start.isoformat(), hour_end.isoformat(), summary),
    )
    db_conn.commit()
    return summary

# --- Keyword extraction (YAKE fallback) ---
try:
    import yake
    kw_extractor = yake.KeywordExtractor(lan="en", n=1, top=10)
    def extract_keywords(text):
        return [k for k,_ in kw_extractor.extract_keywords(text)]
except Exception:
    def extract_keywords(text):
        return []

# --- Audio / VAD helpers ---
vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
frame_len = int(SAMPLE_RATE * FRAME_MS / 1000)

audio_q = queue.Queue()
stop_flag = threading.Event()
proc_q = queue.Queue()

# --- Live transcription state ---
_live_transcript = ""
_live_lock = threading.Lock()

def set_live_transcript(text: str):
    global _live_transcript
    with _live_lock:
        _live_transcript = text

def get_live_transcript() -> str:
    with _live_lock:
        return _live_transcript

# --- Simple controller state for UI control ---
_threads = {}
_running_lock = threading.Lock()
_running = False

def is_running() -> bool:
    with _running_lock:
        return _running

def _start_threads():
    global _threads, _running
    with _running_lock:
        if _running:
            return False
        stop_flag.clear()
        t_rec = threading.Thread(target=record_loop, daemon=True, name="recorder-audio")
        t_seg = threading.Thread(target=segmenter_loop, daemon=True, name="recorder-segmenter")
        t_proc = threading.Thread(target=process_worker, daemon=True, name="recorder-asr")
        t_hourly = threading.Thread(target=hourly_worker, daemon=True, name="recorder-hourly")
        for t in (t_rec, t_seg, t_proc, t_hourly):
            t.start()
        _threads = {"rec": t_rec, "seg": t_seg, "proc": t_proc, "hourly": t_hourly}
        _running = True
        return True

def _stop_threads():
    global _threads, _running
    with _running_lock:
        if not _running:
            return False
        stop_flag.set()
        for key, t in list(_threads.items()):
            try:
                t.join(timeout=2.5)
            except Exception:
                pass
        _threads = {}
        _running = False
        return True

# --- Modern Flask HTML template ---
_FLASK_INDEX = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Recorder</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #fafafa; color: #333; line-height: 1.5; }
    .container { max-width: 1200px; margin: 0 auto; padding: 2rem 1rem; }
    header { margin-bottom: 2rem; }
    h1 { font-size: 1.5rem; font-weight: 600; margin-bottom: 0.5rem; }
    .status { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; color: #666; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #ddd; }
    .dot.on { background: #ef4444; animation: pulse 2s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
    .controls { display: flex; gap: 0.5rem; margin: 1.5rem 0; }
    button { padding: 0.5rem 1rem; border: 1px solid #ddd; border-radius: 6px; background: white; color: #333; font-size: 0.9rem; cursor: pointer; transition: all 0.15s; }
    button:hover { border-color: #999; background: #f9f9f9; }
    button:active { transform: scale(0.98); }
    .btn-start { border-color: #10b981; color: #10b981; }
    .btn-start:hover { background: #f0fdf4; border-color: #059669; }
    .btn-stop { border-color: #ef4444; color: #ef4444; }
    .btn-stop:hover { background: #fef2f2; border-color: #dc2626; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    .card { background: white; border: 1px solid #e5e5e5; border-radius: 8px; padding: 1.25rem; }
    .card h2 { font-size: 1rem; font-weight: 600; margin-bottom: 1rem; }
    .search { width: 100%; padding: 0.5rem; border: 1px solid #e5e5e5; border-radius: 6px; font-size: 0.9rem; margin-bottom: 0.75rem; }
    .search:focus { outline: none; border-color: #999; }
    .list { max-height: 500px; overflow-y: auto; }
    .item { padding: 0.75rem; border-bottom: 1px solid #f5f5f5; cursor: pointer; }
    .item:hover { background: #fafafa; }
    .item:last-child { border-bottom: none; }
    .time { font-size: 0.8rem; color: #999; }
    .text { font-size: 0.9rem; margin-top: 0.25rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .summary { background: #fafafa; padding: 1rem; border-radius: 6px; font-size: 0.9rem; white-space: pre-wrap; max-height: 500px; overflow-y: auto; line-height: 1.6; }
    .empty { text-align: center; padding: 2rem; color: #999; font-size: 0.9rem; }
    .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.4); z-index: 1000; align-items: center; justify-content: center; }
    .modal.active { display: flex; }
    .modal-content { background: white; padding: 1.5rem; border-radius: 8px; max-width: 600px; width: 90%; max-height: 80vh; overflow-y: auto; }
    .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
    .modal-close { background: none; border: none; font-size: 1.5rem; cursor: pointer; color: #999; }
    @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
    @media (prefers-color-scheme: dark) {
      body { background: #111; color: #ddd; }
      .card, button, .search, .modal-content { background: #1a1a1a; border-color: #333; color: #ddd; }
      button:hover { background: #222; }
      .item:hover { background: #222; }
      .summary { background: #222; }
      .dot { background: #444; }
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Recorder</h1>
      <div class="status">
        <div class="dot" id="dot"></div>
        <span id="statusText">Loading...</span>
      </div>
    </header>

    <div class="controls">
      <button class="btn-start" id="startBtn">Start</button>
      <button class="btn-stop" id="stopBtn">Stop</button>
      <button id="refreshBtn">Refresh</button>
      <button id="summaryBtn">Summary</button>
      <button id="exportBtn">Export</button>
    </div>

    <div class="card live-card" id="liveCard" style="display:none; margin-bottom:1rem;">
      <h2>🎤 Live</h2>
      <div class="summary" id="liveBox" style="min-height:60px;"></div>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Segments</h2>
        <input type="text" class="search" id="searchBox" placeholder="Search...">
        <div class="list" id="segmentList">
          <div class="empty">No segments</div>
        </div>
      </div>

      <div class="card">
        <h2>Summary</h2>
        <div class="summary" id="summaryBox">Click Summary to generate</div>
      </div>
    </div>
  </div>

  <div class="modal" id="segmentModal">
    <div class="modal-content">
      <div class="modal-header">
        <h2>Details</h2>
        <button class="modal-close" onclick="closeModal()">×</button>
      </div>
      <div id="modalBody"></div>
    </div>
  </div>

  <script>
    let segments = [];
    let searchTerm = '';

    async function api(method, path) {
      const res = await fetch(path, { method });
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    async function updateStatus() {
      try {
        const data = await api('GET', '/api/health');
        const dot = document.getElementById('dot');
        const text = document.getElementById('statusText');
        if (data.running) {
          dot.classList.add('on');
          text.textContent = 'Recording';
        } else {
          dot.classList.remove('on');
          text.textContent = 'Idle';
        }
      } catch (e) {
        document.getElementById('statusText').textContent = 'Error';
      }
    }

    async function loadSegments() {
      try {
        const data = await api('GET', '/api/segments');
        segments = data.segments || [];
        renderSegments();
      } catch (e) {
        console.error(e);
      }
    }

    function renderSegments() {
      const list = document.getElementById('segmentList');
      const filtered = segments.filter(s => 
        !searchTerm || (s.transcript && s.transcript.toLowerCase().includes(searchTerm.toLowerCase()))
      );
      
      if (filtered.length === 0) {
        list.innerHTML = '<div class="empty">No segments</div>';
        return;
      }

      list.innerHTML = filtered.map(s => `
        <div class="item" onclick="showSegment(${s.id})">
          <div class="time">${formatTime(s.start_ts)} • ${s.duration_sec.toFixed(1)}s</div>
          <div class="text">${s.transcript || '...'}</div>
        </div>
      `).join('');
    }

    function formatTime(iso) {
      const d = new Date(iso);
      return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    }

    function showSegment(id) {
      const seg = segments.find(s => s.id === id);
      if (!seg) return;
      const modal = document.getElementById('segmentModal');
      const body = document.getElementById('modalBody');
      body.innerHTML = `
        <p><strong>Time:</strong> ${new Date(seg.start_ts).toLocaleString()}</p>
        <p><strong>Duration:</strong> ${seg.duration_sec.toFixed(1)}s</p>
        ${seg.speakers ? `<p style="margin-top:1rem"><strong>Speakers:</strong></p><div class="summary" style="white-space:pre-wrap">${seg.speakers}</div>` : ''}
        <p style="margin-top:1rem"><strong>Transcript:</strong></p>
        <div class="summary">${seg.transcript || '(no transcript)'}</div>
        ${seg.summary ? `<p style="margin-top:1rem"><strong>Summary:</strong></p><div class="summary">${seg.summary}</div>` : ''}
        ${seg.keywords ? `<p style="margin-top:1rem"><strong>Keywords:</strong> ${seg.keywords}</p>` : ''}
      `;
      modal.classList.add('active');
    }

    function closeModal() {
      document.getElementById('segmentModal').classList.remove('active');
    }

    async function generateSummary() {
      const box = document.getElementById('summaryBox');
      box.textContent = 'Generating...';
      try {
        const data = await api('GET', '/api/summary');
        box.textContent = data.summary || '(no data)';
      } catch (e) {
        box.textContent = 'Error: ' + e.message;
      }
    }

    async function exportData() {
      try {
        const data = await api('GET', '/api/segments');
        const blob = new Blob([JSON.stringify(data.segments, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `recorder-${new Date().toISOString().split('T')[0]}.json`;
        a.click();
      } catch (e) {
        alert('Export failed');
      }
    }

    document.getElementById('startBtn').onclick = async () => { await api('POST', '/api/start'); await updateStatus(); };
    document.getElementById('stopBtn').onclick = async () => { await api('POST', '/api/stop'); await updateStatus(); };
    document.getElementById('refreshBtn').onclick = () => { loadSegments(); updateStatus(); };

    async function updateLive() {
      try {
        const data = await api('GET', '/api/live');
        const card = document.getElementById('liveCard');
        const box = document.getElementById('liveBox');
        if (data.running) {
          card.style.display = 'block';
          box.textContent = data.transcript || '(listening...)';
        } else {
          card.style.display = 'none';
        }
      } catch (e) {}
    }
    setInterval(updateLive, 1000);
    document.getElementById('summaryBtn').onclick = generateSummary;
    document.getElementById('exportBtn').onclick = exportData;
    document.getElementById('searchBox').oninput = (e) => { searchTerm = e.target.value; renderSegments(); };
    document.getElementById('segmentModal').onclick = (e) => { if (e.target.id === 'segmentModal') closeModal(); };

    updateStatus();
    loadSegments();
    setInterval(updateStatus, 3000);
    setInterval(loadSegments, 5000);
  </script>
</body>
</html>"""

# --- Flask app factory ---
def create_flask_app(host: str = "127.0.0.1", port: int = 5000) -> "Flask":
    if Flask is None:
        raise RuntimeError("Flask is not installed. Run: pip install flask")
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template_string(_FLASK_INDEX)

    @app.get("/api/health")
    def api_health():
        return jsonify({"ok": True, "running": is_running()})

    @app.get("/api/live")
    def api_live():
        return jsonify({"ok": True, "transcript": get_live_transcript(), "running": is_running()})

    @app.post("/api/start")
    def api_start():
        started = _start_threads()
        return jsonify({"ok": True, "running": is_running(), "started": bool(started)})

    @app.post("/api/stop")
    def api_stop():
        stopped = _stop_threads()
        return jsonify({"ok": True, "running": is_running(), "stopped": bool(stopped)})

    @app.get("/api/segments")
    def api_segments():
        try:
            now = datetime.datetime.now()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            local_conn = get_db_connection()
            c = local_conn.cursor()
            rows = c.execute(
                """
                SELECT id, start_ts, end_ts, duration_sec, audio_path, transcript, summary, keywords, important, speakers
                FROM segments
                WHERE start_ts >= ?
                ORDER BY start_ts DESC
                """,
                (start_of_day.isoformat(),)
            ).fetchall()
            local_conn.close()
            segments = []
            for r in rows:
                segments.append({
                    "id": r[0],
                    "start_ts": r[1],
                    "end_ts": r[2],
                    "duration_sec": r[3] or 0,
                    "audio_path": r[4],
                    "transcript": r[5] or "",
                    "summary": r[6] or "",
                    "keywords": r[7] or "",
                    "important": r[8] or 0,
                    "speakers": r[9] or ""
                })
            return jsonify({"ok": True, "segments": segments})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/summary")
    def api_summary():
        # Summarize today's transcripts only
        try:
            now = datetime.datetime.now()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            local_conn = get_db_connection()
            c = local_conn.cursor()
            rows = c.execute(
                """
                SELECT COALESCE(NULLIF(transcript,''), NULLIF(summary,'')) AS txt
                FROM segments
                WHERE start_ts >= ?
                ORDER BY start_ts ASC
                """,
                (start_of_day.isoformat(),)
            ).fetchall()
            local_conn.close()
            texts = [(r[0] or "").strip() for r in rows if (r and r[0])]
            combined = "\n".join(texts)
            summary = _compose_hour_summary(combined) if combined else "(no transcripts today)"
            return jsonify({"ok": True, "summary": summary, "count": len(texts)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    app.config["HOST"] = host
    app.config["PORT"] = int(port)
    return app

# ----------------- workers -----------------
def denoise_audio(wav_path: str) -> np.ndarray:
    """Load audio and apply noise reduction if available."""
    import scipy.io.wavfile as wavfile
    sr, audio = wavfile.read(wav_path)
    audio = audio.astype(np.float32) / 32768.0
    if USE_NOISEREDUCE and nr:
        print("[nr] applying noise reduction...")
        audio = nr.reduce_noise(y=audio, sr=sr, prop_decrease=0.8)
    return audio

def diarize_audio(wav_path: str, transcript_segments) -> str:
    """Run speaker diarization and merge with transcript."""
    if not USE_DIARIZATION or not diarization_pipeline:
        return ""
    try:
        print("[diarization] identifying speakers...")
        diarization = diarization_pipeline(wav_path)
        # Build speaker timeline
        speaker_turns = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            speaker_turns.append((turn.start, turn.end, speaker))
        if not speaker_turns:
            return ""
        # Match transcript segments to speakers
        result = []
        for seg in transcript_segments:
            seg_mid = (seg.start + seg.end) / 2
            speaker = "?"
            for start, end, spk in speaker_turns:
                if start <= seg_mid <= end:
                    speaker = spk
                    break
            result.append(f"[{speaker}] {seg.text.strip()}")
        return "\n".join(result)
    except Exception as e:
        print(f"[diarization] error: {e}")
        return ""

def process_worker():
    local_conn = get_db_connection(check_same_thread=False)
    print(f"[db] worker connected to {DB_PATH} (thread={threading.get_ident()})")
    local_cur = local_conn.cursor()
    while not stop_flag.is_set():
        try:
            item = proc_q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            wav_path, seg_start_ts_iso, seg_end_ts_iso, duration = item
            print("[asr] transcribing...", wav_path)
            
            # Denoise audio
            audio = denoise_audio(wav_path)
            
            # Transcribe
            segments_list, _ = asr_model.transcribe(audio, beam_size=BEAM_SIZE, vad_filter=False, language="en")
            segments_list = list(segments_list)  # materialize generator
            txt = " ".join(s.text.strip() for s in segments_list).strip()
            
            # Diarization
            speakers_txt = diarize_audio(wav_path, segments_list)
            
            if not txt:
                print("[asr] transcription empty for", wav_path)
            else:
                preview = (txt[:160] + "…") if len(txt) > 160 else txt
                print("[asr] transcript:", preview)
            summary = llm_summarize(txt) if USE_LLM else simple_summarize(txt)
            keywords = ",".join(extract_keywords(txt))
            import sqlite3 as _sqlite3
            attempts = 0
            while True:
                try:
                    local_cur.execute(
                        "INSERT INTO segments(start_ts,end_ts,duration_sec,audio_path,transcript,summary,keywords,speakers) VALUES (?,?,?,?,?,?,?,?)",
                        (seg_start_ts_iso, seg_end_ts_iso, duration, wav_path, txt, summary, keywords, speakers_txt)
                    )
                    local_conn.commit()
                    break
                except _sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() and attempts < 5:
                        attempts += 1
                        backoff = 0.1 * (2 ** attempts)
                        print(f"[db] locked; retrying in {backoff:.2f}s (attempt {attempts})")
                        time.sleep(backoff)
                        continue
                    raise
        except Exception as e:
            print("[proc] error:", e)
        finally:
            proc_q.task_done()
    local_conn.close()

def hourly_worker():
    local_conn = get_db_connection(check_same_thread=False)
    local_cur = local_conn.cursor()
    try:
        while not stop_flag.is_set():
            now_aligned = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
            for i in range(6, 0, -1):
                h0 = now_aligned - datetime.timedelta(hours=i)
                exists = local_cur.execute(
                    "SELECT 1 FROM hourly_digests WHERE hour_start = ?",
                    (h0.isoformat(),),
                ).fetchone()
                if exists:
                    continue
                texts, h1 = _fetch_texts_for_hour(local_conn, h0)
                if not texts:
                    _upsert_hourly(local_conn, h0, h1, "")
                    print(f"[hourly] {h0:%Y-%m-%d %H}: no content")
                else:
                    combined = "\n".join(texts)
                    summary = _upsert_hourly(local_conn, h0, h1, combined)
                    preview = (summary[:140] + "…") if len(summary) > 140 else summary
                    print(f"[hourly] {h0:%Y-%m-%d %H} summary → {preview}")
            for _ in range(60):
                if stop_flag.is_set():
                    break
                time.sleep(5)
    finally:
        local_conn.close()

def audio_callback(indata, frames, time_info, status):
    if status:
        pass
    audio_q.put(bytes(indata))

def record_loop():
    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=frame_len,
                           dtype='int16', channels=1, callback=audio_callback):
        while not stop_flag.is_set():
            time.sleep(0.05)

def segmenter_loop():
    buffer = bytes()
    active = False
    last_voice_ts = time.time()
    seg_start_ts = datetime.datetime.now()
    seg_end_ts = seg_start_ts
    last_live_transcribe = 0  # timestamp of last live transcription

    def flush_segment():
        nonlocal buffer, active, seg_start_ts, seg_end_ts
        set_live_transcript("")  # clear live transcript on flush
        if not buffer:
            seg_start_ts = datetime.datetime.now()
            return
        ts_str = seg_start_ts.isoformat(timespec="seconds").replace(":","-")
        wav_path = os.path.join(AUDIO_DIR, f"seg_{ts_str}.wav")
        with wave.open(wav_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(buffer)
        duration = len(buffer) / 2 / SAMPLE_RATE
        print(f"[segment] saved {wav_path} ({duration:.1f}s)")
        proc_q.put((wav_path, seg_start_ts.isoformat(), seg_end_ts.isoformat(), duration))
        buffer = bytes()
        active = False
        seg_start_ts = datetime.datetime.now()

    def do_live_transcribe():
        nonlocal last_live_transcribe
        if not buffer or len(buffer) < SAMPLE_RATE * 2:  # need at least 1 sec
            return
        now = time.time()
        if now - last_live_transcribe < 2:  # throttle to every 2 sec
            return
        last_live_transcribe = now
        try:
            audio_np = np.frombuffer(buffer, dtype=np.int16).astype(np.float32) / 32768.0
            segs, _ = asr_model.transcribe(audio_np, beam_size=1, vad_filter=False, language="en")
            txt = " ".join(s.text.strip() for s in segs).strip()
            set_live_transcript(txt)
        except Exception as e:
            print(f"[live] transcribe error: {e}")

    while not stop_flag.is_set():
        try:
            chunk = audio_q.get(timeout=0.2)
        except queue.Empty:
            chunk = None

        now = time.time()
        if not seg_end_ts:
            seg_end_ts = seg_start_ts
        if chunk is not None:
            for i in range(0, len(chunk), frame_len*2):
                frame = chunk[i:i+frame_len*2]
                if len(frame) < frame_len*2:
                    continue
                is_speech = vad.is_speech(frame, SAMPLE_RATE)
                if is_speech:
                    if not active:
                        print("[vad] speech detected — recording segment…")
                    last_voice_ts = now
                    active = True
                    seg_end_ts = datetime.datetime.now()
                # Buffer ALL frames once active (keeps silence between words)
                if active:
                    buffer += frame

        # Live transcription while recording
        if active:
            do_live_transcribe()

        window_elapsed = (datetime.datetime.now() - seg_start_ts).total_seconds()
        if window_elapsed >= MAX_SEGMENT_SEC:
            print(f"[rollover] {window_elapsed:.1f}s ≥ {MAX_SEGMENT_SEC}s — rolling segment…")
            flush_segment()

        if active and (now - last_voice_ts) >= OFF_TIME_SEC:
            print(f"[vad] silence {now - last_voice_ts:.1f}s ≥ {OFF_TIME_SEC}s — flushing segment…")
            flush_segment()
            seg_start_ts = datetime.datetime.now()

    if buffer:
        print("[segment] flushing final segment...")
        seg_start_ts = datetime.datetime.now()
        flush_segment()

# ----------------- entrypoint -----------------
def main():
    parser = argparse.ArgumentParser(description="Audio journal recorder with hourly summaries")
    parser.add_argument("--only-hourly", action="store_true", help="Run only the hourly summarization worker (no audio capture/ASR)")
    parser.add_argument("--flask-ui", action="store_true", help="Run a simple Flask UI instead of auto-starting the recorder")
    parser.add_argument("--ui-host", default=os.getenv("RECORDER_UI_HOST", "127.0.0.1"), help="Flask UI host (default 127.0.0.1)")
    parser.add_argument("--ui-port", default=int(os.getenv("RECORDER_UI_PORT", "5000")), type=int, help="Flask UI port (default 5000)")
    args = parser.parse_args()

    if args.only_hourly:
        print("Hourly-only mode. Ctrl+C to stop.")
        try:
            hourly_worker()
        except KeyboardInterrupt:
            print("Stopping hourly-only mode…")
            stop_flag.set()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return

    if args.flask_ui:
        if Flask is None:
            print("Flask is not installed. Run: pip install flask", file=sys.stderr)
            return
        app = create_flask_app(host=args.ui_host, port=args.ui_port)
        host = app.config["HOST"]
        port = app.config["PORT"]
        # Find a free port if the requested one is busy.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sock.connect_ex((host, port)) == 0:
                print(f"[flask] Port {port} in use, trying {port+1}", file=sys.stderr)
                port += 1
        finally:
            sock.close()
        print(f"Open http://{host}:{port} in your browser.")
        # Do not auto-start recorder here; use the UI buttons.
        app.run(host=host, port=port, debug=False)
        return

    # Default: start recorder immediately (no UI)
    print("Starting forever-listen. Ctrl+C to stop.")
    print(f"[config] SAMPLE_RATE={SAMPLE_RATE}, FRAME_MS={FRAME_MS}, OFF_TIME_SEC={OFF_TIME_SEC}, MAX_SEGMENT_SEC={MAX_SEGMENT_SEC}, VAD_AGGRESSIVENESS={VAD_AGGRESSIVENESS}")
    _start_threads()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping…")
        _stop_threads()
        conn.close()

if __name__ == "__main__":
    main()