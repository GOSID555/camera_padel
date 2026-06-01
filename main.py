import argparse
import asyncio
import os
import re
import subprocess
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

# ── camera picker ─────────────────────────────────────────────────────────────

def list_cameras() -> list[tuple[str, str]]:
    """คืนค่า list ของ (index, ชื่อกล้อง) จาก avfoundation"""
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True
    )
    cameras = []
    # parse บรรทัดที่มี [x] ชื่อกล้อง
    for line in result.stderr.splitlines():
        m = re.search(r'\[(\d+)\]\s+(.+)', line)
        if m and "AVFoundation video" not in line and "AVFoundation audio" not in line:
            cameras.append((m.group(1), m.group(2).strip()))
    return cameras


def pick_device(args_device: str) -> str:
    """ถ้าไม่ได้ระบุ --device ให้แสดงเมนูเลือกกล้อง"""
    if args_device != "__pick__":
        return args_device

    cameras = list_cameras()
    if not cameras:
        print("[Camera] ไม่พบกล้อง — ใช้ device 0 แทน")
        return "0"

    print("\n📷  กล้องที่พบในระบบ:")
    print("─" * 36)
    for idx, name in cameras:
        print(f"  [{idx}]  {name}")
    print("─" * 36)

    while True:
        try:
            choice = input("เลือกหมายเลขกล้อง: ").strip()
            if choice in [idx for idx, _ in cameras]:
                name = next(n for i, n in cameras if i == choice)
                print(f"✅  ใช้กล้อง [{choice}] {name}\n")
                return choice
            print(f"    กรุณาพิมพ์หมายเลขที่มีในรายการ ({', '.join(i for i,_ in cameras)})")
        except (KeyboardInterrupt, EOFError):
            raise SystemExit(0)

# ── args ─────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="__pick__",
                    help="index ของกล้อง (ถ้าไม่ระบุจะให้เลือกในเมนู)")
args = parser.parse_args()

device = pick_device(args.device)

# ── objects ──────────────────────────────────────────────────────────────────

capture = Capture(output_dir=str(SEGMENTS_DIR), segment_duration=2, device=device)
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
