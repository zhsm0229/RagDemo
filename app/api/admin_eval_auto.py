r"""评测自动配置路由（05 票，评测集自动更新）：GET/PUT /admin/eval-auto/config。

配置持久化在 app_settings('eval_auto')（重启不丢），读取回落 Settings 缺省；
PUT 全量覆写（Literal/Field 校验，非法值 → 422/1003），审计 eval.auto_config
（detail {before, after} 生效值口径）。骨架说明：评测自动闭环的开关与预算，
语义见 services/eval_auto_config.py。
"""

from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, DbSession
from app.core.clock import get_clock
from app.services.eval_auto_config import load_eval_auto_config, save_eval_auto_config

router = APIRouter(prefix="/admin/eval-auto", tags=["admin-eval"])


class EvalAutoConfigRequest(BaseModel):
    """全量覆写请求体：数值边界即预算闸合法性（非法 → 422/1003 统一处理）。"""

    enabled: bool
    synthPerSeed: int = Field(ge=1, le=50)
    docSamplePerDept: int = Field(ge=1, le=500)
    weeklyMaxQuestions: int = Field(ge=1, le=5000)
    regressionPp: Annotated[float, Field(gt=0, le=50)]


@router.get("/config")
def get_config(user: AdminUser, db: DbSession) -> dict:
    """读当前生效配置（app_settings 覆盖值 + Settings 缺省合并）。"""
    config = load_eval_auto_config(db)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "enabled": config.enabled,
            "synthPerSeed": config.synth_per_seed,
            "docSamplePerDept": config.doc_sample_per_dept,
            "weeklyMaxQuestions": config.weekly_max_questions,
            "regressionPp": config.regression_pp,
        },
    }


@router.put("/config")
def put_config(user: AdminUser, db: DbSession, payload: EvalAutoConfigRequest) -> dict:
    """全量覆写配置（写审计 eval.auto_config，detail {before, after}）。"""
    save_eval_auto_config(
        db,
        get_clock(),
        actor_id=user.id,
        enabled=payload.enabled,
        synth_per_seed=payload.synthPerSeed,
        doc_sample_per_dept=payload.docSamplePerDept,
        weekly_max_questions=payload.weeklyMaxQuestions,
        regression_pp=payload.regressionPp,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "enabled": payload.enabled,
            "synthPerSeed": payload.synthPerSeed,
            "docSamplePerDept": payload.docSamplePerDept,
            "weeklyMaxQuestions": payload.weeklyMaxQuestions,
            "regressionPp": payload.regressionPp,
        },
    }
