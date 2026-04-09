# ─────────────────────────────────────────────────────────────────────────────
# SRE Agent — Dockerfile
# Base: CUDA 12.6 devel (nvcc included — required to compile llama-cpp-python)
# llama-cpp-python: JamePeng fork, built from source with CUDA support.
# ─────────────────────────────────────────────────────────────────────────────
FROM nvidia/cuda:12.6.3-cudnn-devel-ubuntu22.04

# ── System deps ───────────────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-dev \
    python3-pip \
    build-essential \
    cmake \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
 && update-alternatives --install /usr/bin/pip    pip    /usr/bin/pip3       1

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python deps (everything except the llama fork) ───────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
    fastapi==0.115.12 \
    "uvicorn[standard]==0.34.0" \
    python-multipart==0.0.20 \
    httpx==0.28.1 \
    python-dotenv==1.1.0 \
    structlog==25.1.0 \
    langfuse==3.0.3 \
    chromadb==1.0.4 \
    sentence-transformers==4.1.0 \
    huggingface-hub==0.30.2

# ── JamePeng llama-cpp-python fork — compiled with CUDA ──────────────────────
# Provides Qwen35ChatHandler + multimodal support required by inference.py.
# Build takes ~5-10 min on first docker build; result is cached afterwards.
RUN CMAKE_ARGS="-DGGML_CUDA=on" \
    FORCE_CMAKE=1 \
    pip install --no-cache-dir \
    "llama-cpp-python @ git+https://github.com/JamePeng/llama-cpp-python.git"

# ── Copy source code ──────────────────────────────────────────────────────────
COPY . .

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p /app/models /app/data

# ── GPU visibility ────────────────────────────────────────────────────────────
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

# ── App defaults (override via .env or docker-compose environment) ────────────
ENV MODELS_DIR=/app/models
ENV LLM_CTX=8192
ENV LLM_GPU_LAYERS=35
ENV LOG_LEVEL=INFO
ENV LOG_FORMAT=json

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]