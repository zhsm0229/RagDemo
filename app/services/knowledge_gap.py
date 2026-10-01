r"""知识缺口报告服务（F-1010，W5）：拒答问题按周聚合榜 + CSV 导出（只读）。

口径（brief 钉死）：messages role='user' AND is_eval=false，LATERAL 配对其同会话中
id 大于问题行且最小的 assistant 行（最小 id 配对同 services.admin_views._answer_pair
——占位行与回答在 api/chat._persist_placeholder 同事务先后发号，该序即问答次序），
配对回答活跃且 answer_type='refuse' 即记一次"知识缺口"；按 user.content 分组
count DESC 取前 TOP_LIMIT（榜单截断，total 为窗口内去重问题数，供"还有更多"与导出
截断判定）。周界按 UTC 周一 00:00（与库内 created_at 的 UTC 写入口径一致，不依赖
服务器 TimeZone GUC），weekOffset=0 为本周（注入时钟推算），过滤 [weekStart,
weekEnd) 左闭右开；同数问题以问题文本升序兜底（行次序确定，榜单与导出可复现）。
查询一律参数绑定、只读不写库不留审计（同 services.admin_views 口径）。

CSV 公式注入不变式（services.audit_query 在本模块的落地）：问题列是用户自由文本，
首字符可能为 = + - @（Excel 按单元格首字符解释公式），导出时前置制表符强制按文本
解释；出现次数为服务端生成整数恒安全。列表接口仍返回问题原文，不改动用户输入。
"""

import csv
import datetime as dt
import io
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select, true
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.selectable import Lateral

from app.core.clock import get_clock
from app.db.models import Message

# 榜单长度（brief 钉死 limit 50）：列表只出前 50，total 仍为窗口内去重问题总数
TOP_LIMIT: int = 50
# 导出行数上限：防内存放大（同 services.audit_query 先例），api 层以同窗口计数判定截断
EXPORT_LIMIT: int = 100_000

_CSV_HEADERS = ["问题", "出现次数"]
# Excel 公式触发首字符（OWASP CSV 注入口径：= + - @，另含制表符/回车防裁剪绕过）
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class WeekWindow:
    """一周窗口：[start, end) 左闭右开（UTC 周一 00:00 为界，恒含 7 整日）。"""

    start: dt.datetime
    end: dt.datetime


@dataclass(frozen=True)
class GapItem:
    """榜单项：question 为用户问题原文，count 为窗口内该问题的拒答次数。"""

    question: str
    count: int


def resolve_week_window(week_offset: int) -> WeekWindow:
    """weekOffset（0=本周，负数回看）→ UTC 周一为界的 [start, end)。

    注入时钟推算（W1 Clock 先例），Python 侧折算成绝对时刻由调用方参数绑定
    （SQL 不做 now()-interval 运算，同 services.overview_views 口径）。"""
    now = get_clock().now()
    monday = now.date() - dt.timedelta(days=now.weekday())
    start = dt.datetime.combine(monday, dt.time.min, tzinfo=dt.UTC) + dt.timedelta(
        weeks=week_offset
    )
    return WeekWindow(start, start + dt.timedelta(days=7))


def _refuse_pair(question: Any) -> Lateral:
    """LATERAL 子查询：同会话中 id 大于问题行且最小的 assistant 行的活跃位与类型。

    配对过滤照抄 services.admin_views._answer_pair（最小 id 配对 + assistant 侧
    is_eval 防御）；不筛 is_active——中断占位行占住次序，其活跃位在主查询判定
    （answer_active 非 true 即无有效回答，不算拒答），而非错误地滑向下一条回答。
    """
    answer = aliased(Message)
    return (
        select(
            answer.is_active.label("answer_active"),
            answer.answer_type.label("answer_type"),
        )
        .where(
            answer.conversation_id == question.conversation_id,
            answer.role == "assistant",
            # 防御性：评测现走独立会话，防未来评测复用真实会话时评测回答错配进运营配对
            answer.is_eval.is_(False),
            answer.id > question.id,
        )
        .order_by(answer.id.asc())
        .limit(1)
        .lateral(name="refuse_pair")
    )


