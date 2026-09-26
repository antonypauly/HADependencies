ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gfortran \
    libopenblas-dev \
    pkg-config \
    libsodium-dev \
    libffi-dev \
    curl \
    ca-certificates \
    patchelf \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel auditwheel
