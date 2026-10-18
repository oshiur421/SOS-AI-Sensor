# ==============================================================================
# SOS-AI-SENSOR: MULTI-SERVICE AI SURVEILLANCE & SENSOR CONTAINER
# ==============================================================================
# Base Image: Lightweight Python 3.11 Debian Slim
# Provides a minimal footprint while retaining glibc compatibility required
# by MediaPipe, OpenCV, Scikit-Learn, PyTorch/YOLO, and PortAudio.
# ==============================================================================
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr for real-time logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    INPUT_SOURCE=api

# Set the working directory inside the container
WORKDIR /app

# Install runtime system libraries:
# - libgl1 & libglib2.0-0: OpenCV computer vision runtime dependencies
# - libegl1 & libegl1-mesa: EGL runtime dependencies (fixes libEGL.so.1)
# - libgomp1: OpenMP runtime for accelerated matrix operations
# - libportaudio2 & libasound2: SoundDevice audio capture runtime
# - curl: Container health checks and network diagnostics
RUN apt-get update && apt-get install --no-install-recommends -y \
    libgl1 \
    libglib2.0-0 \
    libegl1 \
    libegl1-mesa \
    libgomp1 \
    libportaudio2 \
    libasound2 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy all project files into the container
COPY . .

# Expose microservice ports:
# - 8001: Central Camera Streaming & PTZ Server (FastAPI)
# - 8002: Pose Fall Detection Server (FastAPI)
# - 5000: Hand Gesture Landmark & Detection Server (Flask)
# - 8003: Emergency Voice & Acoustic Monitor (FastAPI)
EXPOSE 8001 8002 5000 8003

# Set entry point to execute the master coordination script.
# This script spawns and supervises all sensor microservices (camera_server.py,
# fall_detection.py, test_landmark_gesture.py, and voice_emergency.py)
# concurrently using subprocesses, honoring the configured --input-source.
ENTRYPOINT ["python", "start_aegis.py"]
CMD []
