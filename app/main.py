"""应用装配：路由挂载 + 全局异常处理器注册 + lifespan 定时任务挂接。

装配层是全项目唯一允许组装依赖与选择实现的位置（standards §1.2）；业务模块
只 import 本模块暴露的 app（uvicorn app.main:app）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api import admin_qa_pairs, auth, chat, conversations, documents, feedback
from app.core import errors
from app.core.assembly import build_embedder, build_llm, build_reranker, load_prompt_pack
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
from app.services.chat import ChatDeps
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.scheduler import add_hit_flush_job, build_scheduler


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
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="企业智能工作台", version="0.1.0", lifespan=lifespan)
    app.include_router(auth.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    app.include_router(admin_qa_pairs.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(conversations.router, prefix="/api")
    app.include_router(feedback.router, prefix="/api")
    # 注册顺序即匹配优先级：ApiError → 参数校验 → 兜底 Exception（500 通用错误）
    app.add_exception_handler(errors.ApiError, errors.api_error_handler)
    app.add_exception_handler(RequestValidationError, errors.validation_error_handler)
    app.add_exception_handler(Exception, errors.unhandled_error_handler)
    return app


app = create_app()
