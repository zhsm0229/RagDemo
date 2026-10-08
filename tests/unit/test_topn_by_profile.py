r"""查询期 top_n 按型档单元测试（metrics-balance §3.2/§5 + §6 测试 seams）。

resolve_top_n 纯函数：top1 命中 hit 的 doc_type 查 PROFILE_PARAMS 取档——五档映射
（policy_clause 5 / operation_manual 6 / table_report 5 / notice 3 / general 5）、
空序列与未知 doc_type 一律 general 缺省 5、只看 top1 不看后续命中。rerank_and_fold
的 top_n 参数扩为 int | Callable：传 int 行为与现状逐字一致（对照既有用例规格）；
传可调用时必须在 rerank 排序完成后求值（入参为 ranked 分降序序列，top1 即最高分
命中型）——若回退为排序前以输入序求值，"notice 居输入首位但 rerank 分垫底"的样例
必取错档，本文件以该时点样例钉死。doc_profile 纯函数域红线（禁 import services/db）
以结构断言看护。
"""

from pathlib import Path

import pytest

from app.core.llm import StubEmbedder, StubReranker, sigmoid
from app.rag import doc_profile
from app.rag.doc_profile import resolve_top_n
from app.services.retrieval import DocHit, rerank_and_fold

_DEDUP_SIM = 0.98


def _vec(text: str) -> list[float]:
    """确定性归一化向量（StubEmbedder，同输入必同输出）。"""
    return StubEmbedder().embed([text])[0]


def _hit(chunk_id: int, doc_type: str, embedding: list[float]) -> DocHit:
    """手工构造命中：doc_type 显式指定（缺省 general 的旁路在此全部显式化）。"""
    return DocHit(
        chunk_id=chunk_id,
        document_id=chunk_id * 10,
        title=f"标题{chunk_id}",
        page_no=None,
        title_path=f"路径{chunk_id}",
        content=f"正文{chunk_id}",
        similarity=0.0,
        embedding=embedding,
        doc_type=doc_type,
    )


# ---------------------------------------------------------------------------
# resolve_top_n：top1 命中型 → 档位；缺省与兜底
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc_type", "expected"),
    [
        pytest.param("policy_clause", 5, id="制度条款5条"),
        pytest.param("operation_manual", 6, id="操作手册6条"),
        pytest.param("table_report", 5, id="表格报表5条"),
        pytest.param("notice", 3, id="通知公告3条"),
        pytest.param("general", 5, id="通用5条"),
    ],
)
def test_resolve_top_n_top1命中五档映射(doc_type: str, expected: int) -> None:
    assert resolve_top_n([_hit(1, doc_type, _vec("样本正文"))]) == expected


def test_resolve_top_n_空序列缺省general档5条() -> None:
    assert resolve_top_n([]) == 5


def test_resolve_top_n_未知doc_type缺省general档5条() -> None:
    assert resolve_top_n([_hit(1, "unknown_kind", _vec("样本正文"))]) == 5


def test_resolve_top_n_只看top1_后续命中型不参与取档() -> None:
    hits = [_hit(1, "notice", _vec("通知正文")), _hit(2, "operation_manual", _vec("手册正文"))]
    assert resolve_top_n(hits) == 3  # 操作手册档的 6 不得透传


def test_doc_profile_纯函数域禁止import_services_结构断言() -> None:
    """doc_profile 是 golden 镜像独立重算的纯函数域（模块头红线）：源码不得出现
    services/db 依赖字样。"""
    source = Path(doc_profile.__file__).read_text(encoding="utf-8")
    assert "app.services" not in source
    assert "app.db" not in source


# ---------------------------------------------------------------------------
# rerank_and_fold：top_n 参数化 int | Callable
# ---------------------------------------------------------------------------


def test_rerank_and_fold_传int时行为与现状一致() -> None:
    """对照既有用例规格（排序降序 + 截断 + similarity 覆写）：int 通路零改动。"""
    hits = [_hit(i, "general", _vec(f"候选话题{i}")) for i in range(1, 8)]
    weights = [float(8 - i) for i in range(1, 8)]  # chunk1 分最高，依次递减

    folded = rerank_and_fold(
        StubReranker(weights=weights), "改写后问题", hits, top_n=5, dedup_sim=_DEDUP_SIM
    )

    assert [hit.chunk_id for hit in folded] == [1, 2, 3, 4, 5]  # 截断 7→5，rerank 分降序
    assert [hit.similarity for hit in folded] == [sigmoid(float(8 - i)) for i in (1, 2, 3, 4, 5)]


def test_rerank_and_fold_传resolve_top_n时按排序后top1型截取() -> None:
    """求值时点钉死：notice 居输入首位但 rerank 分垫底，排序后 top1 为操作手册
    （6 条）——若在排序前以输入序求值必取 notice 的 3 条。入参为分降序 ranked 序列。"""
    hits = [
        _hit(1, "notice", _vec("通知正文甲")),
        _hit(2, "operation_manual", _vec("手册正文乙")),
        _hit(3, "general", _vec("通用正文丙")),
        _hit(4, "general", _vec("通用正文丁")),
        _hit(5, "general", _vec("通用正文戊")),
        _hit(6, "general", _vec("通用正文己")),
        _hit(7, "general", _vec("通用正文庚")),
    ]
    weights = [1.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.5]  # ranked：2,3,4,5,6,7,1（手册居首）
    seen: list[list[int]] = []

    def spy(ranked_hits: list[DocHit]) -> int:
        seen.append([hit.chunk_id for hit in ranked_hits])
        return resolve_top_n(ranked_hits)

    folded = rerank_and_fold(
        StubReranker(weights=weights), "改写后问题", hits, top_n=spy, dedup_sim=_DEDUP_SIM
    )

    assert seen == [[2, 3, 4, 5, 6, 7, 1]]  # 排序完成后恰求值一次，入参即 ranked 序列
    assert [hit.chunk_id for hit in folded] == [2, 3, 4, 5, 6, 7]  # 手册档 6 条：chunk1 掉出


def test_rerank_and_fold_notice居首按3条截取() -> None:
    """生产接线形态（直传 resolve_top_n）：排序后 top1 为通知档 → 只保留前 3 名。"""
    hits = [
        _hit(1, "notice", _vec("通知正文一")),
        _hit(2, "general", _vec("通用正文二")),
        _hit(3, "general", _vec("通用正文三")),
        _hit(4, "general", _vec("通用正文四")),
        _hit(5, "general", _vec("通用正文五")),
    ]
    weights = [5.0, 4.0, 3.0, 2.0, 1.0]  # 召回序即 ranked 序：notice 居首

    folded = rerank_and_fold(
        StubReranker(weights=weights), "改写后问题", hits, top_n=resolve_top_n, dedup_sim=_DEDUP_SIM
    )

    assert [hit.chunk_id for hit in folded] == [1, 2, 3]  # 4、5 掉出：通知档 top_n=3


def test_rerank_and_fold_空候选短路_不调用top_n函数() -> None:
    """空候选在求值前短路（既有语义）：可调用 top_n 不得被触碰。"""

    def _boom(ranked_hits: list[DocHit]) -> int:
        raise AssertionError("空候选不得求值 top_n")

    assert (
        rerank_and_fold(StubReranker(), "改写后问题", [], top_n=_boom, dedup_sim=_DEDUP_SIM) == []
    )
