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

    预算取 settings（eval_auto_synth_per_seed / eval_auto_doc_sample_per_dept /
    eval_auto_weekly_max_questions，两通道共享全局总量闸）；审计 eval.autosynth 记
    合并摘要——摘要先于审计可知，审计独立于合成事务二次提交（失败不落审计）。
    """
    settings = get_settings()
    llm = build_llm(settings, 0.0)
    seed_summary = eval_synth.synthesize_from_seeds(
        db,
        get_clock(),
        llm=llm,
        max_per_seed=settings.eval_auto_synth_per_seed,
        max_total=settings.eval_auto_weekly_max_questions,
    )
    chunk_summary = eval_synth.synthesize_from_chunks(
        db,
        get_clock(),
        llm=llm,
        sample_per_dept=settings.eval_auto_doc_sample_per_dept,
        max_total=settings.eval_auto_weekly_max_questions - seed_summary["synthesized"],
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


def record_audit_summary(db: DbSession, actor_id: int, summary: dict) -> None:
    """合成结果审计（独立二次提交：摘要先于审计可知，见模块 docstring）。"""
    from app.services.audit import record_audit

    record_audit(db, actor_id, "eval.autosynth", "eval", None, dict(summary))
