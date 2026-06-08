import asyncio
import json
import os
import re
import subprocess
import sys
import threading
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

app = FastAPI()

# ── platform abstraction (macOS avfoundation / Windows dshow) ─────────────────
# macOS : device = index ("0", "1"...)   ใช้ avfoundation
# Windows: device = ชื่อกล้อง            ใช้ dshow (video=ชื่อ)
_IS_WINDOWS = sys.platform.startswith("win")


def _list_cameras() -> list[dict]:
    """คืน list ของ {index, name} — บน Windows index = ชื่อกล้อง"""
    if _IS_WINDOWS:
        result = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, errors="ignore",
        )
        cameras = []
        for line in result.stderr.splitlines():
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

# ── callbacks (set by main.py) ────────────────────────────────────────────────
_trigger_callback: Callable | None = None        # (court, uid)
_stop_callback: Callable | None = None            # (court_id | None)  None = หยุดทั้งหมด+exit
_list_courts_callback: Callable | None = None     # () -> list[dict]
_upsert_callback: Callable | None = None          # (cfg dict) -> bool
_remove_callback: Callable | None = None          # (court_id) -> None
_save_session_callback: Callable | None = None    # (name) -> None  บันทึก session ลง config
_session_name: str = ""

# ── live preview ผ่าน HLS ─────────────────────────────────────────────────────
# dashboard เล่นไฟล์ HLS ที่ capture เขียนไว้ตรงๆ ผ่าน hls.js (/hls/<court>/...) →
# server ไม่ต้อง encode/ส่งเฟรม preview เอง (เดิมใช้ MJPEG over WS กิน CPU ตลอดเวลา)
_SEGMENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "segments")


def set_trigger_callback(fn: Callable):
    global _trigger_callback
    _trigger_callback = fn


def set_stop_callback(fn: Callable):
    global _stop_callback
    _stop_callback = fn


def set_list_courts_callback(fn: Callable):
    global _list_courts_callback
    _list_courts_callback = fn


def set_upsert_callback(fn: Callable):
    global _upsert_callback
    _upsert_callback = fn


def set_remove_callback(fn: Callable):
    global _remove_callback
    _remove_callback = fn


def set_save_session_callback(fn: Callable):
    global _save_session_callback
    _save_session_callback = fn


def set_session(name: str):
    global _session_name
    _session_name = name or ""


def get_session_name() -> str:
    return _session_name


def _courts() -> list[dict]:
    return _list_courts_callback() if _list_courts_callback else []


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


@app.get("/hls.min.js")
async def hls_js():
    return FileResponse("web/hls.min.js", media_type="application/javascript")


