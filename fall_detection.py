import os
import atexit
import math
import time
import base64
import argparse
import threading
from typing import Optional
import requests
import cv2
import numpy as np
import mediapipe as mp

from mediapipe.tasks import python
from mediapipe.tasks.python import vision
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn


# ==========================================
# CLI ARGUMENT PARSING
# ==========================================

parser = argparse.ArgumentParser(description="AEGIS Fall Detection Sensor")
parser.add_argument(
    "--input-source",
    type=str,
    default=os.environ.get("INPUT_SOURCE", "hardware"),
    choices=["hardware", "api", "cloud"],
    help="Input source mode: 'hardware' (local camera server stream) or 'api'/'cloud' (client-side API ingestion)"
)
parser.add_argument("--port", type=int, default=8002, help="Port to run fall detection server on in API mode (default: 8002)")
parser.add_argument("--host", type=str, default=os.environ.get("HOST", "0.0.0.0"), help="Host address to bind to (default: 0.0.0.0)")
parser.add_argument("--camera-url", type=str, default=os.environ.get("CAMERA_URL", "http://127.0.0.1:8001/frame"), help="Camera frame URL")
parser.add_argument("--gateway-port", type=int, default=int(os.environ.get("GATEWAY_PORT", 8000)), help="Gateway public port for backend alerts (default: 8000)")

cli_args, _ = parser.parse_known_args()

INPUT_SOURCE = "api" if cli_args.input_source.lower() in ["api", "cloud", "client"] else "hardware"
SERVER_HOST = cli_args.host
SERVER_PORT = cli_args.port
CAMERA_URL = cli_args.camera_url
GATEWAY_PORT = cli_args.gateway_port
BACKEND_ALERT_URL = os.environ.get("BACKEND_ALERT_URL", f"http://127.0.0.1:{GATEWAY_PORT}/api/v1/alerts")
http_session = requests.Session()

# ==========================================
# FILE PATHS & MODEL CONFIGURATION
# ==========================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "pose_landmarker_lite.task")
if not os.path.exists(MODEL_PATH):
    MODEL_PATH = "models/pose_landmarker_lite.task"


# ==========================================
# MEDIAPIPE POSE SETUP
# ==========================================

base_options = python.BaseOptions(
    model_asset_path=MODEL_PATH
)

options = vision.PoseLandmarkerOptions(
    base_options=base_options,
    running_mode=vision.RunningMode.VIDEO,
    num_poses=1
)

detector = vision.PoseLandmarker.create_from_options(options)


def fetch_camera_frame():
    """Fetches the latest decoded BGR frame directly from the Single Camera Server."""
    try:
        resp = http_session.get(CAMERA_URL, timeout=1.5)
        if resp.status_code == 200 and resp.content:
            img_arr = np.frombuffer(resp.content, dtype=np.uint8)
            frame = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            if frame is not None and frame.size > 0:
                return True, frame
    except Exception:
        pass
    return False, None


# ==========================================
# TELEGRAM BOT ALERT CONFIGURATION
# ==========================================

def send_telegram_alert(alert_type, threat_level, lat, lon, image_b64=None):
    """
    Forwards an emergency distress notification to the user's mobile device via Telegram Bot.
    Decodes the image_b64 string into raw image bytes and sends a photo with caption via sendPhoto.
    """
    TOKEN = "8934386182:AAEQbKhrV13XUAssd7jBz73XHqc1JFqNzgY"
    CHAT_ID = "5557136788"

    caption = (
        f"🚨 EMERGENCY ALERT TRIGGERED 🚨\n"
        f"Type: {alert_type}\n"
        f"Threat Level: {threat_level}\n"
        f"Location: https://www.google.com/maps?q={lat},{lon}"
    )

    try:
        if image_b64:
            try:
                image_bytes = base64.b64decode(image_b64)
                url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
                payload = {
                    'chat_id': CHAT_ID,
                    'caption': caption
                }
                files = {
                    'photo': ('snapshot.jpg', image_bytes, 'image/jpeg')
                }
                response = requests.post(url, data=payload, files=files, timeout=10)
                print(f"\n[TELEGRAM DEBUG] Status: {response.status_code}")
                print(f"[TELEGRAM DEBUG] Response: {response.text}\n")

                if response.status_code == 200:
                    print(f"📱 [TELEGRAM ALERT] Photo distress notification delivered to Telegram (Chat ID: {CHAT_ID}).")
                    return True
            except Exception as e:
                print(f"\n[TELEGRAM ERROR] Exception occurred: {e}\n")

        # Fallback to sendMessage if image was absent or photo send failed
        msg_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        msg_payload = {
            'chat_id': CHAT_ID,
            'text': caption
        }
        response = requests.post(msg_url, json=msg_payload, timeout=8)
        print(f"\n[TELEGRAM DEBUG] Status: {response.status_code}")
        print(f"[TELEGRAM DEBUG] Response: {response.text}\n")

        if response.status_code == 200:
            print(f"📱 [TELEGRAM ALERT] Text distress notification delivered to Telegram (Chat ID: {CHAT_ID}).")
            return True
        return False

    except Exception as e:
        print(f"\n[TELEGRAM ERROR] Exception occurred: {e}\n")
        return False


