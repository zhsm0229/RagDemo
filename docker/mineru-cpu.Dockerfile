# MinerU 解析服务（pipeline 后端，CPU 推理）—— 本机/无 GPU 环境用。
# 生产 4C16G VM 若配 GPU，改用官方 GPU 镜像并按 §12.8 以 digest 引用；
# 模型在首次启动经 ModelScope 国内源下载进卷（dev 权宜；生产按 runbook 预置并登记 hash）。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    MINERU_MODEL_SOURCE=modelscope

# libgl1/libglib2.0-0：opencv 运行库；curl：healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

# 版本钉住：2.5.x 为 pipeline（ONNX 后端，§12.5）稳定线；PyPI 走阿里镜像。
# torch 预装 CPU 版（+cpu 仅在 PyTorch 轮子镜像源有）：否则 pip 默认解析 CUDA 栈，
# 多下载 3-4GB（弱网环境构建时间不可接受，实测国内源 150KB/s 级）。
# arm64 特例：阿里轮子源 2.5.1+cpu 无 aarch64 轮（arm64 +cpu 自 2.6.0 起），
# 故 arm64 钉 2.6.0+cpu；amd64 维持 2.5.1+cpu 不动。
# 拆两层：torch 层成功即缓存，mineru 依赖再失败不重下 174MB；PIP_DEFAULT_TIMEOUT
# 放宽（默认 15s，弱网必超时）+ retries 10。
ENV PIP_DEFAULT_TIMEOUT=120
ARG TARGETPLATFORM
RUN if [ "$TARGETPLATFORM" = "linux/arm64" ]; then TORCH_SPEC="torch==2.6.0+cpu"; \
    else TORCH_SPEC="torch==2.5.1+cpu"; fi \
 && pip install --no-cache-dir --retries 10 \
      -f https://mirrors.aliyun.com/pytorch-wheels/cpu/ \
      "$TORCH_SPEC" \
 && pip install --no-cache-dir --retries 10 -i https://mirrors.aliyun.com/pypi/simple/ \
      "mineru[core]==2.5.4" "modelscope" "$TORCH_SPEC"
# 末尾再次追加 $TORCH_SPEC：mineru 声明的 torch<3,>=2.6.0 是范围 specifier，
# pip 不拿已装的本地版本（+cpu）构建去满足范围——不加会从全量镜像重拉更高版 torch
# （GB 级下载且漂出稳定线）。精确钉住后命中已装版本直接 already satisfied。

EXPOSE 8000
CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8000"]
