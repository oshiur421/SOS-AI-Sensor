import sounddevice as sd
import speech_recognition as sr

print("🎤 Speech Recognition Test")
print("Speak clearly for 5 seconds...")
print("Try saying: HELP ME")

duration = 5
sample_rate = 16000

audio = sd.rec(
    int(duration * sample_rate),
    samplerate=sample_rate,
    channels=1,
    dtype="int16"
)

sd.wait()

print("\n🎧 Recording finished.")
print("🔄 Converting speech to text...")

audio_bytes = audio.tobytes()

recognizer = sr.Recognizer()

audio_data = sr.AudioData(
    audio_bytes,
    sample_rate,
    2
)

try:
    text = recognizer.recognize_google(audio_data)

    print("\n🗣️ You said:")
    print(text)

except sr.UnknownValueError:
    print("\n❌ Could not understand the speech.")

except sr.RequestError as e:
    print("\n❌ Speech recognition service error:")
    print(e)