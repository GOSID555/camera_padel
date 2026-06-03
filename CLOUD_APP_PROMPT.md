# Prompt สำหรับสร้างแอป Cloud (รับคลิป + ให้ลูกค้าดึงไปดู)

> วางข้อความด้านล่างนี้ให้ AI / dev เพื่อสร้างฝั่ง cloud ที่เชื่อมกับระบบกล้อง Padel

---

## บริบท

ผมมีระบบกล้อง instant-replay ที่สนาม Padel (รันบน Mac mini ที่สนาม)
เมื่อผู้เล่นกดปุ่ม REPLAY บนมือถือ ระบบจะตัดคลิป 5 วินาทีล่าสุด แล้ว **POST ไฟล์ขึ้น cloud พร้อม userId ของคนกด**

ผมอยากให้คุณสร้าง **backend API บน cloud** ที่:
1. รับคลิปจากกล้อง (ingest)
2. เก็บคลิปแยกตาม user
3. ให้แอปมือถือของลูกค้าดึงคลิปของตัวเองไปดู (highlight feed)

---

## 1. Endpoint รับคลิปจากกล้อง (ingest) — สเปกตายตัว ห้ามเปลี่ยน

กล้องจะยิงมาแบบนี้เป๊ะๆ (แก้ฝั่งกล้องยาก ให้ cloud รับตามนี้):

```
POST /clips
Content-Type: multipart/form-data
Authorization: Bearer <DEVICE_API_KEY>

form fields:
  file        : ไฟล์วิดีโอ replay.mp4  (Content-Type: video/mp4, ~1 MB)
  userId      : string  เช่น "USER123"   (ใครเป็นคนกดปุ่ม)
  court       : string  เช่น "A"         (คอร์ทไหน)
  capturedAt  : string  ISO-8601 เช่น "2026-06-03T20:23:09"
```

ต้องตอบกลับ:
- สำเร็จ → HTTP 2xx + JSON `{ "ok": true, "clipId": "<id>" }`
- key ผิด → 401
- error → 4xx/5xx (กล้องจะ log ไว้)

**Auth:** ตรวจ `Authorization: Bearer` ให้ตรงกับ device key ที่ตั้งไว้ (เครื่องกล้องแต่ละสนามมี key ของตัวเอง)

---

## 2. เก็บคลิป

- อัปไฟล์ขึ้น object storage (S3 / Cloudflare R2 / GCS) — อย่าเก็บใน DB
- บันทึก metadata ลง DB หนึ่ง record ต่อคลิป:
  ```
  clipId, userId, court, capturedAt, videoUrl, sizeBytes, createdAt
  ```

---

## 3. Endpoint ให้แอปลูกค้าดึงคลิป

แอปมือถือของลูกค้า (login เป็น user แล้ว) ต้องดึง **เฉพาะคลิปของตัวเอง**:

```
GET /me/clips
Authorization: Bearer <USER_TOKEN>
→ 200  [ { clipId, court, capturedAt, videoUrl, thumbnailUrl }, ... ]
        เรียงใหม่→เก่า

GET /clips/{clipId}
Authorization: Bearer <USER_TOKEN>
→ 200  { clipId, userId, court, capturedAt, videoUrl }
→ 403  ถ้าไม่ใช่คลิปของ user คนนี้

DELETE /clips/{clipId}      (ลูกค้าลบคลิปตัวเองได้)
Authorization: Bearer <USER_TOKEN>
```

- `videoUrl` ควรเป็น signed URL หมดอายุได้ (กันคนอื่นเดา URL)
- ถ้ามีเวลา ทำ thumbnail (เฟรมแรกของคลิป) ด้วย

---

## 4. การยืนยันตัวตน (สำคัญ)

ตอนนี้กล้องส่ง `userId` ดิบมาตรงๆ ซึ่งใครก็ปลอมได้ ผมอยากให้ปลอดภัยขึ้น:

**แนะนำ flow:**
1. ลูกค้าจองคอร์ทในแอป → แอปสร้าง **session token ชั่วคราว** ผูกกับ (userId, court, ช่วงเวลาที่จอง)
2. แอปเปิดหน้า operator ของกล้องโดยฝัง token นั้นใน URL แทน userId ดิบ
   เช่น `https://court-server/operator?token=<SHORT_LIVED_TOKEN>&court=A`
3. กล้องส่ง token นี้ขึ้น cloud → **cloud เป็นคน verify** ว่า token ยังไม่หมดอายุ + map กลับเป็น userId จริง

> ถ้าทำขั้นนี้ บอกผมด้วย จะได้แก้ฝั่งกล้องให้ส่ง `token` แทน `userId`
> (ตอนนี้ field ชื่อ `userId` — เปลี่ยนเป็น `token` ได้)

---

## 5. เทคที่แนะนำ

- **Backend:** อะไรก็ได้ที่ถนัด (Node/Express, FastAPI, Go)
- **Storage:** Cloudflare R2 หรือ AWS S3
- **DB:** Postgres หรือ SQLite (เริ่มเล็กๆ)
- **Deploy:** Railway / Render / Fly.io (มี HTTPS public URL ให้กล้องยิงถึง)

---

## สิ่งที่ผมต้องการกลับมา

1. URL ของ ingest endpoint (`CLOUD_API_URL`) + device key (`CLOUD_API_KEY`) — เอาไปตั้งที่กล้อง
2. ตัวอย่าง response ของ `GET /me/clips` — เอาไปต่อแอปมือถือ
3. บอกด้วยว่าจะใช้ `userId` ดิบ หรือเปลี่ยนเป็น `token` (ข้อ 4)
