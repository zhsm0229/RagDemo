r"""入库向量化段（自 ingest_pipeline.py 拆出，行数红线 §1.4）：分批嵌入的批次预算与调用。

TEI --max-batch-tokens 2048（防 OOM 特意调低）下单请求超限即 422——2026-10-02 大文档
全量单请求全败根因。按字符预算切批（BGE tokenizer 中文≈1字1token，预算留 40% 余量），
逐批串行调用（TEI 单并发最稳），批间经 progress 回调上报进度（落库归 ingest 层）。
参数收原始依赖（embedder/batch_chars）而非 IngestDeps：本模块被 ingest_pipeline
导入，反向依赖会成环。
"""

import logging
import time
from collections.abc import Callable

from app.core.llm import Embedder

logger = logging.getLogger(__name__)


def embed_in_batches(
    embedder: Embedder,
    doc_id: int,
    texts: list[str],
    progress: Callable[[str, int], None] | None,
    batch_chars: int,
) -> list[list[float]]:
    """分批嵌入：按字符预算切批，逐批串行调用并上报进度，记录总耗时日志。"""
    if not texts:
        return []
    batches = split_by_chars(texts, batch_chars)
    logger.info(
        "向量化开始 doc_id=%s chunks=%d 批=%d 预算=%d字/批",
        doc_id,
        len(texts),
        len(batches),
        batch_chars,
    )
    vectors: list[list[float]] = []
    started = time.monotonic()
    for i, batch in enumerate(batches, 1):
        vectors.extend(embedder.embed(batch))
        if progress is not None:
            progress("向量化中", 45 + int(50 * i / len(batches)))
    logger.info(
        "向量化批次完成 doc_id=%s 批=%d 耗时=%.1fs",
        doc_id,
        len(batches),
        time.monotonic() - started,
    )
    return vectors


def split_by_chars(texts: list[str], budget: int) -> list[list[str]]:
    """按字符预算切批：超长单条独立成批（不截断内容——截断会丢向量语义）。
    budget<=0 = 不分批（仅测试桩场景）。"""
    if budget <= 0:
        return [texts]
    batches: list[list[str]] = []
    current: list[str] = []
    used = 0
    for text in texts:
        length = len(text)
        if current and used + length > budget:
            batches.append(current)
            current, used = [], 0
        current.append(text)
        used += length
    if current:
        batches.append(current)
    return batches
