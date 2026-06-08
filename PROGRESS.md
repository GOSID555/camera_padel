# Progress Log

บันทึกความคืบหน้า/การแก้ไขระบบกล้อง instant-replay (Padel)

---

## 2026-06-08

### 🔍 วินิจฉัยปัญหาเครื่องรีบูต
- ตรวจ Windows Event Log → **Event 41 (Kernel-Power), BugcheckCode=0, ไม่มี BSOD/WHEA** = ไฟตัดระดับฮาร์ดแวร์ (thermal/power) ไม่ใช่ซอฟต์แวร์ crash
- สรุป: การย้ายไป NVENC ก่อนหน้านี้ช่วยได้แต่ไม่หายขาด → ต้องลดโหลดเพิ่ม

### 🎥 ลดโหลด preview (MJPEG → HLS ในเบราว์เซอร์)
- ถอด MJPEG output ตัวที่ 2 ที่รันตลอดเวลาต่อคอร์ทออก (กิน CPU ตลอด แม้ไม่มีคนดู)
- dashboard เล่น HLS ตรงๆ ผ่าน hls.js → **server ไม่ต้อง encode preview เลย**
- ไฟล์: `capture.py` (เหลือ output เดียว), `server.py` (เพิ่ม `/hls/<court>/<file>`, `/hls.min.js`, ถอด `/ws/preview` + frame hub), `main.py` (เลิก on_frame), `web/dashboard.html` (`<img>`+WS → `<video>`+hls.js), เพิ่ม `web/hls.min.js`

### ⚡ ปรับ pipeline 4K → โหลดเกือบเป็นศูนย์
- พบว่ากล้อง VIGI C385 สตรีมจริงเป็น **4K (3840×2160@25fps)** ไม่ใช่ 1080p → เดิม decode 4K + scale ลง CPU
- เปลี่ยนเป็น **สาย GPU เต็ม** (เฉพาะ RTSP+NVENC): `-hwaccel_output_format cuda` + `scale_cuda` → NVDEC→scale_cuda→NVENC
- ผลวัดจริง: คอร์ท 4K = **0.2% CPU**, webcam 720p = 0.6% (รวม <1% จาก 16 cores)
- ไฟล์: `capture.py` (เพิ่ม `_use_cuda_frames()`, scale_cuda แบบมีเงื่อนไข)

### 📡 ฟอร์มเพิ่มกล้องแบบ VIGI + Verify
- ฟอร์มประกอบ RTSP URL อัตโนมัติ: เลือกยี่ห้อ (VIGI/Hikvision/Dahua/custom) + Main/Sub + IP/user/pass → สร้าง URL ให้เอง
- ปุ่ม 🔍 หากล้องในเครือข่าย (ไล่สแกนพอร์ต 554) เติม IP ให้
- ปุ่ม ✓ ทดสอบการเชื่อมต่อ (ffprobe จริง → บอกความละเอียด/codec, รหัสผิดบอก 401)
- ไฟล์: `server.py` (เพิ่ม `/verify-rtsp` + `_probe_rtsp`), `web/dashboard.html` (builder + scan + verify)
- _หมายเหตุ: เคยลองเพิ่มสแกน ONVIF ใน dashboard ก่อน แต่พบว่า VIGI ใช้บัญชี ONVIF แยก/เมนูไม่มีในเว็บกล้อง → เปลี่ยนมาใช้ builder แทน_

### 🧩 รวม UI จัดการกล้องไว้ที่ dashboard ที่เดียว
- เลิกใช้หน้า setup wizard แยก (เมนูคนละแบบ น่าสับสน)
- `/` และ `/setup` → redirect `/dashboard` เสมอ
- empty state → ปุ่ม "+ เพิ่มกล้อง" เปิด editor ตรงๆ
- ย้ายการตั้ง **session name** มาอยู่ในปุ่มตั้งค่าบน dashboard
- ไฟล์: `server.py` (route redirect + `GET/POST /session`), `main.py` (`save_session()` + callback), `web/dashboard.html` (empty state, ช่อง session)
- `setup.html` ยังอยู่ในโฟลเดอร์แต่เข้าไม่ถึงแล้ว (route redirect ทิ้ง) — ไม่ลบเผื่อย้อนดู

### 🛟 กู้ข้อมูล + ข้อควรระวัง
- คอร์ท `a3` หายจาก `courts.json` (มี DELETE ถูกเรียกตอน dashboard ตัวเก่าค้าง) → คืนกลับ + ทำ `courts.json.bak`
- พบ: `kill` ใน bash บน Windows ไม่ฆ่า ffmpeg ลูกของ python → ต้องใช้ `Get-Process python,ffmpeg | Stop-Process -Force`

### สรุป
- **ไฟล์ที่แก้:** `capture.py`, `server.py`, `main.py`, `web/dashboard.html` + เพิ่ม `web/hls.min.js`
- **สถานะ:** ทดสอบผ่านทั้งหมด ✅ ยังไม่ commit

### TODO / ค้างต่อ
- [ ] ลองหน้าจริงในเบราว์เซอร์ แล้ว commit
- [ ] (ทางเลือก) ตั้ง main stream ของ VIGI ในกล้องให้เป็น 1080p เพื่อลดงาน NVDEC อีก
- [ ] เฝ้าดูว่ายังรีบูตอีกไหมหลังลดโหลด — ถ้ายัง พิจารณา cap CPU max state ~80% / ทำความสะอาดพัดลม / charger 180W

---

## English Summary — 2026-06-08

**1. Diagnosed reboots** — Windows Event Log showed Kernel-Power Event 41, BugcheckCode=0, no BSOD/WHEA → a **hardware power/thermal cut**, not a software crash. NVENC alone didn't fix it; needed to cut load further.

**2. Preview load removed (MJPEG → browser HLS)** — Dropped the always-on per-court MJPEG output; dashboard now plays HLS directly via hls.js, so the **server does zero preview encoding**.
_Files:_ `capture.py`, `server.py` (added `/hls/...`, `/hls.min.js`; removed `/ws/preview`), `main.py`, `dashboard.html`, +`hls.min.js`

**3. 4K pipeline → near-zero CPU** — Camera (VIGI C385) actually streams **4K@25fps**. Switched RTSP+NVENC to a full-GPU path (`-hwaccel_output_format cuda` + `scale_cuda`). Measured: 4K court **0.2% CPU**, webcam 0.6% (<1% of 16 cores).
_Files:_ `capture.py`

**4. VIGI-style add-camera form + Verify** — Auto-builds RTSP URL by brand (VIGI/Hikvision/Dahua/custom) + Main/Sub; network IP scan; "Verify" button runs real ffprobe.
_Files:_ `server.py` (`/verify-rtsp`), `dashboard.html`

**5. Unified UI on dashboard** — Retired the separate setup wizard; `/` and `/setup` redirect to `/dashboard`; empty state opens the editor; session-name moved into dashboard settings.
_Files:_ `server.py` (`GET/POST /session`), `main.py`, `dashboard.html`

**6. Data recovery / gotchas** — Restored court `a3` after a stray DELETE wiped it (+`courts.json.bak`). Note: bash `kill` doesn't kill python's ffmpeg children on Windows — use `Get-Process python,ffmpeg | Stop-Process -Force`.

**Files changed:** `capture.py`, `server.py`, `main.py`, `web/dashboard.html`, +`web/hls.min.js` · **Status:** all tested ✅, not committed yet.
