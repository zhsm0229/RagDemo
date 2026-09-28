r"""TEI（HuggingFace Text-Embeddings-Inference）真实现骨架：W2 激活，当前仅保留接口形状。

激活口径（standards §4.4）：embed 走 TEI /embed（bge-m3，dim=1024，L2 归一化）；
rerank 走 TEI /rerank（bge-reranker-v2-m3，原始 logit 经 app.core.llm.sigmoid 归一化
到开区间 (0,1)）。超时/重试与断流语义（UpstreamError.broken）须与 Stub 一致，
便于后续管道测试以替身无缝替换。
"""

from app.core.llm import EMBED_DIM


class TeiEmbedder:
    """TEI /embed 真实现：W2 激活（HTTP 批量调用 + 重试/超时）。"""

    def __init__(self, base_url: str, model: str = "bge-m3", timeout_s: float = 10.0) -> None:
        self.base_url: str = base_url
        self.model: str = model
        self.timeout_s: float = timeout_s

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入：返回与 texts 等长的 EMBED_DIM 维向量列表（W2 激活）。"""
        raise NotImplementedError(f"TEI embed W2 激活（dim={EMBED_DIM}）")


class TeiReranker:
    """TEI /rerank 真实现：W2 激活（相关性打分 + sigmoid 归一化）。"""

    def __init__(
        self, base_url: str, model: str = "bge-reranker-v2-m3", timeout_s: float = 10.0
    ) -> None:
        self.base_url: str = base_url
        self.model: str = model
        self.timeout_s: float = timeout_s

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        """相关性重排：返回每个 doc 的 (0,1) 分数（W2 激活）。"""
        raise NotImplementedError("TEI rerank W2 激活")
