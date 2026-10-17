"""
=============================================================================
SOS AI SYSTEM - SINGLE CAMERA VIDEO SERVER (camera_server.py)
=============================================================================
Lightweight, ultra-low latency camera streaming microservice providing:
  - Hardware video acquisition (DirectShow / V4L2 / RTSP / HTTP)
  - Real-time digital Pan, Tilt, and Zoom (PTZ) via OpenCV
  - Tactical vision filter rendering (Optical Normal, Night Vision IR, Thermal FLIR)
  - GET /video_feed          -> Multipart MJPEG stream for web dashboard
  - GET /frame               -> Instant non-blocking raw JPEG binary for AI sensors
  - GET /snapshot            -> Base64 snapshot JSON payload
  - GET /status              -> Live hardware telemetry (FPS, Mbps bitrate, quality, PTZ)
  - POST/GET /api/camera/ptz -> PTZ control endpoint
  - POST/GET /api/camera/filter -> Vision filter control endpoint
  - POST/GET /api/camera/reset  -> Defaults reset endpoint
  - GET /                    -> In-browser surveillance testing dashboard
=============================================================================
"""

import os
import sys
import time
import base64
import atexit
import argparse
import threading
from contextlib import asynccontextmanager
from typing import Optional

# Ensure UTF-8 output on Windows terminal
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn


# =============================================================================
# CLI ARGUMENT PARSING
# =============================================================================

def parse_camera_source(source_val):
    s = str(source_val).strip()
    if s.isdigit():
        return int(s)
    try:
        return int(s)
    except ValueError:
        return s


parser = argparse.ArgumentParser(description="AEGIS Single Camera Video Server")
parser.add_argument("--port", type=int, default=8001, help="Port to run the camera server on (default: 8001)")
parser.add_argument("--source", type=str, default="0", help="Camera source (webcam index like 0, or URL, default: 0)")

cli_args, _ = parser.parse_known_args()

SERVER_HOST = "127.0.0.1"
SERVER_PORT = cli_args.port
CAMERA_SOURCE = parse_camera_source(cli_args.source)
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
JPEG_QUALITY = 75


# =============================================================================
# GLOBAL STATE & CONTROL VARIABLES
# =============================================================================

CAMERA_FILTER = "NORMAL"   # Options: NORMAL, NIGHT_VISION, THERMAL
ZOOM_LEVEL = 1.0           # Range: 1.0 to 5.0
PAN_X = 0                  # Pixel offset
TILT_Y = 0                 # Pixel offset

current_fps = 0.0
current_mbps = 0.0
video_quality = "720p HD"
frame_count = 0

latest_frame: Optional[np.ndarray] = None
latest_jpeg: Optional[bytes] = None
latest_b64: Optional[str] = None
latest_brightness: float = 0.0
latest_timestamp: Optional[float] = None

frame_lock = threading.Lock()
is_running = True
cap: Optional[cv2.VideoCapture] = None


# =============================================================================
# HARDWARE VIDEO CAPTURE HELPER
# =============================================================================

