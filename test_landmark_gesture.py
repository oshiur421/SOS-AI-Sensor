import os
import atexit
import time
import base64
import threading
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import cv2
import joblib
import numpy as np
import mediapipe as mp
import requests
from flask import Flask, Response, render_template_string

from mediapipe.tasks import python
from mediapipe.tasks.python import vision


# ==========================================
# FILE PATHS
# ==========================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "hand_landmarker.task")
CLASSIFIER_PATH = os.path.join(BASE_DIR, "gesture_landmark_model.pkl")


# ==========================================
# ALERT CONFIGURATION & HELPERS
# ==========================================

last_alert_time = 0
COOLDOWN_SECONDS = 10
sos_timestamps = []


def send_gesture_alert(gesture_name, confidence=None, dynamic_threat_level="CRITICAL", snapshot_b64=None, frame=None):
    url = "http://127.0.0.1:8000/api/v1/alerts"
    conf_val = float(confidence) if confidence is not None else 1.0

    # If snapshot_b64 not pre-encoded but frame is passed, encode now
    if snapshot_b64 is None and frame is not None:
        try:
            _, buffer = cv2.imencode('.jpg', frame)
            snapshot_b64 = base64.b64encode(buffer).decode('utf-8')
        except Exception as enc_err:
            print(f"[SNAPSHOT ERROR] Failed to encode frame: {enc_err}")

    # Backend FastAPI schema accepts LOW, MEDIUM, HIGH, CRITICAL.
    # Map MODERATE to MEDIUM for the backend payload so requests succeed without 422
    backend_threat_level = "MEDIUM" if str(dynamic_threat_level).upper() == "MODERATE" else str(dynamic_threat_level).upper()

    raw_data = {
        "gesture": gesture_name,
        "confidence": round(conf_val, 2),
        "threat_level": dynamic_threat_level,
        "dynamic_threat_level": dynamic_threat_level,
        "sensor": "MediaPipe Hand Landmarker",
        "sector": "Sector 4B",
        "coords": "23°52'36.8\"N 90°19'13.4\"E"
    }
    if snapshot_b64:
        raw_data["snapshot_b64"] = snapshot_b64

    payload = {
        "title": f"SOS Gesture Detected: {gesture_name}",
        "threat_level": backend_threat_level,
        "detection_source": "AI_CAMERA_GESTURE",
        "latitude": 23.8769,
        "longitude": 90.3204,
        "location_name": "Daffodil Smart City - Ashulia, Dhaka",
        "description": f"Camera identified emergency hand sign: {gesture_name} with confidence {conf_val:.2f} (Threat: {dynamic_threat_level}) at Daffodil Smart City - Ashulia, Dhaka.",
        "raw_detection_data": raw_data,
        "snapshot_b64": snapshot_b64
    }
    try:
        response = requests.post(url, json=payload, timeout=2.0)
        print(f"[ALERT SENT] Gesture: {gesture_name} | Threat Level: {dynamic_threat_level} | Status: {response.status_code}")
        return response.status_code
    except requests.exceptions.RequestException as e:
        error_detail = ""
        status_code = 0
        if hasattr(e, "response") and e.response is not None:
            error_detail = f" | Response: {e.response.text}"
            status_code = e.response.status_code
        print(f"[ALERT ERROR] Failed to send gesture alert: {e}{error_detail}")
        return status_code
    except Exception as e:
        print(f"[ALERT ERROR] Unexpected error sending gesture alert: {e}")
        return 0


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
                # Decode the image_b64 string into raw image bytes
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
# LOAD RANDOM FOREST MODEL
# ==========================================

classifier = joblib.load(CLASSIFIER_PATH)
print("Gesture classifier loaded!")


# ==========================================
# LOAD MEDIAPIPE HAND LANDMARKER
# ==========================================

base_options = python.BaseOptions(
    model_asset_path=MODEL_PATH
)

options = vision.HandLandmarkerOptions(
    base_options=base_options,
    num_hands=1
)

