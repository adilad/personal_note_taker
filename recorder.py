import os, queue, sys, time, wave, json, datetime, sqlite3, threading
import numpy as np
import sounddevice as sd
import webrtcvad

# Optional imports (guarded)
USE_LLM = False
try:
    from llama_cpp import Llama  # requires GGUF model file
    USE_LLM = False  # set True after you configure your model path below
except Exception:
    pass

# ASR
from faster_whisper import WhisperModel

APP_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(APP_DIR, "audio")
DB_PATH = os.path.join(APP_DIR, "journal.db")
MODEL_SIZE = "small"  # "tiny","base","small","medium","large-v2" (download on first run)
VAD_AGGRESSIVENESS = 2  # 0-3 (3=more aggressive)
SAMPLE_RATE = 16000
FRAME_MS = 30  # 10, 20, or 30 ms for webrtcvad
OFF_TIME_SEC = 20  # silence to close segment

OFF_TIME_SEC = int(os.getenv("OFF_TIME_SEC", OFF_TIME_SEC))
MAX_SEGMENT_SEC = int(os.getenv("MAX_SEGMENT_SEC", "60"))  # fixed length segments in seconds

os.makedirs(AUDIO_DIR, exist_ok=True)

# --- DB setup ---
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  start_ts TEXT,
  end_ts TEXT,
  duration_sec REAL,
  audio_path TEXT,
  transcript TEXT,
  summary TEXT,
  keywords TEXT,
  important INTEGER DEFAULT 0
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS daily_digests (
  id INTEGER PRIMARY KEY,
  date TEXT UNIQUE,
  summary TEXT
)
""")
cur.execute("PRAGMA journal_mode=WAL;")
conn.commit()

# --- ASR model (local) ---
asr_model = WhisperModel(MODEL_SIZE, device="auto", compute_type="int8")  # CPU-friendly

# --- Optional local LLM ---
llm = None
LLM_MODEL_PATH = os.path.expanduser("~/models/llama-3-8b-instruct.Q4_K_M.gguf")
if USE_LLM and os.path.exists(LLM_MODEL_PATH):
    llm = Llama(model_path=LLM_MODEL_PATH, n_ctx=4096, n_threads=8)

# --- Simple extractive summarizer (fallback) ---
def simple_summarize(text, max_sentences=4):
    # ultra-light extractive: pick diverse top sentences by length & basic scoring
    sents = [s.strip() for s in text.replace("\n"," ").split(".") if s.strip()]
    if not sents:
        return ""
    scores = [(len(s), i, s) for i,s in enumerate(sents)]  # length proxy
    scores.sort(reverse=True)  # longest first
    chosen = []
    used_idxs = set()
    for _, i, s in scores:
        if len(chosen) >= max_sentences: break
        # avoid near-duplicates
        if any(s.lower()[:40] in c.lower() or c.lower()[:40] in s.lower() for c in chosen): 
            continue
        chosen.append(s)
        used_idxs.add(i)
    return ". ".join(chosen) + "."

def llm_summarize(text):
    if not llm:
        return simple_summarize(text)
    prompt = f"""You are a meticulous note-taker.
Summarize the following transcript into concise bullet points with timestamps, action items (who/what/when), decisions, and key takeaways.
Keep it faithful to the text, no speculation.

Transcript:
{text}"""
    out = llm(prompt, max_tokens=512, temperature=0.1, stop=["</s>"])
    return out["choices"][0]["text"].strip()

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

def process_worker():
    local_conn = sqlite3.connect(DB_PATH)
    local_cur = local_conn.cursor()
    while not stop_flag.is_set():
        try:
            item = proc_q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            wav_path, seg_start_ts_iso, seg_end_ts_iso, duration = item
            print("[asr] transcribing...", wav_path)
            segments, _ = asr_model.transcribe(wav_path, beam_size=1, vad_filter=False, language="en")
            txt = " ".join(s.text.strip() for s in segments).strip()
            if not txt:
                print("[asr] transcription empty for", wav_path)
            else:
                preview = (txt[:160] + "…") if len(txt) > 160 else txt
                print("[asr] transcript:", preview)
            summary = llm_summarize(txt) if USE_LLM else simple_summarize(txt)
            keywords = ",".join(extract_keywords(txt))
            local_cur.execute(
                "INSERT INTO segments(start_ts,end_ts,duration_sec,audio_path,transcript,summary,keywords) VALUES (?,?,?,?,?,?,?)",
                (seg_start_ts_iso, seg_end_ts_iso, duration, wav_path, txt, summary, keywords)
            )
            local_conn.commit()
        except Exception as e:
            print("[proc] error:", e)
        finally:
            proc_q.task_done()
    local_conn.close()

def audio_callback(indata, frames, time_info, status):
    if status:  # overflow/underflow info
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

    def flush_segment():
        nonlocal buffer, active, seg_start_ts, seg_end_ts
        if not buffer:
            # even if no speech, roll the window forward
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

    while not stop_flag.is_set():
        try:
            chunk = audio_q.get(timeout=0.2)
        except queue.Empty:
            chunk = None

        now = time.time()
        if not seg_end_ts:
            seg_end_ts = seg_start_ts
        if chunk is not None:
            # split into FRAME_MS frames
            for i in range(0, len(chunk), frame_len*2):  # *2 bytes per int16
                frame = chunk[i:i+frame_len*2]
                if len(frame) < frame_len*2:
                    continue
                is_speech = vad.is_speech(frame, SAMPLE_RATE)
                if is_speech:
                    if not active:
                        print("[vad] speech detected — recording segment…")
                    buffer += frame
                    last_voice_ts = now
                    active = True
                    seg_end_ts = datetime.datetime.now()
                else:
                    # pad a tiny bit to keep natural pauses
                    pass

        # Fixed-length rollover: if the current segment window reached MAX_SEGMENT_SEC, flush regardless of silence
        window_elapsed = (datetime.datetime.now() - seg_start_ts).total_seconds()
        if window_elapsed >= MAX_SEGMENT_SEC:
            print(f"[rollover] {window_elapsed:.1f}s ≥ {MAX_SEGMENT_SEC}s — rolling segment…")
            flush_segment()

        if active and (now - last_voice_ts) >= OFF_TIME_SEC:
            print(f"[vad] silence {now - last_voice_ts:.1f}s ≥ {OFF_TIME_SEC}s — flushing segment…")
            flush_segment()
            seg_start_ts = datetime.datetime.now()

    # flush on exit
    if buffer:
        print("[segment] flushing final segment...")
        seg_start_ts = datetime.datetime.now()
        flush_segment()

def main():
    print("Starting forever-listen. Ctrl+C to stop.")
    print(f"[config] SAMPLE_RATE={SAMPLE_RATE}, FRAME_MS={FRAME_MS}, OFF_TIME_SEC={OFF_TIME_SEC}, MAX_SEGMENT_SEC={MAX_SEGMENT_SEC}, VAD_AGGRESSIVENESS={VAD_AGGRESSIVENESS}")
    t_rec = threading.Thread(target=record_loop, daemon=True)
    t_seg = threading.Thread(target=segmenter_loop, daemon=True)
    t_proc = threading.Thread(target=process_worker, daemon=True)
    t_rec.start()
    t_seg.start()
    t_proc.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping...")
        stop_flag.set()
        t_rec.join(timeout=2)
        t_seg.join(timeout=2)
        t_proc.join(timeout=2)
        conn.close()

if __name__ == "__main__":
    main()