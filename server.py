import json
import os
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

app = FastAPI()

# set by main.py after startup
_trigger_callback: Callable | None = None
_stop_callback: Callable | None = None


def set_trigger_callback(fn: Callable):
    global _trigger_callback
    _trigger_callback = fn


def set_stop_callback(fn: Callable):
    global _stop_callback
    _stop_callback = fn

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
