r"""我的记忆路由（薄控制器，F-210 管理面）：GET/POST /api/memories、DELETE
/api/memories/{memory_id}、PUT /api/memories/settings —— 记忆是个人数据（全员
可用，ReadyUser 门），只操作本人数据（归属裁决在 service，IDOR §21.8）。

业务全部委托 services/memory；变更类动作（添加/删除/开关）由 service 与审计
同事务留痕。统一响应体 {code, message, data}（standards §3.4）。
"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import DbSession, ReadyUser
from app.services import memory as memory_service

router = APIRouter(prefix="/memories", tags=["memories"])


class AddMemoryBody(BaseModel):
    """POST /api/memories 请求体：内容 1-200 字（strip 口径，校验在 service）。"""

    content: str


class MemorySettingsBody(BaseModel):
    """PUT /api/memories/settings 请求体：用户级记忆开关（false=冻结，记忆保留）。"""

    enabled: bool


@router.get("")
def list_memories(
    user: ReadyUser,
    db: DbSession,
    keyword: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """本人记忆列表：软删行不出现，updated_at 降序；keyword 对内容过滤（通配符
    已转义 §22）；{items, total, page, pageSize}（standards §3-5）。"""
    data = memory_service.list_for_user(db, user.id, keyword=keyword, page=page, page_size=pageSize)
    return {"code": 0, "message": "ok", "data": data}


@router.post("", status_code=201)
def add_memory(body: AddMemoryBody, user: ReadyUser, db: DbSession) -> dict:
    """手动添加（201）：strip 后 1-200 字，容量超限自动淘汰最旧（软删）；
    落库 + 审计 memory.add 同事务提交。"""
    memory = memory_service.add_manual(db, user.id, body.content)
    return {"code": 0, "message": "ok", "data": memory_service.item_payload(memory)}


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, user: ReadyUser, db: DbSession) -> dict:
    """软删（行保留可追溯）：不存在/已删/归属他人一律 404 同话术（service 裁决）；
    审计 memory.delete 同事务提交。"""
    memory = memory_service.soft_delete(db, user.id, memory_id)
    return {"code": 0, "message": "ok", "data": {"id": memory.id, "isActive": memory.is_active}}


@router.put("/settings")
def update_memory_settings(body: MemorySettingsBody, user: ReadyUser, db: DbSession) -> dict:
    """用户级记忆开关（§3.5）：false=冻结（不注入不提炼），已有记忆保留不删除；
    审计 memory.settings 同事务提交；返回落库值供前端以后端为准校准。"""
    updated = memory_service.set_enabled(db, user, body.enabled)
    return {"code": 0, "message": "ok", "data": {"memoryEnabled": updated.memory_enabled}}
