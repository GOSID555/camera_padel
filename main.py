import asyncio
import json
import os
import sys
from pathlib import Path

# Windows: console เริ่มต้นบางเครื่องเป็น cp1252 — print ภาษาไทย/อักษรพิเศษจะ crash
# บังคับ stdout/stderr เป็น UTF-8 กันล้มตอน start กล้อง
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn
from dotenv import load_dotenv

from buffer import Buffer
from capture import Capture
from server import (app, broadcast, set_trigger_callback, set_stop_callback,
                    set_list_courts_callback, set_upsert_callback,
                    set_remove_callback, set_session, get_session_name,
                    set_save_session_callback)

# โหลดค่าจาก .env (ถ้ามี) — key/url ต่างๆ ไม่ต้องพิมพ์ใน command line
load_dotenv()

# ── paths (absolute so threads never have CWD issues) ────────────────────────

BASE = Path(__file__).parent
SEGMENTS_DIR = BASE / "segments"
WEB_DIR = BASE / "web"
COURTS_JSON = BASE / "courts.json"

SEGMENTS_DIR.mkdir(exist_ok=True)
WEB_DIR.mkdir(exist_ok=True)

# ── cloud config (ตั้งผ่าน env var) ──────────────────────────────────────────
CLOUD_API_URL = os.environ.get("CLOUD_API_URL", "")
CLOUD_API_KEY = os.environ.get("CLOUD_API_KEY", "")
COURT_ID      = os.environ.get("COURT_ID", "A")

# ── runtime state ─────────────────────────────────────────────────────────────
# 1 คอร์ท = 1 Capture (ffmpeg อัดต่อเนื่อง) + 1 Buffer (ตัดคลิป) + segment dir ของตัวเอง
captures: dict[str, Capture] = {}
buffers:  dict[str, Buffer]  = {}

_loop: asyncio.AbstractEventLoop | None = None

# ── config persistence (courts.json) ──────────────────────────────────────────

def _seg_dir(court_id: str) -> Path:
    return SEGMENTS_DIR / court_id

def _replay_path(court_id: str) -> Path:
    return WEB_DIR / f"replay_{court_id}.mp4"

def load_config() -> dict:
    if COURTS_JSON.exists():
        try:
            return json.loads(COURTS_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Config] อ่าน courts.json ไม่ได้: {e}", flush=True)
    return {"session": "", "courts": []}

def save_config(cfg: dict):
    COURTS_JSON.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

_config: dict = {"session": "", "courts": []}

def _find_court(court_id: str) -> dict | None:
    return next((c for c in _config["courts"] if c["id"] == court_id), None)

# ── capture lifecycle ─────────────────────────────────────────────────────────

def _start_court(cfg: dict) -> bool:
    """เปิด/รีสตาร์ทกล้องของคอร์ทหนึ่ง — cfg = {id,name,device,fps,width,height}"""
    cid = cfg["id"]
    # ปิดของเดิม (ถ้ามี) + ล้าง segment คอร์ทนี้
    if cid in captures:
        captures[cid].stop()
        captures.pop(cid, None)
    seg = _seg_dir(cid)
    seg.mkdir(parents=True, exist_ok=True)
    for f in seg.glob("*.ts"):
        f.unlink(missing_ok=True)
    (seg / "playlist.m3u8").unlink(missing_ok=True)

    cap = Capture(
        output_dir=str(seg), segment_duration=1,
        device=cfg["device"], framerate=int(cfg.get("fps", 30)),
        width=int(cfg.get("width", 1280)), height=int(cfg.get("height", 720)),
    )
    cap.start()
    if not cap.wait_until_ready(timeout=15.0):
        cap.stop()
        print(f"[System] คอร์ท {cid}: เริ่มกล้องไม่สำเร็จ — device {cfg['device']}", flush=True)
        return False
    captures[cid] = cap
    buffers[cid] = Buffer(segments_dir=str(seg), segment_duration=1)
    print(f"[System] คอร์ท {cid}: Recording started — device {cfg['device']} "
          f"@ {cfg.get('fps',30)}fps {cfg.get('width',1280)}x{cfg.get('height',720)}", flush=True)
    return True

def _stop_court(court_id: str):
    if court_id in captures:
        captures[court_id].stop()
        captures.pop(court_id, None)
    buffers.pop(court_id, None)
    print(f"[System] คอร์ท {court_id}: stopped", flush=True)

def _stop_all():
    for cid in list(captures.keys()):
        captures[cid].stop()
    captures.clear()
    buffers.clear()

# ── callbacks ที่ server เรียก ────────────────────────────────────────────────

def list_courts() -> list[dict]:
    """รายการคอร์ทใน config + สถานะกำลังอัดอยู่ไหม"""
    out = []
    for c in _config["courts"]:
        running = c["id"] in captures and captures[c["id"]].process is not None \
                  and captures[c["id"]].process.poll() is None
        out.append({**c, "running": running})
    return out