def create_video_capture(source):
    """Initializes cv2.VideoCapture with CAP_DSHOW on Windows for hardware webcams."""
    if isinstance(source, int):
        capture = cv2.VideoCapture(source, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture = cv2.VideoCapture(source)
        return capture
    else:
        return cv2.VideoCapture(str(source))


# =============================================================================
# OPENCV DIGITAL PTZ & VIDEO FILTER PROCESSING
# =============================================================================

def apply_ptz(frame: np.ndarray, zoom: float, pan: int, tilt: int) -> np.ndarray:
    """Applies digital Pan/Tilt/Zoom (PTZ) transformation using OpenCV."""
    if frame is None or frame.size == 0:
        return frame

    h, w = frame.shape[:2]
    zoom = max(1.0, float(zoom))

    if zoom <= 1.0 and pan == 0 and tilt == 0:
        return frame

    if zoom > 1.0:
        crop_w = int(w / zoom)
        crop_h = int(h / zoom)

        half_w = crop_w // 2
        half_h = crop_h // 2

        center_x = (w // 2) + int(pan)
        center_y = (h // 2) + int(tilt)

        center_x = max(half_w, min(w - (crop_w - half_w), center_x))
        center_y = max(half_h, min(h - (crop_h - half_h), center_y))

        x1 = max(0, center_x - half_w)
        y1 = max(0, center_y - half_h)
        x2 = min(w, x1 + crop_w)
        y2 = min(h, y1 + crop_h)

        cropped = frame[y1:y2, x1:x2]
        if cropped.size > 0:
            return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)
        return frame
    else:
        M = np.float32([[1, 0, -pan], [0, 1, -tilt]])
        return cv2.warpAffine(frame, M, (w, h), borderMode=cv2.BORDER_REPLICATE)


def apply_filter(frame: np.ndarray, filter_mode: str) -> np.ndarray:
    """Applies real-time tactical video filters (Night Vision, Thermal) using OpenCV."""
    if frame is None or frame.size == 0:
        return frame

    mode = str(filter_mode).upper().strip()

    if mode in ["NIGHT_VISION", "NV", "NIGHT", "IR"]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        enhanced = cv2.equalizeHist(gray)
        nv = np.zeros_like(frame)
        nv[:, :, 1] = enhanced
        nv[:, :, 0] = (enhanced * 0.15).astype(np.uint8)
        nv[:, :, 2] = (enhanced * 0.05).astype(np.uint8)
        return nv

    elif mode in ["THERMAL", "FLIR", "HEAT", "IRONBOW"]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        enhanced = cv2.equalizeHist(gray)
        return cv2.applyColorMap(enhanced, cv2.COLORMAP_INFERNO)

    return frame


# =============================================================================
# CONTINUOUS HARDWARE CAPTURE LOOP
# =============================================================================

def capture_loop():
    """Background thread continuously fetching frames, applying PTZ/filters, and caching JPEG."""
    global cap, latest_frame, latest_jpeg, latest_b64, latest_brightness
    global latest_timestamp, current_fps, current_mbps, frame_count, is_running

    print(f"\n[CAMERA SERVER] Initializing video capture (Source: {CAMERA_SOURCE})...")
    cap = create_video_capture(CAMERA_SOURCE)

    if cap and cap.isOpened():
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        print(f"[CAMERA SERVER] Hardware camera connected successfully.\n")
    else:
        print(f"[CAMERA SERVER WARNING] Failed to connect to camera source: {CAMERA_SOURCE}. Will retry in background...\n")

    frame_timestamps = []

    while is_running:
        if cap is None or not cap.isOpened():
            time.sleep(2.0)
            try:
                cap = create_video_capture(CAMERA_SOURCE)
                if cap and cap.isOpened():
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    cap.set(cv2.CAP_PROP_FPS, 30)
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
                    print(f"[CAMERA SERVER] Camera reconnected.")
            except Exception:
                pass
            continue

        try:
            ret, raw_frame = cap.read()
        except Exception:
            ret, raw_frame = False, None

        if not ret or raw_frame is None or raw_frame.size == 0:
            time.sleep(0.03)
            continue

        # Horizontally flip frame to fix left-right mirror reversal
        raw_frame = cv2.flip(raw_frame, 1)

        now = time.time()
        brightness = float(cv2.mean(raw_frame)[0])

        # Moving window timestamp-based FPS counter (last 10 frames)
        frame_timestamps.append(now)
        if len(frame_timestamps) > 10:
            frame_timestamps.pop(0)

        calc_fps = 0.0
        if len(frame_timestamps) >= 2:
            time_window = frame_timestamps[-1] - frame_timestamps[0]
            if time_window > 0:
                calc_fps = round((len(frame_timestamps) - 1) / time_window, 1)

        # Snapshot active PTZ & filter settings
        with frame_lock:
            cur_zoom = ZOOM_LEVEL
            cur_pan = PAN_X
            cur_tilt = TILT_Y
            cur_filter = CAMERA_FILTER

        # Apply PTZ and Filters
        processed = apply_ptz(raw_frame, cur_zoom, cur_pan, cur_tilt)
        processed = apply_filter(processed, cur_filter)

        # Encode to JPEG
        ret_enc, buffer = cv2.imencode('.jpg', processed, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ret_enc or buffer is None:
            continue

        # Bandwidth calculation: (bytes * fps * 8) / 1_000_000
        frame_bytes = len(buffer)
        calc_mbps = round((frame_bytes * calc_fps * 8) / 1_000_000, 2)
        jpeg_bytes = buffer.tobytes()
        b64_str = base64.b64encode(buffer).decode('utf-8')

        with frame_lock:
            latest_frame = processed
            latest_jpeg = jpeg_bytes
            latest_b64 = b64_str
            latest_brightness = brightness
            latest_timestamp = now
            current_fps = calc_fps
            current_mbps = calc_mbps
            frame_count += 1


# =============================================================================
# FASTAPI APPLICATION SETUP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start acquisition worker thread on startup
    t = threading.Thread(target=capture_loop, daemon=True, name="SingleCameraCapture")
    t.start()
    yield
    # Shutdown
    global is_running
    is_running = False
    if cap is not None and cap.isOpened():
        try:
            cap.release()
        except Exception:
            pass


app = FastAPI(
    title="AEGIS Camera Video Server",
    description="Single-stream low latency video feed microservice for dashboard and AI sensors.",
    version="3.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@atexit.register
def cleanup():
    global is_running
    is_running = False
    if cap is not None and cap.isOpened():
        try:
            cap.release()
        except Exception:
            pass


# =============================================================================
# MJPEG STREAMING GENERATOR
# =============================================================================

def mjpeg_generator():
    """Generates continuous multipart MJPEG stream for dashboard <img> src."""
    while is_running:
        with frame_lock:
            jpeg = latest_jpeg
        if jpeg is not None:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
        time.sleep(0.015)


# =============================================================================
# CORE STREAM & FRAME ENDPOINTS
# =============================================================================

@app.get("/video_feed", summary="MJPEG Video Stream")
def video_feed():
    """Returns continuous multipart MJPEG video stream."""
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/frame", summary="Instant Non-Blocking JPEG Frame")
def get_frame():
    """
    Returns the latest captured frame as raw image/jpeg binary.
    Instantly fetched by AI sensors (fall_detection, test_landmark_gesture).
    """
    with frame_lock:
        jpeg = latest_jpeg

    if jpeg is None:
        raise HTTPException(status_code=503, detail="Camera initializing or frame not available yet")

    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


@app.get("/snapshot", summary="Latest Frame as Base64 JSON")
def get_snapshot():
    """Returns latest snapshot pre-encoded in base64 with metadata."""
    with frame_lock:
        b64 = latest_b64
        brightness = latest_brightness
        ts = latest_timestamp
        num = frame_count
        filt = CAMERA_FILTER
        zoom = ZOOM_LEVEL
        pan = PAN_X
        tilt = TILT_Y

    if b64 is None:
        raise HTTPException(status_code=503, detail="Frame not ready yet")

    return {
        "status": "success",
        "snapshot_b64": b64,
        "brightness": round(brightness, 2),
        "timestamp": ts,
        "frame_number": num,
        "filter": filt,
        "zoom": zoom,
        "pan": pan,
        "tilt": tilt
    }


@app.get("/status", summary="Sensor & Stream Telemetry")
def get_status():
    """Returns real-time hardware telemetry (FPS, Mbps, quality, PTZ coordinates)."""
    now = time.time()
    with frame_lock:
        is_online = bool(cap and cap.isOpened() and latest_timestamp and (now - latest_timestamp < 4.0))
        return {
            "status": "ONLINE" if is_online else "OFFLINE",
            "source": str(CAMERA_SOURCE),
            "port": SERVER_PORT,
            "fps": current_fps,
            "mbps": current_mbps,
            "quality": video_quality,
            "brightness": round(latest_brightness, 2),
            "frame_count": frame_count,
            "last_capture_age_sec": round(now - latest_timestamp, 3) if latest_timestamp else None,
            "resolution": f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
            "filter": CAMERA_FILTER,
            "zoom": ZOOM_LEVEL,
            "pan": PAN_X,
            "tilt": TILT_Y
        }


# =============================================================================
# DIGITAL PTZ & FILTER CONTROL API ENDPOINTS
# =============================================================================

class PTZRequest(BaseModel):
    zoom: Optional[float] = None
    pan: Optional[int] = None
    tilt: Optional[int] = None
    zoom_delta: Optional[float] = None
    pan_delta: Optional[int] = None
    tilt_delta: Optional[int] = None


class FilterRequest(BaseModel):
    filter: Optional[str] = None
    mode: Optional[str] = None


@app.api_route("/api/camera/ptz", methods=["GET", "POST"], summary="Control Pan, Tilt, and Zoom")
@app.api_route("/ptz", methods=["GET", "POST"])
def set_ptz(
    zoom: Optional[float] = None,
    pan: Optional[int] = None,
    tilt: Optional[int] = None,
    zoom_delta: Optional[float] = None,
    pan_delta: Optional[int] = None,
    tilt_delta: Optional[int] = None,
    body: Optional[PTZRequest] = None
):
    """Updates digital Pan, Tilt, and Zoom settings."""
    global ZOOM_LEVEL, PAN_X, TILT_Y

    if body:
        if body.zoom is not None: zoom = body.zoom
        if body.pan is not None: pan = body.pan
        if body.tilt is not None: tilt = body.tilt
        if body.zoom_delta is not None: zoom_delta = body.zoom_delta
        if body.pan_delta is not None: pan_delta = body.pan_delta
        if body.tilt_delta is not None: tilt_delta = body.tilt_delta

    with frame_lock:
        if zoom is not None:
            ZOOM_LEVEL = round(max(1.0, min(5.0, float(zoom))), 2)
        elif zoom_delta is not None:
            ZOOM_LEVEL = round(max(1.0, min(5.0, ZOOM_LEVEL + float(zoom_delta))), 2)

        if pan is not None:
            PAN_X = int(pan)
        elif pan_delta is not None:
            PAN_X += int(pan_delta)

        if tilt is not None:
            TILT_Y = int(tilt)
        elif tilt_delta is not None:
            TILT_Y += int(tilt_delta)

        cur_zoom = ZOOM_LEVEL
        cur_pan = PAN_X
        cur_tilt = TILT_Y

    return {
        "status": "success",
        "zoom": cur_zoom,
        "pan": cur_pan,
        "tilt": cur_tilt
    }


@app.api_route("/api/camera/filter", methods=["GET", "POST"], summary="Control Video Filter Mode")
@app.api_route("/filter", methods=["GET", "POST"])
def set_filter(
    filter: Optional[str] = None,
    mode: Optional[str] = None,
    body: Optional[FilterRequest] = None
):
    """Sets the active video filter (NORMAL, NIGHT_VISION, THERMAL)."""
    global CAMERA_FILTER

    target = None
    if body:
        target = body.filter or body.mode
    if not target:
        target = filter or mode

    new_filter = "NORMAL"
    if target:
        norm = str(target).upper().strip()
        if norm in ["NIGHT_VISION", "NV", "NIGHT", "IR"]:
            new_filter = "NIGHT_VISION"
        elif norm in ["THERMAL", "FLIR", "HEAT", "IRONBOW"]:
            new_filter = "THERMAL"

    with frame_lock:
        CAMERA_FILTER = new_filter

    return {
        "status": "success",
        "filter": new_filter
    }


@app.api_route("/api/camera/reset", methods=["GET", "POST"], summary="Reset PTZ & Filter to Defaults")
@app.api_route("/reset", methods=["GET", "POST"])
def reset_camera():
    """Resets Pan, Tilt, Zoom, and Video Filter back to initial optical settings."""
    global CAMERA_FILTER, ZOOM_LEVEL, PAN_X, TILT_Y

    with frame_lock:
        CAMERA_FILTER = "NORMAL"
        ZOOM_LEVEL = 1.0
        PAN_X = 0
        TILT_Y = 0

    return {
        "status": "success",
        "message": "Camera PTZ and filters reset to defaults",
        "zoom": 1.0,
        "pan": 0,
        "tilt": 0,
        "filter": "NORMAL"
    }


# =============================================================================
# OPERATIONAL DASHBOARD
# =============================================================================

@app.get("/", response_class=HTMLResponse, summary="Monitor Dashboard")
def dashboard():
    """Live interactive single camera operational dashboard with digital PTZ & Filter controls."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AEGIS Camera Server // Live Feed</title>
    <style>
        :root {{
            --bg-dark: #070b14;
            --panel-bg: rgba(13, 21, 38, 0.85);
            --border: rgba(30, 41, 59, 0.9);
            --cyan: #38bdf8;
            --accent: #ff0055;
            --green: #22c55e;
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
        body {{
            background: var(--bg-dark);
            color: var(--text-main);
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 24px;
            min-height: 100vh;
        }}
        .header {{ text-align: center; margin-bottom: 20px; }}
        .header h1 {{ font-size: 22px; color: var(--cyan); letter-spacing: 0.15em; margin-bottom: 6px; }}
        .header p {{ font-size: 13px; color: var(--text-muted); }}

        .monitor-card {{
            width: 100%;
            max-width: 960px;
            background: var(--panel-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 20px 40px rgba(0,0,0,0.8);
        }}
        .stream-view {{
            position: relative;
            width: 100%;
            aspect-ratio: 16/9;
            background: #000;
        }}
        .stream-view img {{
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }}
        .telemetry-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 18px;
            background: rgba(10, 16, 28, 0.95);
            border-top: 1px solid var(--border);
            font-size: 13px;
        }}
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 8px;
            background: rgba(34, 197, 94, 0.1);
            color: var(--green);
            border: 1px solid rgba(34, 197, 94, 0.3);
            border-radius: 4px;
            font-weight: bold;
        }}
        .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--green); }}

        .controls-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            padding: 18px;
            border-top: 1px solid var(--border);
            background: rgba(15, 23, 42, 0.5);
        }}
        .control-group {{
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}
        .control-title {{
            font-size: 11px;
            color: var(--text-muted);
            letter-spacing: 0.1em;
            text-transform: uppercase;
        }}
        .btn-row {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        button {{
            background: #1e293b;
            color: #f1f5f9;
            border: 1px solid #334155;
            padding: 8px 14px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.2s ease;
        }}
        button:hover {{ background: #334155; border-color: var(--cyan); color: var(--cyan); }}
        button.active {{ background: rgba(56, 189, 248, 0.2); border-color: var(--cyan); color: var(--cyan); font-weight: bold; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>AEGIS SINGLE CAMERA VIDEO SERVER</h1>
        <p>Direct Hardware Stream & Telemetry Interface // Port {SERVER_PORT}</p>
    </div>

    <div class="monitor-card">
        <div class="stream-view">
            <img id="live-feed" src="/video_feed" alt="AEGIS Live Camera Stream">
        </div>

        <div class="telemetry-bar">
            <div>
                <span class="badge"><span class="dot"></span> LIVE OPTICAL</span>
                <span style="margin-left: 12px; color: var(--text-muted);">SOURCE: <strong style="color: #fff;">{CAMERA_SOURCE}</strong></span>
            </div>
            <div style="display: flex; gap: 16px;">
                <span>FPS: <strong id="val-fps" style="color: var(--cyan);">0.0</strong></span>
                <span>BITRATE: <strong id="val-mbps" style="color: var(--cyan);">0.00 Mbps</strong></span>
                <span>PAN: <strong id="val-pan">0.0°</strong></span>
                <span>TILT: <strong id="val-tilt">0.0°</strong></span>
                <span>ZOOM: <strong id="val-zoom">1.0X</strong></span>
            </div>
        </div>

        <div class="controls-grid">
            <div class="control-group">
                <span class="control-title">Tactical Vision Modes</span>
                <div class="btn-row">
                    <button class="active" onclick="setFilter('NORMAL')">NORMAL</button>
                    <button onclick="setFilter('NIGHT_VISION')">NIGHT VISION (IR)</button>
                    <button onclick="setFilter('THERMAL')">THERMAL (FLIR)</button>
                </div>
            </div>

            <div class="control-group">
                <span class="control-title">Digital PTZ Controls</span>
                <div class="btn-row">
                    <button onclick="sendPtz({{pan_delta: -20}})">◀ PAN L</button>
                    <button onclick="sendPtz({{tilt_delta: -20}})">▲ TILT U</button>
                    <button onclick="sendPtz({{tilt_delta: 20}})">▼ TILT D</button>
                    <button onclick="sendPtz({{pan_delta: 20}})">PAN R ▶</button>
                    <button onclick="sendPtz({{zoom_delta: 0.2}})">ZOOM +</button>
                    <button onclick="sendPtz({{zoom_delta: -0.2}})">ZOOM −</button>
                    <button style="border-color: var(--accent); color: var(--accent);" onclick="resetPtz()">RESET</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        async function fetchTelemetry() {{
            try {{
                const res = await fetch('/status');
                if (!res.ok) return;
                const d = await res.json();
                document.getElementById('val-fps').innerText = Number(d.fps || 0).toFixed(1);
                document.getElementById('val-mbps').innerText = Number(d.mbps || 0).toFixed(2) + ' Mbps';
                document.getElementById('val-pan').innerText = d.pan + 'px';
                document.getElementById('val-tilt').innerText = d.tilt + 'px';
                document.getElementById('val-zoom').innerText = Number(d.zoom || 1).toFixed(1) + 'X';
            }} catch(e) {{}}
        }}
        setInterval(fetchTelemetry, 1000);

        async function setFilter(mode) {{
            await fetch('/api/camera/filter', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{filter: mode}})
            }});
            document.querySelectorAll('.control-group button').forEach(b => {{
                if (['NORMAL', 'NIGHT_VISION', 'THERMAL'].includes(b.innerText)) {{
                    b.classList.toggle('active', b.innerText === mode);
                }}
            }});
        }}

        async function sendPtz(payload) {{
            await fetch('/api/camera/ptz', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(payload)
            }});
            fetchTelemetry();
        }}

        async function resetPtz() {{
            await fetch('/api/camera/reset', {{method: 'POST'}});
            fetchTelemetry();
        }}
    </script>
</body>
</html>
"""


# =============================================================================
# SERVER ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    print("=" * 65)
    print(f"AEGIS Single Camera Video Server starting on http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"Camera Source: {CAMERA_SOURCE} | Quality: {JPEG_QUALITY}% | Target: 30 FPS")
    print(f"Direct Browser URL: http://localhost:{SERVER_PORT}")
    print(f"Stream URL:        http://localhost:{SERVER_PORT}/video_feed")
    print(f"Sensor Frame API:  http://localhost:{SERVER_PORT}/frame")
    print(f"Telemetry API:     http://localhost:{SERVER_PORT}/status")
    print("Press Ctrl+C to stop.")
    print("=" * 65 + "\n")

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
