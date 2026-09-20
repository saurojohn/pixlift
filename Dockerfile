# PixLift Dockerfile
# 单镜像：PyTorch + Real-ESRGAN + FastAPI
# MPS（macOS）不在容器内走 — Linux 上跑 CUDA 或 CPU
#
# Build: docker build -t pixlift:latest .
# Run:   docker run -p 8000:8000 -v $(pwd)/models:/app/models pixlift:latest

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 系统依赖：PyTorch 在 Linux 需要 libgomp / libjpeg 等
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libjpeg-dev \
        libpng-dev \
        libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

# 先 copy 依赖文件，利用 Docker 缓存
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install \
        "torch>=2.3,<3.0" \
        "torchvision" \
        "realesrgan" \
        "basicsr" \
        "opencv-python-headless" \
        "scipy" \
        "tqdm" \
        "addict" \
        "yapf" \
        "fastapi>=0.115,<1.0" \
        "uvicorn[standard]>=0.30,<1.0" \
        "python-multipart>=0.0.9" \
        "pillow>=10.4.0,<13.0" \
        "pydantic>=2.5,<3.0"

# 再 copy 源码
COPY pixlift/ ./pixlift/
COPY run.py ./

# 模型 + 临时目录（卷挂载）
RUN mkdir -p /app/models /app/tmp /app/logs

# 非 root 运行
RUN useradd --create-home --shell /bin/bash pixlift && \
    chown -R pixlift:pixlift /app
USER pixlift

EXPOSE 8000

# 健康检查（用 service 自己暴露的 /api/health）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health').read()" || exit 1

# 默认参数：LAN 模式（HOST=0.0.0.0），CPU 推理（容器内通常无 GPU）
ENV HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=info \
    MAX_CONCURRENT_UPSCALES=1

CMD ["python", "run.py"]