r"""周评测自动跑分任务体（06 票，评测集自动更新）：三视角各建一个 `[自动]` run 并
逐一执行评测引擎。编排口径见 run_weekly_eval docstring；命名/幂等/部门解析的
辅助函数集中本模块，查询全部走 SQLAlchemy 表达式语言绑定参数。

2026-10-31 商用就绪工单 05：`[自动]` 前缀 name 部分唯一索引（迁移 0017）为并发
双跑兜底——`_run_exists` 查-插间隙被并发穿透时由数据库约束拒绝，插入处捕获
IntegrityError 幂等跳过并记日志；executor 异常分支补 logger.exception（评审 P1-13/14，
此前静默只留 run 行脱敏通用语）。
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import Department, EvalRun
from app.services.eval_auto_config import EvalAutoConfig, load_eval_auto_config

logger = logging.getLogger(__name__)

# 自动跑分视角集：检索范围恒为 [视角部门, 全员]，三视角并跑才覆盖全部语料视角
_PERSPECTIVE_DEPTS: tuple[str, ...] = ("service", "hr", "all")


def _weekly_run_name(iso_year: int, iso_week: int, code: str) -> str:
    """自动 run 命名：`[自动]ISO年-W周序-视角`（同周同视角唯一，幂等键）。"""
    return "[自动]" + str(iso_year) + "-W" + str(iso_week).zfill(2) + "-" + code


def _run_exists(session: Session, name: str) -> bool:
    """同周同视角 run 是否已在册（幂等判定）。"""
    stmt = select(EvalRun.id).where(EvalRun.name.in_([name]))
    return session.scalar(stmt) is not None


def _dept_id_by_code(session: Session, code: str) -> int | None:
    """部门 code → id（未播种返回 None，调用侧防御性跳过）。"""
    stmt = select(Department.id).where(Department.code.in_([code]))
    return session.scalar(stmt)


def _default_executor(session: Session, run_id: int, deps: Any, dept_id: int) -> None:
    """缺省执行器：真评测引擎（幂等闸在引擎侧——run 不存在或已收口不重跑）。

    引擎自管短会话（session_factory 指向应用 SessionLocal 同源连接池），与本任务体
    的会话分离——逐题 commit 的进度对本任务体不可见属预期（进度查询走各 run 行）。
    """
    from sqlalchemy.orm import sessionmaker

    from app.db.base import SessionLocal
    from app.services.eval_engine import run_eval_run

    run_eval_run(run_id, deps, dept_id, session_factory=sessionmaker(bind=SessionLocal.kw["bind"]))


def run_weekly_eval(
    session: Session,
    clock: Clock,
    settings: Settings,
    *,
    deps: Any,
    executor: Any = None,
) -> list[int]:
    """周评测自动跑分任务体（cron/runbook/测试共用入口），返回本次新建 run id 列表。"""
    config: EvalAutoConfig = load_eval_auto_config(session, settings)
    if config.enabled is False:  # 总闸关：零副作用（不建 run 不执行引擎）
        return []
    exec_run = executor if executor is not None else _default_executor
    iso = clock.now().isocalendar()
    created: list[int] = []
    for code in _PERSPECTIVE_DEPTS:
        name = _weekly_run_name(iso.year, iso.week, code)
        if _run_exists(session, name):
            continue  # 同周同视角已建：不重建不重跑（幂等）
        dept_id = _dept_id_by_code(session, code)
        if dept_id is None:
            continue  # 部门未播种（种子迁移保证三部门在册，防御性跳过）
        run = EvalRun(
            name=name,
            status="running",
            total=0,
            correct=0,
            accuracy=0,
            triggered_by=None,
            config={"perspective": code},
            created_at=clock.now(),
        )
        session.add(run)
        try:
            # 引擎自管短会话只看已提交行：先落行再执行（引擎幂等闸前提）。
            # 并发双跑兜底（0017 部分唯一索引）：查-插间隙被并发穿透时约束拒绝，
            # 视为对手进程已建同周 run——幂等跳过并记日志（非报错）
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.warning("同周自动 run 已由并发进程创建，幂等跳过：%s", name)
            continue
        created.append(run.id)
        try:
            exec_run(session, run.id, deps, dept_id)
        except Exception:  # 单视角故障置 failed，不拖垮其余视角（BLE001 全局豁免在配置）
            logger.exception("自动跑分单视角失败 run_id=%s perspective=%s", run.id, code)
            session.rollback()
            failed = session.get(EvalRun, run.id)
            if failed is not None and failed.status == "running":
                failed.status = "failed"
                failed.detail = {"error": "自动跑分执行失败"}
                session.commit()
    return created
