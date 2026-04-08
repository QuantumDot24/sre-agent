FROM python:3.11-slim

# System deps for llama-cpp-python (CPU build by default in Docker)
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# CPU-only llama-cpp-python build for Docker (GPU available on host dev)
RUN pip install --no-cache-dir --upgrade pip && \
    CMAKE_ARGS="-DLLAMA_CUBLAS=OFF" pip install --no-cache-dir llama-cpp-python==0.2.90 && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Create necessary directories
RUN mkdir -p data/chroma data/medusa_repo models

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
