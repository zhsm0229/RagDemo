r"""ToolProvider 注册表与知识库检索工具单测（工单 #04）：部门隔离红线证明。

以 RecordingStore 模拟 F-502 SQL 的部门过滤（命中按 dept_id ∈ dept_ids 放行，与
services/retrieval._chunk_search_statement 的 where 语义同构），证明：A 部门调用者
（含 args 夹带部门字段）检索不到 B 部门语料；dept_ids 逐次原样传给 search_chunks
（强制继承，无绕过）。Embedder/Reranker 用 core.llm Stub（同输入必同输出），
Settings 显式传参（不读 .env，测试确定性）。

工单 03（retrieval-policy §6/§8）追加：match_qa 直答通道（阈值读 settings，命中跳过
文档检索）、近重复 0.98 折叠（fold_top_n 主/agent 共用纯函数）、excerpt 500 字截断
回归锁定、熔断 open 态行为不变；fold_top_n 纯函数规格（留高分弃低分/截断先于折叠/
return_folded 缝/与主链路 rerank_and_fold 同尺对照/纯函数域结构断言）同文件承载
（工单 03 文件所有权边界，不另开单测文件）。
"""

import dataclasses
import math
from pathlib import Path

import pytest

from app.core.circuit_breaker import CircuitBreaker, set_rerank_breaker
from app.core.clock import FakeClock
from app.core.config import Settings
from app.core.llm import StubEmbedder, StubReranker, UpstreamError
from app.core.tools import RunContext, ToolProvider, ToolRegistry, ToolSpec
from app.rag import fold as fold_module
from app.rag.fold import fold_top_n
from app.rag.retrieval_policy import RetrievalPolicy
from app.services.agent_tools import (
    KNOWLEDGE_SEARCH_SPEC,
    KnowledgeBaseSearchTool,
    KnowledgeSearchDeps,
)
from app.services.retrieval import DocHit, QaHit, rerank_and_fold


def _vec(text: str) -> list[float]:
    """确定性归一化向量（StubEmbedder，同输入必同输出）。"""
    return StubEmbedder().embed([text])[0]


def _hit(chunk_id: int, embedding: list[float], content: str) -> DocHit:
    """伪造召回命中：similarity 置 0.0（召回口径占位），fold 后覆写为 rerank 分。"""
    return DocHit(
        chunk_id=chunk_id,
        document_id=chunk_id * 10,
        title=f"标题{chunk_id}",
        page_no=1,
        title_path=f"部门_主题{chunk_id}",
        content=content,
        similarity=0.0,
        embedding=embedding,
    )


def _dot(left: list[float], right: list[float]) -> float:
    """两 L2 归一化向量的余弦相似度（点积）——测试内前提自检用。"""
    return sum(a * b for a, b in zip(left, right, strict=True))


def _near(base: list[float], noise: list[float]) -> list[float]:
    """与 base 余弦≈0.99995 的近重复向量：100×base+noise 后重归一化。"""
    mixed = [b * 100.0 + n for b, n in zip(base, noise, strict=True)]
    norm = math.sqrt(sum(component * component for component in mixed))
    return [component / norm for component in mixed]


class RecordingStore:
    """替身 VectorStore：行为模拟 F-502（dept_id ∈ dept_ids 才可见）并按相似度降序
    返回（对齐 pgvector ORDER BY embedding <=> query_vec 的真实契约），记录每次调用。
    qa_hit 非空时 match_qa 恒返该命中（qa 直答通道用），调用连同 threshold 一并入册。"""

    def __init__(self, corpus: list[tuple[int, DocHit]], qa_hit: QaHit | None = None) -> None:
        self._corpus = corpus  # (归属 dept_id, 命中)
        self._qa_hit = qa_hit
        self.calls: list[dict] = []
        self.qa_calls: list[dict] = []

    def search_chunks(self, db, query_vec, dept_ids, top_k, ef_search):
        self.calls.append({"dept_ids": list(dept_ids), "top_k": top_k, "ef_search": ef_search})
        visible = {dept_id for dept_id in dept_ids}
        hits = [hit for owner, hit in self._corpus if owner in visible]
        return sorted(hits, key=lambda h: h.similarity, reverse=True)

    def match_qa(self, db, query_vec, dept_ids, threshold):
        self.qa_calls.append({"dept_ids": list(dept_ids), "threshold": threshold})
        return self._qa_hit


