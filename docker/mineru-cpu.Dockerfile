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
# torch 预装 CPU 版（+cpu 仅存在于 PyTorch 轮子镜像源）：否则 pip 默认解析 CUDA 栈，
# 多下载 3-4GB（弱网环境构建时间不可接受，实测国内源 150KB/s 级）。
RUN pip install --no-cache-dir \
      -f https://mirrors.aliyun.com/pytorch-wheels/cpu/ \
      "torch==2.5.1+cpu" \
    && pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
      "mineru[core]==2.5.4" "modelscope"

EXPOSE 8000
CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8000"]
