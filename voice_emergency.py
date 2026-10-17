import os
import sys
import io
import time
import base64
import argparse
import threading
from typing import Optional
import requests
try:
    import sounddevice as sd
except Exception:
    sd = None
import speech_recognition as sr
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Ensure Windows terminal handles UTF-8 emojis without charmap encoding errors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==========================================
# CLI ARGUMENT PARSING
# ==========================================

parser = argparse.ArgumentParser(description="AEGIS AI Emergency Voice Detector")
parser.add_argument(
    "--input-source",
    type=str,
    default=os.environ.get("INPUT_SOURCE", "hardware"),
    choices=["hardware", "api", "cloud"],
    help="Input source mode: 'hardware' (local microphone) or 'api'/'cloud' (client-side audio API)"
)
parser.add_argument("--port", type=int, default=8003, help="Port to run voice detection API server on (default: 8003)")
parser.add_argument("--host", type=str, default=os.environ.get("HOST", "0.0.0.0"), help="Host address to bind to (default: 0.0.0.0)")

cli_args, _ = parser.parse_known_args()

INPUT_SOURCE = "api" if cli_args.input_source.lower() in ["api", "cloud", "client"] else "hardware"
SERVER_HOST = cli_args.host
SERVER_PORT = cli_args.port

# ==========================================
# CONFIGURATION & ALERT COOLDOWN
# ==========================================
BACKEND_ALERT_URL = "http://127.0.0.1:8000/api/v1/alerts"
CAMERA_SERVER_URL = os.environ.get("CAMERA_SERVER_URL", "http://127.0.0.1:8001")
COOLDOWN_SECONDS = 10
last_alert_time = 0

# ==========================================
# EMERGENCY KEYWORD THREAT TIERS
# ==========================================
# Critical Keywords (Threat Level: "CRITICAL")
CRITICAL_KEYWORDS = [
    "bachao",
    "bacao",
    "save me",
    "help me",
    "call police",
    "call an ambulance",
    "sos"
]

# High/Moderate Keywords (Threat Level: "HIGH")
HIGH_KEYWORDS = [
    "i need help",
    "please help",
    "emergency"
]

# Low Keywords (Threat Level: "LOW")
LOW_KEYWORDS = [
    "help"
]

ALL_KEYWORDS = CRITICAL_KEYWORDS + HIGH_KEYWORDS + LOW_KEYWORDS


def evaluate_voice_threat(transcript_clean):
    """
    Evaluates dynamic threat level and matched keyword based on the transcript.
    Evaluates Critical tier first, then High/Moderate tier, then Low tier.
    """
    # 1. Critical keywords
    for kw in CRITICAL_KEYWORDS:
        if kw in transcript_clean:
            return kw, "CRITICAL"

    # 2. High/Moderate keywords
    for kw in HIGH_KEYWORDS:
        if kw in transcript_clean:
            return kw, "HIGH"

    # 3. Low keywords (e.g. casual or soft "help")
    for kw in LOW_KEYWORDS:
        if kw in transcript_clean:
            return kw, "LOW"

    return None, None


def capture_snapshot_with_retry(timeout=2.0, max_retries=2, **kwargs):
    """
    Acquires an optical snapshot directly from the Shared Camera Server (camera_server.py).
    Decouples webcam hardware access from voice recognition, guaranteeing high-exposure,
    uncontended snapshot delivery without hardware collision or black frames.

    Returns:
        tuple: (snapshot_b64, error_message)
    """
    snapshot_url = f"{CAMERA_SERVER_URL}/snapshot"
    frame_url = f"{CAMERA_SERVER_URL}/frame"
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            # 1. Fetch pre-encoded base64 snapshot from /snapshot endpoint
            res = requests.get(snapshot_url, timeout=timeout)
            if res.status_code == 200:
                data = res.json()
                b64 = data.get("snapshot_b64")
                brightness = data.get("brightness", 0.0)
                if b64:
                    print(f"📸 Live optical snapshot confirmed via Shared Camera Server (Brightness: {brightness:.1f}, Length: {len(b64)} chars).")
                    return b64, None

            # 2. Fallback to raw frame binary if needed
            res = requests.get(frame_url, timeout=timeout)
            if res.status_code == 200 and len(res.content) > 0:
                b64 = base64.b64encode(res.content).decode('utf-8')
                print(f"📸 Live optical snapshot confirmed via Shared Camera Server binary frame (Length: {len(b64)} chars).")
                return b64, None
            else:
                last_error = f"Camera server returned HTTP {res.status_code} (Attempt {attempt}/{max_retries})"

        except requests.exceptions.RequestException as req_err:
            last_error = f"Camera server connection error: {req_err} (Is camera_server.py running?)"
        except Exception as ex:
            last_error = f"Snapshot fetch exception: {ex}"

        time.sleep(0.2)

    return None, last_error


