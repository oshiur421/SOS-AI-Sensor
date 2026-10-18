"""
=============================================================================
AEGIS EMERGENCY SOS SYSTEM - UNIFIED API GATEWAY & MASTER LAUNCHER (master.py)
=============================================================================
Primary API Gateway and microservice orchestrator for SOS-AI-Sensor.
Designed for cloud platforms with a single public port constraint (such as Render).

Key Features:
  1. Single Public Port:
     - Binds to $PORT (default 8000).
     - Exposes all sensor APIs under a unified root domain.

  2. Microservice Supervision:
     - Spawns and supervises all internal sensor microservices as subprocesses:
         * camera_server.py         -> Internal port 8001
         * fall_detection.py        -> Internal port 8002
         * voice_emergency.py       -> Internal port 8003
         * test_landmark_gesture.py -> Internal port 5000
     - Gracefully terminates all subprocesses on shutdown (SIGTERM / SIGKILL).

  3. Asynchronous Reverse Proxy (httpx):
     - /api/camera/* -> http://127.0.0.1:8001/api/camera/*
     - /video_feed   -> http://127.0.0.1:8001/video_feed (MJPEG streaming)
     - /status       -> http://127.0.0.1:8001/status
     - /frame        -> http://127.0.0.1:8001/frame
     - /snapshot     -> http://127.0.0.1:8001/snapshot
     - /api/voice/*  -> http://127.0.0.1:8003/api/voice/*
     - /api/fall/*   -> http://127.0.0.1:8002/api/fall/*
     - /api/gesture/*-> http://127.0.0.1:5000/api/gesture/*
     - /health, /    -> Gateway telemetry & internal service health check

  4. Full CORS Support:
     - Allows cross-origin browser requests from any frontend domain.
=============================================================================
"""

import os
import sys
import time
import argparse
import subprocess
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Configure console UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# PORT CONFIGURATIONS & INTERNAL SERVICE ENDPOINTS
# ---------------------------------------------------------------------------
GATEWAY_PORT = int(os.environ.get("PORT", 8000))
os.environ["GATEWAY_PORT"] = str(GATEWAY_PORT)
GATEWAY_HOST = os.environ.get("HOST", "0.0.0.0")
INPUT_SOURCE = os.environ.get("INPUT_SOURCE", "api").lower()

CAMERA_PORT = 8001 if GATEWAY_PORT != 8001 else 8101
FALL_PORT = 8002 if GATEWAY_PORT != 8002 else 8102
VOICE_PORT = 8003 if GATEWAY_PORT != 8003 else 8103
GESTURE_PORT = 5000 if GATEWAY_PORT != 5000 else 5100

CAMERA_SERVICE_URL = f"http://127.0.0.1:{CAMERA_PORT}"
FALL_SERVICE_URL = f"http://127.0.0.1:{FALL_PORT}"
VOICE_SERVICE_URL = f"http://127.0.0.1:{VOICE_PORT}"
GESTURE_SERVICE_URL = f"http://127.0.0.1:{GESTURE_PORT}"

# Global subprocesses & shared HTTP client
background_processes: List[subprocess.Popen] = []
http_client: Optional[httpx.AsyncClient] = None


