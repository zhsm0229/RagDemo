r"""管理端只读聚合服务（Task 11）：运营概览（F-704）与健康度看板（F-1009）。

口径（brief 钉死）：overview 聚合 messages role='assistant' AND is_eval=false AND
created_at >= now-days——不筛 is_active（中断占位行计入 questions，其 answer_type
NULL 不进命中/拒答分子），is_eval 恒排除（W3 约束：评测数据不污染运营查询）；命中率
=answer_type IN ('qa','rag') 占比、refuseRate=refuse 占比、downRate=feedback='down'
占比、tokens=sum(prompt+completion)；比率统一 4 位小数（0-1 浮点），byDept 只含有
数据的部门（分组天然如此）。health：totalDocs=deleted_at IS NULL（软删不计，含
inactive）；readyRate=ready/totalDocs（口径只看 status+deleted_at，待确认的 ready
也计入就绪）；pendingConfirm=is_active=false AND deleted_at IS NULL（近似重复/脱敏
待确认）；零召回=is_active AND status='ready' AND created_at < now-30d 且 NOT
EXISTS(该文档有 chunk 在近 30 天被命中)——文档级反连接，任一块近期命中即整篇不算，
恰在 30 天线的 last_hit_at 即算近期、恰 30 天前建档不算（created_at 严格小于）；
expiringSoon=expiry_date <= today+7 AND is_active AND deleted_at IS NULL（含已到期
未下线——到期下线未跑完的窗口期仍要露出）；零召回块清单（F-1011，W5 追加）与零召回
文档榜为同一口径的文档级/块级两面，口径见 list_zero_recall_chunks docstring 互指。

时间基准一律取注入时钟 get_clock().now()（W1 Clock 先例，测试 FakeClock 前拨构造
边界）；窗口在 Python 侧折算成绝对时刻再参数绑定（SQL 不做 now()-interval 运算）；
零召回观察窗口 30 天为固定常量不入 config（F-1009 口径钉死）。查询一律参数绑定、
只读不写库不留审计（同 services.admin_views 口径）。
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import get_clock
from app.db.models import Chunk, Department, Document, Message

_RATE_DIGITS = 4  # 比率统一 4 位小数（0-1 浮点，前端按百分比展示时自行换算）
_ZERO_RECALL_DAYS = 30  # 零召回观察窗口 30 天，F-1009 口径（固定常量不入 config）
_EXPIRING_DAYS = 7  # 待过期提前量 7 天，F-1009 口径（"待过期 ≤7 天即黄"）
_PREVIEW_CHARS = 50  # 零召回块内容预览截断长度（F-1011，api.md §4.5.3）


@dataclass(frozen=True)
class UsageStats:
    """一组聚合指标：questions 为窗口内 assistant 非评测回答行数（含中断行）。"""

    questions: int
    hit_rate: float
    refuse_rate: float
    down_rate: float
    tokens: int


@dataclass(frozen=True)
class DeptUsage:
    """分部门聚合项（deptCode 取 departments.code，无数据的部门不出现）。"""

    dept_code: str
    stats: UsageStats


@dataclass(frozen=True)
class OverviewReport:
    """运营概览：totals 为全量（分部门求和后再算比率），byDept 按 deptCode 升序。"""

    totals: UsageStats
    by_dept: list[DeptUsage]


@dataclass(frozen=True)
class ZeroRecallDoc:
    """零召回榜单项：created_at 为建档时刻（榜单按其升序=最久未召回在前）。"""

    id: int
    title: str
    dept_code: str
    created_at: dt.datetime


@dataclass(frozen=True)
class ZeroRecallChunk:
    """零召回块清单项（F-1011）：文档零召回榜的块级下钻，content_preview 截
    _PREVIEW_CHARS 字（预览列不整段拖表），created_at 为块建档时刻。"""

    chunk_id: int
    document_title: str
    dept_code: str
    title_path: str
    content_preview: str
    created_at: dt.datetime


@dataclass(frozen=True)
class ZeroRecallChunkPage:
    """零召回块分页结果：items 按 created_at 降序（同刻 id 降序兜底），total 同
    WHERE 全量计数（§3.5 列表口径，page/pageSize 由 api 层回显）。"""

    items: list[ZeroRecallChunk]
    total: int


@dataclass(frozen=True)
class ExpiringDoc:
    """待过期清单项（按到期日升序；已到期未下线的同样在列）。"""

    id: int
    title: str
    expiry_date: dt.date


@dataclass(frozen=True)
class HealthReport:
    """健康度看板：三计数 + 两榜单（F-1009 顶部概览条数据源）。"""

    total_docs: int
    ready_count: int
    ready_rate: float
    pending_confirm: int
    zero_recall: list[ZeroRecallDoc]
    expiring_soon: list[ExpiringDoc]


def _rate(numerator: int, denominator: int) -> float:
    """占比 = 分子/分母保留 4 位小数；分母 0（窗口内无数据）记 0.0 不抛零除。"""
    return round(numerator / denominator, _RATE_DIGITS) if denominator else 0.0


def _to_stats(row: Any) -> UsageStats:
    """聚合行 → 指标：分母恒为 questions（行数），各比率同口径可横向对比。"""
    return UsageStats(
        questions=row.questions,
        hit_rate=_rate(row.hits, row.questions),
        refuse_rate=_rate(row.refusals, row.questions),
        down_rate=_rate(row.downs, row.questions),
        tokens=row.tokens,
    )


def get_overview(db: Session, days: int) -> OverviewReport:
    """运营概览（F-704）：days 天窗口内 assistant 非评测回答的总量与分部门聚合。

    窗口起点 = 注入时钟 now - days（Python 侧折算绝对时刻后参数绑定）；分组天然
    只含有数据的部门，按 deptCode 升序保证响应顺序确定（前端图表稳定）。"""
    since = get_clock().now() - dt.timedelta(days=days)
    rows = db.execute(
        select(
            Department.code.label("dept_code"),
            func.count().label("questions"),
            func.count().filter(Message.answer_type.in_(("qa", "rag"))).label("hits"),
            func.count().filter(Message.answer_type == "refuse").label("refusals"),
            func.count().filter(Message.feedback == "down").label("downs"),
            func.coalesce(func.sum(Message.prompt_tokens + Message.completion_tokens), 0).label(
                "tokens"
            ),
        )
        .join(Department, Department.id == Message.dept_id)
        .where(
            Message.role == "assistant",
            Message.is_eval.is_(False),  # W3：评测数据不进运营聚合
            Message.created_at >= since,  # 参数绑定的绝对时刻
        )
        .group_by(Department.code)
        .order_by(Department.code.asc())
    ).all()
    by_dept = [DeptUsage(row.dept_code, _to_stats(row)) for row in rows]
    total = sum(u.stats.questions for u in by_dept)
    totals = UsageStats(
        questions=total,
        hit_rate=_rate(sum(r.hits for r in rows), total),
        refuse_rate=_rate(sum(r.refusals for r in rows), total),
        down_rate=_rate(sum(r.downs for r in rows), total),
        tokens=sum(u.stats.tokens for u in by_dept),
    )
    return OverviewReport(totals=totals, by_dept=by_dept)


def _zero_recall_docs(db: Session, now: dt.datetime) -> list[ZeroRecallDoc]:
    """零召回文档榜：active+ready 且建档严格早于 30 天前，且没有任何 chunk 在近
    30 天被命中（NOT EXISTS 文档级反连接——恰在 30 天线的 last_hit_at 即算近期）。
    created_at 升序 = 最久未召回在前，供运营优先处理。"""
    since = now - dt.timedelta(days=_ZERO_RECALL_DAYS)
    recent_hit = (
        select(Chunk.document_id)
        .where(Chunk.document_id == Document.id, Chunk.last_hit_at >= since)
        .exists()
    )
    rows = db.execute(
        select(Document.id, Document.title, Department.code, Document.created_at)
        .join(Department, Department.id == Document.dept_id)
        .where(
            Document.is_active.is_(True),
            Document.status == "ready",
            Document.created_at < since,  # 恰 30 天前建档不算（严格小于）
            ~recent_hit,
        )
        .order_by(Document.created_at.asc(), Document.id.asc())
    ).all()
    return [ZeroRecallDoc(row.id, row.title, row.code, row.created_at) for row in rows]


def list_zero_recall_chunks(
    db: Session, days: int, *, page: int, page_size: int
) -> ZeroRecallChunkPage:
    """零召回块清单（F-1011）：与 F-1009 文档级零召回榜（_zero_recall_docs）为同一
    口径的文档级/块级两面——文档级是"任一块近窗口命中即整篇不算"的 NOT EXISTS 反
    连接，块级（本函数）逐块判 last_hit_at（从未召回或最近一次召回早于窗口起点即
    入列）。文档闸与文档级逐字一致（is_active+ready，不看 deleted_at，差异依据见
    _zero_recall_docs）；块侧另加 is_active（停用块同样不可被召回）。days 缺省 30
    与文档级 _ZERO_RECALL_DAYS 同值，参数可调宽供运营排查（文档级固定 30 天不入
    config）；窗口起点 = 注入时钟 now-days，Python 侧折算后参数绑定。"""
    since = get_clock().now() - dt.timedelta(days=days)
    base = (
        select(
            Chunk.id,
            Document.title,
            Department.code,
            Chunk.title_path,
            Chunk.content,
            Chunk.created_at,
        )
        .join(Document, Document.id == Chunk.document_id)
        .join(Department, Department.id == Document.dept_id)
        .where(
            Chunk.is_active.is_(True),
            Chunk.last_hit_at.is_(None) | (Chunk.last_hit_at < since),  # 从未召回或超窗口
            Document.is_active.is_(True),
            Document.status == "ready",
        )
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        base.order_by(Chunk.created_at.desc(), Chunk.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        ZeroRecallChunk(
            chunk_id=row.id,
            document_title=row.title,
            dept_code=row.code,
            title_path=row.title_path,
            content_preview=row.content[:_PREVIEW_CHARS],
            created_at=row.created_at,
        )
        for row in rows
    ]
    return ZeroRecallChunkPage(items=items, total=total)


def _expiring_docs(db: Session, today: dt.date) -> list[ExpiringDoc]:
    """待过期清单：expiry_date <= today+7 且 active 未软删（含已到期未下线）。
    按到期日升序 = 最紧急在前。"""
    deadline = today + dt.timedelta(days=_EXPIRING_DAYS)
    rows = db.execute(
        select(Document.id, Document.title, Document.expiry_date)
        .where(
            Document.expiry_date <= deadline,  # date 参数绑定
            Document.is_active.is_(True),
            Document.deleted_at.is_(None),
        )
        .order_by(Document.expiry_date.asc(), Document.id.asc())
    ).all()
    return [ExpiringDoc(row.id, row.title, row.expiry_date) for row in rows]


def get_health(db: Session) -> HealthReport:
    """语料健康度（F-1009）：总量/就绪/待确认三计数 + 零召回榜 + 待过期清单。

    三个计数同用 deleted_at IS NULL 作软删排除（totalDocs 口径），分别叠加各自
    条件；比率经 _rate 统一 4 位小数。"""
    now = get_clock().now()
    not_deleted = Document.deleted_at.is_(None)
    total_docs = db.scalar(select(func.count()).select_from(Document).where(not_deleted)) or 0
    ready_count = (
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(not_deleted, Document.status == "ready")
        )
        or 0
    )
    pending_confirm = (
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(not_deleted, Document.is_active.is_(False))
        )
        or 0
    )
    return HealthReport(
        total_docs=total_docs,
        ready_count=ready_count,
        ready_rate=_rate(ready_count, total_docs),
        pending_confirm=pending_confirm,
        zero_recall=_zero_recall_docs(db, now),
        expiring_soon=_expiring_docs(db, now.date()),
    )
