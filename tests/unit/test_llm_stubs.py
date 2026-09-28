r"""LLM 三件套接口与确定性 Stub 测试：embed/rerank/chat_stream 全部可替身、可注入故障。

断言表来源 task-9-brief（分数口径 standards §4.4）：Stub 同输入必同输出——播种一律
sha256/blake2b 摘要（进程间确定，禁 PYTHONHASHSEED 依赖）；embed 向量 dim=1024 且
L2 归一化，fail_times=1 先抛 UpstreamError 后恢复；chat_stream break_after=2 产出
2 段后抛 UpstreamError(broken=True)；rerank 分数全在开区间 (0,1) 且按注入权重保序；
cosine_to_similarity 严格 1 − dist；hang_s 挂起路径以 monkeypatch 短 sleep 验证
（不真睡，避免测试变慢）。
"""

import math
import time

import pytest

import app.core.llm as llm_mod
from app.core.llm import (
    DEFAULT_CHAT_CHUNKS,
    EMBED_DIM,
    Embedder,
    LLMProvider,
    Reranker,
    StubEmbedder,
    StubLLMProvider,
    StubReranker,
    UpstreamError,
    cosine_to_similarity,
    sigmoid,
)

_MESSAGES: list[dict[str, str]] = [{"role": "user", "content": "介绍一下企业智能工作台"}]


# ---------------------------------------------------------------------------
# 分数口径（standards §4.4）：cosine_to_similarity = 1 − dist；sigmoid → (0,1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dist", "expected"),
    [
        pytest.param(0.2, 0.8, id="score-brief-0p2-to-0p8"),
        pytest.param(0.0, 1.0, id="score-identical-to-one"),
        pytest.param(1.0, 0.0, id="score-orthogonal-to-zero"),
        pytest.param(0.25, 0.75, id="score-quarter-to-three-quarters"),
        pytest.param(0.5, 0.5, id="score-half-to-half"),
    ],
)
def test_cosine_to_similarity_is_exactly_one_minus_dist(dist: float, expected: float) -> None:
    assert cosine_to_similarity(dist) == expected  # 精确等式，禁止 approx


def test_sigmoid_known_values_and_open_interval() -> None:
    assert sigmoid(0.0) == 0.5  # sigmoid(0) 恰为 1/2
    assert sigmoid(2.0) == pytest.approx(1.0 / (1.0 + math.exp(-2.0)))
    assert sigmoid(-2.0) == pytest.approx(1.0 / (1.0 + math.exp(2.0)))
    for x in (-30.0, -1.0, 0.0, 0.5, 30.0):
        assert 0.0 < sigmoid(x) < 1.0  # 开区间，含 ±30 大 logit（数值稳定不溢出）


def test_upstream_error_default_is_not_broken() -> None:
    assert UpstreamError("boom").broken is False


# ---------------------------------------------------------------------------
# 接口 Protocol：三件套 Stub 与接口结构化兼容（runtime_checkable）
# ---------------------------------------------------------------------------


def test_stubs_satisfy_llm_protocols() -> None:
    assert isinstance(StubEmbedder(), Embedder)
    assert isinstance(StubReranker(), Reranker)
    assert isinstance(StubLLMProvider(), LLMProvider)


# ---------------------------------------------------------------------------
# StubEmbedder：sha256 播种，同输入必同输出，dim=1024，L2 归一化，故障注入
# ---------------------------------------------------------------------------


def test_embed_same_text_equal_dim_1024_and_cross_instance_stable() -> None:
    stub = StubEmbedder()
    first = stub.embed(["企业智能工作台"])
    second = stub.embed(["企业智能工作台"])
    assert len(first) == 1
    assert len(first[0]) == 1024
    assert EMBED_DIM == 1024
    assert first == second  # 同实例：同文本两次 embed 相等
    assert StubEmbedder().embed(["企业智能工作台"]) == first  # 跨实例一致（sha256 播种）


def test_embed_different_texts_differ_and_l2_normalized() -> None:
    vec_a, vec_b = StubEmbedder().embed(["文档上传与解析", "检索问答流水线"])
    assert vec_a != vec_b
    for vec in (vec_a, vec_b):
        assert math.sqrt(sum(component * component for component in vec)) == pytest.approx(1.0)


def test_embed_empty_input_returns_empty() -> None:
    assert StubEmbedder().embed([]) == []


def test_embed_fail_times_one_raises_then_recovers() -> None:
    stub = StubEmbedder(fail_times=1)
    with pytest.raises(UpstreamError) as exc_info:
        stub.embed(["第一次"])
    assert exc_info.value.broken is False  # 调用前失败：未产出任何向量，可整体重试
    assert stub.embed(["第二次"]) == StubEmbedder().embed(["第二次"])  # 恢复后与干净 Stub 一致


