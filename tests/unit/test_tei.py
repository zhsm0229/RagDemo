r"""TEI embed/rerank HTTP 客户端测试（task-3-brief Step 1 用例 ①-⑥）：MockTransport 注入。

断言表来源 task-3-brief：①正常批量 embed 逐值透传、顺序保持（POST {base_url}/embed，
body {"inputs": texts}，1024 维）；②非 2xx → UpstreamError(broken=False)（4xx 不重试）；
③第一次 500 第二次 200 → 重试 1 次后成功且恰调用 2 次；④两次皆败 → UpstreamError
且恰调用 2 次；⑤rerank 响应结果无需按序 → 按 index 还原输入顺序再逐个 sigmoid，
rerank 重试语义同 embed；⑥超时（传输层 httpx.TimeoutException）→ UpstreamError。
另覆盖 fail-closed 守卫：embed 响应维度/条数不符、JSON 解析失败、rerank 结果缺漏
一律 UpstreamError，防静默错维入库（standards §4.4）。
"""

import json

import httpx
import pytest

from app.core.llm import EMBED_DIM, Embedder, Reranker, UpstreamError, sigmoid
from app.core.tei import TeiEmbedder, TeiReranker

BASE_URL = "http://tei.internal:8000"


def _vec(text: str) -> list[float]:
    """确定性 1024 维假向量：第 i 维 = ((i + 文本长度) % 7) / 7，不同长度文本向量不同。"""
    offset = len(text)
    return [(i + offset) % 7 / 7.0 for i in range(EMBED_DIM)]


# ---------------------------------------------------------------------------
# 用例 ①：正常批量 embed → POST {base_url}/embed，逐值透传、顺序保持
# ---------------------------------------------------------------------------


def test_embed_posts_inputs_and_passes_vectors_through_in_order() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=[_vec(text) for text in captured["body"]["inputs"]])  # type: ignore

    texts = ["文档上传", "检索问答流水线"]
    vectors = TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(texts)

    assert captured["url"] == f"{BASE_URL}/embed"  # POST {base_url}/embed
    assert captured["body"] == {"inputs": texts}  # 请求体恰为 {"inputs": texts}
    assert vectors == [_vec(text) for text in texts]  # 响应逐值透传、顺序保持
    assert all(len(vector) == EMBED_DIM for vector in vectors)  # 每条 1024 维


def test_embed_empty_input_returns_empty_without_calling() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=[])

    assert TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed([]) == []
    assert len(calls) == 1  # 空输入仍发起一次调用，透传空列表


# ---------------------------------------------------------------------------
# 用例 ②：非 2xx → UpstreamError（broken=False）；4xx 请求本身有误，不重试
# ---------------------------------------------------------------------------


def test_embed_non_200_raises_upstream_error_not_broken_without_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(422, json={"detail": "unprocessable"})

    with pytest.raises(UpstreamError) as exc_info:
        TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(["文本"])

    assert exc_info.value.broken is False  # 调用前失败：未产出任何向量，可整体重试
    assert calls == [1]  # 4xx 不重试：恰 1 次调用


# ---------------------------------------------------------------------------
# 用例 ③④：5xx 重试 1 次（恰 2 次独立请求）；两次皆败抛 UpstreamError
# ---------------------------------------------------------------------------


def test_embed_first_500_then_200_succeeds_after_exactly_two_calls() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, text="internal error")
        return httpx.Response(200, json=[_vec("文档上传")])

    vectors = TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(["文档上传"])

    assert len(calls) == 2  # 恰 2 次尝试：失败重试 1 次
    assert vectors == [_vec("文档上传")]  # 重试成功后逐值透传


def test_embed_both_attempts_fail_raises_upstream_error_exactly_two_calls() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="service unavailable")

    with pytest.raises(UpstreamError) as exc_info:
        TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(["文本"])

    assert exc_info.value.broken is False
    assert len(calls) == 2  # 两次皆败：恰调用 2 次（一次重试，不更多）


# ---------------------------------------------------------------------------
# 用例 ⑤：rerank 响应无需按序 → 按 index 还原输入顺序再逐个 sigmoid；重试同 embed
# ---------------------------------------------------------------------------


def test_rerank_restores_input_order_and_sigmoid_normalizes() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"results": [{"index": 1, "score": 2.0}, {"index": 0, "score": -1.0}]}
        )

    query, docs = "什么是企业智能工作台", ["命中片段甲", "命中片段乙"]
    scores = TeiReranker(BASE_URL, transport=httpx.MockTransport(handler)).rerank(query, docs)

    assert captured["url"] == f"{BASE_URL}/rerank"  # POST {base_url}/rerank
    assert captured["body"] == {"query": query, "texts": docs}  # 请求体恰为 query + texts
    assert scores == [sigmoid(-1.0), sigmoid(2.0)]  # index 乱序返回 → 按输入顺序还原
    assert all(0.0 < score < 1.0 for score in scores)  # sigmoid 归一化到开区间 (0,1)