def _settings() -> Settings:
    """显式传参不读 .env：检索口径与生产缺省一致（top_k=20/top_n=5/dedup=0.98）。"""
    return Settings(
        jwt_secret="x" * 32,
        retriever_top_k=20,
        rerank_top_n=5,
        chunk_dedup_sim=0.98,
        hnsw_ef_search=40,
    )


def _deps(store: RecordingStore, weights: list[float] | None = None) -> KnowledgeSearchDeps:
    reranker = StubReranker(weights=weights) if weights is not None else StubReranker()
    return KnowledgeSearchDeps(
        embedder=StubEmbedder(), store=store, reranker=reranker, settings=_settings()
    )


def _ctx(dept_ids: tuple[int, ...] = (2, 99)) -> RunContext:
    """A 部门（dept_id=2）调用者上下文；99 为全员部门占位。"""
    return RunContext(user_id=7, username="客服甲", dept_ids=dept_ids, db=None)


# ---------------------------------------------------------------------------
# 注册表与声明制契约
# ---------------------------------------------------------------------------


def test_注册表注册取回_重名拒绝_未注册报错() -> None:
    registry = ToolRegistry()
    tool = KnowledgeBaseSearchTool(_deps(RecordingStore([])))

    registry.register(tool)

    assert registry.names() == ["knowledge_base_search"]
    assert registry.get("knowledge_base_search") is tool
    assert registry.specs() == [KNOWLEDGE_SEARCH_SPEC]
    with pytest.raises(ValueError, match="重名"):
        registry.register(tool)  # 重名 fail-fast：防静默覆盖改变语义
    with pytest.raises(KeyError, match="未注册"):
        registry.get("nope")


def test_工具实现满足第七接口协议() -> None:
    assert isinstance(KnowledgeBaseSearchTool(_deps(RecordingStore([]))), ToolProvider)


def test_工具声明部门过滤与审计语义_schema单点() -> None:
    spec: ToolSpec = KNOWLEDGE_SEARCH_SPEC
    assert spec.name == "knowledge_base_search"
    assert spec.dept_scoped is True  # 部门过滤语义声明：强制继承调用者
    assert spec.audit_note != ""  # 审计语义声明：落点与内容可核对

    schema = KnowledgeBaseSearchTool(_deps(RecordingStore([]))).schema()
    assert schema["function"]["name"] == spec.name  # schema 与 spec 单点对接（specs §4.4）
    assert schema["function"]["parameters"]["required"] == ["query"]
    assert "query" in schema["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# 部门隔离红线（串库方向证明）
# ---------------------------------------------------------------------------


def test_A部门调用者检索不到B部门语料() -> None:
    a_hit = _hit(1, _vec("客服退货时限七天"), "客服：退货时限七天")
    b_hit = _hit(2, _vec("HR薪酬保密制度全文"), "HR：薪酬保密制度全文")
    store = RecordingStore([(2, a_hit), (3, b_hit)])  # dept 2=客服，3=HR；99=全员占位
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "薪酬保密制度内容是什么"}, _ctx())

    assert result.ok is True
    assert store.calls[0]["dept_ids"] == [2, 99]  # 过滤键=调用者上下文
    assert "薪酬保密" not in str(result.value)  # B 部门语料不可见
    assert "退货时限" in str(result.value)  # 本部门语料可见


def test_args夹带部门字段一律无效() -> None:
    store = RecordingStore(
        [
            (2, _hit(1, _vec("客服语料正文"), "客服语料正文")),
            (3, _hit(2, _vec("HR语料正文"), "HR语料正文")),
        ]
    )
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "语料正文", "dept_ids": [3], "dept_id": 3}, _ctx())

    assert result.ok is True
    assert store.calls[0]["dept_ids"] == [2, 99]  # 强制继承：未被 args 改写
    assert "HR语料" not in str(result.value)  # 夹带也拿不到 B 部门语料


