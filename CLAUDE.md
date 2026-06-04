# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

ระบบกล้อง instant-replay สำหรับสนาม Padel — อัดวิดีโอต่อเนื่อง เมื่อผู้เล่นได้คะแนนกดปุ่ม ระบบตัดคลิป 20 วินาทีล่าสุดแล้วส่งตรงไปแสดงในมือถือหรือหน้าจอของลูกค้าทันที

**Prototype:** MacBook webcam + Space bar trigger + browser UI  
**Roadmap:** Space bar → ESP32 WiFi button | Webcam → IP camera | Browser → Mobile app

## Running the system

```bash
pip install -r requirements.txt
python main.py
```

The web UI is served at `http://localhost:8000`. Press **SPACE** on the terminal running `main.py` to trigger a replay clip of the last 20 seconds.

**Dependency:** `ffmpeg` must be installed and on `$PATH` (used for both recording and clip extraction).

### Windows

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

Install `ffmpeg` and add it to `PATH`. Camera capture uses DirectShow (`dshow`) — the setup wizard at `http://localhost:8000` lists cameras by **name** and works the same as on macOS.

### Mac mini M4 (USB webcam)

Mac mini has no built-in camera, so the USB webcam is likely not device `0`. Find the correct index first:

```bash
python main.py --list-devices
```

Look for the webcam name in the video device list (e.g. `[0] FaceTime HD Camera` or `[1] USB Camera`), then run with:

```bash
python main.py --device 1
```

## Architecture

This is a **Padel instant-replay system** built around a continuous record → rolling-buffer → on-demand clip pipeline:

```
Capture (ffmpeg avfoundation) → segments/seg_NNNNN.ts
                                        ↓
                              Buffer (rolling cleanup, max ~30s)
                                        ↓  SPACE key
                              extract_clip() → web/replay.mp4
                                        ↓
              FastAPI server ──────────────────→ browser via WebSocket
```

**`capture.py` — Capture**
Wraps an `ffmpeg` subprocess that records from the default webcam (`avfoundation` device `0`) into 2-second `.ts` segments under `segments/`. Uses `ultrafast` / `zerolatency` presets to minimise latency.

**`buffer.py` — Buffer**
Manages the rolling window of segments. A background thread prunes old segments every 2 s, keeping only the newest `max_segments` files (≈ 32 for a 30 s window). `extract_clip()` concatenates the last N completed segments (skipping the one still being written) via `ffmpeg -f concat`, outputting `web/replay.mp4`.

**`server.py` — FastAPI app**
Three endpoints:
- `GET /` — serves `web/index.html`
- `GET /replay` — serves `web/replay.mp4`
- `WS /ws` — pushes `{"event": "replay_ready", "url": "/replay"}` to all connected browser clients; also handles client keep-alive pings

**`main.py` — entry point**
Wires everything together in a single `asyncio` event loop. Keyboard events come in on a `pynput` listener thread; replay extraction runs in a separate daemon thread and posts back to the asyncio loop via `asyncio.run_coroutine_threadsafe`.

**`web/index.html`**
Single-page UI: connects to `/ws`, listens for `replay_ready`, then loads `/replay?t=<timestamp>` (cache-bust) into an HTML `<video>` element and auto-plays.

## Key constraints

- The last `.ts` segment is always skipped during clip extraction because `ffmpeg` may still be writing it.
- `web/replay.mp4` is overwritten in-place on every replay trigger; the browser cache-busts with a query param.
- **Cross-platform capture:** macOS uses `avfoundation` (device = index "0"); Windows uses `dshow` (device = camera **name**, e.g. `video=Integrated Camera`). Both `capture.py` and the camera endpoints in `server.py` (`/cameras`, `/camera-formats`, `/ws/preview`) branch on `sys.platform` / `platform.system()`. Linux (`v4l2 -i /dev/video0`) is stubbed in `capture.py` but the server-side camera enumeration/probe is not implemented for Linux.
  - On Windows the `device` passed to `/start`, `/preview`, `/camera-formats` is the camera **name**, not an index — `/cameras` returns `index == name` there.
