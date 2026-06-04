import asyncio
import json
import os
import re
import subprocess
import sys
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI()

# ── platform abstraction (macOS avfoundation / Windows dshow) ─────────────────
# macOS : device = index ("0", "1"...)   ใช้ avfoundation
# Windows: device = ชื่อกล้อง            ใช้ dshow (video=ชื่อ)
_IS_WINDOWS = sys.platform.startswith("win")


def _camera_input_args(device: str, fps: str) -> list[str]:
    """ffmpeg input flags สำหรับเปิดกล้อง ตาม OS"""
    if _IS_WINDOWS:
        return ["-f", "dshow", "-framerate", fps, "-i", f"video={device}"]
    return ["-f", "avfoundation", "-framerate", fps, "-i", f"{device}:none"]


def _list_cameras() -> list[dict]:
    """คืน list ของ {index, name} — บน Windows index = ชื่อกล้อง"""
    if _IS_WINDOWS:
        result = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, errors="ignore",
        )
        cameras = []
        for line in result.stderr.splitlines():
            # dshow แสดง:  "Integrated Camera" (video)
            m = re.search(r'"(.+?)"\s*\(video\)', line)
            if m:
                name = m.group(1)
                cameras.append({"index": name, "name": name})
        return cameras

    # macOS — avfoundation ใช้ index
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    cameras = []
    in_video = False
    for line in result.stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            break
        if in_video:
            m = re.search(r'\[(\d+)\]\s+(.+)', line)
            if m:
                cameras.append({"index": m.group(1), "name": m.group(2).strip()})
    return cameras

# set by main.py after startup
_trigger_callback: Callable | None = None
_stop_callback: Callable | None = None
_start_callback: Callable | None = None
_started = False
_session_name: str = ""
_preview_proc: asyncio.subprocess.Process | None = None


def set_trigger_callback(fn: Callable):
    global _trigger_callback
    _trigger_callback = fn


def set_stop_callback(fn: Callable):
    global _stop_callback
    _stop_callback = fn


def set_start_callback(fn: Callable):
    global _start_callback
    _start_callback = fn


def get_session_name() -> str:
    return _session_name

# ชุดของ WebSocket clients ที่เชื่อมต่ออยู่
_clients: set[WebSocket] = set()


# ── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    print(f"[WS] Client connected  (total: {len(_clients)})")
    try:
        while True:
            await ws.receive_text()  # keep-alive ping จาก client
    except WebSocketDisconnect:
        _clients.discard(ws)
        print(f"[WS] Client disconnected  (total: {len(_clients)})")


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    from fastapi.responses import RedirectResponse
    if _started:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/setup")


@app.get("/setup")
async def setup():
    return FileResponse("web/setup.html")


@app.get("/dashboard")
async def dashboard():
    return FileResponse("web/dashboard.html")


@app.get("/status")
async def status():
    return JSONResponse({"started": _started})


@app.get("/qrcode.min.js")
async def qrcode_js():
    return FileResponse("web/qrcode.min.js", media_type="application/javascript")


@app.get("/info")
async def info():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "localhost"
    return JSONResponse({"ip": ip, "port": 8000})


@app.get("/operator")
async def operator():
    return FileResponse("web/operator.html")


@app.get("/cameras")
async def list_cameras():
    cameras = await asyncio.get_running_loop().run_in_executor(None, _list_cameras)
    return JSONResponse({"cameras": cameras})


def _probe_camera_modes(device: str) -> list[dict]:
    """รัน ffmpeg เพื่อ probe โหมดที่กล้องรองรับ (blocking — เรียกใน executor)"""
    if _IS_WINDOWS:
        cmd = ["ffmpeg", "-f", "dshow", "-list_options", "true", "-i", f"video={device}"]
        # dshow แสดง: ... max s=1280x720 fps=30
        pattern = re.compile(r'max s=(\d+)x(\d+) fps=([\d.]+)')
    else:
        cmd = ["ffmpeg", "-f", "avfoundation", "-framerate", "9999", "-i", f"{device}:none"]
        # avfoundation แสดง: 1280x720@[25.0 25.0]fps
        pattern = re.compile(r'(\d+)x(\d+)@\[[\d.]+\s+([\d.]+)\]fps')

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                              errors="ignore")
    except subprocess.TimeoutExpired:
        return []

    modes = []
    seen = set()
    for line in proc.stderr.splitlines():
        m = pattern.search(line)
        if m:
            w, h, fps = int(m.group(1)), int(m.group(2)), round(float(m.group(3)))
            key = (w, h, fps)
            if key not in seen:
                seen.add(key)
                modes.append({"width": w, "height": h, "fps": fps,
                              "label": f"{w}×{h} · {fps}fps"})
    modes.sort(key=lambda x: (x["width"] * x["height"], x["fps"]), reverse=True)
    return modes


