r"""难例池管理路由（03 票，评测集自动更新）：列表 / 置忽略 / 手动合成。

独立路由文件（与 admin_eval_questions.py 同理：同域不同职责，避免单文件超 300 行）：
- GET  /admin/hardcase-seeds            分页列表（status/dept 筛选）
- PATCH/admin/hardcase-seeds/{id}       置 ignored 终态（审计 hardcase.ignore）
- POST /admin/hardcase-seeds/synthesize 手动触发合成（点踩通道；预算取 settings）
合成走 eval_synth.synthesize_from_seeds，LLM 经 build_llm(settings, 0.0) 第二实例
（与人工评测判分同款口径；模块级导入便于测试 monkeypatch 替身）。审计 eval.autosynth
记合成结果摘要——摘要先于审计可知，故审计独立于合成事务二次提交（失败不落审计）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import AdminUser, DbSession
from app.core.assembly import build_llm
from app.core.clock import get_clock
from app.core.config import get_settings
from app.services import eval_synth, hardcase_admin

router = APIRouter(prefix="/admin/hardcase-seeds", tags=["admin-eval"])

_SeedStatus = Literal["pending", "synthesized", "no_ground", "ignored"]


class SeedStatusPatchRequest(BaseModel):
    """置状态请求体：人工 API 仅开放 ignored 终态（其余流转由合成任务体驱动）。"""

    status: Literal["ignored"]


def _page(page: Annotated[int, Query(ge=1)] = 1) -> int:
    return page


def _page_size(pageSize: Annotated[int, Query(ge=1, le=100)] = 20) -> int:
    return pageSize


@router.get("")
def list_seeds(
    user: AdminUser,
    db: DbSession,
    page: Annotated[int, Depends(_page)],
    pageSize: Annotated[int, Depends(_page_size)],
    status: Annotated[_SeedStatus | None, Query()] = None,
    dept: Annotated[str | None, Query()] = None,
) -> dict:
    """难例池分页列表（admin）：status/dept 筛选，时间倒序稳定排序。"""
    total, rows = hardcase_admin.list_seeds(
        db,
        page=page,
        page_size=pageSize,
        status=status,
        dept_code=dept,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [
                {
                    "id": seed.id,
                    "sourceType": seed.source_type,
                    "status": seed.status,
                    "deptCode": dept_code,
                    "questionText": seed.question_text,
                    "answerSnapshot": seed.answer_snapshot,
                    "synthCount": seed.synth_count,
                    "createdAt": seed.created_at.isoformat(),
                }
                for seed, dept_code in rows
            ],
            "total": total,
            "page": page,
            "pageSize": pageSize,
        },
    }


@router.patch("/{seed_id}")
def ignore_seed(
    user: AdminUser,
    db: DbSession,
    seed_id: int,
    payload: SeedStatusPatchRequest,
) -> dict:
    """置 ignored 终态（人工否决该种子），审计 hardcase.ignore。"""
    seed = hardcase_admin.ignore_seed(
        db,
        get_clock(),
        seed_id=seed_id,
        actor_id=user.id,
    )
    return {"code": 0, "message": "ok", "data": {"id": seed.id, "status": seed.status}}


@router.post("/synthesize")
def synthesize(user: AdminUser, db: DbSession) -> dict:
    """手动触发合成（单次双通道）：点踩种子变体 + 文档抽样造题，产物均入扩充集。

    预算走 load_eval_auto_config（app_settings 在线配置，与周任务同一真相源——
    评审 P2：设置页预算对手动按钮生效，不读 env 缺省）；审计 eval.autosynth 记
    合并摘要 + trigger="manual"（与周任务 "weekly" 对齐）——审计独立于合成事务
    二次提交（失败不落审计）。
    """
    from app.services.eval_auto_config import load_eval_auto_config

    settings = get_settings()
    config = load_eval_auto_config(db, settings)
    llm = build_llm(settings, 0.0)
    seed_summary = eval_synth.synthesize_from_seeds(
        db,
        get_clock(),
        llm=llm,
        max_per_seed=config.synth_per_seed,
        max_total=config.weekly_max_questions,
    )
    chunk_summary = eval_synth.synthesize_from_chunks(
        db,
        get_clock(),
        llm=llm,
        sample_per_dept=config.doc_sample_per_dept,
        max_total=config.weekly_max_questions - seed_summary["synthesized"],
    )
    summary: dict = {
        "synthesized": seed_summary["synthesized"] + chunk_summary["synthesized"],
        "skipped": seed_summary["skipped"] + chunk_summary["skipped"],
        "seedsProcessed": seed_summary["seedsProcessed"],
        "noGround": seed_summary["noGround"],
        "docChunksSampled": chunk_summary["chunksSampled"],
    }
    record_audit_summary(db, user.id, summary)
    db.commit()
    return {"code": 0, "message": "ok", "data": summary}


@router.post("/rescan")
def rescan_seeds(user: AdminUser, db: DbSession) -> dict:
    """重扫待补语料种子（工单 16）：有块引用且 skip 累计型的 no_ground 重置计数转回
    pending（合成通道重新走 grounding 判定）；无引用种子保持不动（重扫无判据）。
    审计 hardcase.rescan 记转回条数——运营在难例池对"待补语料"列表一键重试。"""
    from app.services.audit import record_audit
    from app.services.hardcase_mine import rescan_no_ground

    rescanned = rescan_no_ground(db)
    record_audit(db, user.id, "hardcase.rescan", "hardcase_seed", None, {"rescanned": rescanned})
    db.commit()
    return {"code": 0, "message": "ok", "data": {"rescanned": rescanned}}


def record_audit_summary(db: DbSession, actor_id: int, summary: dict) -> None:
    """合成结果审计（独立二次提交：摘要先于审计可知，见模块 docstring）。"""
    from app.services.audit import record_audit

    record_audit(db, actor_id, "eval.autosynth", "eval", None, {"trigger": "manual", **summary})
