"""鉴权路由（薄控制器）：Pydantic 参数校验 + 委托 services/auth，无业务判定。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import COOKIE_NAME, AdminUser, CurrentUser, DbSession
from app.core.config import get_settings
from app.db.models import User
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录入参：格式下限保采集完整，72 字节上限由服务层按字节校验（bcrypt）。"""

    username: str = Field(min_length=1, max_length=64, description="用户名")
    password: str = Field(min_length=1, max_length=200, description="密码")


class ChangePasswordRequest(BaseModel):
    old: str = Field(min_length=1, max_length=200, description="原密码")
    new: str = Field(min_length=1, max_length=200, description="新密码（≥8 位且 ≤72 字节）")


def _user_payload(user: User) -> dict:
    """用户信息响应（camelCase，standards §3.4；数据库行不直接返回）。"""
    return {
        "userId": user.id,
        "username": user.username,
        "role": user.role,
        "mustChangePassword": user.must_change_password,
    }


@router.post("/login")
def login(body: LoginRequest, request: Request, db: DbSession) -> JSONResponse:
    """登录：成功 Set-Cookie(jwt, httpOnly, SameSite=Lax, 8h)；失败经全局异常处理器。"""
    ip = request.client.host if request.client else None
    user, token = auth_service.login(db, body.username, body.password, ip)
    response = JSONResponse(content={"code": 0, "message": "ok", "data": _user_payload(user)})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=get_settings().access_token_expire_minutes * 60,
        httponly=True,
        samesite="lax",  # Secure 与否随部署形态配置化（§6.5），内网 http 不启用
    )
    return response


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, user: CurrentUser, db: DbSession) -> dict:
    """改密（含首登强制）：data 返回更新后的用户信息（mustChangePassword 已清）。"""
    updated = auth_service.change_password(db, user, body.old, body.new)
    return {"code": 0, "message": "ok", "data": _user_payload(updated)}


@router.get("/me")
def me(user: CurrentUser) -> dict:
    """当前用户信息：前端凭 mustChangePassword 决定是否引导改密页（不设改密门）。"""
    return {"code": 0, "message": "ok", "data": _user_payload(user)}


@router.get("/admin/ping")
def admin_ping(user: AdminUser) -> dict:
    """admin-only 探针：验证 require_admin 依赖链（AUTH-04/06 的测试锚点）。"""
    return {"code": 0, "message": "ok", "data": {"pong": True}}