# ==========================================
# DASHBOARD ALERT CONFIGURATION & HELPER
# ==========================================

last_fall_alert_time = 0
FALL_COOLDOWN_SECONDS = 10


def send_fall_alert(threat_level="CRITICAL", snapshot_b64=None):
    url = BACKEND_ALERT_URL
    raw_data = {
        "incident": "fall_detected",
        "threat_level": threat_level,
        "sensor": "MediaPipe Pose Landmarker",
        "sector": "Sector 4B",
        "coords": "23°52'36.8\"N 90°19'13.4\"E"
    }
    if snapshot_b64:
        raw_data["snapshot_b64"] = snapshot_b64

    payload = {
        "title": "Fall Detected (Man Down)",
        "threat_level": threat_level,
        "detection_source": "AI_CAMERA_FALL",
        "latitude": 23.8769,
        "longitude": 90.3204,
        "location_name": "Daffodil Smart City - Ashulia, Dhaka",
        "description": f"Pose landmarker identified sudden human fall incident (Threat: {threat_level}) at Daffodil Smart City - Ashulia, Dhaka.",
        "raw_detection_data": raw_data,
        "snapshot_b64": snapshot_b64
    }
    try:
        response = requests.post(url, json=payload, timeout=2.0)
        response.raise_for_status()
        print(f"[ALERT SENT] Fall detected alert sent | Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        error_detail = ""
        if hasattr(e, "response") and e.response is not None:
            error_detail = f" | Response: {e.response.text}"
        print(f"[ALERT ERROR] Failed to send fall alert: {e}{error_detail}")
        return False
    except Exception as e:
        print(f"[ALERT ERROR] Unexpected error sending fall alert: {e}")
        return False


# ==========================================
# POSE ANGLE CALCULATION
# ==========================================

def calculate_body_angle(landmarks):
    left_shoulder = landmarks[11]
    right_shoulder = landmarks[12]

    left_hip = landmarks[23]
    right_hip = landmarks[24]

    shoulder_x = (
        left_shoulder.x +
        right_shoulder.x
    ) / 2

    shoulder_y = (
        left_shoulder.y +
        right_shoulder.y
    ) / 2

    hip_x = (
        left_hip.x +
        right_hip.x
    ) / 2

    hip_y = (
        left_hip.y +
        right_hip.y
    ) / 2

    dx = hip_x - shoulder_x
    dy = hip_y - shoulder_y

    angle = math.degrees(
        math.atan2(abs(dy), abs(dx))
    )

    return angle


# ==========================================
# ==========================================
# FALL TRACKING STATE & LOCK
# ==========================================

fall_start_time = None
is_fallen = False
high_alert_sent = False
critical_alert_sent = False
current_threat_level = "NORMAL"

OBSERVATION_HIGH_SECONDS = 15.0
OBSERVATION_CRITICAL_SECONDS = 20.0
frame_timestamp_ms = 0
detection_lock = threading.Lock()


# ==========================================
# CLEANUP HANDLER
# ==========================================

@atexit.register
def cleanup():
    try:
        http_session.close()
    except Exception:
        pass
    try:
        detector.close()
    except Exception:
        pass
    print("[CLEANUP] Fall detector and camera session closed.")


# ==========================================
# CORE POSE & FALL DETECTION PROCESSOR
# ==========================================

def process_frame_for_fall(frame: np.ndarray) -> dict:
    """
    Processes an incoming BGR frame for human pose landmarks and fall patterns.
    Updates observation timers and escalates alerts to backend & Telegram.
    Returns detection metrics.
    """
    global fall_start_time, is_fallen, high_alert_sent, critical_alert_sent
    global current_threat_level, frame_timestamp_ms

    if frame is None or frame.size == 0:
        return {"pose_detected": False, "is_fallen": False, "threat_level": "NORMAL"}

    with detection_lock:
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

        frame_timestamp_ms += 33
        result = detector.detect_for_video(mp_image, frame_timestamp_ms)

        if not result.pose_landmarks:
            return {
                "pose_detected": False,
                "is_fallen": is_fallen,
                "threat_level": current_threat_level if is_fallen else "NORMAL"
            }

        landmarks = result.pose_landmarks[0]
        h, w, _ = frame.shape

        nose = landmarks[0]
        left_shoulder = landmarks[11]
        right_shoulder = landmarks[12]
        left_hip = landmarks[23]
        right_hip = landmarks[24]
        left_ankle = landmarks[27]
        right_ankle = landmarks[28]

        key_landmarks = [
            nose, left_shoulder, right_shoulder,
            left_hip, right_hip, left_ankle, right_ankle
        ]

        visible_pts = []
        for lm in key_landmarks:
            vis = getattr(lm, 'visibility', 1.0)
            if vis is None or vis > 0.2:
                visible_pts.append((lm.x * w, lm.y * h))

        if len(visible_pts) < 3:
            visible_pts = [(lm.x * w, lm.y * h) for lm in key_landmarks]

        xs = [pt[0] for pt in visible_pts]
        ys = [pt[1] for pt in visible_pts]

        min_x = min(xs)
        max_x = max(xs)
        min_y = min(ys)
        max_y = max(ys)

        pose_width = max_x - min_x
        pose_height = max_y - min_y

        left_hip_vis = getattr(left_hip, 'visibility', 0.0)
        right_hip_vis = getattr(right_hip, 'visibility', 0.0)
        left_hip_vis = 0.0 if left_hip_vis is None else float(left_hip_vis)
        right_hip_vis = 0.0 if right_hip_vis is None else float(right_hip_vis)
        hip_visibility = (left_hip_vis + right_hip_vis) / 2.0

        norm_upper_y = (nose.y + left_shoulder.y + right_shoulder.y) / 3.0
        norm_lower_y = (left_hip.y + right_hip.y) / 2.0

        if hip_visibility > 0.5:
            condition_a = pose_width > pose_height
        else:
            condition_a = False

        condition_b = norm_upper_y > norm_lower_y
        is_upright = norm_upper_y < (norm_lower_y - 0.1)

        if is_upright:
            is_advanced_fall_condition = False
        else:
            is_advanced_fall_condition = condition_a or condition_b

        is_standing = is_upright or ((pose_height > pose_width) and (norm_upper_y < norm_lower_y))
        cause = None

        # 1. Trigger initial fall observation
        if is_advanced_fall_condition and not is_fallen:
            is_fallen = True
            fall_start_time = time.time()
            high_alert_sent = False
            critical_alert_sent = False
            cause = "Horizontal (Width > Height)" if condition_a else "Head Below Hips"
            if condition_a and condition_b:
                cause = "Horizontal & Inverted"
            print(f"[FALL DETECTED] Condition: {cause} (W:{pose_width:.0f}px, H:{pose_height:.0f}px, HipVis:{hip_visibility:.2f}). Timer started...")

        # 2. Monitor ongoing fall state & escalate
        elapsed_fall = 0.0
        if is_fallen and fall_start_time is not None:
            if is_standing:
                print("[RECOVERY] Person upright / recovered. Fall tracking reset.")
                is_fallen = False
                fall_start_time = None
                high_alert_sent = False
                critical_alert_sent = False
                current_threat_level = "NORMAL"
            else:
                elapsed_fall = time.time() - fall_start_time

                # Check CRITICAL escalation (>= 20s)
                if elapsed_fall >= OBSERVATION_CRITICAL_SECONDS and not critical_alert_sent:
                    critical_alert_sent = True
                    current_threat_level = "CRITICAL"
                    try:
                        snapshot_b64 = None
                        try:
                            _, buffer = cv2.imencode('.jpg', frame)
                            snapshot_b64 = base64.b64encode(buffer).decode('utf-8')
                        except Exception as enc_err:
                            print(f"[SNAPSHOT ERROR] Failed to encode frame: {enc_err}")

                        if send_fall_alert(threat_level=current_threat_level, snapshot_b64=snapshot_b64):
                            print(f"[ALERT TRIGGER] CRITICAL Fall alert dispatched after {elapsed_fall:.1f}s.")
                            threading.Thread(
                                target=send_telegram_alert,
                                args=("Fall Down Detected - Unresponsive (20s+)", current_threat_level, 23.8769, 90.3204, snapshot_b64),
                                daemon=True
                            ).start()
                    except Exception as e:
                        print(f"[ALERT ERROR] Error during CRITICAL fall alert trigger: {e}")

                # Check HIGH alert (>= 15s)
                elif elapsed_fall >= OBSERVATION_HIGH_SECONDS and not high_alert_sent:
                    high_alert_sent = True
                    current_threat_level = "HIGH"
                    try:
                        snapshot_b64 = None
                        try:
                            _, buffer = cv2.imencode('.jpg', frame)
                            snapshot_b64 = base64.b64encode(buffer).decode('utf-8')
                        except Exception as enc_err:
                            print(f"[SNAPSHOT ERROR] Failed to encode frame: {enc_err}")

                        if send_fall_alert(threat_level=current_threat_level, snapshot_b64=snapshot_b64):
                            print(f"[ALERT TRIGGER] HIGH Fall alert dispatched after {elapsed_fall:.1f}s.")
                            threading.Thread(
                                target=send_telegram_alert,
                                args=("Fall Down Detected - On Ground (15s+)", current_threat_level, 23.8769, 90.3204, snapshot_b64),
                                daemon=True
                            ).start()
                    except Exception as e:
                        print(f"[ALERT ERROR] Error during HIGH fall alert trigger: {e}")

        return {
            "pose_detected": True,
            "is_fallen": is_fallen,
            "fall_condition": cause if is_fallen else None,
            "threat_level": current_threat_level if is_fallen else "NORMAL",
            "elapsed_fall_sec": round(elapsed_fall, 1)
        }


# ==========================================
# FASTAPI APPLICATION (CLIENT-SIDE API MODE)
# ==========================================

fall_app = FastAPI(
    title="AEGIS Fall Detection API",
    description="Client-side API for human pose fall detection in cloud environments.",
    version="3.0.0"
)

fall_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FallFramePayload(BaseModel):
    frame: Optional[str] = None
    image: Optional[str] = None
    snapshot_b64: Optional[str] = None


@fall_app.get("/status", summary="Fall Detection Status")
def get_fall_status():
    with detection_lock:
        elapsed = round(time.time() - fall_start_time, 1) if (is_fallen and fall_start_time) else 0.0
        return {
            "status": "ONLINE",
            "service": "AEGIS Fall Detection Sensor",
            "input_source": INPUT_SOURCE,
            "port": SERVER_PORT,
            "gateway_port": GATEWAY_PORT,
            "backend_url": BACKEND_ALERT_URL,
            "is_fallen": is_fallen,
            "threat_level": current_threat_level if is_fallen else "NORMAL",
            "elapsed_fall_sec": elapsed
        }


@fall_app.post("/api/fall/frame", summary="Process Frame for Fall Detection")
@fall_app.post("/detect", summary="Process Frame for Fall Detection")
async def api_detect_fall(
    file: Optional[UploadFile] = File(None),
    payload: Optional[FallFramePayload] = None,
    request: Request = None
):
    """
    Client-side API endpoint for cloud deployment.
    Accepts video frames as multipart file, JSON base64, or raw binary image.
    """
    raw_bytes = None
    if file:
        raw_bytes = await file.read()
    elif payload and (payload.frame or payload.image or payload.snapshot_b64):
        b64_str = payload.frame or payload.image or payload.snapshot_b64
        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(b64_str)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 frame encoding: {e}")
    elif request:
        body = await request.body()
        if body:
            try:
                import json
                data = json.loads(body)
                b64_cand = data.get("frame") or data.get("image") or data.get("snapshot_b64")
                if b64_cand:
                    if "," in b64_cand:
                        b64_cand = b64_cand.split(",", 1)[1]
                    raw_bytes = base64.b64decode(b64_cand)
            except Exception:
                raw_bytes = body

    if not raw_bytes:
        raise HTTPException(status_code=400, detail="No image payload provided.")

    img_arr = np.frombuffer(raw_bytes, dtype=np.uint8)
    frame = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
    if frame is None or frame.size == 0:
        raise HTTPException(status_code=400, detail="Could not decode image bytes.")

    res = process_frame_for_fall(frame)
    return {"status": "success", **res}


def background_camera_worker():
    """Background worker fetching frames from CAMERA_URL if available."""
    while True:
        try:
            ret, frame = fetch_camera_frame()
            if ret and frame is not None:
                process_frame_for_fall(frame)
                time.sleep(0.03)
            else:
                time.sleep(0.3)
        except Exception:
            time.sleep(0.3)


# ==========================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================

if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("AEGIS AI FALL DETECTION SENSOR")
    source_label = "CLIENT-SIDE APIS (Cloud Mode)" if INPUT_SOURCE == "api" else "LOCAL HARDWARE STREAM"
    print(f"Input Source: {INPUT_SOURCE.upper()} - {source_label}")
    print(f"Camera Frame Source: {CAMERA_URL}")
    print(f"Backend Target URL:  {BACKEND_ALERT_URL}")
    print("Pose Aspect Ratio & Landmark detection active.")
    print("Duration-based observation timer active (HIGH: 15s, CRITICAL: 20s).")
    print("=" * 65 + "\n")

    if INPUT_SOURCE == "api":
        # Launch background polling thread to consume frames from camera_server if active
        t = threading.Thread(target=background_camera_worker, daemon=True, name="FallCameraWorker")
        t.start()
        print(f"Listening for client-side API frames on http://{SERVER_HOST}:{SERVER_PORT}/api/fall/frame ...")
        uvicorn.run(fall_app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
    else:
        # Hardware continuous loop
        while True:
            ret, frame = fetch_camera_frame()
            if not ret or frame is None:
                time.sleep(0.03)
                continue
            process_frame_for_fall(frame)
            time.sleep(0.03)