# ---------------------------------------------------------------------------
# SUBPROCESS SUPERVISION
# ---------------------------------------------------------------------------
def start_internal_services():
    """Spawns the internal AI sensor microservices concurrently on local ports."""
    global background_processes

    env = os.environ.copy()
    env["INPUT_SOURCE"] = INPUT_SOURCE
    env["CAMERA_SERVER_URL"] = CAMERA_SERVICE_URL
    env["GATEWAY_PORT"] = str(GATEWAY_PORT)

    service_configs = [
        ("camera_server.py", [sys.executable, os.path.join(BASE_DIR, "camera_server.py"), "--port", str(CAMERA_PORT), "--input-source", INPUT_SOURCE]),
        ("fall_detection.py", [sys.executable, os.path.join(BASE_DIR, "fall_detection.py"), "--port", str(FALL_PORT), "--input-source", INPUT_SOURCE]),
        ("test_landmark_gesture.py", [sys.executable, os.path.join(BASE_DIR, "test_landmark_gesture.py"), "--port", str(GESTURE_PORT), "--input-source", INPUT_SOURCE]),
        ("voice_emergency.py", [sys.executable, os.path.join(BASE_DIR, "voice_emergency.py"), "--port", str(VOICE_PORT), "--input-source", INPUT_SOURCE])
    ]

    print("======================================================================")
    print("🚀 [GATEWAY] Initializing internal sensor microservices...")
    print("======================================================================")

    for name, cmd in service_configs:
        script_file = cmd[1]
        if not os.path.exists(script_file):
            print(f"[⚠️ WARNING] Service file '{name}' not found at {script_file}. Skipping.")
            continue

        port_str = cmd[cmd.index("--port") + 1]
        print(f"[*] Starting {name} (Port: {port_str}, Input Source: {INPUT_SOURCE.upper()})...")

        try:
            proc = subprocess.Popen(cmd, cwd=BASE_DIR, env=env)
            proc.service_name = name
            background_processes.append(proc)
            print(f"[✅ ACTIVE]   {name} spawned successfully (PID: {proc.pid})")
            if name == "camera_server.py":
                time.sleep(1.2)
            else:
                time.sleep(0.4)
        except Exception as err:
            print(f"[❌ ERROR]    Failed to spawn {name}: {err}")

    print("======================================================================\n")


def stop_internal_services():
    """Gracefully terminates all background sensor microservices."""
    global background_processes

    if not background_processes:
        return

    print("\n======================================================================")
    print("🛑 [GATEWAY] Terminating internal background microservices...")
    print("======================================================================")

    for proc in background_processes:
        name = getattr(proc, "service_name", f"PID {proc.pid}")
        if proc.poll() is None:
            print(f"[*] Stopping {name} (PID: {proc.pid})...")
            try:
                proc.terminate()
            except Exception:
                pass

    time.sleep(1.0)

    for proc in background_processes:
        name = getattr(proc, "service_name", f"PID {proc.pid}")
        if proc.poll() is None:
            print(f"[!] Forcefully killing {name} (PID: {proc.pid})...")
            try:
                proc.kill()
            except Exception:
                pass

    background_processes.clear()
    print("[✓] All internal services stopped.\n")


# ---------------------------------------------------------------------------
# FASTAPI LIFECYCLE MANAGEMENT
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))

    if os.environ.get("DISABLE_INTERNAL_SERVICES", "0") != "1":
        start_internal_services()

    yield

    if http_client:
        await http_client.aclose()
        http_client = None

    stop_internal_services()


# ---------------------------------------------------------------------------
# FASTAPI GATEWAY APPLICATION
# ---------------------------------------------------------------------------
app = FastAPI(
    title="AEGIS Emergency SOS - Unified Sensor API Gateway",
    description="Single-port public API Gateway routing camera, voice, fall, and gesture sensor microservices.",
    version="2.0.0",
    lifespan=lifespan
)

# Enable CORS for all external origins (GitHub Pages, local dev, remote frontends)
# Configured strictly to ensure all responses (including 503 errors) reach frontend without CORS blocks
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)


# ---------------------------------------------------------------------------
# GLOBAL EXCEPTION HANDLERS (503 SERVICE UNAVAILABLE WITH CORS)
# ---------------------------------------------------------------------------
@app.exception_handler(httpx.TimeoutException)
async def gateway_timeout_handler(request: Request, exc: httpx.TimeoutException):
    return JSONResponse(
        status_code=503,
        content={
            "status": "error",
            "error": "Service Unavailable",
            "message": "Internal microservice request timed out.",
            "detail": str(exc),
            "url": str(request.url)
        },
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.exception_handler(httpx.RequestError)
async def gateway_request_error_handler(request: Request, exc: httpx.RequestError):
    return JSONResponse(
        status_code=503,
        content={
            "status": "error",
            "error": "Service Unavailable",
            "message": "Internal microservice is unreachable, starting up, or crashed.",
            "detail": str(exc),
            "url": str(request.url)
        },
        headers={"Access-Control-Allow-Origin": "*"}
    )


# ---------------------------------------------------------------------------
# REVERSE PROXY HELPER
# ---------------------------------------------------------------------------
async def proxy_request(target_url: str, request: Request) -> Response:
    """Forwards incoming HTTP request to internal microservice and returns the response."""
    global http_client
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))

    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    try:
        upstream_resp = await http_client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
            timeout=30.0
        )

        resp_headers = dict(upstream_resp.headers)
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("content-length", None)
        resp_headers.pop("transfer-encoding", None)

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type")
        )
    except httpx.TimeoutException as exc:
        print(f"⏱️ [GATEWAY 503 TIMEOUT] Target {target_url} timed out: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "Service Unavailable",
                "message": f"Internal sensor service at {target_url} timed out.",
                "target": target_url,
                "detail": str(exc)
            },
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except httpx.RequestError as exc:
        print(f"⚠️ [GATEWAY 503 UNAVAILABLE] Target {target_url} unreachable: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "Service Unavailable",
                "message": f"Internal sensor service at {target_url} is unreachable, starting up, or crashed.",
                "target": target_url,
                "detail": str(exc)
            },
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as exc:
        print(f"❌ [GATEWAY 503 ERROR] Unexpected error forwarding to {target_url}: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "Service Unavailable",
                "message": f"Gateway encountered an error communicating with internal service at {target_url}.",
                "target": target_url,
                "detail": str(exc)
            },
            headers={"Access-Control-Allow-Origin": "*"}
        )


