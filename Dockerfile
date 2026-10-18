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
# - libegl1 & libgles2: EGL & GLES runtime dependencies (fixes libEGL.so.1 & libGLESv2.so.2 for MediaPipe)
# - libgomp1: OpenMP runtime for accelerated matrix operations
# - libportaudio2 & libasound2: SoundDevice audio capture runtime
# - curl: Container health checks and network diagnostics
# - ffmpeg: Audio/video transcoding and decoding (WebM, OGG, WAV)
RUN apt-get update && apt-get install --no-install-recommends -y \
    libgl1 \
    libglib2.0-0 \
    libegl1 \
    libgles2 \
    libgomp1 \
    libportaudio2 \
    libasound2 \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy all project files into the container
COPY . .

# Expose single public entry port for container platforms (Render, Cloud Run, etc.)
# 8000: AEGIS Unified Sensor API Gateway (routes to internal camera :8001, voice :8003, fall :8002, gesture :5000)
EXPOSE 8000

# Set entry point to execute the master API Gateway orchestrator.
# Spawns internal microservices and routes all external requests through the unified port.
CMD ["python", "master.py"]