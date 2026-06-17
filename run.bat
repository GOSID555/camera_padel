@echo off
REM ── Padel Replay — start camera system ──────────────────────────────
REM ดับเบิลคลิกไฟล์นี้ หรือพิมพ์  run  ใน terminal เพื่อรันกล้อง
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] ไม่พบ .venv — ติดตั้งก่อนด้วย:  python -m venv .venv  แล้ว  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

echo ====================================================
echo   Padel Replay — starting...
echo   Web: http://localhost:8000
echo   ปิด: กด Ctrl+C ในหน้าต่างนี้
echo ====================================================
.venv\Scripts\python.exe main.py

REM ถ้า main.py ตาย/ปิด ให้ค้างหน้าต่างไว้จะได้อ่าน error
pause
