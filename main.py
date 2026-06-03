import argparse
import asyncio
import os
import subprocess
from pathlib import Path

import uvicorn

from buffer import Buffer
from capture import Capture
from server import app, broadcast, set_trigger_callback, set_stop_callback, set_start_callback, get_session_name

# ── paths (absolute so threads never have CWD issues) ────────────────────────

BASE = Path(__file__).parent
SEGMENTS_DIR = BASE / "segments"
WEB_DIR = BASE / "web"
REPLAY_PATH = WEB_DIR / "replay.mp4"

SEGMENTS_DIR.mkdir(exist_ok=True)
WEB_DIR.mkdir(exist_ok=True)

# ── cloud config (ตั้งผ่าน env var) ──────────────────────────────────────────
# CLOUD_API_URL : endpoint ของแอปบน cloud ที่รับคลิป เช่น https://api.xxx.com/clips
# CLOUD_API_KEY : api key ของเครื่องกล้องนี้ (ส่งเป็น Bearer token)
# COURT_ID      : รหัสคอร์ทเริ่มต้น (ถ้า operator ไม่ส่ง court มา)
CLOUD_API_URL = os.environ.get("CLOUD_API_URL", "")
CLOUD_API_KEY = os.environ.get("CLOUD_API_KEY", "")
COURT_ID      = os.environ.get("COURT_ID", "A")

# ── args ─────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--device", default=None,
                    help="index ของกล้อง (ถ้าไม่ระบุจะใช้ web setup แทน)")
args = parser.parse_args()

# ── objects ──────────────────────────────────────────────────────────────────

capture: Capture | None = None
buffer  = Buffer(segments_dir=str(SEGMENTS_DIR), segment_duration=2)

_loop: asyncio.AbstractEventLoop | None = None

# ── start callback (called by web setup wizard) ───────────────────────────────

def _on_start(device: str, framerate: int = 30, width: int = 1280, height: int = 720) -> bool:
    global capture
    # ถ้ามี capture เก่าค้างอยู่ ปิดก่อน
    if capture:
        capture.stop()
        capture = None
    # ล้าง segment เก่า กันไม่ให้ footage เซสชันก่อนหลุดมาอัพ
    for f in SEGMENTS_DIR.glob("*.ts"):
        f.unlink(missing_ok=True)
    (SEGMENTS_DIR / "playlist.m3u8").unlink(missing_ok=True)
    capture = Capture(output_dir=str(SEGMENTS_DIR), segment_duration=2,
                      device=device, framerate=framerate, width=width, height=height)
    capture.start()
    if not capture.wait_until_ready():
        capture.stop()
        capture = None
        print(f"[System] เริ่มกล้องไม่สำเร็จ — device {device} @ {framerate}fps", flush=True)
        return False
    print(f"[System] Recording started — device {device} @ {framerate}fps", flush=True)
    return True

# ── replay trigger ────────────────────────────────────────────────────────────

def _send_to_cloud(local_path: str, uid: str, court: str):
    """POST คลิป + userId ไปแอปบน cloud (multipart)"""
    import httpx, datetime
    captured_at = datetime.datetime.now().isoformat()
    try:
        with open(local_path, "rb") as f:
            files   = {"file": ("replay.mp4", f, "video/mp4")}
            data    = {"userId": uid, "court": court or COURT_ID,
                       "capturedAt": captured_at}
            headers = {"Authorization": f"Bearer {CLOUD_API_KEY}"} if CLOUD_API_KEY else {}
            r = httpx.post(CLOUD_API_URL, data=data, files=files,
                           headers=headers, timeout=30)
        if r.status_code // 100 == 2:
            print(f"[Cloud] ส่งคลิปสำเร็จ → user={uid or '-'} court={court or COURT_ID} "
                  f"({r.status_code})", flush=True)
        else:
            print(f"[Cloud] ส่งไม่สำเร็จ {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[Cloud] error: {e}", flush=True)


def _upload_to_drive(local_path: str):
    import subprocess, datetime
    timestamp = datetime.datetime.now().strftime("%H%M%S")
    session   = get_session_name() or datetime.datetime.now().strftime("%Y-%m-%d")
    remote    = f"replay_kross:KROSS PADEL/{session}/replay_{timestamp}.mp4"
    result = subprocess.run(
        ["rclone", "copyto", local_path, remote],
        capture_output=True,
    )
    if result.returncode == 0:
        print(f"[Drive] Uploaded → {remote}", flush=True)
    else:
        print(f"[Drive] Upload failed: {result.stderr.decode()[:200]}", flush=True)


def _do_replay(uid: str = "", court: str = ""):
    if capture is None:
        print("[Trigger] ระบบยังไม่เริ่ม — ไปที่ /setup ก่อน", flush=True)
        return
    print(f"[Trigger] Extracting last 5s... (user={uid or '-'})", flush=True)
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
        # ถ้าตั้ง CLOUD_API_URL ไว้ → ส่งเข้าแอป cloud, ไม่งั้น fallback Drive
        if CLOUD_API_URL:
            _send_to_cloud(clip, uid, court)
        else:
            _upload_to_drive(clip)
    else:
        print("[Trigger] extract_clip returned None", flush=True)

# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    global _loop
    _loop = asyncio.get_running_loop()

    set_trigger_callback(_do_replay)
    set_stop_callback(lambda: (capture.stop() if capture else None, os._exit(0)))
    set_start_callback(_on_start)

    if args.device is not None:
        _on_start(args.device)

    print("=" * 50, flush=True)
    print("  Padel Replay System", flush=True)
    print(f"  Web : http://localhost:8000", flush=True)
    print("=" * 50, flush=True)

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        if capture:
            capture.stop()


if __name__ == "__main__":
    asyncio.run(main())