@app.get("/hls/{court}/{filename}")
async def hls_file(court: str, filename: str):
    """เสิร์ฟ playlist.m3u8 + seg_*.ts ของคอร์ทให้ dashboard เล่นพรีวิวสดผ่าน hls.js"""
    # กัน path traversal — รับเฉพาะชื่อไฟล์ HLS ที่คาดไว้
    if not (filename.endswith(".m3u8") or filename.endswith(".ts")):
        return Response(status_code=404)
    if "/" in court or "\\" in court or ".." in court \
            or "/" in filename or "\\" in filename or ".." in filename:
        return Response(status_code=404)
    path = os.path.join(_SEGMENTS_DIR, court, filename)
    if not os.path.isfile(path):
        return Response(status_code=404)
    if filename.endswith(".m3u8"):
        return FileResponse(path, media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"})
    return FileResponse(path, media_type="video/mp2t")


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/dashboard")


@app.get("/setup")
async def setup():
    # เลิกใช้ setup wizard แยกแล้ว — จัดการกล้อง + session ทั้งหมดบน dashboard ที่เดียว
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/dashboard")


@app.get("/dashboard")
async def dashboard():
    return FileResponse("web/dashboard.html", headers={"Cache-Control": "no-store"})


@app.get("/operator")
async def operator():
    return FileResponse("web/operator.html")


@app.get("/qrcode.min.js")
async def qrcode_js():
    return FileResponse("web/qrcode.min.js", media_type="application/javascript")


@app.get("/status")
async def status():
    return JSONResponse({"courts": _courts()})


class SessionIn(BaseModel):
    session: str


@app.get("/session")
async def get_session():
    return JSONResponse({"session": get_session_name()})


@app.post("/session")
async def post_session(body: SessionIn):
    set_session(body.session)
    if _save_session_callback:
        await asyncio.get_running_loop().run_in_executor(
            None, _save_session_callback, body.session)
    return JSONResponse({"status": "ok", "session": body.session})


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


# ── courts management ─────────────────────────────────────────────────────────

@app.get("/courts")
async def get_courts():
    return JSONResponse({"courts": _courts()})


class CourtIn(BaseModel):
    id: str
    name: str | None = None
    device: str
    fps: int = 30
    width: int = 1280
    height: int = 720
    session: str | None = None


@app.post("/courts")
async def add_or_update_court(court: CourtIn):
    """เพิ่ม/แก้คอร์ท (เปลี่ยนกล้อง/ความละเอียด/ชื่อ) — start แล้วบันทึก courts.json"""
    if not _upsert_callback:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    cfg = court.model_dump(exclude_none=True)
    ok = await asyncio.get_running_loop().run_in_executor(None, _upsert_callback, cfg)
    if ok:
        return JSONResponse({"status": "ok"})
    return JSONResponse(
        {"status": "camera_error",
         "detail": "เปิดกล้องไม่ได้ — ลองเลือกความละเอียด/เฟรมเรทอื่น หรือกล้องอื่น"},
        status_code=502,
    )


@app.delete("/courts/{court_id}")
async def delete_court(court_id: str):
    if not _remove_callback:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    await asyncio.get_running_loop().run_in_executor(None, _remove_callback, court_id)
    return JSONResponse({"status": "ok"})


# ── camera enumeration / probe ────────────────────────────────────────────────

@app.get("/cameras")
async def list_cameras():
    cameras = await asyncio.get_running_loop().run_in_executor(None, _list_cameras)
    return JSONResponse({"cameras": cameras})


def _probe_camera_modes(device: str) -> list[dict]:
    """รัน ffmpeg เพื่อ probe โหมดที่กล้องรองรับ (blocking — เรียกใน executor)"""
    if _IS_WINDOWS:
        cmd = ["ffmpeg", "-f", "dshow", "-list_options", "true", "-i", f"video={device}"]
        pattern = re.compile(r'max s=(\d+)x(\d+) fps=([\d.]+)')
    else:
        cmd = ["ffmpeg", "-f", "avfoundation", "-framerate", "9999", "-i", f"{device}:none"]
        pattern = re.compile(r'(\d+)x(\d+)@\[[\d.]+\s+([\d.]+)\]fps')

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5, errors="ignore")
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


_RTSP_MODES = [
    {"width": 1920, "height": 1080, "fps": 30, "label": "1920×1080 · 30fps"},
    {"width": 1280, "height": 720, "fps": 30, "label": "1280×720 · 30fps"},
    {"width": 854, "height": 480, "fps": 30, "label": "854×480 · 30fps"},
]


@app.get("/camera-formats")
async def camera_formats(device: str = "0"):
    # กล้อง IP/RTSP probe โหมดแบบ dshow/avfoundation ไม่ได้ — คืนชุดความละเอียดมาตรฐานให้เลือก
    # (output จะ scale ภาพจาก stream ต้นทางให้เท่าที่เลือก)
    if device.startswith(("rtsp://", "rtsps://")):
        return JSONResponse({"modes": _RTSP_MODES})
    modes = await asyncio.get_running_loop().run_in_executor(
        None, _probe_camera_modes, device
    )
    return JSONResponse({"modes": modes})


# ── ทดสอบการเชื่อมต่อ RTSP (Verify) ──────────────────────────────────────────

class VerifyRtspIn(BaseModel):
    url: str


