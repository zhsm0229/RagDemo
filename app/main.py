"""应用装配：路由挂载 + 全局异常处理器注册 + lifespan 定时任务挂接。

装配层是全项目唯一允许组装依赖与选择实现的位置（standards §1.2）；业务模块
只 import 本模块暴露的 app（uvicorn app.main:app）。
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin_audit,
    admin_eval,
    admin_knowledge_gap,
    admin_overview,
    admin_qa_pairs,
    admin_retrieval_test,
    admin_services_health,
    admin_users,
    admin_views,
    agent_runs,
    auth,
    chat,
    citations,
    conversations,
    document_admin,
    document_files,
    document_rebuild,
    documents,
    feedback,
    healthz,
    memories,
)
from app.core import errors
from app.core.agent_model import ChannelTurn, OpenAICompatToolChannel
from app.core.assembly import (
    build_embedder,
    build_llm,
    build_reranker,
    build_storage,
    load_prompt_pack,
)
from app.core.circuit_breaker import CircuitBreakerReranker, get_rerank_breaker
from app.core.clock import SystemClock
from app.core.config import Settings, get_settings
from app.core.llm import (
    Embedder,
    LLMProvider,
    Reranker,
    StubEmbedder,
    StubLLMProvider,
    StubReranker,
)
from app.core.tools import ToolRegistry
from app.services.agent_runs import AgentDeps
from app.services.agent_tools import (
    KnowledgeBaseSearchTool,
    KnowledgeSearchDeps,
    SaveDraftDeps,
    SaveDraftTool,
)
from app.services.chat import ChatDeps
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.retrieval import PgVectorStore
from app.services.scheduler import add_hit_flush_job, build_scheduler
from app.services.vector_store import VectorStore


def _build_chat_deps(settings: Settings) -> ChatDeps:
    """问答链路依赖包装配（Task 9）：test_mode → Stub 三件套，否则 TEI / OpenAI 兼容
    真实现（build_llm 非 test_mode 且 llm_base_url 空即 RuntimeError 拒启，§6.4）。
    提示词包先装载：温度随模板同版本化（§17.4），pack.temperature 传入 LLM 装配——
    manifest 改温度在装配期生效，不允许与 provider 缺省值静默脱钩。
    进程内缓存/每日限额/召回计数用 SystemClock 直连：它们是进程级单例，不随测试拨动
    全局可注入时钟而换实例（TTL 与限额口径在进程生命周期内稳定）。"""
    prompts = load_prompt_pack()
    if settings.test_mode:
        embedder: Embedder = StubEmbedder()
        reranker: Reranker = StubReranker()
        llm: LLMProvider = StubLLMProvider()
    else:
        embedder = build_embedder(settings)
        reranker = build_reranker(settings)
        llm = build_llm(settings, prompts.temperature)
    # rerank 熔断装饰器在装配层包装 build_reranker 产物（retrieval-policy §3/§11 裁决 3：
    # 实现类只在装配层出现）；test_mode 同样包装——故障语义与生产一致，状态接
    # services_health 共享同一单例
    reranker = CircuitBreakerReranker(reranker, get_rerank_breaker())
    # VectorStore 第六接口在装配层选择实现（services 层协议不进 core/assembly——
    # 五层单向依赖）；pg 实现无状态不随 test_mode 变化（stub 的是向量产物，检索恒打真库）
    store: VectorStore = PgVectorStore()
    clock = SystemClock()
    return ChatDeps(
        embedder=embedder,
        reranker=reranker,
        llm=llm,
        cache=ExactCache(clock, settings.cache_ttl_s),
        counter=DailyCounter(clock),
        hit_counter=HitCounter(clock),
        settings=settings,
        prompts=prompts,
        store=store,
    )


class ScriptedAgentChannel:
    """脚本化确定性通道（test_mode agent 装配用，Stub 三件套同哲学：同输入必同输出、
    零网络）。进度 = 对话里已有的工具结果条数（agent 循环每收一个观察回灌一条 tool
    消息）：0 条发首回合、N 条发第 N+1 回合，脚本耗尽恒重放末条——无实例状态，
    lifespan 级单例跨 run 安全（实例计数版会让第二个 run 起恒拿末条正文、无工具步，
    AGENT-01 时间线断言随机挂，2026-10-11 重启容器 A/B 实验实证）。定义在装配模块
    而非 core：core 只承载协议与适配层，业务剧本属装配选择（main 内定义实现类有
    _HashedAssets 先例）。"""

    def __init__(self, turns: list[ChannelTurn]) -> None:
        if not turns:
            raise ValueError("脚本化通道至少需要一个回合")
        self._turns = turns

    def complete(
        self, messages: Any, tools: Any, max_tokens: int
    ) -> ChannelTurn:  # AgentModelChannel 协议面：tools 只透传不消费
        tool_results = sum(
            1 for m in messages if isinstance(m, dict) and m.get("role") == "tool"
        )
        return self._turns[min(tool_results, len(self._turns) - 1)]


# test_mode 装配的 agent 演示剧本（ScriptedAgentChannel 注入）：
# 首轮发起一次 knowledge_base_search 工具调用、次轮给最终正文——恰好覆盖 AG-UI 的
# 工具事件组与文本事件组，事件序测试即以此剧本为准。
_TEST_AGENT_TURNS = (
    ChannelTurn(
        content="",
        tool_calls=(
            {
                "name": "knowledge_base_search",
                "args": {"query": "年假"},
                "id": "call_1",
                "type": "tool_call",
            },
        ),
    ),
    ChannelTurn(content="根据《考勤制度》，年假按工龄五到十天。"),
)


def _build_agent_deps(settings: Settings, chat_deps: ChatDeps) -> AgentDeps:
    """Agent 链路依赖装配（F-212，工单 #07/#06）：检索工具与问答共享 embedder/reranker/
    store（test_mode 同为 Stub/PgVectorStore 组合）；save_draft 包装草稿审核流数据面
    （draft_review.create_draft），storage 走装配工厂单例。工具注册表是全集——每任务
    实际绑定哪些工具由任务档案（services/agent_tasks.TASK_PROFILES）白名单决定。
    通道按档位设置装配，留空复用主问答通道（Settings.agent_model_* 注释口径），
    test_mode 换脚本化通道（零网络确定性）。counter 直取 chat_deps 同一实例：
    agent 与问答共享每日额度，换端点不绕限。"""
    registry = ToolRegistry()
    registry.register(
        KnowledgeBaseSearchTool(
            KnowledgeSearchDeps(
                embedder=chat_deps.embedder,
                store=chat_deps.store,
                reranker=chat_deps.reranker,
                settings=settings,
            )
        )
    )
    registry.register(SaveDraftTool(SaveDraftDeps(storage=build_storage(settings))))
    if settings.test_mode:
        channel = ScriptedAgentChannel(_TEST_AGENT_TURNS)
    else:
        channel = OpenAICompatToolChannel(
            base_url=settings.agent_model_base_url or settings.llm_base_url,
            api_key=settings.agent_model_api_key or settings.llm_api_key,
            model=settings.agent_model_model or settings.llm_model,
            timeout_s=settings.agent_model_timeout_s,
            effort=settings.llm_effort or None,
        )
    return AgentDeps(
        settings=settings,
        registry=registry,
        channel=channel,
        react_fallback=settings.agent_model_react_fallback,
        counter=chat_deps.counter,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """生命周期挂接：装配问答依赖挂 app.state + 定时任务（Task 7 交接④：HitCounter
    由装配方持有并注入 add_hit_flush_job，调度刷库与请求侧 add 共享同一份聚合）；
    test_mode 只装配不自启，退出关停。"""
    settings = get_settings()
    chat_deps = _build_chat_deps(settings)  # 先装配问答依赖：§6.4 fail-fast 不留已启调度器
    scheduler = build_scheduler(settings)
    add_hit_flush_job(scheduler, chat_deps.hit_counter, settings.cache_ttl_s)
    app.state.chat_deps = chat_deps
    app.state.agent_deps = _build_agent_deps(settings, chat_deps)
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    # 根日志在装配前初始化：解析管道/调度器的 logger 依赖 root handler 输出——此前
    # 无任何 logging 配置，logger.info 全被 lastResort 吞掉、异常 traceback 丢失，
    # 排障无日志可查（2026-10-02 试点反馈）。LOG_LEVEL 可调，缺省 INFO。
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(title="企业智能工作台", version="0.1.0", lifespan=lifespan)
    app.include_router(healthz.router)  # 无前缀：探针路径 /healthz（docker healthcheck 消费）
    app.include_router(auth.router, prefix="/api")
    # documents 域按域拆三个子 router（行数红线 §1.4，同 prefix=/documents）：入库/治理/回显
    app.include_router(documents.router, prefix="/api")
    app.include_router(document_admin.router, prefix="/api")
    app.include_router(
        document_admin.admin_router, prefix="/api"
    )  # 分型域 /api/admin/documents/*（metrics-balance 工单 01，spec §3.4 钉路径）
    app.include_router(document_files.router, prefix="/api")
    app.include_router(document_rebuild.router, prefix="/api")
    app.include_router(admin_qa_pairs.router, prefix="/api")
    app.include_router(admin_eval.router, prefix="/api")
    app.include_router(admin_eval.runs_router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(
        agent_runs.router, prefix="/api"
    )  # AG-UI 双轨新轨（工单 #07），旧四事件契约冻结
    app.include_router(citations.router, prefix="/api")
    app.include_router(conversations.router, prefix="/api")
    app.include_router(feedback.router, prefix="/api")
    app.include_router(memories.router, prefix="/api")
    app.include_router(admin_views.router, prefix="/api")
    app.include_router(admin_retrieval_test.router, prefix="/api")
    app.include_router(admin_overview.router, prefix="/api")
    app.include_router(admin_services_health.router, prefix="/api")
    app.include_router(admin_users.router, prefix="/api")
    app.include_router(admin_audit.router, prefix="/api")
    app.include_router(admin_knowledge_gap.router, prefix="/api")
    # 注册顺序即匹配优先级：ApiError → 参数校验 → 兜底 Exception（500 通用错误）
    app.add_exception_handler(errors.ApiError, errors.api_error_handler)
    app.add_exception_handler(RequestValidationError, errors.validation_error_handler)
    app.add_exception_handler(Exception, errors.unhandled_error_handler)
    _mount_spa(app)
    return app


class _HashedAssets(StaticFiles):
    """/assets 响应追加 immutable 长缓存（vite hash 文件名，内容变则名变）。"""

    def file_response(self, *args: Any, **kwargs: Any) -> FileResponse:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _no_cache(response: FileResponse) -> FileResponse:
    """index.html 等无 hash 入口文件：no-cache = 可缓存但每次必须协商校验。"""
    response.headers["Cache-Control"] = "no-cache"
    return response


def _mount_spa(app: FastAPI) -> None:
    """前端 dist 托管（§12.2 生产仅 app 发布 80 端口的形态）。

    STATIC_DIR 未设置（本机 dev 裸 uvicorn、CI 的 test_mode API 测试）时不挂载，
    行为与历史完全一致；设置了且存在 index.html 才托管。必须最后注册：catch-all
    只兜未匹配路径（/docs、/api/** 等先注册者优先生效）。

    缓存策略：/assets 是 vite hash 文件名产物（内容变则名变）→ immutable 长缓存；
    index.html 无 hash，必须 no-cache 强制协商校验——否则浏览器启发式缓存旧壳，
    发新版后用户仍加载旧 JS（2026-10-02 zip 按钮旧文案事故的根因之一）。
    """
    static_dir = os.getenv("STATIC_DIR", "")
    if not static_dir or not (Path(static_dir) / "index.html").is_file():
        return
    root = Path(static_dir)
    app.mount("/assets", _HashedAssets(directory=root / "assets"), name="spa-assets")

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def _spa(spa_path: str) -> FileResponse:
        if spa_path.startswith("api/"):  # 未匹配的 API 路径保持 404 JSON，绝不回前端页
            raise HTTPException(status_code=404)
        candidate = (root / spa_path).resolve()
        if spa_path and candidate.is_relative_to(root.resolve()) and candidate.is_file():
            return _no_cache(FileResponse(candidate))  # vite 根部零散文件（favicon 等）
        return _no_cache(FileResponse(root / "index.html"))  # 客户端路由（/chat、/kb/**）fallback


app = create_app()