def test_全员部门语料对所有调用者可见() -> None:
    shared = _hit(9, _vec("全员考勤制度"), "全员：考勤制度")
    store = RecordingStore([(99, shared)])
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "考勤制度"}, _ctx())

    assert result.ok is True
    assert "考勤制度" in str(result.value)


def test_运行上下文不可变_防工具改写部门() -> None:
    ctx = _ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.dept_ids = (3,)  # type: ignore[misc]  # tuple 不可替换：dept 只能整体继承


# ---------------------------------------------------------------------------
# 检索行为与结果契约
# ---------------------------------------------------------------------------


def test_命中按向量序截取topn_结构字段齐全() -> None:
    """store 契约 = 按余弦降序返回；工具截前 top_n、score 透传向量相似度。
    （2026-10-07 起不再调 rerank 精排：CPU TEI 实测 20 条 OCR 长块 >120s，
    详见 app/services/agent_tools.py invoke docstring 与 agent-evolution §七）"""
    # 模拟真实 store：按余弦降序排列（标题3 > 标题2 > 标题1）
    hits = []
    for i, sim in [(1, 0.10), (2, 0.55), (3, 0.80)]:
        hit = _hit(i, _vec(f"客服话题{i}"), f"客服正文{i}")
        hit = dataclasses.replace(hit, similarity=sim)
        hits.append((2, hit))
    store = RecordingStore(hits)
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "话题", "top_n": 2}, _ctx())

    items = result.value["items"]
    assert [item["title"] for item in items] == ["标题3", "标题2"]  # 向量序截前 2
    assert items[0]["excerpt"] == "客服正文3"
    assert items[0]["titlePath"] == "部门_主题3"
    assert items[0]["page"] == 1
    assert [item["score"] for item in items] == [0.8, 0.55]  # score=向量余弦透传


def test_top_n钳制在向量序上生效() -> None:
    hits = [(2, _hit(i, _vec(f"客服话题{i}"), f"客服正文{i}")) for i in (1, 2, 3, 4)]
    store = RecordingStore(hits)
    tool = KnowledgeBaseSearchTool(_deps(store, weights=[1.0, 2.0, 3.0, 4.0]))

    result = tool.invoke({"query": "话题", "top_n": 1}, _ctx())

    items = result.value["items"]
    assert [item["title"] for item in items] == ["标题1"]  # store 序截前 1


def test_空命中返回未找到相关内容_拒答语义() -> None:
    tool = KnowledgeBaseSearchTool(_deps(RecordingStore([])))

    result = tool.invoke({"query": "任意问题"}, _ctx())

    assert result.ok is True
    assert result.value == {"items": [], "note": "知识库中未找到相关内容"}


def test_embed重排瞬时故障降级为fail观察_不炸运行() -> None:
    """TEI embed ReadTimeout（实测）：检索不可用应降级为 fail 观察，
    让 agent 循环继续（模型可重试或如实说明），而不是异常炸掉整个 run。"""

    class BoomEmbedder:
        def embed(self, texts):
            raise UpstreamError("tei /embed 传输失败: ReadTimeout: timed out")

    deps = dataclasses.replace(_deps(RecordingStore([])), embedder=BoomEmbedder())
    tool = KnowledgeBaseSearchTool(deps)

    result = tool.invoke({"query": "漫剧生产流程"}, _ctx())

    assert result.ok is False
    assert result.error is not None and "暂时不可用" in result.error


def test_空query返回失败不触发检索() -> None:
    store = RecordingStore([])
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "   "}, _ctx())

    assert result.ok is False
    assert result.error is not None and "query" in result.error
    assert store.calls == []  # 未发起任何检索调用


