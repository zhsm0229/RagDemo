"""应用装配：路由挂载 + 全局异常处理器注册。

装配层是全项目唯一允许组装依赖与选择实现的位置（standards §1.2）；业务模块
只 import 本模块暴露的 app（uvicorn app.main:app）。
"""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api import auth, documents
from app.core import errors


def create_app() -> FastAPI:
    app = FastAPI(title="企业智能工作台", version="0.1.0")
    app.include_router(auth.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    # 注册顺序即匹配优先级：ApiError → 参数校验 → 兜底 Exception（500 通用错误）
    app.add_exception_handler(errors.ApiError, errors.api_error_handler)
    app.add_exception_handler(RequestValidationError, errors.validation_error_handler)
    app.add_exception_handler(Exception, errors.unhandled_error_handler)
    return app


app = create_app()
