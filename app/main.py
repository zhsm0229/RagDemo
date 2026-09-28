"""应用装配：路由挂载 + 全局异常处理器注册 + lifespan 定时任务挂接。

装配层是全项目唯一允许组装依赖与选择实现的位置（standards §1.2）；业务模块
只 import 本模块暴露的 app（uvicorn app.main:app）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api import auth, documents
from app.core import errors
from app.core.config import get_settings
from app.services.scheduler import build_scheduler


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """生命周期挂接定时任务：启动装配（test_mode 只装配不自启），退出关停。"""
    scheduler = build_scheduler(get_settings())
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="企业智能工作台", version="0.1.0", lifespan=lifespan)
    app.include_router(auth.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    # 注册顺序即匹配优先级：ApiError → 参数校验 → 兜底 Exception（500 通用错误）
    app.add_exception_handler(errors.ApiError, errors.api_error_handler)
    app.add_exception_handler(RequestValidationError, errors.validation_error_handler)
    app.add_exception_handler(Exception, errors.unhandled_error_handler)
    return app


app = create_app()
