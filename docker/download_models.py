r"""预热 TEI 本地模型目录（./data/tei-models/）——compose 中 tei-embed/tei-rerank 以
本地路径挂载启动，绕开 TEI 自带下载器（其 Rust 客户端与 hf-mirror 不兼容：不回
content-range 头；直连 huggingface.co 在内网不可达）。

依赖（不进 pyproject，手动装）：.venv/Scripts/python -m pip install huggingface_hub
用法：HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 \
      .venv/Scripts/python docker/download_models.py
注意 HF_HUB_DISABLE_XET=1 必须：新版 huggingface_hub 默认走 Xet 存储，会绕过镜像直连
cas-server.xethub.hf.co（401）。bge-m3 仓库无 safetensors，权重取 pytorch_model.bin；
bge-reranker-v2-m3 无 onnx 导出（走 candle 后端，启动内存峰值高，compose 已按此设限）。
"""

import os
from pathlib import Path

if os.environ.get("HF_ENDPOINT") != "https://hf-mirror.com":
    raise SystemExit("请以 HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 运行（见模块注释）")

from huggingface_hub import snapshot_download  # noqa: E402

DEST = Path(__file__).resolve().parents[1] / "data" / "tei-models"
CONFIG_PATTERNS = ["config.json", "tokenizer.json", "tokenizer_config.json",
                   "special_tokens_map.json", "sentencepiece.bpe.model"]

JOBS = (
    ("BAAI/bge-m3", [*CONFIG_PATTERNS, "pytorch_model.bin", "onnx/model.onnx"]),
    ("BAAI/bge-reranker-v2-m3", [*CONFIG_PATTERNS, "model.safetensors"]),
)

for repo, patterns in JOBS:
    path = snapshot_download(repo, local_dir=str(DEST / repo.split("/")[-1]),
                             allow_patterns=patterns)
    print("DONE", repo, "->", path)