# ==========================================
# TELEGRAM BOT ALERT CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8934386182:AAEQbKhrV13XUAssd7jBz73XHqc1JFqNzgY"


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


recognizer = sr.Recognizer()


def process_audio_data(audio_data: sr.AudioData) -> dict:
    """
    Transcribes audio data, evaluates emergency keyword threat tiers,
    and dispatches alerts to backend and Telegram with optical camera snapshot.
    """
    global last_alert_time

    try:
        transcript = recognizer.recognize_google(audio_data)
        transcript_clean = transcript.lower().strip()
        print(f"🗣️ Transcribed Speech: \"{transcript}\"")

        recognized_keyword, dynamic_threat = evaluate_voice_threat(transcript_clean)
        alert_dispatched = False

        if recognized_keyword:
            print(f"🚨 EMERGENCY SPEECH DETECTED! Keyword: '{recognized_keyword}' | Threat Level: {dynamic_threat}")
            current_time = time.time()

            if current_time - last_alert_time >= COOLDOWN_SECONDS:
                print("📸 Fetching live optical snapshot from Shared Camera Server...")
                snapshot_b64, snapshot_err = capture_snapshot_with_retry(timeout=2.0, max_retries=2)

                if snapshot_b64:
                    print(f"✅ [SNAPSHOT CONFIRMED] Live image captured & base64 encoded ({len(snapshot_b64)} chars).")
                else:
                    print(f"🚨 [CRITICAL ERROR] Snapshot fetch failed: {snapshot_err}. Proceeding without snapshot.")

                raw_data = {
                    "transcript": transcript,
                    "keyword": recognized_keyword,
                    "threat_level": dynamic_threat,
                    "sensor": "Speech Recognition Microphone (API)" if INPUT_SOURCE == "api" else "Speech Recognition Microphone"
                }

                payload = {
                    "title": f"Voice Distress Detected: {recognized_keyword}",
                    "threat_level": dynamic_threat,
                    "detection_source": "AI_VOICE_SENSOR",
                    "latitude": 23.8769,
                    "longitude": 90.3204,
                    "location_name": "Daffodil Smart City - Ashulia, Dhaka",
                    "description": f"Emergency voice command detected in audio stream: '{transcript}' [Threat: {dynamic_threat}]",
                    "raw_detection_data": raw_data,
                    "snapshot_b64": snapshot_b64,
                    "send_sms": False
                }

                try:
                    response = requests.post(BACKEND_ALERT_URL, json=payload, timeout=2.5)
                    if response.status_code == 201:
                        last_alert_time = current_time
                        alert_dispatched = True
                        alert_id = response.json().get('id', 'N/A')
                        print(f"✅ [SUCCESS] Emergency voice alert delivered to backend! (Alert #{alert_id} | Threat: {dynamic_threat} | Status: 201)")

                        alert_type_desc = f"Voice Distress ({recognized_keyword})"
                        threading.Thread(
                            target=send_telegram_alert,
                            args=(alert_type_desc, dynamic_threat, 23.8769, 90.3204, snapshot_b64),
                            daemon=True
                        ).start()
                    else:
                        print(f"⚠️ [WARNING] Backend returned status {response.status_code}: {response.text}")
                except Exception as req_err:
                    print(f"❌ [ALERT ERROR] Failed to send voice alert to backend: {req_err}")
            else:
                cooldown_remaining = int(COOLDOWN_SECONDS - (current_time - last_alert_time))
                print(f"⏳ Cooldown active ({cooldown_remaining}s remaining). Alert suppressed.")

        return {
            "transcript": transcript,
            "recognized_keyword": recognized_keyword,
            "threat_level": dynamic_threat,
            "alert_dispatched": alert_dispatched
        }

    except sr.UnknownValueError:
        return {
            "transcript": "",
            "recognized_keyword": None,
            "threat_level": None,
            "alert_dispatched": False,
            "detail": "No clear speech recognized in audio segment"
        }
    except sr.RequestError as e:
        return {"error": f"Speech recognition service error: {e}"}


# ==========================================
# FASTAPI APPLICATION (CLIENT-SIDE API MODE)
# ==========================================

voice_app = FastAPI(
    title="AEGIS Emergency Voice Detection API",
    description="Client-side audio ingestion API for cloud deployment.",
    version="3.0.0"
)