def _probe_rtsp(url: str) -> dict:
    """ลองต่อ RTSP จริงด้วย ffprobe — คืนความละเอียด/codec/fps ถ้าต่อติด"""
    if not url.startswith(("rtsp://", "rtsps://")):
        return {"ok": False, "detail": "ไม่ใช่ RTSP URL"}
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
           "-select_streams", "v:0",
           "-show_entries", "stream=width,height,codec_name,avg_frame_rate",
           "-of", "json", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "หมดเวลาเชื่อมต่อ — กล้องไม่ตอบ / รหัสผิด / URL ผิด"}
    except FileNotFoundError:
        return {"ok": False, "detail": "ไม่พบ ffprobe (ต้องติดตั้ง ffmpeg)"}
    if proc.returncode != 0:
        err = [l for l in (proc.stderr or "").strip().splitlines() if l.strip()]
        return {"ok": False, "detail": err[-1] if err else "เชื่อมต่อไม่สำเร็จ"}
    try:
        streams = (json.loads(proc.stdout or "{}").get("streams") or [])
        if not streams:
            return {"ok": False, "detail": "ต่อได้แต่ไม่พบสตรีมวิดีโอ"}
        s = streams[0]
        fps = 0
        try:
            n, d = (s.get("avg_frame_rate") or "0/1").split("/")
            fps = round(int(n) / int(d)) if int(d) else 0
        except Exception:
            pass
        return {"ok": True, "width": s.get("width"), "height": s.get("height"),
                "codec": s.get("codec_name"), "fps": fps}
    except Exception as e:
        return {"ok": False, "detail": f"อ่านผลไม่ได้: {e}"}


@app.post("/verify-rtsp")
async def verify_rtsp(body: VerifyRtspIn):
    info = await asyncio.get_running_loop().run_in_executor(None, _probe_rtsp, body.url)
    return JSONResponse(info, status_code=(200 if info.get("ok") else 502))


# ── CCTV / ONVIF auto-discovery ───────────────────────────────────────────────

@app.get("/scan-cctv")
async def scan_cctv(deep: bool = False):
    """ค้นหากล้อง IP/CCTV บนวง LAN ด้วย ONVIF WS-Discovery (ไม่ต้องใช้รหัส)

    deep=1 หรือ discovery ไม่เจออะไรเลย → fallback ไล่สแกนพอร์ต RTSP ทั้ง subnet
    (เผื่อกล้องปิด ONVIF discovery / router บล็อก multicast แต่ยังเปิด 554 อยู่)
    """
    import onvif_scan
    loop = asyncio.get_running_loop()
    cams = await loop.run_in_executor(None, onvif_scan.discover)
    if deep or not cams:
        extra = await loop.run_in_executor(None, onvif_scan.scan_subnet)
        seen = {c["ip"] for c in cams}        # ONVIF discovery ชนะ (มีชื่อ/รุ่นกล้อง)
        cams += [c for c in extra if c["ip"] not in seen]
    return JSONResponse({"cameras": cams})


class CctvRtspIn(BaseModel):
    xaddr: str
    user: str
    password: str
    ip: str | None = None


@app.post("/cctv-rtsp")
async def cctv_rtsp(body: CctvRtspIn):
    """ดึง RTSP URL ของกล้อง ONVIF อัตโนมัติด้วย user/pass (GetStreamUri)"""
    import onvif_scan
    try:
        url = await asyncio.get_running_loop().run_in_executor(
            None, onvif_scan.get_rtsp_url, body.xaddr, body.user, body.password, body.ip or "")
        return JSONResponse({"url": url})
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=502)


# ── stop / trigger / replay ───────────────────────────────────────────────────

@app.post("/stop")
async def stop_recording(court: str = ""):
    """court ว่าง = หยุดทั้งระบบ (process exit) | court ระบุ = หยุดเฉพาะคอร์ทนั้น"""
    if not _stop_callback:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    if court:
        await asyncio.get_running_loop().run_in_executor(None, _stop_callback, court)
        return JSONResponse({"status": "stopped", "court": court})
    # หยุดทั้งหมด: บอก client ให้ปล่อยกล้อง + ขึ้นจอหยุด ก่อน process ตาย
    await broadcast({"event": "system_stopped"})
    await asyncio.sleep(0.4)
    threading.Thread(target=_stop_callback, daemon=True).start()
    return JSONResponse({"status": "stopped"})


@app.post("/trigger")
async def trigger_replay(court: str = "", uid: str = ""):
    if _trigger_callback:
        t = threading.Thread(target=_trigger_callback, args=(court, uid), daemon=True)
        t.start()
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/replay")
async def get_replay(court: str = ""):
    path = f"web/replay_{court}.mp4" if court else "web/replay.mp4"
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
