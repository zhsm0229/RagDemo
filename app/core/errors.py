r"""业务错误基座：ApiError(code, message, http_status)——全仓库唯一业务异常形态
（standards §2.5：禁止裸抛字符串/字典；响应体 `{code, message, data}`）。

错误码取自共享枚举（standards §3.2：1xxx 参数/文件、2xxx 鉴权、3xxx 业务、5xxx
系统），收敛为本模块常量，禁止魔法数字散落各处。全局异常处理器同落本模块
（ApiError → 统一响应体；Pydantic 校验失败 → 422/1003；兜底 Exception → 500
通用错误，堆栈进日志不进响应体）——处理器依赖 fastapi 的 Request/JSONResponse，
属装配关切，经任务约定集中于此，由 app/main.py 注册。
"""

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

ERR_UNSUPPORTED_FORMAT: int = 1001  # 1xxx 文件类：上传格式不支持（.doc 请另存为 docx 后上传）
ERR_PARSE_FAILED: int = 1002  # 1xxx 文件类：语料解析失败（包体非法/超限/含危险声明）
ERR_PARAM_INVALID: int = 1003  # 1xxx 参数类：请求参数校验失败（缺字段/超限/格式非法）

# 2xxx 鉴权类（Task 12 追加）
ERR_INVALID_CREDENTIALS: int = 2001  # 用户名或密码错误
ERR_ACCOUNT_LOCKED: int = 2002  # 连续失败触发锁定 / 锁定窗口内（正确密码亦拒绝）
ERR_UNAUTHENTICATED: int = 2003  # 未登录 / 令牌无效或过期 / 账号已禁用
ERR_FORBIDDEN: int = 2004  # 已登录但无权限（含首登未改密即访问业务接口）
ERR_RATE_LIMITED: int = 2005  # 登录请求过于频繁（进程内滑动窗口限流）

# 3xxx 业务类
ERR_DUPLICATE_DOC: int = 3001  # 上传文件与库内已有文档完全重复（F-1001 精确查重，409）
ERR_DAILY_LIMITED: int = 3002  # 单用户每日提问次数已达上限（问答端点开流前拒绝，429）
ERR_NOT_FOUND: int = 3003  # 会话不存在或无权访问（问答端点归属校验，404，IDOR 防线 §21.8）

# 5xxx 系统类
ERR_INTERNAL: int = 5000  # 兜底系统错误（响应体只给通用提示，堆栈进日志 §16.4）
ERR_LLM_UNAVAILABLE: int = (
    5001  # 生成服务暂时不可用（SSE error 事件，retryable=true，对外脱敏 §6.8）
)


class ApiError(Exception):
    """业务错误：code 为稳定错误码（见模块 docstring 枚举约定），message 为面向
    用户的中文提示（技术细节进日志不进界面），http_status 为响应状态码。"""

    def __init__(self, code: int, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code: int = code
        self.message: str = message
        self.http_status: int = http_status


def _envelope(code: int, message: str, data: object = None) -> dict:
    """统一响应体（standards §3.2）：键名 camelCase，code=0 成功。"""
    return {"code": code, "message": message, "data": data}


def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """ApiError → {code, message, data} 统一响应体，状态码取异常携带值。"""
    return JSONResponse(status_code=exc.http_status, content=_envelope(exc.code, exc.message))


def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic 参数校验失败 → 422/1003，data 携带字段级错误（standards §21.9）。

    普通字段级错误 loc 原样映射（行为不变）；model_validator 的跨字段 value_error
    loc 为空（仅 body 前缀），字段名由校验器抛出异常的 .field 属性经 ctx.error
    带出（Task 11 反馈端点先例，CHAT-09）。msg 统一去掉 "Value error, " 前缀保
    字段级中文。两类小改均向后兼容：无 .field 属性的错误按原 loc 逻辑映射。
    """
    fields = []
    for err in exc.errors():
        joined = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
        ctx_error = (err.get("ctx") or {}).get("error")
        fields.append(
            {
                "field": joined or getattr(ctx_error, "field", ""),
                "message": str(err.get("msg", "")).removeprefix("Value error, "),
            }
        )
    return JSONResponse(
        status_code=422, content=_envelope(ERR_PARAM_INVALID, "请求参数错误", fields)
    )


def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底 Exception → 500 通用错误：堆栈进日志，响应体不透传内部细节（§16.4）。"""
    logger.exception("未处理异常 path=%s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content=_envelope(ERR_INTERNAL, "系统繁忙，请稍后重试"))
