# TEI v1.9.2 arm64 原生构建（工单 29）——官方 cpu-1.9.2 仅 linux/amd64（buildx inspect 实证），
# arm64 服务器（含国产化 ARM）pull 直接失败。本文件基于官方 Dockerfile 精简：
# 去 sccache（官方拉 x86_64 musl 二进制，arm64 不可用）、去 MKL（intel-oneapi 无 arm64 包，
# CPU 矩阵运算走 ort/candle 特性），其余编译特性与官方 cpu 镜像对齐。
# 用法：
#   git clone --depth 1 --branch v1.9.2 https://github.com/huggingface/text-embeddings-inference.git /tmp/tei-src
#   docker buildx build --platform linux/arm64 -f docker/tei-arm64.Dockerfile /tmp/tei-src \
#     -t ragdemo-tei-cpu-arm64:cpu-1.9.2 --load
# 模型不烘进镜像（与 compose 挂载口径一致：./data/tei-models/ 以卷挂载绕开 TEI 下载器）。
FROM lukemathwalker/cargo-chef:latest-rust-1.92-bookworm AS chef
WORKDIR /usr/src

FROM chef AS planner
COPY backends backends
COPY core core
COPY router router
COPY Cargo.toml Cargo.lock ./
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential pkg-config libssl-dev && \
    rm -rf /var/lib/apt/lists/*
# planner 产物：依赖配方进 builder（cargo-chef 两阶段标准衔接，官方 Dockerfile 同款）
COPY --from=planner /usr/src/recipe.json recipe.json
# 依赖层缓存（registry/git 走 buildx cache mounts：依赖不变时增量重建跳过全量编译；
# Cargo.lock 钉 v1.9.2 依赖图，构建可复现）
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo chef cook --release --features ort,candle,static-linking,http --no-default-features --recipe-path recipe.json

COPY backends backends
COPY core core
COPY router router
COPY Cargo.toml Cargo.lock ./
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo build --release --bin text-embeddings-router --features ort,candle,static-linking,http --no-default-features

FROM debian:bookworm-slim
ENV HUGGINGFACE_HUB_CACHE=/data \
    PORT=80 \
    RAYON_NUM_THREADS=8
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libomp-dev ca-certificates libssl3 curl && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/src/target/release/text-embeddings-router /usr/local/bin/text-embeddings-router
ENTRYPOINT ["text-embeddings-router"]
CMD ["--json-output"]
