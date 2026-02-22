#!/usr/bin/env python3
import sys
sys.path.insert(0, '/Users/aditya/Repos/recorder')

# Test imports
try:
    import recorder
    print("✓ Recorder module imported")
except Exception as e:
    print(f"✗ Failed to import recorder: {e}")
    sys.exit(1)

# Test starting threads
try:
    print("\nStarting threads...")
    result = recorder._start_threads()
    print(f"Start result: {result}")
    print(f"Is running: {recorder.is_running()}")
    
    import time
    time.sleep(2)
    
    # Check thread status
    print(f"\nThreads: {recorder._threads}")
    for name, thread in recorder._threads.items():
        print(f"  {name}: alive={thread.is_alive()}")
    
    # Check queues
    print(f"\nQueue sizes:")
    print(f"  audio_q: {recorder.audio_q.qsize()}")
    print(f"  proc_q: {recorder.proc_q.qsize()}")
    
    print("\nWaiting 5s for audio...")
    time.sleep(5)
    
    print(f"\nAfter 5s:")
    print(f"  audio_q: {recorder.audio_q.qsize()}")
    print(f"  proc_q: {recorder.proc_q.qsize()}")
    
    recorder._stop_threads()
    print("\n✓ Stopped threads")
    
except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
