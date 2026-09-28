"""API 依赖：请求级 DB 会话、当前用户解析与权限门（见包 __init__ 的依赖链说明）。

会话：每请求一个 SessionLocal，commit 归 service（业务写库与审计同事务）；
测试经 dependency_overrides 换成测试事务 session（见 tests/api/conftest.py）。
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ERR_FORBIDDEN, ERR_UNAUTHENTICATED, ApiError
from app.core.security import decode_token
from app.db.base import SessionLocal
from app.db.models import User
from app.services.auth import get_user_by_id

COOKIE_NAME = "jwt"  # httpOnly Cookie 名，登录签发与本处解析共用
_MSG_UNAUTHENTICATED = "未登录或登录已过期"


def get_db() -> Iterator[Session]:
    """请求级会话：不代 commit（事务边界归 service），仅保证关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Annotated 依赖别名（FastAPI 推荐写法，免 B008）：逐个定义在其函数之后供复用
DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(request: Request, db: DbSession) -> User:
    """Cookie → decode（exp 按可注入时钟）→ 查库 is_active → 注入用户。

    无 cookie / 令牌无效或过期 / 用户不存在或已禁用：一律 401/2003 同文案
    （§5.6 禁用即时生效；不区分原因，不给探测空间）。
    """
    token = request.cookies.get(COOKIE_NAME)
    payload = decode_token(token, get_settings().jwt_secret) if token else None
    if payload is None:
        raise ApiError(ERR_UNAUTHENTICATED, _MSG_UNAUTHENTICATED, 401)
    user = get_user_by_id(db, payload.user_id)
    if user is None or not user.is_active:
        raise ApiError(ERR_UNAUTHENTICATED, _MSG_UNAUTHENTICATED, 401)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_password_changed(user: CurrentUser) -> User:
    """首登强制改密门：must_change 用户不得访问业务接口（403/2004，AUTH-04）。"""
    if user.must_change_password:
        raise ApiError(ERR_FORBIDDEN, "首次登录请先修改密码", 403)
    return user


ReadyUser = Annotated[User, Depends(require_password_changed)]


def require_admin(user: ReadyUser) -> User:
    """admin 权限门：链在改密门之后（不改密连管理接口也不可达）；403/2004。"""
    if user.role != "admin":
        raise ApiError(ERR_FORBIDDEN, "无权限执行该操作", 403)
    return user


AdminUser = Annotated[User, Depends(require_admin)]
