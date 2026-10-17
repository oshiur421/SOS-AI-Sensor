import os
import atexit
import math
import time
import base64
import threading
import requests
import cv2
import numpy as np
import mediapipe as mp

from mediapipe.tasks import python
from mediapipe.tasks.python import vision


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


# ==========================================
# SHARED CAMERA SERVER CONFIGURATION
# ==========================================

CAMERA_URL = os.environ.get("CAMERA_URL", "http://127.0.0.1:8001/frame")
http_session = requests.Session()


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
    url = "http://127.0.0.1:8000/api/v1/alerts"
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
# FALL TRACKING STATE
# ==========================================

fall_start_time = None
is_fallen = False
high_alert_sent = False
critical_alert_sent = False

OBSERVATION_HIGH_SECONDS = 15.0
OBSERVATION_CRITICAL_SECONDS = 20.0
frame_timestamp_ms = 0


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
# MAIN REAL-TIME PROCESSING LOOP
# ==========================================

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("AEGIS Fall Detection running in Single Camera mode.")
    print(f"Connecting to Camera Server frame API at {CAMERA_URL}...")
    print("Pose Aspect Ratio & Landmark detection active.")
    print("Duration-based observation timer active (HIGH: 15s, CRITICAL: 20s).")
    print("Press Ctrl+C to stop.")
    print("=" * 60 + "\n")

    while True:
        ret, frame = fetch_camera_frame()

        if not ret or frame is None:
            time.sleep(0.03)
            continue

        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

        frame_timestamp_ms += 33

        result = detector.detect_for_video(mp_image, frame_timestamp_ms)

        if result.pose_landmarks:
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
            if is_fallen and fall_start_time is not None:
                if is_standing:
                    print("[RECOVERY] Person upright / recovered. Fall tracking reset.")
                    is_fallen = False
                    fall_start_time = None
                    high_alert_sent = False
                    critical_alert_sent = False
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

        # Headless loop stabilization & CPU throttling
        time.sleep(0.03)