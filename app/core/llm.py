r"""LLM 三件套接口与确定性 Stub：embed / rerank / chat_stream 全部可替身、可注入故障。

接口以 Protocol 结构化声明（与 clock.py 的 Clock 同款）：Embedder（dim=1024）、
Reranker（sigmoid 归一化）、LLMProvider（流式逐段产出 str）。Stub 三件套同输入必
同输出——播种一律用 sha256/blake2b 摘要（内置 hash 受 PYTHONHASHSEED 影响禁用）。
故障注入构造参数：fail_times（前 N 次调用抛 UpstreamError，未产出内容）、hang_s
（挂起路径：记录 hang 事件并经 time.sleep 阻塞，测试用 monkeypatch 短 sleep 替换）、
break_after（流式产出 N 段后抛 UpstreamError(broken=True)，表示已产出部分内容的
断裂）。分数口径（standards §4.4，全局统一，两套分数不得混用阈值）：
cosine_to_similarity(dist) = 1 − dist；reranker logit 经 sigmoid 归一化到开区间
(0,1)。TEI / OpenAI 兼容云真实现见同目录 tei.py、openai_compat.py 骨架（W2 激活）。
"""

import hashlib
import math
import random
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol, runtime_checkable

EMBED_DIM: int = 1024

DEFAULT_CHAT_CHUNKS: tuple[str, str, str] = (
    "根据知识库检索结果，",
    "企业智能工作台支持文档上传、解析清洗与向量检索问答。",
    "如需引用来源请查看下方命中片段。",
)


class UpstreamError(RuntimeError):
    """上游 LLM 服务故障：broken=False 为调用前失败（未产出内容，可整体重试）；
    broken=True 为流式中途断裂（已产出部分内容，需按断流语义处理）。"""

    def __init__(self, message: str = "upstream llm failure", broken: bool = False) -> None:
        super().__init__(message)
        self.broken: bool = broken


@runtime_checkable
class Embedder(Protocol):
    """向量嵌入接口：输入文本列表，输出与输入等长的 EMBED_DIM 维向量列表。"""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class Reranker(Protocol):
    """重排接口：query 对候选 docs 打分，输出每个 doc 的 (0,1) 相关性分数。"""

    def rerank(self, query: str, docs: list[str]) -> list[float]: ...


@runtime_checkable
class LLMProvider(Protocol):
    """流式对话接口：按产出顺序逐段 yield 文本片段，总量受 max_tokens 约束。"""

    def chat_stream(
        self, messages: Sequence[Mapping[str, str]], max_tokens: int
    ) -> Iterator[str]: ...


def cosine_to_similarity(dist: float) -> float:
    """余弦距离 → 相似度（standards §4.4：pgvector <=>，相似度 = 1 − 距离）。"""
    return 1.0 - dist


def sigmoid(x: float) -> float:
    """logit → (0,1) 分数（standards §4.4：bge-reranker 分数经 sigmoid 归一化）。

    按 x 符号分支计算，避免大负数 math.exp 上溢（数值稳定）。
    """
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def _record_and_sleep(hang_s: float, hang_events: list[float]) -> None:
    """挂起注入：记录 hang 事件并阻塞 hang_s（测试以 monkeypatch 短 sleep 替换 time.sleep）。"""
    if hang_s > 0:
        hang_events.append(hang_s)
        time.sleep(hang_s)


class StubEmbedder:
    """确定性假 Embedder：sha256(text) 播种 MT19937 高斯采样后 L2 归一化，同输入必同输出。"""

    def __init__(self, dim: int = EMBED_DIM, fail_times: int = 0, hang_s: float = 0.0) -> None:
        self.dim: int = dim
        self.fail_times: int = fail_times
        self.hang_s: float = hang_s
        self.hang_events: list[float] = []
        self.embed_calls: int = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        """挂起 → 注入失败 → 逐文本确定性向量；fail 次数耗尽后与干净 Stub 完全一致。"""
        self.embed_calls += 1
        _record_and_sleep(self.hang_s, self.hang_events)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise UpstreamError(f"stub embed forced failure (call #{self.embed_calls})")
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        vec = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(component * component for component in vec))
        return [component / norm for component in vec]


class StubReranker:
    """确定性假 Reranker：logit 取注入权重（缺省 blake2b(query+NUL+doc) 派生），sigmoid 归一化。"""

    def __init__(
        self,
        weights: Sequence[float] | None = None,
        fail_times: int = 0,
        hang_s: float = 0.0,
    ) -> None:
        self.weights: Sequence[float] | None = weights
        self.fail_times: int = fail_times
        self.hang_s: float = hang_s
        self.hang_events: list[float] = []
        self.rerank_calls: int = 0

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        """权重与 docs 等长校验后逐位 sigmoid；分数全在开区间 (0,1) 且随 logit 保序。"""
        self.rerank_calls += 1
        _record_and_sleep(self.hang_s, self.hang_events)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise UpstreamError(f"stub rerank forced failure (call #{self.rerank_calls})")
        if self.weights is not None:
            if len(self.weights) != len(docs):
                raise ValueError(f"weights length {len(self.weights)} != docs length {len(docs)}")
            logits: list[float] = [float(weight) for weight in self.weights]
        else:
            logits = [self._default_logit(query, doc) for doc in docs]
        return [sigmoid(logit) for logit in logits]

    @staticmethod
    def _default_logit(query: str, doc: str) -> float:
        """blake2b(query + NUL + doc) 8 字节映射到 [-4.0, 4.0)：确定性且对 query/doc 均敏感。"""
        payload = (query + "\x00" + doc).encode("utf-8")
        digest = hashlib.blake2b(payload, digest_size=8).digest()
        return (int.from_bytes(digest, "big") % 8000) / 1000.0 - 4.0


class StubLLMProvider:
    """确定性假 LLM：固定文案分段产出；fail_times 调用即抛（未产出段），
    break_after=N 产出 N 段后抛 UpstreamError(broken=True)，总量截到 max_tokens 字符。"""

    def __init__(
        self,
        chunks: Sequence[str] | None = None,
        fail_times: int = 0,
        hang_s: float = 0.0,
        break_after: int | None = None,
    ) -> None:
        self.chunks: tuple[str, ...] = tuple(chunks) if chunks is not None else DEFAULT_CHAT_CHUNKS
        self.fail_times: int = fail_times
        self.hang_s: float = hang_s
        self.break_after: int | None = break_after
        self.hang_events: list[float] = []
        self.stream_calls: int = 0

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        """挂起/失败发生在取迭代器时（未产出任何段）；断流发生在迭代中途（已产出部分段）。"""
        self.stream_calls += 1
        _record_and_sleep(self.hang_s, self.hang_events)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise UpstreamError(f"stub chat_stream forced failure (call #{self.stream_calls})")
        return self._stream(max_tokens)

    def _stream(self, max_tokens: int) -> Iterator[str]:
        remaining = max_tokens
        for produced, chunk in enumerate(self.chunks):
            # produced 恰为当前 chunk 之前已产出的段数：break_after=N 产出 N 段后断裂
            if self.break_after is not None and produced >= self.break_after:
                raise UpstreamError(f"stub stream broken after {produced} chunks", broken=True)
            if remaining <= 0:
                return
            piece = chunk[:remaining]
            remaining -= len(piece)
            yield piece