def test_top_n钳制_缺省回退与合法域一到二十() -> None:
    hits = [(2, _hit(i, _vec(f"话题{i}"), f"正文{i}")) for i in (1, 2, 3, 4)]
    store = RecordingStore(hits)
    tool = KnowledgeBaseSearchTool(_deps(store, weights=[1.0, 2.0, 3.0, 4.0]))
    ctx = _ctx()

    assert len(tool.invoke({"query": "q", "top_n": 2}, ctx).value["items"]) == 2
    assert len(tool.invoke({"query": "q", "top_n": 99}, ctx).value["items"]) == 4  # 钳到 20：全保留
    assert len(tool.invoke({"query": "q"}, ctx).value["items"]) == 4  # 缺省 rerank_top_n=5
    assert len(tool.invoke({"query": "q", "top_n": 0}, ctx).value["items"]) == 1  # 钳到 1
    assert (
        len(tool.invoke({"query": "q", "top_n": True}, ctx).value["items"]) == 4
    )  # bool 视为非法：回退缺省


# ---------------------------------------------------------------------------
# match_qa 直答通道（retrieval-policy §6，工单 03）：主链路一把尺
# ---------------------------------------------------------------------------


def _qa_hit(qa_id: int = 11, similarity: float = 0.93, answer: str = "七天内可退货") -> QaHit:
    return QaHit(qa_id=qa_id, question="退货时限是多少", answer=answer, similarity=similarity)


def test_qa直答命中_跳过文档检索_引用形态与主链路qa计划等价() -> None:
    store = RecordingStore([(2, _hit(1, _vec("客服语料"), "客服正文"))], qa_hit=_qa_hit())
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "退货时限是多少"}, _ctx())

    assert result.ok is True
    assert store.qa_calls[0]["dept_ids"] == [2, 99]  # 直答通道同受 F-502 部门过滤
    assert store.calls == []  # qa 命中：文档检索未发起（主链路 qa 短路同语义）
    item = result.value["items"][0]
    assert item["refType"] == "qa"  # 与主链路 _qa_plan citation 同键（refType/refId/title=问题）
    assert item["refId"] == 11
    assert item["title"] == "退货时限是多少"
    assert item["excerpt"] == "七天内可退货"  # 答案走 excerpt 通道
    assert item["score"] == 0.93  # 余弦相似度（qa 阈值口径，§4.4）


def test_qa阈值读settings非硬编码() -> None:
    store = RecordingStore([], qa_hit=_qa_hit())
    deps = KnowledgeSearchDeps(
        embedder=StubEmbedder(),
        store=store,
        reranker=StubReranker(),
        settings=Settings(
            jwt_secret="x" * 32,
            retriever_top_k=20,
            rerank_top_n=5,
            chunk_dedup_sim=0.98,
            hnsw_ef_search=40,
            qa_threshold=0.9,  # 故意偏离缺省 0.85：断言工具透传 settings 值
        ),
    )

    KnowledgeBaseSearchTool(deps).invoke({"query": "q"}, _ctx())

    assert (
        store.qa_calls[0]["threshold"] == 0.9
    )  # 与主链路 prepare_turn 同尺（settings.qa_threshold）


def test_qa未命中_回落文档检索_文档命中不带qa引用形态() -> None:
    store = RecordingStore([(2, _hit(1, _vec("客服语料"), "客服正文"))])
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "客服语料"}, _ctx())

    assert result.ok is True
    assert len(store.qa_calls) == 1 and len(store.calls) == 1  # 先 qa 后文档：两段次序锁定
    assert all("refType" not in item for item in result.value["items"])  # 文档命中无 qa 引用键


def test_近重复块按0点98折叠_留高分弃低分_向量序上执行() -> None:
    base = _vec("折叠基准文本")
    near = _near(base, _vec("扰动噪声"))
    assert _dot(near, base) >= 0.98  # 前提自检：确超折叠阈值
    hits = [
        (2, dataclasses.replace(_hit(1, base, "折叠基准文本"), similarity=0.90)),
        (2, dataclasses.replace(_hit(2, near, "近重复文本"), similarity=0.85)),
        (2, dataclasses.replace(_hit(3, _vec("独立无关文本"), "独立正文"), similarity=0.80)),
    ]
    store = RecordingStore(hits)
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "折叠查询"}, _ctx())

    titles = [item["title"] for item in result.value["items"]]
    assert titles == ["标题1", "标题3"]  # 标题2 与高分已保留项近重复：被折叠丢弃
    scores = [item["score"] for item in result.value["items"]]
    assert scores == [0.9, 0.8]  # 保留项分数=召回余弦，保持降序