async def stream_mjpeg(target_url: str):
    """Streams continuous multipart MJPEG video from camera microservice."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(timeout=None, connect=5.0))
    try:
        req = client.build_request("GET", target_url)
        res = await client.send(req, stream=True)

        if res.status_code != 200:
            await res.aclose()
            await client.aclose()
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "error": "Service Unavailable",
                    "message": f"Camera microservice returned HTTP {res.status_code}",
                    "target": target_url
                },
                headers={"Access-Control-Allow-Origin": "*"}
            )

        async def body_stream():
            try:
                async for chunk in res.aiter_raw():
                    yield chunk
            except Exception as stream_err:
                print(f"⚠️ [STREAM INTERRUPTED] {stream_err}")
            finally:
                await res.aclose()
                await client.aclose()

        return StreamingResponse(
            body_stream(),
            status_code=res.status_code,
            media_type=res.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame"),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Connection": "close",
                "Access-Control-Allow-Origin": "*"
            }
        )
    except httpx.TimeoutException as exc:
        await client.aclose()
        print(f"⏱️ [STREAM 503 TIMEOUT] Camera stream at {target_url} timed out: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "Service Unavailable",
                "message": f"Camera video stream at {target_url} timed out.",
                "detail": str(exc)
            },
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except httpx.RequestError as exc:
        await client.aclose()
        print(f"⚠️ [STREAM 503 UNAVAILABLE] Camera stream at {target_url} unreachable: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "Service Unavailable",
                "message": f"Camera video stream at {target_url} is unreachable or offline.",
                "detail": str(exc)
            },
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as exc:
        await client.aclose()
        print(f"❌ [STREAM 503 ERROR] Unexpected error in camera stream: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "Service Unavailable",
                "message": "Camera video stream is currently unavailable.",
                "detail": str(exc)
            },
            headers={"Access-Control-Allow-Origin": "*"}
        )


# ---------------------------------------------------------------------------
# ROUTE MAPPINGS
# ---------------------------------------------------------------------------

# 1. Camera Server Routes (Port 8001)
@app.get("/video_feed", summary="MJPEG Video Feed Stream")
@app.get("/api/camera/video_feed", summary="MJPEG Video Feed Stream")
async def proxy_video_stream():
    return await stream_mjpeg(f"{CAMERA_SERVICE_URL}/video_feed")


@app.api_route("/api/camera/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], summary="Camera Service Routing")
async def route_camera(path: str, request: Request):
    if path == "video_feed":
        return await stream_mjpeg(f"{CAMERA_SERVICE_URL}/video_feed")
    if path == "status":
        return await proxy_request(f"{CAMERA_SERVICE_URL}/status", request)
    return await proxy_request(f"{CAMERA_SERVICE_URL}/api/camera/{path}", request)


# Camera direct shortcuts
@app.api_route("/status", methods=["GET"], summary="Camera Hardware Telemetry")
async def route_camera_status(request: Request):
    return await proxy_request(f"{CAMERA_SERVICE_URL}/status", request)


@app.api_route("/frame", methods=["GET"], summary="Instant Single Frame")
async def route_camera_frame(request: Request):
    return await proxy_request(f"{CAMERA_SERVICE_URL}/frame", request)


@app.api_route("/snapshot", methods=["GET"], summary="Snapshot JSON")
async def route_camera_snapshot(request: Request):
    return await proxy_request(f"{CAMERA_SERVICE_URL}/snapshot", request)


@app.api_route("/api/upload_frame", methods=["POST"])
@app.api_route("/upload_frame", methods=["POST"])
async def route_camera_upload_frame(request: Request):
    return await proxy_request(f"{CAMERA_SERVICE_URL}/api/upload_frame", request)


@app.api_route("/ptz", methods=["GET", "POST"])
async def route_camera_ptz_shortcut(request: Request):
    return await proxy_request(f"{CAMERA_SERVICE_URL}/ptz", request)


@app.api_route("/filter", methods=["GET", "POST"])
async def route_camera_filter_shortcut(request: Request):
    return await proxy_request(f"{CAMERA_SERVICE_URL}/filter", request)


@app.api_route("/reset", methods=["GET", "POST"])
async def route_camera_reset_shortcut(request: Request):
    return await proxy_request(f"{CAMERA_SERVICE_URL}/reset", request)


# 2. Voice Emergency Routes (Port 8003)
@app.api_route("/api/voice/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], summary="Voice Service Routing")
async def route_voice(path: str, request: Request):
    if path == "status":
        return await proxy_request(f"{VOICE_SERVICE_URL}/status", request)
    return await proxy_request(f"{VOICE_SERVICE_URL}/api/voice/{path}", request)


@app.api_route("/upload_audio", methods=["POST"])
async def route_voice_upload_audio(request: Request):
    return await proxy_request(f"{VOICE_SERVICE_URL}/upload_audio", request)


# 3. Fall Detection Routes (Port 8002)
@app.api_route("/api/fall/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], summary="Fall Detection Service Routing")
async def route_fall(path: str, request: Request):
    if path == "status":
        return await proxy_request(f"{FALL_SERVICE_URL}/status", request)
    return await proxy_request(f"{FALL_SERVICE_URL}/api/fall/{path}", request)


# 4. Hand Gesture Landmark Routes (Port 5000)
@app.api_route("/api/gesture/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], summary="Gesture Recognition Service Routing")
async def route_gesture(path: str, request: Request):
    if path == "status":
        return await proxy_request(f"{GESTURE_SERVICE_URL}/status", request)
    return await proxy_request(f"{GESTURE_SERVICE_URL}/api/gesture/{path}", request)


# 5. Alerts API (Ingestion & Polling for SOS Dashboard)
alerts_db: List[Dict[str, Any]] = []
alert_id_counter = 1


@app.get("/api/v1/alerts", summary="Get All Emergency Alerts")
async def get_alerts():
    return alerts_db


@app.post("/api/v1/alerts", status_code=201, summary="Ingest Emergency Alert")
async def create_alert(request: Request):
    global alert_id_counter
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    alert = {
        "id": alert_id_counter,
        "title": payload.get("title", "Emergency Incident Alert"),
        "threat_level": (payload.get("threat_level") or "HIGH").upper(),
        "detection_source": payload.get("detection_source", "AI_SENSOR"),
        "latitude": payload.get("latitude", 23.8769),
        "longitude": payload.get("longitude", 90.3204),
        "location_name": payload.get("location_name", "Daffodil Smart City - Ashulia, Dhaka"),
        "description": payload.get("description", "Emergency telemetry alert registered."),
        "raw_detection_data": payload.get("raw_detection_data") or {},
        "snapshot_b64": payload.get("snapshot_b64"),
        "status": (payload.get("status") or "ACTIVE").upper(),
        "sms_status": "SENT" if (payload.get("threat_level") or "").upper() in ("CRITICAL", "HIGH") else "SKIPPED",
        "timestamp": now_iso,
        "created_at": now_iso
    }
    alert_id_counter += 1
    alerts_db.insert(0, alert)
    return alert


@app.patch("/api/v1/alerts/{alert_id}", summary="Update Alert Status")
async def update_alert(alert_id: int, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    for alert in alerts_db:
        if alert.get("id") == alert_id:
            if "status" in payload:
                alert["status"] = payload["status"].upper()
            if "threat_level" in payload:
                alert["threat_level"] = payload["threat_level"].upper()
            if "notes" in payload:
                alert["notes"] = payload["notes"]
            return alert
    return JSONResponse(status_code=404, content={"detail": f"Alert #{alert_id} not found"})


@app.delete("/api/v1/alerts", summary="Purge All Alerts")
async def purge_alerts():
    alerts_db.clear()
    return {"status": "success", "message": "All alerts purged"}


@app.get("/api/v1/health", summary="Health Check")
async def v1_health_check():
    return {"status": "healthy", "service": "AEGIS Unified Sensor API Gateway", "online": True}


# 6. Gateway Health & Discovery
@app.get("/", summary="Gateway Status & Topology")
@app.get("/health", summary="Gateway Health Check")
async def gateway_health():
    services_telemetry = {}
    for name, port, url in [
        ("camera", CAMERA_PORT, CAMERA_SERVICE_URL),
        ("voice", VOICE_PORT, VOICE_SERVICE_URL),
        ("fall", FALL_PORT, FALL_SERVICE_URL),
        ("gesture", GESTURE_PORT, GESTURE_SERVICE_URL),
    ]:
        online = False
        try:
            if http_client:
                r = await http_client.get(f"{url}/status", timeout=1.0)
                online = (r.status_code == 200)
        except Exception:
            online = False
        services_telemetry[name] = {"port": port, "internal_url": url, "online": online}

    return {
        "status": "online",
        "service": "AEGIS Unified Sensor API Gateway",
        "version": "2.0.0",
        "public_port": GATEWAY_PORT,
        "input_source": INPUT_SOURCE,
        "routes": {
            "camera": f"/api/camera/* -> {CAMERA_SERVICE_URL}",
            "voice": f"/api/voice/* -> {VOICE_SERVICE_URL}",
            "fall": f"/api/fall/* -> {FALL_SERVICE_URL}",
            "gesture": f"/api/gesture/* -> {GESTURE_SERVICE_URL}",
            "video_feed": f"/video_feed -> {CAMERA_SERVICE_URL}/video_feed"
        },
        "services": services_telemetry
    }


# ---------------------------------------------------------------------------
# CLI ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    global GATEWAY_PORT, GATEWAY_HOST, INPUT_SOURCE

    parser = argparse.ArgumentParser(description="AEGIS Emergency SOS System - Master API Gateway Launcher")
    parser.add_argument("--port", type=int, default=GATEWAY_PORT, help=f"Public port to bind to (default: {GATEWAY_PORT})")
    parser.add_argument("--host", type=str, default=GATEWAY_HOST, help=f"Host interface to bind to (default: {GATEWAY_HOST})")
    parser.add_argument("--input-source", type=str, default=INPUT_SOURCE, choices=["hardware", "api", "cloud"], help="Input source mode: 'hardware' or 'api'")
    args, _ = parser.parse_known_args()

    GATEWAY_PORT = args.port
    GATEWAY_HOST = args.host
    INPUT_SOURCE = args.input_source
    os.environ["PORT"] = str(GATEWAY_PORT)
    os.environ["GATEWAY_PORT"] = str(GATEWAY_PORT)
    os.environ["INPUT_SOURCE"] = INPUT_SOURCE

    print("\n======================================================================")
    print("🛡️  AEGIS EMERGENCY SOS SYSTEM - UNIFIED API GATEWAY")
    print("======================================================================")
    print(f"[*] Public Entry Port:      {GATEWAY_PORT} (Host: {GATEWAY_HOST})")
    print(f"[*] Input Source Mode:      {INPUT_SOURCE.upper()}")
    print(f"[*] Internal Service Ports:")
    print(f"    - Camera Server:        {CAMERA_PORT} -> /api/camera/*, /video_feed, /status")
    print(f"    - Voice Emergency:      {VOICE_PORT} -> /api/voice/*")
    print(f"    - Fall Detection:       {FALL_PORT} -> /api/fall/*")
    print(f"    - Gesture Landmarks:    {GESTURE_PORT} -> /api/gesture/*")
    print("======================================================================\n")

    uvicorn.run(app, host=GATEWAY_HOST, port=GATEWAY_PORT, log_level="info")


if __name__ == "__main__":
    main()
