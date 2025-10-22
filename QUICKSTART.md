# 🚀 Quick Start Guide

## Start the Application

```bash
cd /Users/aditya/Repos/recorder
source venv/bin/activate
python recorder.py --flask-ui
```

The server will start and display:
```
Open http://127.0.0.1:5000 in your browser.
```

If port 5000 is busy, it will automatically try 5001, 5002, etc.

## Using the Web UI

### 1. **Start Recording**
- Click the **▶️ Start Recording** button
- The status indicator will turn red and pulse
- Status will show "Recording - Listening for audio..."

### 2. **View Segments**
- Recorded segments appear in the "Recent Segments" panel
- Each shows timestamp, duration, and transcript preview
- Click any segment to view full details

### 3. **Search Transcripts**
- Type in the search box to filter segments
- Search is case-insensitive and instant
- Searches through all transcript text

### 4. **Generate Summary**
- Click **📊 Daily Summary** button
- Wait for processing (shows loading spinner)
- Summary appears in the right panel

### 5. **Export Data**
- Click **💾 Export Data** button
- Downloads JSON file with all segments
- Filename includes today's date

### 6. **Stop Recording**
- Click **⏹️ Stop Recording** button
- Status indicator turns gray
- Recording stops immediately

## Keyboard Shortcuts

- **Refresh**: Click 🔄 or reload page
- **Search**: Click search box or press `/`
- **Close Modal**: Click outside or press `Esc`

## Tips

1. **Auto-refresh**: The UI automatically refreshes every 5 seconds
2. **Dark Mode**: Automatically follows your system preference
3. **Mobile**: Works on phones and tablets
4. **Segments**: Shows up to 100 most recent segments from today
5. **Statistics**: Updates automatically as you record

## Troubleshooting

### Port Already in Use
The app will automatically try the next port. Check the console output.

### No Segments Showing
- Make sure recording is started
- Speak into your microphone
- Wait for silence detection to trigger segment save
- Click Refresh button

### Transcription Not Appearing
- Transcription happens in background
- May take 10-30 seconds depending on segment length
- Refresh to see updated transcripts

### Can't Hear Audio
This app only records and transcribes. It doesn't play back audio through the browser.

## Command Line Options

```bash
# Web UI mode (recommended)
python recorder.py --flask-ui

# Auto-start recording (no UI)
python recorder.py

# Only run hourly summarization
python recorder.py --only-hourly

# Custom host/port
python recorder.py --flask-ui --ui-host 0.0.0.0 --ui-port 8080
```

## Environment Variables

```bash
# Silence duration before closing segment (seconds)
export OFF_TIME_SEC=20

# Maximum segment length (seconds)
export MAX_SEGMENT_SEC=60

# Enable LLM summarization
export USE_LLM=1
export LLM_MODEL_PATH=~/models/llama-3-8b-instruct.Q4_K_M.gguf
```

## API Endpoints

If you want to integrate with other tools:

```bash
# Check status
curl http://127.0.0.1:5000/api/health

# Start recording
curl -X POST http://127.0.0.1:5000/api/start

# Stop recording
curl -X POST http://127.0.0.1:5000/api/stop

# Get segments
curl http://127.0.0.1:5000/api/segments

# Get daily summary
curl http://127.0.0.1:5000/api/summary
```

## Next Steps

1. **Customize Settings**: Edit environment variables in your shell profile
2. **Add LLM**: Download a model and set `LLM_MODEL_PATH`
3. **Autostart**: Add to your system startup scripts
4. **Backup**: Regularly backup `journal.db` and `audio/` folder

## Support

For issues or questions:
1. Check the main README.md
2. Review IMPROVEMENTS.md for feature details
3. Check the console output for error messages
4. Verify microphone permissions in system settings

Enjoy recording! 🎙️