detector = vision.HandLandmarker.create_from_options(options)
print("Hand Landmarker loaded!")


# ==========================================
# SHARED CAMERA SERVER SETUP
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
# FLASK WEB SERVER SETUP
# ==========================================

app = Flask(__name__)

latest_frame_jpeg = None
frame_lock = threading.Lock()


def process_gestures():
    """Continuous headless gesture detection loop running in background."""
    global last_alert_time, sos_timestamps, latest_frame_jpeg
    print(f"\n[GESTURE] Single camera gesture processing active via {CAMERA_URL}.\n")

    while True:
        ret, frame = fetch_camera_frame()
        if not ret or frame is None:
            time.sleep(0.03)
            continue

        # Frame fetched from CAMERA_URL is already horizontally mirrored by camera_server.py
        # Clean up rolling window
        current_time = time.time()
        sos_timestamps = [t for t in sos_timestamps if current_time - t <= 20.0]

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = detector.detect(mp_image)

        prediction_text = "NO HAND"

        # ======================================
        # IF HAND IS DETECTED
        # ======================================
        if result.hand_landmarks:
            hand = result.hand_landmarks[0]

            # Wrist landmark
            wrist = hand[0]

            # Create 63 landmark features
            features = []
            for landmark in hand:
                x = landmark.x - wrist.x
                y = landmark.y - wrist.y
                z = landmark.z - wrist.z
                features.extend([x, y, z])

            # Convert to NumPy
            X = np.array(features).reshape(1, -1)

            # Predict gesture
            prediction = classifier.predict(X)[0]

            # Prediction confidence
            probabilities = classifier.predict_proba(X)[0]
            confidence = np.max(probabilities)

            prediction_text = f"{prediction} ({confidence:.2f})"

            # Rolling 20-second Window Tracking
            if prediction == "SOS" and confidence > 0.40:
                sos_timestamps.append(current_time)
                sos_timestamps = [t for t in sos_timestamps if current_time - t <= 20.0]

            sos_count = len(sos_timestamps)
            if sos_count < 15:
                dynamic_threat_level = "LOW"
            elif 15 <= sos_count <= 45:
                dynamic_threat_level = "MODERATE"
            elif 46 <= sos_count <= 90:
                dynamic_threat_level = "HIGH"
            else:
                dynamic_threat_level = "CRITICAL"

            # Real-time debug prediction output
            print(f"Debug - Gesture: {prediction} | Confidence: {confidence:.2f} | SOS Window (20s): {sos_count} frames | Threat Level: {dynamic_threat_level}")

            # Calculate and draw bounding box
            h, w, _ = frame.shape
            x_coords = [int(lm.x * w) for lm in hand]
            y_coords = [int(lm.y * h) for lm in hand]
            x_min, x_max = min(x_coords), max(x_coords)
            y_min, y_max = min(y_coords), max(y_coords)

            padding = 20
            x1 = max(0, x_min - padding)
            y1 = max(0, y_min - padding)
            x2 = min(w, x_max + padding)
            y2 = min(h, y_max + padding)

            box_color = (0, 0, 255) if prediction == "SOS" else (0, 255, 0)
            cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 2)

            # Draw hand landmarks
            for landmark in hand:
                px = int(landmark.x * w)
                py = int(landmark.y * h)
                cv2.circle(frame, (px, py), 5, (0, 255, 0), -1)

            # Send Alert on SOS Gesture (with Snapshot)
            if (
                prediction == "SOS"
                and confidence > 0.40
                and (current_time - last_alert_time >= COOLDOWN_SECONDS)
            ):
                try:
                    _, buffer = cv2.imencode('.jpg', frame)
                    snapshot_b64 = base64.b64encode(buffer).decode('utf-8')

                    status_code = send_gesture_alert(
                        prediction,
                        confidence=confidence,
                        dynamic_threat_level=dynamic_threat_level,
                        snapshot_b64=snapshot_b64
                    )
                    if status_code in (200, 201):
                        last_alert_time = current_time
                        current_threat_level = dynamic_threat_level
                        if current_threat_level in ["HIGH", "CRITICAL"]:
                            threading.Thread(
                                target=send_telegram_alert,
                                args=("Gesture Emergency Detected", current_threat_level, 23.8769, 90.3204, snapshot_b64),
                                daemon=True
                            ).start()
                except Exception as e:
                    print(f"[ALERT ERROR] Error during gesture alert trigger: {e}")
        else:
            sos_count = len(sos_timestamps)
            if sos_count < 15:
                dynamic_threat_level = "LOW"
            elif 15 <= sos_count <= 45:
                dynamic_threat_level = "MODERATE"
            elif 46 <= sos_count <= 90:
                dynamic_threat_level = "HIGH"
            else:
                dynamic_threat_level = "CRITICAL"

        # Display result overlays
        text_color = (0, 0, 255) if "SOS" in prediction_text else (0, 255, 0)
        cv2.putText(
            frame,
            f"{prediction_text} | Threat: {dynamic_threat_level} ({len(sos_timestamps)})",
            (30, 50),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            text_color,
            2
        )

        cv2.putText(
            frame,
            "AI Stream Live | Port: 5000",
            (30, 90),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2
        )

        ret_enc, buffer = cv2.imencode('.jpg', frame)
        if ret_enc:
            with frame_lock:
                latest_frame_jpeg = buffer.tobytes()

        time.sleep(0.03)


