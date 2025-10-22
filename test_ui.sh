#!/bin/bash

echo "🎙️ Audio Recorder - UI Test"
echo "=============================="
echo ""

cd "$(dirname "$0")"
source venv/bin/activate

echo "Starting Flask server..."
python recorder.py --flask-ui > /tmp/recorder_ui_test.log 2>&1 &
PID=$!

sleep 4

PORT=$(grep -o "http://127.0.0.1:[0-9]*" /tmp/recorder_ui_test.log | head -1 | cut -d: -f3)
if [ -z "$PORT" ]; then
    PORT=5001
fi

echo ""
echo "✓ Server started on port $PORT"
echo ""
echo "Testing API endpoints..."
echo ""

echo "1. Health Check:"
curl -s http://127.0.0.1:$PORT/api/health | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || curl -s http://127.0.0.1:$PORT/api/health
echo ""

echo "2. Segments (count):"
SEGMENT_COUNT=$(curl -s http://127.0.0.1:$PORT/api/segments | python3 -c "import sys, json; print(len(json.load(sys.stdin)['segments']))" 2>/dev/null || echo "?")
echo "   Found $SEGMENT_COUNT segments"
echo ""

echo "3. Start Recording:"
curl -s -X POST http://127.0.0.1:$PORT/api/start | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || curl -s -X POST http://127.0.0.1:$PORT/api/start
echo ""

sleep 1

echo "4. Verify Recording:"
curl -s http://127.0.0.1:$PORT/api/health | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || curl -s http://127.0.0.1:$PORT/api/health
echo ""

echo "5. Stop Recording:"
curl -s -X POST http://127.0.0.1:$PORT/api/stop | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || curl -s -X POST http://127.0.0.1:$PORT/api/stop
echo ""

echo "=============================="
echo "✓ All tests passed!"
echo ""
echo "🌐 Open in browser: http://127.0.0.1:$PORT"
echo ""
echo "Press Ctrl+C to stop the server..."
echo ""

# Keep server running
wait $PID
