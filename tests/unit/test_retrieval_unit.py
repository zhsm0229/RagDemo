r"""检索服务纯函数单元测试：normalize_question 归一化规格 + rerank_and_fold 排序/折叠/截断。

规格来源 task-5-brief Step 2：normalize_question 依次 strip → 去全部空白 → 去中英文
标点（，。？！；：、,.?!;:）→ lower；rerank_and_fold 以注入权重的 StubReranker 驱动，
断言排序（rerank 分降序）、折叠（与高分已保留项 embedding 余弦 ≥ dedup_sim 的低分项
被丢弃）、保序与 top_n 截断（20→5）。向量均取自 StubEmbedder（已 L2 归一化，点积即
余弦），近重复向量以 base×100+noise 重归一化构造（余弦 ≈0.99995，确超 0.98 阈值）。
"""

import math

import pytest

from app.core.llm import StubEmbedder, StubReranker, sigmoid
from app.services.retrieval import DocHit, normalize_question, rerank_and_fold

_DEDUP_SIM = 0.98


def _vec(text: str) -> list[float]:
    """确定性归一化向量（StubEmbedder，同输入必同输出）。"""
    return StubEmbedder().embed([text])[0]


def _near(base: list[float], noise: list[float]) -> list[float]:
    """与 base 余弦≈0.99995 的近重复向量：100×base+noise 后重归一化。"""
    mixed = [b * 100.0 + n for b, n in zip(base, noise, strict=True)]
    norm = math.sqrt(sum(component * component for component in mixed))
    return [component / norm for component in mixed]


def _dot(left: list[float], right: list[float]) -> float:
    """两 L2 归一化向量的余弦相似度（点积）——测试内前提自检用。"""
    return sum(a * b for a, b in zip(left, right, strict=True))


def _hit(chunk_id: int, embedding: list[float]) -> DocHit:
    """伪造召回命中：similarity 置 0.0（召回口径占位），折叠后应被覆写为 rerank 分。"""
    return DocHit(
        chunk_id=chunk_id,
        document_id=chunk_id * 10,
        title=f"标题{chunk_id}",
        page_no=chunk_id,
        title_path=f"客服_主题{chunk_id}",
        content=f"内容正文{chunk_id}",
        similarity=0.0,
        embedding=embedding,
    )


# ---------------------------------------------------------------------------
# normalize_question：strip → 去全部空白 → 去中英文标点 → lower
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("  退货，时限？ ", "退货时限", id="brief-例-空白标点全去"),
        pytest.param("退 货\t时\n限", "退货时限", id="制表换行等全部空白"),
        pytest.param("FAQ", "faq", id="大小写归一"),
        pytest.param("", "", id="空串→空串"),
        pytest.param("   ", "", id="纯空白→空串"),
        pytest.param("，。？！；：、,.?!;:", "", id="纯标点→空串"),
    ],
)
def test_normalize_question_table(raw: str, expected: str) -> None:
    assert normalize_question(raw) == expected


def test_full_and_half_width_punctuation_equivalent() -> None:
    """全角/半角标点等价：两种写法归一到同一结果。"""
    assert normalize_question("退货,时限?!") == normalize_question("退货，时限？！")
    assert normalize_question("退货,时限?!") == "退货时限"


def test_case_and_space_normalized_to_same_form() -> None:
    assert normalize_question("Return  Policy") == normalize_question("return policy")
    assert normalize_question("Return  Policy") == "returnpolicy"


# ---------------------------------------------------------------------------
# rerank_and_fold：rerank 打分 → 分降序取 top_n → top_n 内两两折叠去重
# ---------------------------------------------------------------------------


def test_rerank_orders_desc_by_score_and_overwrites_similarity() -> None:
    """权重乱序注入：结果按 rerank 分降序，similarity 覆写为 rerank sigmoid 分且不改原 hit。"""
    hits = [_hit(1, _vec("甲")), _hit(2, _vec("乙")), _hit(3, _vec("丙"))]
    folded = rerank_and_fold(
        StubReranker(weights=[1.0, 3.0, 2.0]), "改写后问题", hits, top_n=5, dedup_sim=_DEDUP_SIM
    )

    assert [hit.chunk_id for hit in folded] == [2, 3, 1]  # 分数 3 > 2 > 1 的降序
    assert [hit.similarity for hit in folded] == [sigmoid(3.0), sigmoid(2.0), sigmoid(1.0)]
    assert hits[0].similarity == 0.0  # frozen dataclass：覆写生成新实例，原 hit 不被改写


def test_fold_drops_near_duplicate_of_higher_scored_hit() -> None:
    """折叠（F-1003）：低分项与高分已保留项 embedding 余弦 ≥ dedup_sim → 丢弃低分项。"""
    base = _vec("折叠基准文本")
    near = _near(base, _vec("扰动噪声"))
    hits = [_hit(1, base), _hit(2, _vec("无关独立文本")), _hit(5, near)]
    assert _dot(near, base) >= _DEDUP_SIM  # 前提自检：fold 阈值确有区分力
    assert _dot(_vec("无关独立文本"), base) < _DEDUP_SIM

    folded = rerank_and_fold(
        StubReranker(weights=[3.0, 2.0, 1.0]), "改写后问题", hits, top_n=5, dedup_sim=_DEDUP_SIM
    )

    assert [hit.chunk_id for hit in folded] == [1, 2]  # chunk5 与 chunk1 近重复：被折叠丢弃
    assert [hit.similarity for hit in folded] == [sigmoid(3.0), sigmoid(2.0)]  # 保序且为 rerank 分


def test_fold_keeps_all_when_no_duplicates() -> None:
    """互不近重复：全部保留，仍按 rerank 分降序。"""
    hits = [_hit(i, _vec(f"完全不同的话题{i}")) for i in (1, 2, 3)]
    folded = rerank_and_fold(
        StubReranker(weights=[1.0, 2.0, 3.0]), "改写后问题", hits, top_n=5, dedup_sim=_DEDUP_SIM
    )
    assert [hit.chunk_id for hit in folded] == [3, 2, 1]


def test_top_n_truncates_candidates_20_to_5() -> None:
    """top_n 截断生效：20 个候选只保留 rerank 分最高的 5 个。"""
    hits = [_hit(i, _vec(f"候选话题{i}")) for i in range(1, 21)]
    weights = [float(21 - i) for i in range(1, 21)]  # chunk1 权重最高，依次递减

    folded = rerank_and_fold(
        StubReranker(weights=weights), "改写后问题", hits, top_n=5, dedup_sim=_DEDUP_SIM
    )

    assert [hit.chunk_id for hit in folded] == [1, 2, 3, 4, 5]


def test_rerank_text_is_title_path_plus_content() -> None:
    """rerank 文本 = title_path\\ncontent：以记录调用参数的替身断言逐条拼接。"""
    hits = [_hit(1, _vec("甲")), _hit(2, _vec("乙"))]
    seen_docs: list[list[str]] = []

    class Recording:
        def rerank(self, query: str, docs: list[str]) -> list[float]:
            seen_docs.append(docs)
            return [0.5, 0.5]

    rerank_and_fold(Recording(), "改写后问题", hits, top_n=5, dedup_sim=_DEDUP_SIM)
    assert seen_docs == [["客服_主题1\n内容正文1", "客服_主题2\n内容正文2"]]


def test_empty_hits_returns_empty_without_calling_reranker() -> None:
    reranker = StubReranker(weights=[])
    assert rerank_and_fold(reranker, "改写后问题", [], top_n=5, dedup_sim=_DEDUP_SIM) == []
    assert reranker.rerank_calls == 0  # 空候选短路：不发 rerank 调用