def upsert_court(cfg: dict) -> bool:
    """เพิ่ม/แก้คอร์ท (เปลี่ยนกล้อง/ความละเอียด/ชื่อ) — start ก่อน ถ้าสำเร็จค่อยบันทึก"""
    cid = cfg.get("id", "").strip()
    if not cid:
        return False
    norm = {
        "id": cid,
        "name": cfg.get("name") or f"Court {cid}",
        "device": cfg["device"],
        "fps": int(cfg.get("fps", 30)),
        "width": int(cfg.get("width", 1280)),
        "height": int(cfg.get("height", 720)),
    }
    if "session" in cfg:
        _config["session"] = cfg["session"]
        set_session(cfg["session"])
    if not _start_court(norm):
        return False
    existing = _find_court(cid)
    if existing:
        existing.update(norm)
    else:
        _config["courts"].append(norm)
    save_config(_config)
    return True

def remove_court(court_id: str):
    _stop_court(court_id)
    _config["courts"] = [c for c in _config["courts"] if c["id"] != court_id]
    save_config(_config)

def save_session(name: str):
    """ตั้ง/บันทึกชื่อ session (เดิมตั้งผ่านหน้า setup — ย้ายมาตั้งบน dashboard)"""
    _config["session"] = name or ""
    set_session(name or "")
    save_config(_config)

# ── replay trigger ────────────────────────────────────────────────────────────

def _send_to_cloud(local_path: str, uid: str, court: str) -> bool:
    """POST คลิป + userId ไปแอปบน cloud (multipart) — คืน True ถ้าสำเร็จ"""
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
            return True
        print(f"[Cloud] ส่งไม่สำเร็จ {r.status_code}: {r.text[:200]}", flush=True)
        return False
    except Exception as e:
        print(f"[Cloud] error: {e}", flush=True)
        return False


def _upload_to_drive(local_path: str, court: str):
    import subprocess, datetime
    timestamp = datetime.datetime.now().strftime("%H%M%S")
    session   = get_session_name() or datetime.datetime.now().strftime("%Y-%m-%d")
    remote    = f"replay_kross:KROSS PADEL/{session}/court{court}/replay_{timestamp}.mp4"
    try:
        result = subprocess.run(["rclone", "copyto", local_path, remote], capture_output=True)
    except FileNotFoundError:
        print(f"[Drive] rclone ไม่พบ — ข้ามการอัพโหลด (คลิปอยู่ที่ {local_path})", flush=True)
        return
    if result.returncode == 0:
        print(f"[Drive] Uploaded → {remote}", flush=True)
    else:
        print(f"[Drive] Upload failed: {result.stderr.decode()[:200]}", flush=True)


def _do_replay(court: str = "", uid: str = ""):
    court = court or COURT_ID
    buf = buffers.get(court)
    if buf is None:
        print(f"[Trigger] คอร์ท {court} ยังไม่เริ่ม — ตั้งค่าก่อน", flush=True)
        return
    print(f"[Trigger] คอร์ท {court}: Extracting last 5s... (user={uid or '-'})", flush=True)
    try:
        clip = buf.extract_clip(duration=5, output_path=str(_replay_path(court)))
    except Exception as e:
        print(f"[Trigger] Error: {e}", flush=True)
        return
    if clip and _loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({"event": "replay_ready", "court": court,
                       "url": f"/replay?court={court}"}),
            _loop,
        )
        print(f"[Trigger] คอร์ท {court}: Done — broadcast sent", flush=True)
        if not (CLOUD_API_URL and _send_to_cloud(clip, uid, court)):
            _upload_to_drive(clip, court)
    else:
        print("[Trigger] extract_clip returned None", flush=True)

# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    global _loop, _config
    _loop = asyncio.get_running_loop()

    _config = load_config()
    set_session(_config.get("session", ""))

    set_trigger_callback(_do_replay)
    set_list_courts_callback(list_courts)
    set_upsert_callback(upsert_court)
    set_remove_callback(remove_court)
    set_save_session_callback(save_session)
    set_stop_callback(lambda court_id=None: (
        _stop_court(court_id) if court_id else (_stop_all(), os._exit(0))
    ))

    # auto-start ทุกคอร์ทจาก config (คอร์ทไหนเปิดไม่ได้ ข้าม ไม่ให้ล้มทั้งระบบ)
    for c in _config["courts"]:
        try:
            _start_court(c)
        except Exception as e:
            print(f"[System] คอร์ท {c.get('id')}: auto-start error: {e}", flush=True)

    print("=" * 50, flush=True)
    print("  Padel Replay System (multi-court)", flush=True)
    print(f"  Web    : http://localhost:8000", flush=True)
    print(f"  Courts : {[c['id'] for c in _config['courts']] or '(ยังไม่ตั้งค่า → /setup)'}", flush=True)
    print("=" * 50, flush=True)

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        _stop_all()


if __name__ == "__main__":
    asyncio.run(main())
