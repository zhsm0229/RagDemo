r"""multi_query 三产物改写与多路召回合并单测（retrieval-policy §4，工单 06；无 DB 全 Stub）。

三产物合并调用（P1 预埋）：multi_query 门控命中时多轮改写升级为一次 LLM 调用产出
改写问题 + L1 意图标签 + 子查询（≤3，整包输出 max_tokens=150 钳制，spec §4）；解析
失败/缺产物一律降级单路（与既有改写降级模式同款：原问题继续、无子查询，绝不炸轮）。
L1 意图标签本工单只解析与校验、不参与路由（激活=B 类证据门槛 §1/§17.3，激活前
L0 词表是唯一意图源）。merge_multi_query_hits 为纯函数：按 chunk_id 去重保各路最高
召回余弦（两套分数绝不互比，standards §4.4——此处全部是召回余弦口径）。
"""

from collections.abc import Iterator, Mapping, Sequence

import pytest

from app.core.llm import UpstreamError
from app.rag.prompt import ContextSource, build_context_block
from app.rag.retrieval_policy import resolve_policy
from app.services.retrieval import DocHit, merge_multi_query_hits
from app.services.rewrite import (
    THREE_PRODUCT_MAX_TOKENS,
    RewriteOutcome,
    rewrite_question_multi,
)

_HISTORY: list[tuple[str, str]] = [
    ("user", "公司的年假制度是什么"),
    ("assistant", "年假按工龄五到十天 [1]"),
]
_QUESTION = "那病假和年假分别怎么规定"
_SYSTEM = "你是问题改写器。"
_MULTI_SYSTEM = "你是三产物改写器。"


class _ScriptedLLM:
    """按调用次序弹出产出文本的替身：记录 (messages, max_tokens) 供钳制与提示词断言。"""

    def __init__(self, *outputs: str) -> None:
        self._outputs = list(outputs)
        self.calls: list[tuple[list[dict[str, str]], int]] = []

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        self.calls.append(([dict(message) for message in messages], max_tokens))
        text = self._outputs.pop(0) if self._outputs else ""
        yield text


class _GeneratorFailureLLM:
    """生成器函数形态替身：调用不抛、首次 next() 才抛（复刻真实现连接失败形态）。"""

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        raise UpstreamError("连接失败")
        yield ""  # 不可达：仅为把本函数标记为生成器函数（真实现同构）


def test_三产物合法解析_改写意图与子查询_子查询钳制到前三() -> None:
    """合法 JSON 三产物逐项解析；子查询超 3 个取前三（多出的不消费），输出钳制 150 token。"""
    llm = _ScriptedLLM(
        '{"rewritten": "病假和年假分别怎么规定", "intent": "multi_hop", "subQueries": ['
        '"年假天数规定", "病假证明要求", "年假病假对比", "多余第四个", " "]}'
    )

    outcome = rewrite_question_multi(
        llm, _HISTORY, _QUESTION, system=_SYSTEM, multi_system=_MULTI_SYSTEM
    )

    assert outcome == RewriteOutcome(
        rewritten="病假和年假分别怎么规定",
        intent="multi_hop",
        sub_queries=["年假天数规定", "病假证明要求", "年假病假对比"],  # 恰前三：第四个与空白被钳掉
    )
    ((messages, max_tokens),) = llm.calls
    assert max_tokens == THREE_PRODUCT_MAX_TOKENS == 150  # spec §4 子查询输出钳制
    assert messages[0] == {"role": "system", "content": _MULTI_SYSTEM}  # 三产物提示词注入


def test_三产物意图标签仅收冻结枚举_single_hop合法且子查询可空() -> None:
    """intent=single_hop（L1 标签本票不参与路由）与空子查询列表均为合法产物。"""
    llm = _ScriptedLLM('{"rewritten": "病假怎么规定", "intent": "single_hop", "subQueries": []}')

    outcome = rewrite_question_multi(
        llm, _HISTORY, _QUESTION, system=_SYSTEM, multi_system=_MULTI_SYSTEM
    )

    assert outcome == RewriteOutcome(rewritten="病假怎么规定", intent="single_hop", sub_queries=[])


