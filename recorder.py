import os, queue, sys, time, wave, json, datetime, sqlite3, threading, argparse, socket
import numpy as np
import sounddevice as sd
import webrtcvad

# --- Load .env file if present ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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

# --- LiteLLM setup (preferred) or fallback to local llama.cpp ---
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "https://litellm.marqeta.com")
LITELLM_MODEL_ID = os.getenv("LITELLM_MODEL_ID", "openai/gpt-4o-mini")
LITELLM_TEMPERATURE = float(os.getenv("LITELLM_TEMPERATURE", "0.3"))
LITELLM_MAX_TOKENS = int(os.getenv("MODEL_MAX_TOKENS", "2000"))

USE_LITELLM = bool(LITELLM_API_KEY)
if USE_LITELLM:
    try:
        import requests
        print(f"[llm] Using LiteLLM: {LITELLM_BASE_URL} model={LITELLM_MODEL_ID}")
    except ImportError:
        USE_LITELLM = False
        print("[llm] requests not installed for LiteLLM")

# --- Fallback: local llama.cpp ---
USE_LOCAL_LLM = False
llm = None
if not USE_LITELLM:
    LLM_MODEL_PATH = os.path.expanduser(os.getenv("LLM_MODEL_PATH", "~/models/llama-3-8b-instruct.Q4_K_M.gguf"))
    try:
        from llama_cpp import Llama
        if os.path.exists(LLM_MODEL_PATH):
            llm = Llama(model_path=LLM_MODEL_PATH, n_ctx=4096, n_threads=8)
            USE_LOCAL_LLM = True
            print(f"[llm] Loaded local model: {LLM_MODEL_PATH}")
        else:
            print(f"[llm] No LLM configured; using fallback summarizer.")
    except Exception as e:
        print(f"[llm] No LLM available: {e}")

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
          speakers TEXT,
          participants TEXT,
          category TEXT,
          action_items TEXT,
          questions TEXT,
          sentiment TEXT
        )
        """
    )
    # Migration: add columns if missing
    cur.execute("PRAGMA table_info(segments)")
    cols = [r[1] for r in cur.fetchall()]
    for col in ["speakers", "participants", "category", "action_items", "questions", "sentiment"]:
        if col not in cols:
            cur.execute(f"ALTER TABLE segments ADD COLUMN {col} TEXT")
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

def llm_summarize(text, is_daily=False):
    if not text.strip():
        return ""
    
    if is_daily:
        prompt = f"""Create comprehensive meeting notes from this day's voice recordings.

The recordings are timestamped like [HH:MM AM/PM].

Structure your notes as:

## 📝 Detailed Notes
Go through each recording chronologically and capture ALL important details:
- What was discussed
- Who said what (if identifiable)
- Specific numbers, dates, names mentioned
- Context and background information
- Problems raised and solutions proposed
- Questions asked and answers given

Be thorough - don't skip anything important.

## 📊 Summary
1-2 paragraph overview of the day.

## 🎯 Key Topics
- Main subjects discussed (bullet points)

## ✅ Decisions Made
- Any decisions or conclusions reached

## ⚡ Action Items
- Person: Task (Due date if mentioned)

## 💬 Notable Quotes
- Important statements worth remembering

## ❓ Open Questions
- Unanswered questions that need follow-up

---
Recordings:
{text}"""
    else:
        prompt = f"""Summarize this transcript concisely in 2-4 bullet points.
Focus on: main topic, key points, any action items or decisions.
Be brief and direct.