@app.get("/camera-formats")
async def camera_formats(device: str = "0"):
    """probe โหมดที่กล้องรองรับ — รันใน thread เพื่อไม่ block event loop"""
    modes = await asyncio.get_running_loop().run_in_executor(
        None, _probe_camera_modes, device
    )
    return JSONResponse({"modes": modes})


@app.websocket("/ws/preview")
async def ws_preview(ws: WebSocket, device: str = "0"):
    global _preview_proc
    await ws.accept()
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *_camera_input_args(device, "30"),
        "-vf", "scale=640:360,fps=15",
        "-f", "mjpeg", "-q:v", "6", "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _preview_proc = proc
    buf = bytearray()
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                start = buf.find(b'\xff\xd8')
                if start == -1:
                    buf.clear()
                    break
                end = buf.find(b'\xff\xd9', start + 2)
                if end == -1:
                    del buf[:start]
                    break
                frame = bytes(buf[start:end + 2])
                del buf[:end + 2]
                try:
                    await ws.send_bytes(frame)
                except Exception:
                    return
    except Exception:
        pass
    finally:
        if proc.returncode is None:
            proc.terminate()
        _preview_proc = None


@app.post("/start")
async def start_recording(device: str = "0", session: str = "",
                          framerate: int = 30, width: int = 1280, height: int = 720):
    global _preview_proc, _started, _session_name
    if _started:
        return JSONResponse({"status": "already_started"})
    if _preview_proc and _preview_proc.returncode is None:
        _preview_proc.terminate()
        await asyncio.sleep(1.0)
    if not _start_callback:
        return JSONResponse({"status": "not_ready"}, status_code=503)

    _session_name = session
    # เรียก start แบบ sync เพื่อรอผลว่ากล้องเปิดได้จริงไหม
    ok = await asyncio.get_running_loop().run_in_executor(
        None, _start_callback, device, framerate, width, height
    )
    if ok:
        _started = True
        return JSONResponse({"status": "ok"})
    return JSONResponse(
        {"status": "camera_error",
         "detail": "เปิดกล้องไม่ได้ — ลองเลือกความละเอียด/เฟรมเรทอื่น"},
        status_code=502,
    )


@app.post("/stop")
async def stop_recording():
    if _stop_callback:
        # บอก client ทุกเครื่องให้ปล่อยกล้อง + ขึ้นจอหยุด ก่อน process จะตาย
        await broadcast({"event": "system_stopped"})
        await asyncio.sleep(0.4)
        import threading
        threading.Thread(target=_stop_callback, daemon=True).start()
        return JSONResponse({"status": "stopped"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.post("/trigger")
async def trigger_replay(uid: str = "", court: str = ""):
    if _trigger_callback:
        import threading
        t = threading.Thread(target=_trigger_callback, args=(uid, court), daemon=True)
        t.start()
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/replay")
async def get_replay():
    path = "web/replay.mp4"
    if os.path.exists(path):
        return FileResponse(path, media_type="video/mp4",
                            headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h3>No replay available yet</h3>", status_code=404)


# ── Broadcast helper (ถูกเรียกจาก main.py) ──────────────────────────────────

async def broadcast(message: dict):
    if not _clients:
        print("[WS] No clients connected — skipping broadcast")
        return

    payload = json.dumps(message)
    disconnected = set()

    for client in _clients:
        try:
            await client.send_text(payload)
        except Exception:
            disconnected.add(client)

    _clients.difference_update(disconnected)
    print(f"[WS] Broadcast sent to {len(_clients)} client(s)")
