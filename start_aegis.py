"""
=============================================================================
AEGIS EMERGENCY SOS SYSTEM - MASTER MICROSERVICES LAUNCHER (start_aegis.py)
=============================================================================
Orchestrates and runs all AEGIS system microservices concurrently from a single terminal:
  1. camera_server.py         - Shared Central Webcam Stream & Hardware/API Server (FastAPI :8001)
  2. fall_detection.py        - MediaPipe Pose Fall Detection Engine (FastAPI :8002 / Stream)
  3. test_landmark_gesture.py - Hand Gesture Landmarker & Video Feed Server (Flask :5000)
  4. voice_emergency.py       - Acoustic Speech Distress & SOS Keyword Monitor (FastAPI :8003 / Mic)
  5. [Optional] Dashboard     - Python dashboard server script (if app.py or server.py exists)

Provides unified configuration flag (--input-source hardware|api) to toggle between
local hardware devices and cloud client-side APIs, managing all microservices concurrently
with graceful shutdown (Ctrl+C).
=============================================================================
"""

import os
import sys
import time
import argparse
import subprocess

# Ensure UTF-8 output across environments
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(CURRENT_DIR, "camera_server.py")):
    BASE_DIR = CURRENT_DIR
elif os.path.exists(os.path.join(CURRENT_DIR, "..", "SOS-AI-Sensor", "camera_server.py")):
    BASE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "SOS-AI-Sensor"))
elif os.path.exists(r"C:\SOS-AI-Sensor\camera_server.py"):
    BASE_DIR = r"C:\SOS-AI-Sensor"
else:
    BASE_DIR = CURRENT_DIR

SERVICES = [
    "camera_server.py",
    "fall_detection.py",
    "test_landmark_gesture.py",
    "voice_emergency.py"
]

for dashboard_candidate in ("server.py", "app.py"):
    candidate_path = os.path.join(BASE_DIR, dashboard_candidate)
    if os.path.exists(candidate_path) and dashboard_candidate not in SERVICES:
        SERVICES.append(dashboard_candidate)
        break


def main():
    parser = argparse.ArgumentParser(description="AEGIS Emergency SOS System - Master Launcher")
    parser.add_argument(
        "--input-source",
        type=str,
        default=os.environ.get("INPUT_SOURCE", "hardware"),
        choices=["hardware", "api", "cloud"],
        help="Input source mode: 'hardware' (local webcam & mic) or 'api'/'cloud' (client-side API ingestion)"
    )
    cli_args, _ = parser.parse_known_args()
    input_source = "api" if cli_args.input_source.lower() in ["api", "cloud", "client"] else "hardware"

    source_desc = (
        "CLIENT-SIDE APIS (Cloud Deployment Mode)"
        if input_source == "api"
        else "LOCAL HARDWARE DEVICES (Webcam & Microphone)"
    )

    print("======================================================================")
    print("🛡️  AEGIS EMERGENCY SOS SYSTEM - MASTER LAUNCHER")
    print("======================================================================")
    print(f"[*] Base Directory:      {BASE_DIR}")
    print(f"[*] Python Interpreter:  {sys.executable}")
    print(f"[*] Input Source Mode:   {input_source.upper()} - {source_desc}")
    print(f"[*] Services to Launch:  {len(SERVICES)}")
    for idx, s in enumerate(SERVICES, 1):
        print(f"    {idx}. {s}")
    print("======================================================================\n")

    processes = []
    env = os.environ.copy()
    env["INPUT_SOURCE"] = input_source

    for script in SERVICES:
        script_path = os.path.join(BASE_DIR, script)
        if not os.path.exists(script_path):
            print(f"[⚠️ WARNING] Script '{script}' not found at {script_path}. Skipping.")
            continue

        cmd = [sys.executable, script_path, "--input-source", input_source]

        print(f"[🚀 LAUNCHING] Starting microservice: {script} (Source: {input_source.upper()}) ...")
        try:
            proc = subprocess.Popen(cmd, cwd=BASE_DIR, env=env)
            proc.service_name = script
            proc._reported_exit = False
            processes.append(proc)
            print(f"[✅ RUNNING]   {script} started successfully (PID: {proc.pid})")

            # Stagger launch slightly to give camera server initialization priority
            if script == "camera_server.py":
                time.sleep(1.5)
            else:
                time.sleep(0.5)
        except Exception as err:
            print(f"[❌ ERROR] Failed to start {script}: {err}")

    print("\n======================================================================")
    print(f"[*] All active microservices ({len(processes)}/{len(SERVICES)}) are running concurrently.")
    print(f"[*] Input Source configured across all services: {input_source.upper()}")
    print("[*] Press Ctrl+C at any time to gracefully terminate all services.")
    print("======================================================================\n")

    try:
        while True:
            time.sleep(1)
            for proc in processes:
                exit_code = proc.poll()
                if exit_code is not None:
                    name = getattr(proc, 'service_name', f"PID {proc.pid}")
                    if not getattr(proc, '_reported_exit', False):
                        print(f"[⚠️ WARNING] Microservice '{name}' (PID: {proc.pid}) exited unexpectedly with code {exit_code}")
                        proc._reported_exit = True
    except KeyboardInterrupt:
        print("\n\n======================================================================")
        print("[🛑 SHUTDOWN] KeyboardInterrupt received. Gracefully stopping all services...")
        print("======================================================================")
        for proc in processes:
            name = getattr(proc, 'service_name', f"PID {proc.pid}")
            if proc.poll() is None:
                print(f"[⏹️ TERMINATING] Shutting down {name} (PID: {proc.pid}) ...")
                try:
                    proc.terminate()
                except Exception as term_err:
                    print(f"[!] Error terminating {name}: {term_err}")
            else:
                print(f"[✓] {name} already stopped.")

        print("\n[*] Waiting for background processes to exit cleanly...")
        for proc in processes:
            name = getattr(proc, 'service_name', f"PID {proc.pid}")
            try:
                proc.wait(timeout=4)
                print(f"[✓] {name} terminated cleanly.")
            except subprocess.TimeoutExpired:
                print(f"[⚠️ TIMEOUT] {name} (PID: {proc.pid}) did not exit in time. Killing forcefully...")
                try:
                    proc.kill()
                    proc.wait()
                    print(f"[✓] {name} forcefully killed.")
                except Exception as kill_err:
                    print(f"[!] Error killing {name}: {kill_err}")

        print("\n======================================================================")
        print("🛡️  All AEGIS background microservices stopped. Cleanup complete.")
        print("======================================================================\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