def test_excerpt截断回归锁定_单条摘录500字上限() -> None:
    long_content = "长" * 600
    store = RecordingStore(
        [(2, dataclasses.replace(_hit(1, _vec("长文语料"), long_content), similarity=0.9))]
    )
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "长文语料"}, _ctx())

    excerpt = result.value["items"][0]["excerpt"]
    assert len(excerpt) == 500  # 回归锁定：agent 上下文预算闸不回退
    assert excerpt == "长" * 500


def test_熔断open态agent检索可用_行为与closed态逐项一致() -> None:
    """spec §6：agent 纯向量序本就无精排可降，熔断 open 态行为不变（用例锁定）——
    工具不读熔断状态、输出与 closed 态逐项一致。"""
    corpus = [
        (2, dataclasses.replace(_hit(1, _vec("语料1"), "正文1"), similarity=0.5)),
        (2, dataclasses.replace(_hit(2, _vec("语料2"), "正文2"), similarity=0.4)),
    ]
    closed = KnowledgeBaseSearchTool(_deps(RecordingStore(corpus))).invoke(
        {"query": "语料"}, _ctx()
    )

    breaker = CircuitBreaker(FakeClock("2026-10-09T08:00:00+00:00"))
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "open"  # 前提：熔断确已开路
    set_rerank_breaker(breaker)
    try:
        opened = KnowledgeBaseSearchTool(_deps(RecordingStore(corpus))).invoke(
            {"query": "语料"}, _ctx()
        )
    finally:
        set_rerank_breaker(None)  # 复位进程级单例，防污染其他用例

    assert opened.ok is True  # open 态 agent 检索照常可用
    assert opened.value == closed.value  # 与 closed 态逐项一致（无精排可降，行为不变）


# ---------------------------------------------------------------------------
# fold_top_n 纯函数（retrieval-policy §6/§11 裁决 10，spec §8 冻结清单）
# ---------------------------------------------------------------------------


def _fold_hit(chunk_id: int, embedding: list[float], similarity: float) -> DocHit:
    """折叠纯函数入参：similarity 即调用方口径的最终分数（fold_top_n 只选择不改写）。"""
    return dataclasses.replace(
        _hit(chunk_id, embedding, f"内容正文{chunk_id}"), similarity=similarity
    )


def test_fold_topn_留高分弃低分保序_分数原样不改写() -> None:
    base = _vec("折叠基准文本")
    near = _near(base, _vec("扰动噪声"))
    hits = [
        _fold_hit(1, base, 0.90),
        _fold_hit(2, near, 0.85),  # 与高分已保留项近重复
        _fold_hit(3, _vec("独立无关文本"), 0.80),
    ]

    kept = fold_top_n(hits, 5, 0.98)

    assert [hit.chunk_id for hit in kept] == [1, 3]  # 留高分弃低分（F-1003）
    assert [hit.similarity for hit in kept] == [0.90, 0.80]  # 选择语义：分数口径归调用方
    assert hits[1].similarity == 0.85  # 入参不被改写（frozen dataclass 不变式）


def test_fold_topn_topn截断先于折叠() -> None:
    base = _vec("折叠基准文本")
    near = _near(base, _vec("扰动噪声"))
    hits = [
        _fold_hit(1, base, 0.90),
        _fold_hit(2, near, 0.85),
        _fold_hit(3, _vec("独立文本丙"), 0.80),
        _fold_hit(4, _vec("独立文本丁"), 0.75),
    ]

    assert [hit.chunk_id for hit in fold_top_n(hits, 2, 0.98)] == [1]  # 截断在折叠前：窗内 2 仍折叠
    assert [hit.chunk_id for hit in fold_top_n(hits, 4, 0.98)] == [1, 3, 4]  # 全窗：2 被折叠