Transcript:
{text}"""
    
    # Try LiteLLM first
    if USE_LITELLM:
        try:
            import requests
            resp = requests.post(
                f"{LITELLM_BASE_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": LITELLM_MODEL_ID,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": LITELLM_TEMPERATURE,
                    "max_tokens": 8000 if is_daily else LITELLM_MAX_TOKENS
                },
                timeout=120
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            else:
                print(f"[llm] LiteLLM error: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"[llm] LiteLLM request failed: {e}")
    
    # Fallback to local llama.cpp
    if USE_LOCAL_LLM and llm:
        try:
            out = llm(prompt, max_tokens=512, temperature=0.1, stop=["</s>"])
            return out["choices"][0]["text"].strip()
        except Exception as e:
            print(f"[llm] Local LLM error: {e}")
    
    # Final fallback: extractive summarizer
    return simple_summarize(text)

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
    return llm_summarize(text)

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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Voice</title>
  <style>
    :root {
      --bg: #0a0a0a; --surface: #141414; --surface2: #1c1c1c;
      --border: #262626; --text: #fafafa; --text2: #a1a1a1;
      --accent: #3b82f6; --red: #ef4444; --green: #22c55e;
    }
    .light {
      --bg: #ffffff; --surface: #f9fafb; --surface2: #f3f4f6;
      --border: #e5e7eb; --text: #111827; --text2: #6b7280;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Inter', -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; min-height: 100vh; transition: background 0.3s, color 0.3s; }
    .app { max-width: 720px; margin: 0 auto; padding: 3rem 1.5rem; }
    header { text-align: center; margin-bottom: 3rem; position: relative; }
    h1 { font-size: 1.25rem; font-weight: 500; letter-spacing: -0.02em; color: var(--text2); }
    .theme-btn { position: absolute; top: 0; right: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; width: 36px; height: 36px; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 1rem; transition: all 0.2s; }
    .theme-btn:hover { border-color: var(--text2); }
    .rec-btn { width: 80px; height: 80px; border-radius: 50%; border: 2px solid var(--border); background: var(--surface); cursor: pointer; margin: 2rem auto; display: flex; align-items: center; justify-content: center; transition: all 0.3s ease; }
    .rec-btn:hover { border-color: var(--text2); transform: scale(1.05); }
    .rec-btn.on { border-color: var(--red); background: rgba(239,68,68,0.1); }
    .rec-btn .inner { width: 24px; height: 24px; border-radius: 50%; background: var(--text2); transition: all 0.3s ease; }
    .rec-btn.on .inner { background: var(--red); border-radius: 4px; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
    .status { text-align: center; font-size: 0.8rem; color: var(--text2); text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 2rem; }
    .live { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: none; }
    .live.on { display: block; }
    .live-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--red); margin-bottom: 0.5rem; display: flex; align-items: center; gap: 0.5rem; }
    .live-label::before { content: ''; width: 6px; height: 6px; background: var(--red); border-radius: 50%; animation: pulse 1.5s infinite; }
    .live-text { font-size: 0.95rem; color: var(--text); min-height: 1.5rem; }
    .actions { display: flex; justify-content: center; gap: 0.5rem; margin-bottom: 3rem; }
    .btn { padding: 0.5rem 1rem; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--text2); font-size: 0.8rem; cursor: pointer; transition: all 0.2s; }
    .btn:hover { border-color: var(--text2); color: var(--text); }
    .section { margin-bottom: 2rem; }
    .section-title { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text2); margin-bottom: 1rem; }
    .search { width: 100%; padding: 0.75rem 1rem; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--text); font-size: 0.9rem; margin-bottom: 1rem; }
    .search:focus { outline: none; border-color: var(--accent); }
    .search::placeholder { color: var(--text2); }
    .segments { display: flex; flex-direction: column; gap: 0.5rem; }
    .seg { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 1rem; cursor: pointer; transition: all 0.2s; }
    .seg:hover { border-color: var(--text2); background: var(--surface2); }
    .seg-time { font-size: 0.75rem; color: var(--text2); margin-bottom: 0.25rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
    .seg-text { font-size: 0.9rem; color: var(--text); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .cat { background: var(--accent); color: white; padding: 0.1rem 0.4rem; border-radius: 4px; font-size: 0.65rem; text-transform: uppercase; }
    .empty { text-align: center; padding: 3rem; color: var(--text2); font-size: 0.9rem; }
    .summary-box { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 1.25rem; font-size: 0.9rem; color: var(--text); white-space: pre-wrap; max-height: 300px; overflow-y: auto; }
    .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.8); backdrop-filter: blur(4px); z-index: 100; align-items: center; justify-content: center; padding: 1rem; }
    .modal.on { display: flex; }
    .modal-box { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; width: 100%; max-width: 500px; max-height: 80vh; overflow-y: auto; }
    .modal-head { padding: 1.25rem; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
    .modal-head h2 { font-size: 0.9rem; font-weight: 500; }
    .modal-close { background: none; border: none; color: var(--text2); font-size: 1.25rem; cursor: pointer; padding: 0.25rem; }
    .modal-close:hover { color: var(--text); }
    .modal-body { padding: 1.25rem; }
    .modal-body p { font-size: 0.8rem; color: var(--text2); margin-bottom: 0.5rem; }
    .modal-body .content { background: var(--surface2); border-radius: 8px; padding: 1rem; font-size: 0.9rem; color: var(--text); margin-bottom: 1rem; white-space: pre-wrap; max-height: 200px; overflow-y: auto; }
    .modal-body .label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text2); margin: 1rem 0 0.5rem; }
    .keywords { display: flex; flex-wrap: wrap; gap: 0.5rem; }
    .kw { background: var(--surface2); border-radius: 4px; padding: 0.25rem 0.5rem; font-size: 0.75rem; color: var(--text2); }
    .collapsible { cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
    .collapsible::after { content: '▼'; font-size: 0.7rem; transition: transform 0.2s; }
    .collapsible.collapsed::after { transform: rotate(-90deg); }
    .collapse-content { max-height: 400px; overflow-y: auto; transition: max-height 0.3s; }
    .collapse-content.collapsed { max-height: 0; overflow: hidden; }
  </style>
</head>
<body>
  <div class="app">
    <header><h1>Voice Recorder</h1><button class="theme-btn" id="themeBtn">☀️</button></header>
    <button class="rec-btn" id="recBtn"><div class="inner"></div></button>
    <div class="status" id="status">Ready</div>
    <div class="live" id="live"><div class="live-label">Live</div><div class="live-text" id="liveText"></div></div>
    <div class="actions">
      <button class="btn" id="summaryBtn">Summary</button>
      <button class="btn" id="exportBtn">Export</button>
      <button class="btn" id="reprocessBtn">Reprocess</button>
    </div>
    <div class="section">
      <div class="section-title collapsible" id="segToggle">Recordings <span id="segCount"></span></div>
      <input type="text" class="search" id="search" placeholder="Search transcripts...">
      <div class="segments collapse-content" id="segments"><div class="empty">No recordings yet</div></div>
    </div>
    <div class="section">
      <div class="section-title collapsible" id="sumToggle">Daily Summary</div>
      <div class="summary-box collapse-content" id="summary">Click Summary to generate</div>
    </div>
  </div>
  <div class="modal" id="modal">
    <div class="modal-box">
      <div class="modal-head"><h2>Recording Details</h2><button class="modal-close" id="modalClose">&times;</button></div>
      <div class="modal-body" id="modalBody"></div>
    </div>
  </div>
<script>
let segs=[],q='',on=false;
const $=id=>document.getElementById(id);
const sentimentEmoji={positive:'😊',negative:'😟',neutral:'😐',mixed:'🤔',urgent:'🚨',frustrated:'😤',excited:'🎉',professional:'💼'};
async function api(m,p){const r=await fetch(p,{method:m});return r.json();}
async function sync(){
  const d=await api('GET','/api/health');
  on=d.running;
  $('recBtn').classList.toggle('on',on);
  $('status').textContent=on?'Recording':'Ready';
}
async function load(){
  const d=await api('GET','/api/segments');
  segs=d.segments||[];
  render();
}
function render(){
  const f=segs.filter(s=>!q||s.transcript.toLowerCase().includes(q.toLowerCase()));
  $('segCount').textContent=`(${f.length})`;
  if(!f.length){$('segments').innerHTML='<div class="empty">No recordings yet</div>';return;}
  $('segments').innerHTML=f.map(s=>`<div class="seg" data-id="${s.id}"><div class="seg-time">${new Date(s.start_ts).toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})} · ${s.duration_sec.toFixed(0)}s ${s.category?`<span class="cat">${s.category}</span>`:''} ${s.sentiment?sentimentEmoji[s.sentiment]||'':''}</div><div class="seg-text">${s.transcript||'...'}</div></div>`).join('');
}
function show(id){
  const s=segs.find(x=>x.id==id);if(!s)return;
  let html=`<p>${new Date(s.start_ts).toLocaleString()} · ${s.duration_sec.toFixed(1)}s</p>`;
  if(s.category||s.sentiment)html+=`<div class="label">Analysis</div><div class="keywords">${s.category?`<span class="kw">${s.category}</span>`:''}${s.sentiment?`<span class="kw">${sentimentEmoji[s.sentiment]||''} ${s.sentiment}</span>`:''}</div>`;
  if(s.participants)html+=`<div class="label">Participants</div><div class="keywords">${s.participants.split(',').map(p=>`<span class="kw">${p.trim()}</span>`).join('')}</div>`;
  if(s.action_items)html+=`<div class="label">⚡ Action Items</div><div class="content" style="white-space:pre-wrap">${s.action_items}</div>`;
  if(s.questions)html+=`<div class="label">❓ Open Questions</div><div class="content" style="white-space:pre-wrap">${s.questions}</div>`;
  html+=`<div class="label">Transcript</div><div class="content">${s.transcript||'(empty)'}</div>`;
  if(s.speakers)html+=`<div class="label">Speaker Attribution</div><div class="content" style="white-space:pre-wrap">${s.speakers}</div>`;
  html+=`<div class="label">Summary</div><div class="content">${s.summary||'(none)'}</div>`;
  if(s.keywords)html+=`<div class="label">Keywords</div><div class="keywords">${s.keywords.split(',').map(k=>`<span class="kw">${k.trim()}</span>`).join('')}</div>`;
  $('modalBody').innerHTML=html;
  $('modal').classList.add('on');
}
async function live(){
  if(!on){$('live').classList.remove('on');return;}
  const d=await api('GET','/api/live');
  $('live').classList.add('on');
  $('liveText').textContent=d.transcript||'Listening...';
}
$('recBtn').onclick=async()=>{await api('POST',on?'/api/stop':'/api/start');sync();};
$('summaryBtn').onclick=async()=>{$('summary').textContent='Generating...';$('summary').classList.remove('collapsed');$('sumToggle').classList.remove('collapsed');const d=await api('GET','/api/summary');$('summary').textContent=d.summary||'(no data)';};
$('exportBtn').onclick=async()=>{const d=await api('GET','/api/segments');const b=new Blob([JSON.stringify(d.segments,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=`voice-${new Date().toISOString().slice(0,10)}.json`;a.click();};
$('reprocessBtn').onclick=async()=>{$('reprocessBtn').textContent='Processing...';const d=await api('POST','/api/reprocess');$('reprocessBtn').textContent=`Reprocess (${d.queued||0} queued)`;setTimeout(()=>{$('reprocessBtn').textContent='Reprocess';load();},3000);};
$('search').oninput=e=>{q=e.target.value;render();};
$('segments').onclick=e=>{const s=e.target.closest('.seg');if(s)show(s.dataset.id);};
$('modal').onclick=e=>{if(e.target.id==='modal')$('modal').classList.remove('on');};
$('modalClose').onclick=()=>$('modal').classList.remove('on');
// Collapsible sections
$('segToggle').onclick=()=>{$('segToggle').classList.toggle('collapsed');$('segments').classList.toggle('collapsed');};
$('sumToggle').onclick=()=>{$('sumToggle').classList.toggle('collapsed');$('summary').classList.toggle('collapsed');};
// Theme toggle
let dark=localStorage.getItem('theme')!=='light';
function setTheme(){document.body.classList.toggle('light',!dark);$('themeBtn').textContent=dark?'☀️':'🌙';localStorage.setItem('theme',dark?'dark':'light');}
setTheme();
$('themeBtn').onclick=()=>{dark=!dark;setTheme();};
sync();load();setInterval(sync,3000);setInterval(load,5000);setInterval(live,1000);
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

    @app.post("/api/reprocess")
    def api_reprocess():
        """Find and queue unprocessed audio files."""
        try:
            local_conn = get_db_connection()
            c = local_conn.cursor()
            c.execute('SELECT audio_path FROM segments')
            processed = set(r[0] for r in c.fetchall())
            local_conn.close()
            
            queued = 0
            for f in sorted(os.listdir(AUDIO_DIR)):  # Sort to process in order
                if f.endswith('.wav'):
                    path = os.path.join(AUDIO_DIR, f)
                    if path not in processed and os.path.abspath(path) not in processed:
                        # Parse timestamp from filename: seg_2026-01-21T09-58-43.wav
                        try:
                            # seg_2026-01-21T09-58-43.wav -> 2026-01-21T09:58:43
                            ts_part = f.replace('seg_', '').replace('.wav', '')  # 2026-01-21T09-58-43
                            date_part, time_part = ts_part.split('T')  # 2026-01-21, 09-58-43
                            time_part = time_part.replace('-', ':')  # 09:58:43
                            ts = datetime.datetime.fromisoformat(f"{date_part}T{time_part}")
                        except:
                            ts = datetime.datetime.now()
                        duration = os.path.getsize(path) / 2 / SAMPLE_RATE
                        proc_q.put((path, ts.isoformat(), ts.isoformat(), duration))
                        queued += 1
            return jsonify({"ok": True, "queued": queued})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

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
                SELECT id, start_ts, end_ts, duration_sec, audio_path, transcript, summary, keywords, important, speakers, participants, category, action_items, questions, sentiment
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
                    "speakers": r[9] or "",
                    "participants": r[10] or "",
                    "category": r[11] or "",
                    "action_items": r[12] or "",
                    "questions": r[13] or "",
                    "sentiment": r[14] or ""
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
                SELECT start_ts, COALESCE(NULLIF(transcript,''), NULLIF(summary,'')) AS txt
                FROM segments
                WHERE start_ts >= ?
                ORDER BY start_ts ASC
                """,
                (start_of_day.isoformat(),)
            ).fetchall()
            local_conn.close()
            texts = []
            for r in rows:
                if r and r[1] and r[1].strip():
                    ts = datetime.datetime.fromisoformat(r[0]).strftime("%I:%M %p")
                    texts.append(f"[{ts}] {r[1].strip()}")
            combined = "\n\n".join(texts)
            summary = llm_summarize(combined, is_daily=True) if combined else "(no transcripts today)"
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

