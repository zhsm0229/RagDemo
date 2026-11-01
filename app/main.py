"""应用装配：路由挂载 + 全局异常处理器注册 + lifespan 定时任务挂接。

装配层是全项目唯一允许组装依赖与选择实现的位置（standards §1.2）；业务模块
只 import 本模块暴露的 app（uvicorn app.main:app）。
"""

import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api import (
    admin_audit,
    admin_eval,
    admin_eval_auto,
    admin_eval_questions,
    admin_hardcase,
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
from app.core.assembly import build_llm
from app.core.clock import get_clock
from app.core.config import Settings, get_settings
from app.services.deps_assembly import (
    ScriptedAgentChannel,
    StateBundle,
    _build_agent_deps,
    _build_chat_deps,
    _build_state,
)
from app.services.scheduler import add_hit_flush_job, build_scheduler, release_leader
from app.spa_static import _mount_spa

__all__ = [
    "ScriptedAgentChannel",
    "StateBundle",
    "_build_agent_deps",
    "_build_chat_deps",
    "_build_state",
    "create_app",
    "lifespan",
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """生命周期挂接：装配问答依赖挂 app.state + 定时任务（Task 7 交接④：HitCounter
    由装配方持有并注入 add_hit_flush_job，调度刷库与请求侧 add 共享同一份聚合）；
    test_mode 只装配不自启，退出关停。"""
    settings = get_settings()
    # 状态五件先行：问答依赖与鉴权路由共享同一 bundle（工单 19 接线三处显式传参）
    state = _build_state(settings)
    chat_deps = _build_chat_deps(settings, state)  # 先装配问答依赖：§6.4 fail-fast 不留已启调度器
    scheduler = build_scheduler(settings)
    add_hit_flush_job(scheduler, chat_deps.hit_counter, settings.cache_ttl_s)
    app.state.chat_deps = chat_deps
    app.state.state_bundle = state
    app.state.agent_deps = _build_agent_deps(settings, chat_deps)
    # 启动补偿放后台线程：合成/跑分是分钟-小时级 LLM 任务，同步执行会阻塞 lifespan
    # （healthz 不就绪、端口不监听——2026-10-31 dev 实测教训）；daemon=True 随进程退出。
    # 补偿只在 leader 跑（工单 20 §3.2）：幂等但烧 LLM 预算，抢主锁是现成的单写者信号
    # （N worker 各跑一遍 = N 倍账单）；is_leader 属性缺席按 False 兜底（test_mode 等
    # 未跑选举的形态零副作用）
    if getattr(scheduler, "is_leader", False):
        threading.Thread(
            target=_startup_catchup, args=(settings,), daemon=True, name="eval-catchup"
        ).start()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        release_leader()  # 先 shutdown 后释放（工单 20 §3.1）：关持锁连接，未抢锁为 no-op


def _startup_catchup(settings: Settings) -> None:
    """启动补偿（商用就绪工单 06，评审 P1-1）：停机跨过周日触发窗口后，重启即补跑
    当周缺失的合成流水与三视角跑分各一轮。test_mode 跳过（演示模式零副作用）；总闸
    关（缺省）零副作用在 catchup 任务体内判定；异常吞掉留日志——补偿失败不阻塞
    服务启动（下轮 cron 兜底）。"""
    if settings.test_mode:
        return
    try:
        from app.db.base import SessionLocal
        from app.services.eval_catchup import catchup_missed_weekly

        with SessionLocal() as session:
            summary = catchup_missed_weekly(
                session,
                get_clock(),
                settings,
                deps=_build_chat_deps(settings),
                llm=build_llm(settings, 0.0),
            )
        if summary["synth"] or summary["eval"]:
            logging.getLogger(__name__).info("启动补偿完成：%s", summary)
    except Exception:
        logging.getLogger(__name__).exception("启动补偿执行失败（下轮 cron 兜底）")


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
    app.include_router(
        admin_eval_questions.router, prefix="/api"
    )  # 题库列表/层级切换（评测集自动更新 02 票）
    app.include_router(
        admin_hardcase.router, prefix="/api"
    )  # 难例池列表/忽略/手动合成（评测集自动更新 03 票）
    app.include_router(
        admin_eval_auto.router, prefix="/api"
    )  # 评测自动配置读写（评测集自动更新 05 票）
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


app = create_app()