def test_fold_topn_return_folded缝隙_保留与丢弃双列表() -> None:
    base = _vec("折叠基准文本")
    near = _near(base, _vec("扰动噪声"))
    hits = [_fold_hit(1, base, 0.90), _fold_hit(2, near, 0.85)]

    kept, folded = fold_top_n(hits, 5, 0.98, return_folded=True)

    assert [hit.chunk_id for hit in kept] == [1]
    assert [hit.chunk_id for hit in folded] == [2]  # 被折叠项带原分数交回（RET-03/04 缝）


def test_fold_topn_空入参返回空() -> None:
    assert fold_top_n([], 5, 0.98) == []
    assert fold_top_n([], 5, 0.98, return_folded=True) == ([], [])


def test_fold_topn与主链路rerank_and_fold折叠同尺对照() -> None:
    """spec §6"不另造"落在 fold 纯函数：同输入（rerank 序=向量序，权重对位注入）下，
    主链路 rerank_and_fold 与 agent 侧 fold_top_n 保留集与顺序逐一相等。"""
    base = _vec("折叠基准文本")
    near = _near(base, _vec("扰动噪声"))
    hits = [
        _fold_hit(1, base, 0.90),
        _fold_hit(2, near, 0.85),
        _fold_hit(3, _vec("独立无关文本"), 0.80),
    ]

    via_main = rerank_and_fold(
        StubReranker(weights=[3.0, 2.0, 1.0]), "改写后问题", hits, top_n=5, dedup_sim=0.98
    )
    via_pure = fold_top_n(hits, 5, 0.98)

    assert [hit.chunk_id for hit in via_main] == [hit.chunk_id for hit in via_pure] == [1, 3]


def test_fold纯函数域禁止import_services_db_结构断言() -> None:
    """fold.py 是 rag 纯函数域（spec §6 模块归属红线）：源码不得出现 services/db 依赖
    字样（test_retrieval_policy 结构断言同款）。"""
    source = Path(fold_module.__file__).read_text(encoding="utf-8")
    assert "app.services" not in source
    assert "app.db" not in source


# ---------------------------------------------------------------------------
# merge_policy 消费接线（retrieval-policy §2/§6，工单 06 rider）：resolver 为 base、
# 档案 profile_policy 为 override（agent_tasks 工单 07 预留的覆盖经此生效）
# ---------------------------------------------------------------------------


def test_profile_policy缺省None_检索参数与现状逐字一致() -> None:
    """两档案本期均 profile_policy=None（test_agent_tasks 冻结断言）：merge_policy(base,
    None) 原样返回 resolver 产出——召回 top_k/ef_search 与工单 02 逐字一致（20/40）。"""
    store = RecordingStore([(2, _hit(1, _vec("客服语料"), "客服正文"))])
    tool = KnowledgeBaseSearchTool(_deps(store))

    result = tool.invoke({"query": "客服语料"}, _ctx())

    assert result.ok is True
    assert store.calls[0]["top_k"] == 20  # resolver 缺省（policy 侧唯一来源）
    assert store.calls[0]["ef_search"] == 40


def test_profile_policy覆盖经merge_policy生效_档案字段优先_resolver兜底() -> None:
    """档案策略覆盖接线辨识：profile_policy 声明的 top_k 覆盖 resolver 产出，未覆盖的
    ef_search 沿用 resolver 缺省（merge_policy 非缺省字段合并语义，测试 07 消费形状照此）。"""
    store = RecordingStore([(2, _hit(i, _vec(f"客服语料{i}"), f"客服正文{i}")) for i in (1, 2, 3)])
    deps = dataclasses.replace(_deps(store), profile_policy=RetrievalPolicy(top_k=2))
    tool = KnowledgeBaseSearchTool(deps)

    result = tool.invoke({"query": "客服语料"}, _ctx())

    assert result.ok is True
    assert store.calls[0]["top_k"] == 2  # 档案覆盖优先
    assert store.calls[0]["ef_search"] == 40  # 未覆盖字段沿用 resolver 产出
