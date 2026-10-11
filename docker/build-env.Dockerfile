ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-bookworm
ARG RUST_VERSION=1.90.0
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    ninja-build \
    gfortran \
    libopenblas-dev \
    pkg-config \
    libsodium-dev \
    libffi-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel auditwheel "patchelf>=0.17"

RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal \
        --default-toolchain ${RUST_VERSION} \
    && rustc --version