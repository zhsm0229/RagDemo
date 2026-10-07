r"""ToolProvider 注册表与知识库检索工具单测（工单 #04）：部门隔离红线证明。

以 RecordingStore 模拟 F-502 SQL 的部门过滤（命中按 dept_id ∈ dept_ids 放行，与
services/retrieval._chunk_search_statement 的 where 语义同构），证明：A 部门调用者
（含 args 夹带部门字段）检索不到 B 部门语料；dept_ids 逐次原样传给 search_chunks
（强制继承，无绕过）。Embedder/Reranker 用 core.llm Stub（同输入必同输出），
Settings 显式传参（不读 .env，测试确定性）。
"""

import dataclasses

import pytest

from app.core.config import Settings
from app.core.llm import StubEmbedder, StubReranker, UpstreamError
from app.core.tools import RunContext, ToolProvider, ToolRegistry, ToolSpec
from app.services.agent_tools import (
    KNOWLEDGE_SEARCH_SPEC,
    KnowledgeBaseSearchTool,
    KnowledgeSearchDeps,
)
from app.services.retrieval import DocHit


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


class RecordingStore:
    """替身 VectorStore：行为模拟 F-502（dept_id ∈ dept_ids 才可见）并按相似度降序
    返回（对齐 pgvector ORDER BY embedding <=> query_vec 的真实契约），记录每次调用。"""

    def __init__(self, corpus: list[tuple[int, DocHit]]) -> None:
        self._corpus = corpus  # (归属 dept_id, 命中)
        self.calls: list[dict] = []

    def search_chunks(self, db, query_vec, dept_ids, top_k, ef_search):
        self.calls.append({"dept_ids": list(dept_ids), "top_k": top_k})
        visible = {dept_id for dept_id in dept_ids}
        hits = [hit for owner, hit in self._corpus if owner in visible]
        return sorted(hits, key=lambda h: h.similarity, reverse=True)

    def match_qa(self, db, query_vec, dept_ids, threshold):
        return None


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
