import asyncio
import json
import os
import re
import subprocess
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI()

# set by main.py after startup
_trigger_callback: Callable | None = None
_stop_callback: Callable | None = None
_start_callback: Callable | None = None
_started = False
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
    return FileResponse("web/index.html")


@app.get("/setup")
async def setup():
    return FileResponse("web/setup.html")


@app.get("/operator")
async def operator():
    return FileResponse("web/operator.html")


@app.get("/cameras")
async def list_cameras():
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
    return JSONResponse({"cameras": cameras})


@app.get("/preview")
async def preview(device: str = "0"):
    global _preview_proc

    async def generate():
        global _preview_proc
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-f", "avfoundation", "-framerate", "10", "-i", f"{device}:none",
            "-f", "mjpeg", "-q:v", "4", "pipe:1",
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
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + frame + b"\r\n")
        finally:
            if proc.returncode is None:
                proc.terminate()
            _preview_proc = None

    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.post("/start")
async def start_recording(device: str = "0"):
    global _preview_proc, _started
    if _started:
        return JSONResponse({"status": "already_started"})
    if _preview_proc and _preview_proc.returncode is None:
        _preview_proc.terminate()
        await asyncio.sleep(1.0)
    if _start_callback:
        import threading
        threading.Thread(target=_start_callback, args=(device,), daemon=True).start()
        _started = True
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.post("/stop")
async def stop_recording():
    if _stop_callback:
        import threading
        threading.Thread(target=_stop_callback, daemon=True).start()
        return JSONResponse({"status": "stopped"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.post("/trigger")
async def trigger_replay():
    if _trigger_callback:
        import asyncio, threading
        t = threading.Thread(target=_trigger_callback, daemon=True)
        t.start()
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/replay")
async def get_replay():
    path = "web/replay.mp4"
    if os.path.exists(path):
        return FileResponse(path, media_type="video/mp4")
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
