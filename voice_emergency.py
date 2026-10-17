import os
import sys
import time
import base64
import threading
import requests
import sounddevice as sd
import speech_recognition as sr

# Ensure Windows terminal handles UTF-8 emojis without charmap encoding errors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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


def main():
    global last_alert_time

    print("===================================")
    print("   AI EMERGENCY VOICE DETECTOR")
    print("===================================")
    print(f"Backend Target: {BACKEND_ALERT_URL}")
    print(f"Cooldown Period: {COOLDOWN_SECONDS}s")
    print("Threat Tiers:")
    print("  - CRITICAL:", ", ".join(CRITICAL_KEYWORDS))
    print("  - HIGH:    ", ", ".join(HIGH_KEYWORDS))
    print("  - LOW:     ", ", ".join(LOW_KEYWORDS))
    print("\n🎧 Continuous microphone listening active (Press Ctrl+C to stop)...")

    duration = 4
    sample_rate = 16000

    recognizer = sr.Recognizer()

    while True:
        try:
            current_time = time.time()
            print("\n🎤 Listening for voice (4 seconds)...")
            
            # Record audio from default microphone
            audio = sd.rec(
                int(duration * sample_rate),
                samplerate=sample_rate,
                channels=1,
                dtype="int16"
            )
            sd.wait()

            # Convert audio to SpeechRecognition format
            audio_bytes = audio.tobytes()
            audio_data = sr.AudioData(audio_bytes, sample_rate, 2)

            try:
                transcript = recognizer.recognize_google(audio_data)
                transcript_clean = transcript.lower().strip()

                print(f"🗣️ You said: \"{transcript}\"")

                # Dynamically evaluate threat level and recognized keyword
                recognized_keyword, dynamic_threat = evaluate_voice_threat(transcript_clean)

                if recognized_keyword:
                    print(f"🚨 EMERGENCY SPEECH DETECTED! Keyword: '{recognized_keyword}' | Threat Level: {dynamic_threat}")

                    # Check cooldown to prevent API spamming
                    if current_time - last_alert_time >= COOLDOWN_SECONDS:
                        # 1. Attempt to capture and confirm snapshot with retries
                        print("📸 Fetching live optical snapshot from Shared Camera Server...")
                        snapshot_b64, snapshot_err = capture_snapshot_with_retry(timeout=2.0, max_retries=2)

                        if snapshot_b64:
                            print(f"✅ [SNAPSHOT CONFIRMED] Live image captured & base64 encoded ({len(snapshot_b64)} chars).")
                        else:
                            print(f"🚨 [CRITICAL ERROR] Snapshot fetch failed from Shared Camera Server: {snapshot_err}")
                            print("⚠️ Proceeding to transmit voice alert without snapshot as required.")

                        # 2. Build payload ONLY after snapshot is confirmed or failed
                        raw_data = {
                            "transcript": transcript,
                            "keyword": recognized_keyword,
                            "threat_level": dynamic_threat,
                            "sensor": "Speech Recognition Microphone"
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

                        # 3. Transmit confirmed payload to backend
                        try:
                            response = requests.post(BACKEND_ALERT_URL, json=payload, timeout=2.5)
                            if response.status_code == 201:
                                last_alert_time = current_time
                                alert_id = response.json().get('id', 'N/A')
                                snapshot_status = f"Attached ({len(snapshot_b64)} chars)" if snapshot_b64 else "None (Camera Busy)"
                                print(f"✅ [SUCCESS] Emergency alert sent to backend! (Alert #{alert_id} | Threat: {dynamic_threat} | Snapshot: {snapshot_status} | Status: 201 Created)")

                                # 4. Dispatch Telegram mobile alert in non-blocking background thread
                                print("📱 Forwarding emergency alert to Telegram mobile bot...")
                                alert_type_desc = f"Voice Distress ({recognized_keyword})"
                                threading.Thread(
                                    target=send_telegram_alert,
                                    args=(alert_type_desc, dynamic_threat, 23.8769, 90.3204, snapshot_b64),
                                    daemon=True
                                ).start()
                            else:
                                print(f"⚠️ [WARNING] Backend responded with status {response.status_code}: {response.text}")
                        except requests.exceptions.RequestException as req_err:
                            print(f"❌ [ALERT ERROR] Failed to send voice alert to backend: {req_err}")
                    else:
                        cooldown_remaining = int(COOLDOWN_SECONDS - (current_time - last_alert_time))
                        print(f"⏳ Cooldown active ({cooldown_remaining}s remaining). Alert suppressed.")

                else:
                    print("✅ No emergency keyword detected.")

            except sr.UnknownValueError:
                # Silence or unrecognised audio segment
                print("🔇 (No speech detected)")
            except sr.RequestError as e:
                print(f"❌ Speech recognition service error: {e}")

        except KeyboardInterrupt:
            print("\n🛑 Stopping AI Emergency Voice Detector.")
            break
        except Exception as err:
            print(f"⚠️ Audio processing error: {err}")
            time.sleep(1)


if __name__ == "__main__":
    main()