def test_rerank_first_500_then_200_succeeds_after_exactly_two_calls() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, text="internal error")
        return httpx.Response(200, json={"results": [{"index": 0, "score": 0.5}]})

    scores = TeiReranker(BASE_URL, transport=httpx.MockTransport(handler)).rerank("查询", ["文档"])

    assert len(calls) == 2  # rerank 重试语义同 embed：恰 2 次尝试
    assert scores == [sigmoid(0.5)]


def test_rerank_non_200_raises_upstream_error_without_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="not found")

    with pytest.raises(UpstreamError) as exc_info:
        TeiReranker(BASE_URL, transport=httpx.MockTransport(handler)).rerank("查询", ["文档"])

    assert exc_info.value.broken is False
    assert calls == [1]  # 4xx 不重试：恰 1 次调用


# ---------------------------------------------------------------------------
# 用例 ⑥：超时（传输层 httpx.TimeoutException）→ UpstreamError（重试 1 次后仍败）
# ---------------------------------------------------------------------------


def test_embed_timeout_raises_upstream_error_after_one_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("connection timed out")

    with pytest.raises(UpstreamError) as exc_info:
        TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(["文本"])

    assert exc_info.value.broken is False
    assert len(calls) == 2  # 超时同连接错误：重试 1 次后仍败，恰 2 次尝试


# ---------------------------------------------------------------------------
# fail-closed 守卫：维度/条数不符、JSON 解析失败、rerank 结果缺漏 → UpstreamError
# ---------------------------------------------------------------------------


def test_embed_wrong_dimension_response_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[0.1, 0.2]])  # 非 EMBED_DIM 维

    with pytest.raises(UpstreamError, match="维度"):
        TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(["文本"])


def test_embed_response_length_mismatch_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_vec("文档上传")])  # 2 入 1 出

    with pytest.raises(UpstreamError, match="等长"):
        TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(
            ["文档上传", "检索问答"]
        )


def test_embed_invalid_json_response_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(UpstreamError, match="JSON"):
        TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(["文本"])


def test_rerank_results_missing_coverage_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})  # 缺 index=1

    with pytest.raises(UpstreamError, match="覆盖"):
        TeiReranker(BASE_URL, transport=httpx.MockTransport(handler)).rerank("查询", ["甲", "乙"])


# ---------------------------------------------------------------------------
# Task 3 评审收口：异常族放宽为 ValueError（200 + 非 UTF-8 体抛 UnicodeDecodeError
# 裸逃）；index/score 校验显式排除 bool（bool 是 int 子类但不是合法 index/分数）
# ---------------------------------------------------------------------------


def test_embed_non_utf8_body_raises_upstream_error_not_raw_decode_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe<not-utf-8>")  # 200 + 非 UTF-8 体

    with pytest.raises(UpstreamError, match="JSON"):
        TeiEmbedder(BASE_URL, transport=httpx.MockTransport(handler)).embed(["文本"])


def test_rerank_bool_index_rejected_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": True, "score": 1.0}]})

    with pytest.raises(UpstreamError, match="index"):
        TeiReranker(BASE_URL, transport=httpx.MockTransport(handler)).rerank("查询", ["甲", "乙"])


def test_rerank_bool_score_rejected_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 0, "score": True}]})

    with pytest.raises(UpstreamError, match="score 不是数值"):
        TeiReranker(BASE_URL, transport=httpx.MockTransport(handler)).rerank("查询", ["甲"])


# ---------------------------------------------------------------------------
# 接口兼容：TeiEmbedder/TeiReranker 与 Embedder/Reranker Protocol 结构化兼容；
# 构造器新参 transport 置于参数表最后，既有 base_url/model/timeout_s 调用不受影响
# ---------------------------------------------------------------------------


def test_tei_clients_satisfy_embedder_and_reranker_protocols() -> None:
    assert isinstance(TeiEmbedder(BASE_URL), Embedder)  # 缺省 transport 可构造
    assert isinstance(TeiReranker(BASE_URL), Reranker)
    assert isinstance(
        TeiEmbedder(BASE_URL, model="bge-m3", timeout_s=10.0), Embedder
    )  # 既有关键字调用形态不受新参影响
