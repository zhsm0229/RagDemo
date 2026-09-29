r"""审计查询路由（F-802，前端 P11 审计日志页后端）：列表 / CSV 导出。

admin-only（AdminUser 依赖链：登录 → 首登改密门 → admin 门）；audit_logs 只增
不改（F-801），两端点均只读、不留审计。筛选经共享依赖解析，两端口径一致且全
参数绑定；列表 {items,total,page,pageSize}（§3.5）。导出为 StreamingResponse 逐行
推送（不整表载内存）：UTF-8 BOM 适配 Excel、中文列头，行数超 EXPORT_LIMIT 截断
并以 X-Truncated: true 标注（内存放大保护），文件名 audit-{yyyymmdd}.csv（取注入
时钟当日）。响应头须在流开始前确定，故截断判定以同 WHERE 的 count 先行完成。
"""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.deps import AdminUser, DbSession
from app.core.clock import get_clock
from app.services import audit_query
from app.services.audit_query import EXPORT_LIMIT, AuditFilters

router = APIRouter(prefix="/admin/audit-logs", tags=["admin-audit"])


def _audit_filters(
    actorId: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query()] = None,
    objectType: Annotated[str | None, Query()] = None,
    dateFrom: Annotated[dt.date | None, Query()] = None,
    dateTo: Annotated[dt.date | None, Query()] = None,
) -> AuditFilters:
    """列表与导出共用的筛选入参（brief 钉死两端口径一致），camelCase 映射服务层。"""
    return AuditFilters(
        actor_id=actorId,
        action=action,
        object_type=objectType,
        date_from=dateFrom,
        date_to=dateTo,
    )


_AuditQuery = Annotated[AuditFilters, Depends(_audit_filters)]


@router.get("")
def list_audit_logs(
    user: AdminUser,
    db: DbSession,
    filters: _AuditQuery,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """审计列表（F-802）：按操作人/动作/对象类型/日期范围（含首尾日，UTC 边界）
    筛选，created_at DESC（同刻 id 降序兜底）。"""
    data = audit_query.list_audit_logs(db, filters, page=page, page_size=pageSize)
    return {"code": 0, "message": "ok", "data": data}


@router.get("/export")
def export_audit_logs(user: AdminUser, db: DbSession, filters: _AuditQuery) -> StreamingResponse:
    """审计导出（F-802/ADM-09）：CSV 流式响应（UTF-8 BOM + 中文列头），筛选口径
    与列表一致；行数超 EXPORT_LIMIT 截断并标 X-Truncated=true（防内存放大）。"""
    total = audit_query.count_audit_logs(db, filters)
    headers = {
        "Content-Disposition": f'attachment; filename="audit-{_file_date()}.csv"',
        "X-Truncated": "true" if total > EXPORT_LIMIT else "false",
    }
    return StreamingResponse(
        audit_query.stream_export_lines(db, filters, max_rows=EXPORT_LIMIT),
        media_type="text/csv",
        headers=headers,
    )


def _file_date() -> str:
    """导出文件名日期段（yyyymmdd）：取注入时钟当日，测试可拨动。"""
    return get_clock().now().strftime("%Y%m%d")