def generate_frames():
    """Yields processed frames for Flask /video_feed stream."""
    while True:
        with frame_lock:
            frame_bytes = latest_frame_jpeg

        if frame_bytes is None:
            time.sleep(0.03)
            continue

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.03)


# ==========================================
# HTTP ROUTES
# ==========================================

@app.route("/")
def index():
    return render_template_string("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Emergency - Landmark Gesture Stream</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: #0b1120;
            color: #f1f5f9;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 24px;
        }
        .header {
            text-align: center;
            margin-bottom: 20px;
        }
        .header h1 {
            font-size: 22px;
            letter-spacing: 0.1em;
            color: #38bdf8;
            margin-bottom: 6px;
        }
        .header p {
            font-size: 13px;
            color: #94a3b8;
        }
        .viewport {
            border: 2px solid #1e293b;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.6), 0 8px 10px -6px rgba(0, 0, 0, 0.6);
            background: #000;
            max-width: 900px;
            width: 100%;
        }
        .viewport img {
            width: 100%;
            height: auto;
            display: block;
        }
        .footer {
            margin-top: 16px;
            font-size: 12px;
            color: #64748b;
            display: flex;
            gap: 20px;
        }
        .badge {
            color: #22c55e;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>AI EMERGENCY SYSTEM // SENSOR FEED</h1>
        <p>MediaPipe Hand Landmarker + Gesture Classifier Stream</p>
    </div>
    <div class="viewport">
        <img src="/video_feed" alt="Real-time Gesture Detection Feed">
    </div>
    <div class="footer">
        <span>STATUS: <strong class="badge">STREAMING (MJPEG)</strong></span>
        <span>PORT: <strong>5000</strong></span>
        <span>ROUTE: <strong>/video_feed</strong></span>
    </div>
</body>
</html>""")


@app.route("/video_feed")
def video_feed():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


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
    print("[CLEANUP] Shared camera session and detector closed.")


# ==========================================
# SERVER ENTRYPOINT
# ==========================================

if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("Flask Gesture Detection Server starting on http://0.0.0.0:5000")
    print("Headless Background Gesture Processor Active")
    print("Direct Browser URL: http://localhost:5000")
    print("Direct Stream Feed: http://localhost:5000/video_feed")
    print("Press Ctrl+C to stop the server.")
    print("=" * 55 + "\n")

    # Launch continuous headless gesture processing thread
    threading.Thread(target=process_gestures, daemon=True).start()

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)