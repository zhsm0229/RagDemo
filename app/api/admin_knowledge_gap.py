r"""知识缺口报告路由（F-1010，W5）：周榜查询 / CSV 导出。

admin-only（AdminUser 依赖链同 admin_audit：登录 → 首登改密门 → admin 门）；本报告
为查询视图，两端点均只读、不留审计（同 services.admin_views 口径）。weekOffset 为
0 与负整数（0=本周、-1=上周……UTC 周一为界，注入时钟推算），可回看 12 周含本周
（前端周切换仅 0/-1/-2，余量留运营），越界 422/1003；筛选经共享依赖解析，两端口径
一致且全参数绑定。导出为 StreamingResponse 逐行推送（不整表载内存）：UTF-8 BOM
适配 Excel、中文列头（问题/出现次数），行数超 EXPORT_LIMIT 截断并以 X-Truncated:
true 标注（内存放大保护），文件名 knowledge-gap-{yyyymmdd}.csv（取注入时钟当日）。
响应头须在流开始前确定，故截断判定以同窗口的计数先行完成。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.deps import AdminUser, DbSession
from app.core.clock import get_clock
from app.services import knowledge_gap
from app.services.knowledge_gap import EXPORT_LIMIT

router = APIRouter(prefix="/admin/knowledge-gap", tags=["admin-knowledge-gap"])


def _week_offset(weekOffset: Annotated[int, Query(ge=-11, le=0)] = 0) -> int:
    """列表与导出共用的周偏移入参（brief 钉死两端口径一致），越界 422/1003。"""
    return weekOffset


_WeekOffset = Annotated[int, Depends(_week_offset)]


@router.get("")
def get_knowledge_gap(user: AdminUser, db: DbSession, weekOffset: _WeekOffset) -> dict:
    """知识缺口周榜（F-1010）：窗口内拒答问题按出现次数降序前 50 + 去重总数，
    weekStart/weekEnd 回显聚合窗口（UTC 周一为界）。"""
    window = knowledge_gap.resolve_week_window(weekOffset)
    total, items = knowledge_gap.top_refused_questions(db, window)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [{"question": i.question, "count": i.count} for i in items],
            "total": total,
            "weekStart": window.start.isoformat(),
            "weekEnd": window.end.isoformat(),
        },
    }


@router.get("/export")
def export_knowledge_gap(
    user: AdminUser, db: DbSession, weekOffset: _WeekOffset
) -> StreamingResponse:
    """知识缺口导出（F-1010）：CSV 流式响应（BOM + 中文列头），聚合口径与周榜一致
    但不限前 50；行数超 EXPORT_LIMIT 截断并标 X-Truncated=true（防内存放大）。"""
    window = knowledge_gap.resolve_week_window(weekOffset)
    total = knowledge_gap.count_refused_questions(db, window)
    headers = {
        "Content-Disposition": f'attachment; filename="knowledge-gap-{_file_date()}.csv"',
        "X-Truncated": "true" if total > EXPORT_LIMIT else "false",
    }
    return StreamingResponse(
        knowledge_gap.stream_export_lines(db, window, max_rows=EXPORT_LIMIT),
        media_type="text/csv",
        headers=headers,
    )


def _file_date() -> str:
    """导出文件名日期段（yyyymmdd）：取注入时钟当日，测试可拨动（同 admin_audit）。"""
    return get_clock().now().strftime("%Y%m%d")
