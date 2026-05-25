import argparse
import asyncio
import os
from pathlib import Path

import uvicorn

from buffer import Buffer
from capture import Capture
from server import app, broadcast, set_trigger_callback, set_stop_callback

# ── paths (absolute so threads never have CWD issues) ────────────────────────

BASE = Path(__file__).parent
SEGMENTS_DIR = BASE / "segments"
WEB_DIR = BASE / "web"
REPLAY_PATH = WEB_DIR / "replay.mp4"

SEGMENTS_DIR.mkdir(exist_ok=True)
WEB_DIR.mkdir(exist_ok=True)

# ── args ─────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="0")
parser.add_argument("--list-devices", action="store_true")
args = parser.parse_args()

if args.list_devices:
    import subprocess
    subprocess.run(["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
    raise SystemExit(0)

# ── objects ──────────────────────────────────────────────────────────────────

capture = Capture(output_dir=str(SEGMENTS_DIR), segment_duration=2, device=args.device)
buffer  = Buffer(segments_dir=str(SEGMENTS_DIR), segment_duration=2)

_loop: asyncio.AbstractEventLoop | None = None

# ── replay trigger ────────────────────────────────────────────────────────────

def _upload_to_drive(local_path: str):
    import subprocess, datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    remote = f"replay_kross:KROSS PADEL/replays/replay_{timestamp}.mp4"
    result = subprocess.run(
        ["rclone", "copyto", local_path, remote],
        capture_output=True,
    )
    if result.returncode == 0:
        print(f"[Drive] Uploaded → {remote}", flush=True)
    else:
        print(f"[Drive] Upload failed: {result.stderr.decode()[:200]}", flush=True)


def _do_replay():
    print("[Trigger] Extracting last 5s...", flush=True)
    try:
        clip = buffer.extract_clip(duration=5, output_path=str(REPLAY_PATH))
    except Exception as e:
        print(f"[Trigger] Error: {e}", flush=True)
        return
    if clip and _loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({"event": "replay_ready", "url": "/replay"}),
            _loop,
        )
        print("[Trigger] Done — broadcast sent", flush=True)
        _upload_to_drive(clip)
    else:
        print("[Trigger] extract_clip returned None", flush=True)

# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    global _loop
    _loop = asyncio.get_running_loop()

    set_trigger_callback(_do_replay)
    set_stop_callback(lambda: (capture.stop(), os._exit(0)))
    capture.start()

    print("=" * 50, flush=True)
    print("  Padel Replay System", flush=True)
    print(f"  Web : http://localhost:8000", flush=True)
    print("=" * 50, flush=True)

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        capture.stop()


if __name__ == "__main__":
    asyncio.run(main())
