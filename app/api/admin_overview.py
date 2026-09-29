r"""管理后台运营概览/健康度路由（Task 11）：/api/admin/overview、/api/admin/health。

api/admin_views.py 已近 300 行红线，其模块 docstring 约定超出时按域拆子 router——
本文件即 M7 的概览域子 router（挂载见 app/main.py）。两端点均为纯只读聚合：过
AdminUser 门（依赖链：登录 → 首登改密 → 角色，401/2003、403/2004，§5.5），不写库
不留审计。口径与实现见 services.overview_views；本层只做参数接收与 DTO 组装
（§1.2 分层），DTO 键全 camelCase（§3.4），响应体 {code,message,data}。
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, DbSession
from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.services.overview_views import (
    ExpiringDoc,
    HealthReport,
    OverviewReport,
    UsageStats,
    ZeroRecallDoc,
    get_health,
    get_overview,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_ALLOWED_DAYS = (1, 7)  # brief 钉死：概览窗口仅"近 1 天/近 7 天"两档（不用 Literal——
# FastAPI 0.141 对 int 值 Literal 查询参数不做 str→int 松弛校验，"7" 会被 422 拒掉）


def _usage_payload(stats: UsageStats) -> dict:
    """聚合指标 DTO：比率为 0-1 浮点（4 位小数），tokens 为 prompt+completion 之和。"""
    return {
        "questions": stats.questions,
        "hitRate": stats.hit_rate,
        "refuseRate": stats.refuse_rate,
        "downRate": stats.down_rate,
        "tokens": stats.tokens,
    }


@router.get("/overview")
def admin_overview(
    user: AdminUser,
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=7)] = 7,
) -> dict:
    """运营概览（F-704）：days=1|7 天窗口（缺省 7）内 assistant 非评测回答的
    totals 与 byDept 聚合——命中率（qa+rag 占比）/拒答率/点踩率/token 消耗；
    byDept 按 deptCode 升序且只含有数据的部门。窗口外取值 422/1003。"""
    if days not in _ALLOWED_DAYS:
        raise ApiError(ERR_PARAM_INVALID, "days 仅支持 1 或 7", 422)
    report: OverviewReport = get_overview(db, days)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "totals": _usage_payload(report.totals),
            "byDept": [
                {"deptCode": dept.dept_code, **_usage_payload(dept.stats)}
                for dept in report.by_dept
            ],
        },
    }


def _zero_recall_payload(doc: ZeroRecallDoc) -> dict:
    """零召回榜单项 DTO（createdAt ISO8601 带 UTC 偏移，榜单已按建档升序）。"""
    return {
        "id": doc.id,
        "title": doc.title,
        "deptCode": doc.dept_code,
        "createdAt": doc.created_at.isoformat(),
    }


def _expiring_payload(doc: ExpiringDoc) -> dict:
    """待过期清单项 DTO（expiryDate 为 date 的 ISO 文本 YYYY-MM-DD）。"""
    return {"id": doc.id, "title": doc.title, "expiryDate": doc.expiry_date.isoformat()}


@router.get("/health")
def admin_health(user: AdminUser, db: DbSession) -> dict:
    """语料健康度（F-1009）：文档总数/就绪数/就绪率/待确认计数 + 零召回文档榜
    （30 天 0 命中）+ 待过期清单（7 天内含已到期未下线）；时间基准取注入时钟。"""
    report: HealthReport = get_health(db)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "totalDocs": report.total_docs,
            "readyCount": report.ready_count,
            "readyRate": report.ready_rate,
            "pendingConfirm": report.pending_confirm,
            "zeroRecall": [_zero_recall_payload(doc) for doc in report.zero_recall],
            "expiringSoon": [_expiring_payload(doc) for doc in report.expiring_soon],
        },
    }
