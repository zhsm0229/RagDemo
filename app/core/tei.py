r"""TEI（HuggingFace Text-Embeddings-Inference）真实现：embed / rerank HTTP 客户端。

激活口径（standards §4.4）：embed 走 TEI /embed（bge-m3，dim=1024，L2 归一化）；
rerank 走 TEI /rerank（bge-reranker-v2-m3，原始 logit 经 app.core.llm.sigmoid 归一化
到开区间 (0,1)）。超时/重试与断流语义（UpstreamError.broken）与 Stub 一致：
超时 timeout_s（构造参数），失败重试 1 次（恰 2 次尝试，每次独立新请求/新短连接），
重试对连接错误/超时/5xx 生效，4xx 请求本身有误不重试；一切失败（传输层错误、非 2xx、
JSON 解析/形状不符、维度或覆盖不符）统一抛 UpstreamError(broken=False)，fail-closed
防静默错维入库。测试以构造参数注入 httpx.MockTransport 替身（transport 缺省 None →
httpx.Client 默认传输）；模块不加全局 client，方法内 with 短连接，单机内网延迟可接受。
"""

import httpx

from app.core.llm import EMBED_DIM, UpstreamError, sigmoid

_MAX_ATTEMPTS: int = 2  # 失败重试 1 次：最多 2 次尝试（每次独立新请求）


def _post_with_retry(
    base_url: str,
    timeout_s: float,
    path: str,
    payload: dict[str, object],
    transport: httpx.BaseTransport | None,
) -> httpx.Response:
    """POST 一次重试：连接错误/超时/5xx 重试 1 次；4xx 不重试直接抛 UpstreamError。"""
    last_error: UpstreamError | None = None
    for _ in range(_MAX_ATTEMPTS):
        try:
            # 每次尝试独立新请求：with 短连接用毕即关（不持长连接，简单可靠优先）
            with httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport) as client:
                response = client.post(path, json=payload)
        except httpx.HTTPError as exc:  # 连接错误/超时等传输层故障：可重试
            last_error = UpstreamError(f"tei {path} 传输失败: {type(exc).__name__}: {exc}")
            continue
        if 200 <= response.status_code < 300:
            return response
        last_error = UpstreamError(f"tei http {response.status_code}")
        if response.status_code < 500:
            raise last_error  # 4xx：请求本身有误，重试无意义
    assert last_error is not None  # 循环至少执行一次，两次皆败路径必已赋值
    raise last_error


def _parse_json(response: httpx.Response, kind: str) -> object:
    """响应体解析为 JSON；解析失败归 UpstreamError（不裸抛 json 异常）。

    异常族取 ValueError 而非仅 json.JSONDecodeError：200 + 非 UTF-8 体在
    httpx 文本解码层抛 UnicodeDecodeError（ValueError 族），同样必须收口。
    """
    try:
        return response.json()
    except ValueError as exc:  # 含 JSONDecodeError 与 UnicodeDecodeError（同族收口）
        raise UpstreamError(f"tei {kind} 响应不是合法 JSON: {exc}") from exc


class TeiEmbedder:
    """TEI /embed 真实现：HTTP 批量调用 + 一次重试/超时（与 Stub 语义一致，可替身）。"""

    def __init__(
        self,
        base_url: str,
        model: str = "bge-m3",
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url: str = base_url
        self.model: str = model
        self.timeout_s: float = timeout_s
        self.transport: httpx.BaseTransport | None = transport

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入：POST /embed，返回与 texts 等长的 EMBED_DIM 维向量列表（fail-closed）。"""
        response = _post_with_retry(
            self.base_url, self.timeout_s, "/embed", {"inputs": texts}, self.transport
        )
        vectors = _parse_json(response, "embed")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise UpstreamError(f"tei embed 响应与输入不等长（期望 {len(texts)} 条）")
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != EMBED_DIM:
                raise UpstreamError(
                    f"tei embed 向量维度不符（期望 EMBED_DIM={EMBED_DIM}，fail-closed 防错维入库）"
                )
        return vectors


class TeiReranker:
    """TEI /rerank 真实现：相关性打分 + sigmoid 归一化（logit → (0,1)，与 Stub 同口径）。"""

    def __init__(
        self,
        base_url: str,
        model: str = "bge-reranker-v2-m3",
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url: str = base_url
        self.model: str = model
        self.timeout_s: float = timeout_s
        self.transport: httpx.BaseTransport | None = transport

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        """相关性重排：POST /rerank，响应按 index 还原输入顺序，逐个 sigmoid(score)。

        真实 TEI 响应为裸数组 [{index, score}]（2026-09-30 对接 cpu-1.9.2 实测）；
        {"results": [...]} 包裹形态保留兼容（历史替身/代理网关可能加包裹）。"""
        payload: dict[str, object] = {"query": query, "texts": docs}
        response = _post_with_retry(
            self.base_url, self.timeout_s, "/rerank", payload, self.transport
        )
        body = _parse_json(response, "rerank")
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list):
            raise UpstreamError("tei rerank 响应形状不符（缺少结果数组）")
        scores: list[float | None] = [None] * len(docs)
        for item in items:
            if not isinstance(item, dict):
                raise UpstreamError("tei rerank 结果项不是对象")
            index, score = item.get("index"), item.get("score")
            if type(index) is not int or not 0 <= index < len(docs) or scores[index] is not None:
                # type is int 显式排除 bool 子类（Task 3 评审收口）：True/False 不是合法 index
                raise UpstreamError("tei rerank index 越界或重复（fail-closed）")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                scores[index] = sigmoid(float(score))
            else:
                raise UpstreamError("tei rerank score 不是数值")
        filled = [score for score in scores if score is not None]
        if len(filled) != len(docs):
            raise UpstreamError("tei rerank 结果未覆盖全部输入（fail-closed）")
        return filled