@pytest.mark.parametrize(
    "payload",
    [
        "完全不是 JSON 的自由文本",  # 非法 JSON
        '"顶层是字符串不是对象"',  # JSON 合法但非对象
        '{"intent": "multi_hop", "subQueries": []}',  # 缺 rewritten
        '{"rewritten": "   ", "intent": "multi_hop", "subQueries": []}',  # rewritten 空白
        '{"rewritten": "改写", "subQueries": []}',  # 缺 intent
        '{"rewritten": "改写", "intent": "both", "subQueries": []}',  # intent 非法值
        '{"rewritten": "改写", "intent": "multi_hop"}',  # 缺 subQueries
        '{"rewritten": "改写", "intent": "multi_hop", "subQueries": "年假"}',  # 子查询非列表
        '{"rewritten": "改写", "intent": "multi_hop", "subQueries": [1]}',  # 子查询元素非字符串
    ],
)
def test_非法或缺产物一律降级单路_原问题继续编排(payload: str) -> None:
    """解析失败/缺任一产物/字段非法 → 降级单路（原问题、无子查询、intent=None），绝不炸轮。"""
    llm = _ScriptedLLM(payload)

    outcome = rewrite_question_multi(
        llm, _HISTORY, _QUESTION, system=_SYSTEM, multi_system=_MULTI_SYSTEM
    )

    assert outcome == RewriteOutcome(rewritten=_QUESTION, intent=None, sub_queries=[])


def test_子查询钳制_取前三后再剔除空白条目() -> None:
    """钳制次序=先按模型产出序取前三、再剔除空白：第三位空白被剔后不足三个不回补。"""
    llm = _ScriptedLLM(
        '{"rewritten": "改写", "intent": "multi_hop", "subQueries": ["甲", "乙", " ", "丙"]}'
    )

    outcome = rewrite_question_multi(
        llm, _HISTORY, _QUESTION, system=_SYSTEM, multi_system=_MULTI_SYSTEM
    )

    assert outcome.sub_queries == ["甲", "乙"]  # 丙在钳制窗（第四位）外，不回补


def test_上游故障降级单路_生成器形态首next才抛() -> None:
    """生成器函数形态故障（首 next() 才抛）同样降级单路——异常边界包住整个迭代。"""
    llm = _GeneratorFailureLLM()

    outcome = rewrite_question_multi(
        llm, _HISTORY, _QUESTION, system=_SYSTEM, multi_system=_MULTI_SYSTEM
    )

    assert outcome == RewriteOutcome(rewritten=_QUESTION, intent=None, sub_queries=[])


# ---------------------------------------------------------------------------
# merge_multi_query_hits 纯函数（多路召回合并，retrieval-policy §4）
# ---------------------------------------------------------------------------


def _hit(chunk_id: int, similarity: float) -> DocHit:
    """伪造多路召回命中：similarity 为召回余弦（本层唯一口径，不与 rerank 分互比）。"""
    return DocHit(
        chunk_id=chunk_id,
        document_id=chunk_id * 10,
        title=f"标题{chunk_id}",
        page_no=None,
        title_path="路径",
        content=f"正文{chunk_id}",
        similarity=similarity,
        embedding=[0.0, 1.0],
    )


def test_多路召回合并_按chunk_id去重保各路最高余弦_整体降序() -> None:
    """同 chunk 多路命中取最高召回余弦，合并集按余弦降序（交 rerank_and_fold 单次精排）。"""
    paths = [
        [_hit(1, 0.9), _hit(2, 0.7)],
        [_hit(2, 0.85), _hit(3, 0.6)],  # chunk 2 两路命中：取高 0.85
        [_hit(1, 0.5)],  # chunk 1 重复命中：保 0.9 不被拉低
    ]

    merged = merge_multi_query_hits(paths)

    assert [(hit.chunk_id, hit.similarity) for hit in merged] == [
        (1, 0.9),
        (2, 0.85),
        (3, 0.6),
    ]


def test_多路召回合并_空路返回空列表() -> None:
    assert merge_multi_query_hits([[], []]) == []


# ---------------------------------------------------------------------------
# 门控与 rider 接线锚点（消费方面向断言：resolver 侧冻结值由 test_retrieval_policy 承担）
# ---------------------------------------------------------------------------


def test_multi_query双门控_cpu档恒关_gpu档需multi_hop信号() -> None:
    """门控消费面：cpu_constrained 恒 False（本机行为零变化）；gpu 档仅 multi_hop 信号
    开启；char_budget 恒缺省 None（chat 组装接线后缺省逐字不变的前提）。"""
    multi_hop = "年假和病假分别有什么区别"  # L0 恰 2 类：区别（比较）+ 分别（多实体）
    single_hop = "病假怎么请"

    assert resolve_policy(multi_hop).multi_query is False  # 缺省 cpu 档
    assert resolve_policy(multi_hop, compute_tier="gpu").multi_query is True
    assert resolve_policy(single_hop, compute_tier="gpu").multi_query is False
    assert resolve_policy(multi_hop).char_budget is None


def test_char_budget缺省None时上下文组装逐字不变() -> None:
    """rider 接线前提：build_context_block 不传与显式传 None 产物逐字相等（policy
    char_budget 恒 None → chat 组装零变化；截断语义本身归 test_prompt）。"""
    sources = [
        ContextSource(
            n=1, ref_type="doc", title="标题", title_path="路径", page=3, content="长" * 600
        )
    ]

    assert build_context_block(sources) == build_context_block(sources, None)
