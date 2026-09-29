r"""管理端用户路由（薄控制器，F-102）：列表 / 发放 / 启停 / 重置密码。

业务全部委托 services/users_admin；随机初始密码仅在发放/重置的响应体出现一次
（F-102 验收：二次不可见，前端当场转交员工）。admin-only（AdminUser 依赖链：
登录 → 首登改密门 → admin 门）；业务错误经全局异常处理器转统一响应体。
"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import AdminUser, DbSession
from app.services import users_admin as users_service

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class CreateUserBody(BaseModel):
    """发放入参：用户名 2-32 位中文/字母/数字（§21.5，服务层校验给中文 422）。"""

    username: str
    deptCode: str
    role: str


class ToggleStatusBody(BaseModel):
    """PATCH /status 请求体：isActive=false 禁用（即时失效），true 启用。"""

    isActive: bool


@router.get("")
def list_users(
    user: AdminUser,
    db: DbSession,
    keyword: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """用户列表（F-102）：keyword 对 username ILIKE（通配符已转义），降序分页。"""
    data = users_service.list_users(db, keyword=keyword, page=page, page_size=pageSize)
    return {"code": 0, "message": "ok", "data": data}


@router.post("", status_code=201)
def create_user(body: CreateUserBody, user: AdminUser, db: DbSession) -> dict:
    """发放账号（F-102）：201 {id, initialPassword}——初始密码仅此一次展示。"""
    data = users_service.create_user(
        db, username=body.username, dept_code=body.deptCode, role=body.role, actor_user_id=user.id
    )
    return {"code": 0, "message": "ok", "data": data}


@router.patch("/{user_id}/status")
def toggle_user_status(
    user_id: int, body: ToggleStatusBody, user: AdminUser, db: DbSession
) -> dict:
    """启用/禁用：禁用即时失效（§5.6 下次请求 401）；禁用自己 422 防自锁。"""
    target = users_service.toggle_user_status(
        db, target_user_id=user_id, is_active=body.isActive, actor_user_id=user.id
    )
    return {"code": 0, "message": "ok", "data": {"id": target.id, "isActive": target.is_active}}


@router.post("/{user_id}/reset-password")
def reset_user_password(user_id: int, user: AdminUser, db: DbSession) -> dict:
    """重置密码：新初始密码一次展示 + must_change=true 首登强制改密。"""
    data = users_service.reset_user_password(db, target_user_id=user_id, actor_user_id=user.id)
    return {"code": 0, "message": "ok", "data": data}
