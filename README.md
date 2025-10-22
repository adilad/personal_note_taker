# 🎙️ Audio Recorder

An intelligent audio recording application with automatic transcription, summarization, and a modern web UI.

## ✨ Features

### Core Recording
- **Continuous Audio Capture** - Records audio segments with voice activity detection (VAD)
- **Automatic Transcription** - Uses Faster Whisper for accurate speech-to-text
- **Smart Segmentation** - Automatically splits recordings based on silence detection
- **Configurable Settings** - Adjust silence thresholds, segment lengths, and VAD sensitivity

### Intelligence
- **AI Summarization** - Optional LLM-based summarization (llama.cpp) with fallback extractive summarizer
- **Keyword Extraction** - Automatic keyword detection using YAKE
- **Hourly Digests** - Automatic hourly summaries of all recordings
- **Daily Summaries** - Generate comprehensive daily summaries on demand

### Modern Web UI
- **Real-time Status** - Live recording indicator with visual feedback
- **Segment Browser** - View all recorded segments with search functionality
- **Statistics Dashboard** - Track segments, duration, and word count
- **Responsive Design** - Works on desktop, tablet, and mobile
- **Dark Mode** - Automatic dark mode support
- **Export Functionality** - Export data as JSON

## 🚀 Quick Start

### Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd recorder

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install flask faster-whisper sounddevice webrtcvad numpy yake llama-cpp-python
```

### Running the Application

#### Web UI Mode (Recommended)
```bash
python recorder.py --flask-ui
```
Then open http://127.0.0.1:5000 in your browser.

#### Auto-start Recording Mode
```bash
python recorder.py
```
Starts recording immediately without UI.

#### Hourly Digest Only Mode
```bash
python recorder.py --only-hourly
```
Runs only the hourly summarization worker.

## 🎨 UI Features

### Main Dashboard
- **Status Bar** - Shows recording status with animated indicator
- **Control Buttons**
  - ▶️ Start Recording - Begin audio capture
  - ⏹️ Stop Recording - Stop audio capture
  - 🔄 Refresh - Reload segments and status
  - 📊 Daily Summary - Generate today's summary
  - 💾 Export Data - Download segments as JSON

### Statistics Cards
- **Segments Today** - Count of recorded segments
- **Total Duration** - Total recording time in minutes
- **Words Transcribed** - Total word count

### Segment Browser
- **Search** - Filter segments by transcript content
- **Recent Segments** - View latest recordings with timestamps
- **Segment Details** - Click any segment to view full transcript, summary, and keywords

### Daily Summary
- Real-time summary generation
- Combines all transcripts from today
- Uses AI or extractive summarization

## ⚙️ Configuration

### Environment Variables

```bash
# Recording settings
export OFF_TIME_SEC=20              # Silence duration to close segment (seconds)
export MAX_SEGMENT_SEC=60           # Maximum segment length (seconds)

# LLM settings (optional)
export USE_LLM=1                    # Enable LLM summarization (1=on, 0=off)
export LLM_MODEL_PATH=~/models/llama-3-8b-instruct.Q4_K_M.gguf
export LLM_N_CTX=4096              # Context window size
export LLM_N_THREADS=8             # CPU threads for inference

# UI settings
export RECORDER_UI_HOST=127.0.0.1  # Flask host
export RECORDER_UI_PORT=5000       # Flask port
```

### Audio Settings (in code)

```python
SAMPLE_RATE = 16000           # Audio sample rate (Hz)
FRAME_MS = 30                 # VAD frame size (10, 20, or 30 ms)
VAD_AGGRESSIVENESS = 2        # VAD sensitivity (0-3, higher = more aggressive)
MODEL_SIZE = "small"          # Whisper model: tiny, base, small, medium, large-v2
```

## 📊 Database Schema

### segments table
- `id` - Primary key
- `start_ts` - Segment start timestamp (ISO format)
- `end_ts` - Segment end timestamp
- `duration_sec` - Duration in seconds
- `audio_path` - Path to WAV file
- `transcript` - Full transcript text
- `summary` - AI-generated summary
- `keywords` - Comma-separated keywords
- `important` - Flag for important segments (0/1)

### hourly_digests table
- `id` - Primary key
- `hour_start` - Hour start timestamp
- `hour_end` - Hour end timestamp
- `summary` - Hourly summary text
- `created_at` - Creation timestamp

### daily_digests table
- `id` - Primary key
- `date` - Date (YYYY-MM-DD)
- `summary` - Daily summary text

## 🔧 API Endpoints

### GET /api/health
Returns recording status.

**Response:**
```json
{
  "ok": true,
  "running": false
}
```

### POST /api/start
Start recording.

**Response:**
```json
{
  "ok": true,
  "running": true,
  "started": true
}
```

### POST /api/stop
Stop recording.

**Response:**
```json
{
  "ok": true,
  "running": false,
  "stopped": true
}
```

### GET /api/segments
Get today's segments (up to 100).

**Response:**
```json
{
  "ok": true,
  "segments": [
    {
      "id": 1,
      "start_ts": "2025-10-14T15:30:00",
      "end_ts": "2025-10-14T15:32:00",
      "duration_sec": 120.5,
      "audio_path": "/path/to/audio.wav",
      "transcript": "Full transcript text...",
      "summary": "Summary of the segment...",
      "keywords": "keyword1,keyword2,keyword3",
      "important": 0
    }
  ]
}
```

### GET /api/summary
Generate daily summary.

**Response:**
```json
{
  "ok": true,
  "summary": "Daily summary text...",
  "count": 15
}
```

## 🎯 Use Cases

- **Meeting Notes** - Automatic transcription and summarization of meetings
- **Voice Journaling** - Record daily thoughts with automatic organization
- **Interview Recording** - Capture and transcribe interviews
- **Lecture Notes** - Record and summarize lectures or presentations
- **Podcast Production** - Transcribe podcast episodes for show notes

## 🐛 Troubleshooting

### Port Already in Use
The app automatically tries the next port if 5000 is busy. Check the console output for the actual port.

### No Audio Detected
- Check microphone permissions
- Verify microphone is selected as default input device
- Adjust `VAD_AGGRESSIVENESS` (lower = more sensitive)

### Transcription Errors
- Ensure `faster-whisper` is properly installed
- Try a different `MODEL_SIZE` (larger = more accurate but slower)
- Check audio quality and microphone placement

### Database Locked Errors
The app uses WAL mode and automatic retry logic. If issues persist:
```bash
# Close all connections and reset
rm journal.db-wal journal.db-shm
```

## 📝 Development

### Project Structure
```
recorder/
├── recorder.py          # Main application
├── journal.db          # SQLite database
├── audio/              # Recorded audio segments
├── venv/               # Virtual environment
└── README.md           # This file
```

### Adding Features
The codebase is modular:
- **Recording**: `record_loop()`, `segmenter_loop()`
- **Processing**: `process_worker()`, `hourly_worker()`
- **UI**: `create_flask_app()`, `_FLASK_INDEX`
- **Database**: `init_schema()`, `get_db_connection()`

## 📄 License

MIT License - feel free to use and modify as needed.

## 🙏 Acknowledgments

- [Faster Whisper](https://github.com/guillaumekln/faster-whisper) - Fast ASR
- [WebRTC VAD](https://github.com/wiseman/py-webrtcvad) - Voice activity detection
- [llama.cpp](https://github.com/ggerganov/llama.cpp) - Local LLM inference
- [YAKE](https://github.com/LIAAD/yake) - Keyword extraction
- [Flask](https://flask.palletsprojects.com/) - Web framework