# ---------------------------------------------------------------------------
# StubReranker：sigmoid 归一化到 (0,1)，按注入权重保序；缺省 logit 由 blake2b 派生
# ---------------------------------------------------------------------------


def test_rerank_scores_in_open_interval_and_ordered_by_injected_weights() -> None:
    weights = [-2.0, 0.0, 3.0]
    scores = StubReranker(weights=weights).rerank("什么是企业智能工作台", ["甲", "乙", "丙"])
    assert len(scores) == 3
    assert all(0.0 < score < 1.0 for score in scores)
    assert scores[0] < scores[1] < scores[2]  # sigmoid 单调递增 → 按注入权重保序
    assert scores == [sigmoid(weight) for weight in weights]  # 精确等于注入权重的 sigmoid


def test_rerank_deterministic_across_instances_without_weights() -> None:
    query, docs = "知识库检索", ["合同范本", "报销制度", "年会通知"]
    assert StubReranker().rerank(query, docs) == StubReranker().rerank(query, docs)
    assert StubReranker().rerank("另一查询", docs) != StubReranker().rerank(query, docs)


def test_rerank_weights_length_mismatch_raises_value_error() -> None:
    with pytest.raises(ValueError, match="weights length"):
        StubReranker(weights=[1.0]).rerank("q", ["a", "b"])


def test_rerank_fail_times_one_raises_then_recovers() -> None:
    stub = StubReranker(weights=[1.0], fail_times=1)
    with pytest.raises(UpstreamError):
        stub.rerank("q", ["a"])
    assert stub.rerank("q", ["a"]) == [sigmoid(1.0)]


# ---------------------------------------------------------------------------
# StubLLMProvider：分段流式产出；fail_times 调用即抛（未产出段）；break_after 中途断流
# ---------------------------------------------------------------------------


def test_chat_stream_yields_default_chunks_in_order() -> None:
    provider = StubLLMProvider()
    pieces = list(provider.chat_stream(_MESSAGES, max_tokens=512))
    assert pieces == list(DEFAULT_CHAT_CHUNKS)
    assert len(pieces) >= 3  # 供 break_after 截断测试的前提


def test_chat_stream_break_after_two_emits_two_chunks_then_broken() -> None:
    provider = StubLLMProvider(break_after=2)
    iterator = provider.chat_stream(_MESSAGES, max_tokens=512)  # 迭代器只取一次，持续推进
    collected: list[str] = []
    with pytest.raises(UpstreamError) as exc_info:
        while True:
            collected.append(next(iterator))
    assert collected == list(DEFAULT_CHAT_CHUNKS[:2])  # 已产出 2 段部分内容
    assert exc_info.value.broken is True  # 流式中途断裂


def test_chat_stream_fail_times_one_raises_at_call_then_recovers() -> None:
    provider = StubLLMProvider(fail_times=1)
    with pytest.raises(UpstreamError) as exc_info:
        provider.chat_stream(_MESSAGES, max_tokens=512)
    assert exc_info.value.broken is False  # 取迭代器即抛：未产出任何段
    assert list(provider.chat_stream(_MESSAGES, max_tokens=512)) == list(DEFAULT_CHAT_CHUNKS)


def test_chat_stream_truncates_total_to_max_tokens() -> None:
    provider = StubLLMProvider(chunks=["aaaaa", "bbbbb", "ccccc"])
    text = "".join(provider.chat_stream([], max_tokens=8))
    assert text == "aaaaabbb"  # 5 + 3：总产出恰好截到 8 字符


# ---------------------------------------------------------------------------
# hang_s 挂起注入：monkeypatch 短 sleep 验证挂起路径（不真睡，避免测试变慢）
# ---------------------------------------------------------------------------


def test_hang_path_invoked_with_hang_s_without_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda seconds: slept.append(seconds))
    embedder = StubEmbedder(hang_s=2.5, fail_times=1)
    reranker = StubReranker(hang_s=0.5)
    provider = StubLLMProvider(hang_s=1.0)

    start = time.monotonic()
    with pytest.raises(UpstreamError):
        embedder.embed(["挂起注入"])
    scores = reranker.rerank("q", ["d"])
    pieces = list(provider.chat_stream(_MESSAGES, max_tokens=64))
    elapsed = time.monotonic() - start

    assert slept == [2.5, 0.5, 1.0]  # 挂起路径按 hang_s 逐次触发（含失败前那次）
    assert embedder.hang_events == [2.5]
    assert len(scores) == 1 and 0.0 < scores[0] < 1.0
    assert pieces == list(DEFAULT_CHAT_CHUNKS)
    assert elapsed < 1.0  # monkeypatch 短 sleep 生效：测试不变慢