def identify_speakers_with_ai(transcript: str, diarized_text: str = "") -> str:
    """Use AI to identify speakers and attribute dialogue from transcript."""
    if not transcript.strip() or not USE_LITELLM:
        return diarized_text
    
    # If we have diarized text, enhance it with names
    if diarized_text:
        prompt = f"""Analyze this conversation and identify speakers by their names if mentioned.

Diarized transcript (with generic labels):
{diarized_text}

Full transcript:
{transcript}

Replace SPEAKER_XX with actual names where identifiable. Keep original labels if unknown.
Return ONLY the reformatted transcript."""
    else:
        # No diarization - infer speakers from transcript
        prompt = f"""Analyze this transcript and identify different speakers. Look for:
- Name mentions ("Hi John", "Thanks Sarah")  
- Turn-taking patterns (questions followed by answers)
- Different speaking styles or topics

Transcript:
{transcript}

Reformat as a dialogue with speaker labels like [Speaker 1], [John], [Sarah], etc.
If only one speaker, return: [Speaker] followed by the text.
Return ONLY the formatted dialogue, nothing else."""

    try:
        import requests
        resp = requests.post(
            f"{LITELLM_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": LITELLM_MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": LITELLM_MAX_TOKENS
            },
            timeout=60
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[ai-speakers] error: {e}")
    return diarized_text or ""

