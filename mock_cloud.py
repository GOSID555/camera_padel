"""
Mock Cloud Server — ใช้ทดสอบว่าคลิปถูกส่งจากกล้องมาถึงจริงไหม

รันแยกอีก terminal:
    .venv/bin/python mock_cloud.py

แล้วรันกล้องโดยชี้ CLOUD_API_URL มาที่นี่:
    CLOUD_API_URL="http://127.0.0.1:9000/clips" camera-start

ทุกครั้งที่กด REPLAY คลิปจะถูกเซฟลงโฟลเดอร์ received_clips/
ชื่อไฟล์ติด userId เพื่อพิสูจน์ว่าแยกตาม user ได้จริง — เปิดไฟล์เล่นดูได้เลย
"""
import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, UploadFile, File, Form

app = FastAPI()

SAVE_DIR = Path(__file__).parent / "received_clips"
SAVE_DIR.mkdir(exist_ok=True)


@app.post("/clips")
async def receive_clip(
    file: UploadFile = File(...),
    userId: str = Form(""),
    court: str = Form(""),
    capturedAt: str = Form(""),
):
    data = await file.read()
    ts = datetime.datetime.now().strftime("%H%M%S")
    safe_user = (userId or "anon").replace("/", "_")
    out = SAVE_DIR / f"{safe_user}_court{court or '-'}_{ts}.mp4"
    out.write_bytes(data)

    print("\n" + "─" * 50, flush=True)
    print(f"✅  รับคลิปแล้ว", flush=True)
    print(f"    user   : {userId or '(ไม่ระบุ)'}", flush=True)
    print(f"    court  : {court or '(ไม่ระบุ)'}", flush=True)
    print(f"    ขนาด   : {len(data):,} bytes", flush=True)
    print(f"    เซฟที่ : {out}", flush=True)
    print("─" * 50, flush=True)

    return {"ok": True, "userId": userId, "bytes": len(data), "saved": out.name}


if __name__ == "__main__":
    print("🌩  Mock Cloud พร้อมรับคลิปที่ http://127.0.0.1:9000/clips")
    print(f"   คลิปที่ได้รับจะเซฟไว้ที่: {SAVE_DIR}/")
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="error")