voice_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AudioUploadPayload(BaseModel):
    audio_b64: Optional[str] = None
    audio: Optional[str] = None
    sample_rate: Optional[int] = 16000


@voice_app.get("/status", summary="Voice Sensor Status")
def get_voice_status():
    return {
        "status": "ONLINE",
        "service": "AEGIS AI Emergency Voice Detector",
        "input_source": INPUT_SOURCE,
        "port": SERVER_PORT,
        "backend_url": BACKEND_ALERT_URL,
        "camera_server_url": CAMERA_SERVER_URL
    }


@voice_app.post("/api/voice/audio", summary="Upload Audio from Client-Side API")
@voice_app.post("/api/voice/detect", summary="Upload Audio from Client-Side API")
@voice_app.post("/upload_audio", summary="Upload Audio from Client-Side API")
async def api_upload_audio(
    file: Optional[UploadFile] = File(None),
    payload: Optional[AudioUploadPayload] = None,
    request: Request = None
):
    """
    Client-side API endpoint for cloud deployment.
    Accepts audio via multipart file ('file' or 'audio'), raw binary WAV/PCM, or JSON base64.
    """
    raw_bytes = None
    sample_rate = 16000

    if file:
        raw_bytes = await file.read()
    elif payload and (payload.audio_b64 or payload.audio):
        b64_str = payload.audio_b64 or payload.audio
        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(b64_str)
            if payload.sample_rate:
                sample_rate = payload.sample_rate
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 audio encoding: {e}")
    elif request:
        body = await request.body()
        if body:
            try:
                import json
                data = json.loads(body)
                b64_cand = data.get("audio_b64") or data.get("audio")
                if b64_cand:
                    if "," in b64_cand:
                        b64_cand = b64_cand.split(",", 1)[1]
                    raw_bytes = base64.b64decode(b64_cand)
                    if "sample_rate" in data:
                        sample_rate = int(data["sample_rate"])
            except Exception:
                raw_bytes = body

    if not raw_bytes:
        raise HTTPException(status_code=400, detail="No audio payload provided.")

    # Parse container (WAV) or raw PCM
    try:
        with sr.AudioFile(io.BytesIO(raw_bytes)) as source:
            audio_data = recognizer.record(source)
    except Exception:
        # Fall back to raw 16-bit PCM (sample_rate, 2 bytes per sample)
        audio_data = sr.AudioData(raw_bytes, sample_rate, 2)

    result = process_audio_data(audio_data)
    return {
        "status": "success",
        "input_source": INPUT_SOURCE,
        **result
    }


def main():
    global last_alert_time

    print("\n" + "=" * 65)
    print("AEGIS AI EMERGENCY VOICE DETECTOR")
    source_label = "CLIENT-SIDE APIS (Cloud Mode)" if INPUT_SOURCE == "api" else "LOCAL HARDWARE MICROPHONE"
    print(f"Input Source:    {INPUT_SOURCE.upper()} - {source_label}")
    print(f"Backend Target:  {BACKEND_ALERT_URL}")
    print(f"Cooldown Period: {COOLDOWN_SECONDS}s")
    print("Threat Tiers:")
    print("  - CRITICAL:", ", ".join(CRITICAL_KEYWORDS))
    print("  - HIGH:    ", ", ".join(HIGH_KEYWORDS))
    print("  - LOW:     ", ", ".join(LOW_KEYWORDS))
    print("=" * 65 + "\n")

    if INPUT_SOURCE == "api":
        print(f"Listening for client-side audio payloads on http://{SERVER_HOST}:{SERVER_PORT}/api/voice/audio ...")
        uvicorn.run(voice_app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
    else:
        if sd is None:
            print("❌ sounddevice is not available or audio device is missing.")
            print("💡 For cloud deployment without microphones, run with: --input-source api")
            sys.exit(1)

        print("\n🎧 Continuous microphone listening active (Press Ctrl+C to stop)...")
        duration = 4
        sample_rate = 16000

        while True:
            try:
                print("\n🎤 Listening for voice (4 seconds)...")
                audio = sd.rec(
                    int(duration * sample_rate),
                    samplerate=sample_rate,
                    channels=1,
                    dtype="int16"
                )
                sd.wait()

                audio_bytes = audio.tobytes()
                audio_data = sr.AudioData(audio_bytes, sample_rate, 2)
                process_audio_data(audio_data)

            except KeyboardInterrupt:
                print("\n🛑 Stopping AI Emergency Voice Detector.")
                break
            except Exception as err:
                print(f"⚠️ Audio processing error: {err}")
                time.sleep(1)


if __name__ == "__main__":
    main()