def extract_participants(transcript: str) -> str:
    """Use AI to extract participant names mentioned in the transcript."""
    if not transcript.strip() or not USE_LITELLM:
        return ""
    
    prompt = f"""Extract all person names mentioned in this transcript. Return as comma-separated list.
If no names found, return empty string.

Transcript:
{transcript}

Names (comma-separated):"""

    try:
        import requests
        resp = requests.post(
            f"{LITELLM_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": LITELLM_MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 200
            },
            timeout=30
        )
        if resp.status_code == 200:
            names = resp.json()["choices"][0]["message"]["content"].strip()
            # Clean up response
            if names.lower() in ("none", "n/a", "no names", "empty", ""):
                return ""
            return names
    except Exception as e:
        print(f"[ai-participants] error: {e}")
    return ""

def analyze_segment(transcript: str) -> dict:
    """AI analysis: category, action items, questions, sentiment."""
    result = {"category": "", "action_items": "", "questions": "", "sentiment": ""}
    if not transcript.strip() or not USE_LITELLM:
        return result
    
    prompt = f"""Analyze this transcript and return a JSON object with these fields:

1. "category": One of: meeting, brainstorm, todo, personal, technical, casual, presentation, interview, other
2. "action_items": List of tasks as "[ ] Person: Task (Due: date)" format, one per line. Empty string if none.
3. "questions": Unanswered questions that need follow-up, one per line. Empty string if none.
4. "sentiment": One of: positive, negative, neutral, mixed, urgent, frustrated, excited, professional

Transcript:
{transcript}

Return ONLY valid JSON, no markdown or explanation."""

    try:
        import requests
        resp = requests.post(
            f"{LITELLM_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": LITELLM_MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1000
            },
            timeout=60
        )
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"].strip()
            # Parse JSON from response
            content = content.replace("```json", "").replace("```", "").strip()
            data = json.loads(content)
            result["category"] = data.get("category", "")
            result["action_items"] = data.get("action_items", "")
            result["questions"] = data.get("questions", "")
            result["sentiment"] = data.get("sentiment", "")
    except json.JSONDecodeError as e:
        print(f"[ai-analyze] JSON parse error: {e}")
    except Exception as e:
        print(f"[ai-analyze] error: {e}")
    return result

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
            
            # Diarization (if enabled via pyannote)
            speakers_txt = diarize_audio(wav_path, segments_list)
            
            # AI: identify/infer speakers (works with or without diarization)
            if txt and USE_LITELLM:
                print("[ai] identifying speakers...")
                speakers_txt = identify_speakers_with_ai(txt, speakers_txt)
            
            # AI: extract participant names
            participants = ""
            if txt and USE_LITELLM:
                print("[ai] extracting participants...")
                participants = extract_participants(txt)
                if participants:
                    print(f"[ai] participants: {participants}")
            
            # AI analysis: category, action items, questions, sentiment
            analysis = {"category": "", "action_items": "", "questions": "", "sentiment": ""}
            if txt and USE_LITELLM:
                print("[ai] analyzing segment...")
                analysis = analyze_segment(txt)
                if analysis["category"]:
                    print(f"[ai] category: {analysis['category']}, sentiment: {analysis['sentiment']}")
            
            if not txt:
                print("[asr] transcription empty for", wav_path, "- skipping")
                continue
            preview = (txt[:160] + "…") if len(txt) > 160 else txt
            print("[asr] transcript:", preview)
            summary = llm_summarize(txt)
            keywords = ",".join(extract_keywords(txt))
            import sqlite3 as _sqlite3
            attempts = 0
            while True:
                try:
                    local_cur.execute(
                        "INSERT INTO segments(start_ts,end_ts,duration_sec,audio_path,transcript,summary,keywords,speakers,participants,category,action_items,questions,sentiment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (seg_start_ts_iso, seg_end_ts_iso, duration, wav_path, txt, summary, keywords, speakers_txt, participants, analysis["category"], analysis["action_items"], analysis["questions"], analysis["sentiment"])
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
        
        # Start background process worker (for reprocessing)
        bg_proc = threading.Thread(target=process_worker, daemon=True, name="bg-processor")
        bg_proc.start()
        print("[bg] Background processor started for reprocessing")
        
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