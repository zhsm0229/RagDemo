r"""审计查询服务（F-802，前端 P11 审计日志页后端）：列表与 CSV 导出的只读查询。

audit_logs 只增不改（F-801），本模块只读不写、不留审计。排序恒 created_at DESC +
同刻 id 降序兜底（复合主键 (id, created_at) 下同刻行不重不漏，§22 既有口径）。
日期边界按 UTC 整日解析（与库内 created_at 的 UTC 写入口径一致，同
services/admin_views 口径，不依赖服务器 TimeZone GUC），dateTo 含当日（< 次日零点）。
查询条件全部参数绑定（standards 红线 2）。导出走逐行生成器（StreamingResponse
消费），不整表载内存；行数上限 EXPORT_LIMIT（缺省 10 万）为内存放大保护，超出
截断并以 X-Truncated 响应头标注（截断判定在 api 层：total 同 WHERE 计数）。

CSV 公式注入不变式（T2/W4 评审）：Excel 按单元格**首字符**决定是否解释为公式
（=/+/-/@ 开头即执行）。本服务导出各列首字符恒安全——操作人 username 由注册
白名单约束（中文/字母/数字，§21.5，不出 = + - @）、action/objectType 为服务端
枚举值、objectId 为数字或空、ip 服务端生成、detail 恒为 JSON 对象序列化（首字符
`{`）；其余列（时间 ISO 格式）亦服务端生成。未来若 detail 允许顶层标量（可能以
`-` 等开头），须回头加制表符前缀类防御，不得直接放行。
"""

import csv
import datetime as dt
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, User

# 导出行数上限：防内存放大（验收口径），api 层以 total 同口径判定截断并标响应头
EXPORT_LIMIT: int = 100_000

_CSV_HEADERS = ["时间", "操作人", "动作", "对象类型", "对象ID", "IP", "详情"]


@dataclass(frozen=True)
class AuditFilters:
    """列表与导出共用的筛选入参（两端口径一致，api 层已做边界校验）。"""

    actor_id: int | None = None
    action: str | None = None
    object_type: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None


def list_audit_logs(db: Session, filters: AuditFilters, *, page: int, page_size: int) -> dict:
    """审计列表：过滤 + total + 排序分页一次成型，返回 {items,total,page,pageSize}。"""
    base = _base_stmt(filters)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(_ordered(base).offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "items": [_item_payload(log, username) for log, username in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def count_audit_logs(db: Session, filters: AuditFilters) -> int:
    """导出截断判定计数（与列表 total 同一 WHERE，内存安全）。"""
    return db.scalar(select(func.count()).select_from(_base_stmt(filters).subquery())) or 0


def stream_export_lines(db: Session, filters: AuditFilters, *, max_rows: int) -> Iterator[str]:
    """CSV 文本逐行产出：首行 BOM + 中文表头（UTF-8 编码后即 Excel 可识的 BOM），
    至多 max_rows 行（截断由 api 层先行判定并标响应头）；逐行 yield 供
    StreamingResponse 消费，不整表载内存。"""
    yield "\ufeff" + _csv_line(_CSV_HEADERS)
    stmt = _ordered(_base_stmt(filters)).limit(max_rows)
    for log, username in db.execute(stmt):
        yield _csv_line(_export_cells(_item_payload(log, username)))


def _base_stmt(filters: AuditFilters) -> Select:
    """AuditLog join User（actorUsername 解析，users 软删不物理删恒可内连）+
    WHERE 增量组装（全参数绑定）。"""
    stmt = select(AuditLog, User.username).join(User, User.id == AuditLog.actor_user_id)
    conditions: list[ColumnElement[bool]] = []
    if filters.actor_id is not None:
        conditions.append(AuditLog.actor_user_id == filters.actor_id)
    if filters.action is not None:
        conditions.append(AuditLog.action == filters.action)
    if filters.object_type is not None:
        conditions.append(AuditLog.object_type == filters.object_type)
    if filters.date_from is not None:
        conditions.append(AuditLog.created_at >= _utc_start(filters.date_from))
    if filters.date_to is not None:
        conditions.append(AuditLog.created_at < _utc_start(filters.date_to + dt.timedelta(days=1)))
    return stmt.where(*conditions)


def _ordered(stmt: Select) -> Select:
    """排序：created_at DESC + id DESC 兜底（同刻行次序确定，分页可重复）。"""
    return stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


def _utc_start(day: dt.date) -> dt.datetime:
    """日期 → UTC 当日零点（timestamptz 比较的确定边界，不依赖服务器时区）。"""
    return dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)


def _item_payload(log: AuditLog, actor_username: str) -> dict:
    """DTO（camelCase）：detail JSONB 原样透传，createdAt ISO 格式，行不直接返回。"""
    return {
        "id": log.id,
        "actorUsername": actor_username,
        "action": log.action,
        "objectType": log.object_type,
        "objectId": log.object_id,
        "detail": log.detail,
        "ip": log.ip,
        "createdAt": log.created_at.isoformat(),
    }


def _export_cells(payload: dict) -> list[str]:
    """导出行单元格（列序同表头）：objectId/ip 空值出空单元格（非 "None" 字样），
    detail 序列化为紧凑 JSON 字符串列（无空格、中文不转义）。"""
    return [
        payload["createdAt"],
        payload["actorUsername"],
        payload["action"],
        payload["objectType"],
        "" if payload["objectId"] is None else str(payload["objectId"]),
        payload["ip"] or "",
        json.dumps(payload["detail"], ensure_ascii=False, separators=(",", ":")),
    ]


def _csv_line(cells: list[str]) -> str:
    """单行 CSV 文本：csv.writer 转义（含逗号/引号/换行的单元格安全），行尾 CRLF。"""
    buf = io.StringIO()
    csv.writer(buf).writerow(cells)
    return buf.getvalue()