def _gap_stmt(window: WeekWindow) -> Select:
    """聚合基础语句：窗口内配对回答为拒答的用户问题按内容分组计数（全参数绑定）。

    拒答本身就要求配对存在，内连接 LATERAL 即可（无回答的问题被配对条件天然排除）。"""
    question = aliased(Message, name="question_msg")
    pair = _refuse_pair(question)
    return (
        select(question.content.label("question"), func.count().label("count"))
        .select_from(question)  # LATERAL 引用问题列：显式锚定左侧 FROM 消除多 FROM 歧义
        .join(pair, true())
        .where(
            question.role == "user",
            question.is_eval.is_(False),  # W3：评测数据不进运营聚合
            question.created_at >= window.start,  # [weekStart, weekEnd) 左闭右开
            question.created_at < window.end,
            pair.c.answer_active.is_(True),  # 中断/缺失回答不算拒答
            pair.c.answer_type == "refuse",
        )
        .group_by(question.content)
    )


def _ranked(db: Session, window: WeekWindow, *, limit: int) -> Any:
    """榜单行迭代器：count DESC + 问题文本升序兜底，至多 limit 行。"""
    grouped = _gap_stmt(window).subquery()
    return db.execute(
        select(grouped.c.question, grouped.c.count)
        .order_by(grouped.c.count.desc(), grouped.c.question.asc())
        .limit(limit)
    ).all()


def count_refused_questions(db: Session, window: WeekWindow) -> int:
    """窗口内去重拒答问题总数（榜单 total 与导出截断判定同一 WHERE，内存安全）。"""
    grouped = _gap_stmt(window).subquery()
    return db.scalar(select(func.count()).select_from(grouped)) or 0


def top_refused_questions(db: Session, window: WeekWindow) -> tuple[int, list[GapItem]]:
    """拒答问题周榜：total（去重问题数）+ count DESC 前 TOP_LIMIT 项。"""
    rows = _ranked(db, window, limit=TOP_LIMIT)
    return count_refused_questions(db, window), [GapItem(row.question, row.count) for row in rows]


def stream_export_lines(db: Session, window: WeekWindow, *, max_rows: int) -> Iterator[str]:
    """CSV 文本逐行产出：首行 BOM + 中文表头（UTF-8 编码后即 Excel 可识的 BOM），
    至多 max_rows 行（截断由 api 层先行判定并标响应头）；逐行 yield 供
    StreamingResponse 消费，不整表载内存。"""
    yield "\ufeff" + _csv_line(_CSV_HEADERS)
    for row in _ranked(db, window, limit=max_rows):
        yield _csv_line([_formula_safe(row.question), str(row.count)])


def _formula_safe(text: str) -> str:
    """首字符为公式触发符时前置制表符：Excel 按单元格首字符决定是否解释为公式
    （audit_query 不变式），前缀强制按文本处理；仅作用于导出，不改问题原文。"""
    return f"\t{text}" if text.startswith(_FORMULA_PREFIXES) else text


def _csv_line(cells: list[str]) -> str:
    """单行 CSV 文本：csv.writer 转义（含逗号/引号/换行的单元格安全），行尾 CRLF。"""
    buf = io.StringIO()
    csv.writer(buf).writerow(cells)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> None:
    """周报导出 CLI（F-1010"每周自动生成"的调度载体，规范/规格审查补齐）。

    用法：``python -m app.services.knowledge_gap [--week-offset 1] [--out 目录]``
    供 OS 调度器每周一拉取上周榜单落盘（同 deploy/backup.sh 的 schtasks/cron 口径，
    应用内不引入调度依赖——AGENTS"能不加就不加"）；--week-offset 缺省 1（上周）。
    """
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="知识缺口周报导出（F-1010）")
    parser.add_argument("--week-offset", type=int, default=1, help="0=本周，1=上周（缺省）")
    parser.add_argument("--out", default=".", help="CSV 输出目录（缺省当前目录）")
    args = parser.parse_args(argv)

    window = resolve_week_window(args.week_offset)
    from app.db.base import SessionLocal  # 延迟导入：与 app.db.seed.main 同口径

    with SessionLocal() as db:
        lines = list(stream_export_lines(db, window, max_rows=EXPORT_LIMIT))
        total = count_refused_questions(db, window)
    path = Path(args.out) / f"knowledge-gap-{window.start:%Y%m%d}.csv"
    path.write_text("".join(lines), encoding="utf-8-sig")  # BOM 内嵌，Excel 直接可读
    print(
        f"[knowledge-gap] 窗口 {window.start:%Y-%m-%d}~{window.end:%Y-%m-%d} "
        f"去重拒答问题 {total} 条 → {path}"
    )


if __name__ == "__main__":
    main()
