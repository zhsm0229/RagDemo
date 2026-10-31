r"""评测自动运行配置（05 票）：app_settings 单行（key='eval_auto'）读写。

行缺失 = 全部回落 Settings 缺省（enabled=False —— 部署缺省关闭，行为与现状一致）；
save 全量覆写五参数并留审计 eval.auto_config（detail {before, after}，before 为解析
后的当前生效值而非原始 JSON 行——管理端与审计要看的是生效口径）。jsonb 整行覆写
（row.value 重新赋值触发变更检测，勿原地 mutate）。
"""

from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.db.models import AppSetting
from app.services.audit import record_audit

_EVAL_AUTO_KEY: str = "eval_auto"  # 评测自动域在 app_settings 的恒等键


@dataclass(frozen=True)
class EvalAutoConfig:
    """评测自动闭环运行参数（存储键为 camelCase JSON，见 save 的 value 组装）。"""

    enabled: bool
    synth_per_seed: int
    doc_sample_per_dept: int
    weekly_max_questions: int
    regression_pp: float


def load_eval_auto_config(session: Session, settings: Settings | None = None) -> EvalAutoConfig:
    """读配置：app_settings 行存在则覆盖 Settings 缺省，行缺失全缺省。"""
    settings = settings or get_settings()
    row = session.get(AppSetting, _EVAL_AUTO_KEY)
    value: dict = row.value if row is not None else {}
    return EvalAutoConfig(
        enabled=bool(value.get("enabled", settings.eval_auto_enabled)),
        synth_per_seed=int(value.get("synthPerSeed", settings.eval_auto_synth_per_seed)),
        doc_sample_per_dept=int(
            value.get("docSamplePerDept", settings.eval_auto_doc_sample_per_dept)
        ),
        weekly_max_questions=int(
            value.get("weeklyMaxQuestions", settings.eval_auto_weekly_max_questions)
        ),
        regression_pp=float(value.get("regressionPp", settings.eval_auto_regression_pp)),
    )


def save_eval_auto_config(
    session: Session,
    clock: Clock,
    *,
    actor_id: int,
    enabled: bool,
    synth_per_seed: int,
    doc_sample_per_dept: int,
    weekly_max_questions: int,
    regression_pp: float,
    settings: Settings | None = None,
) -> None:
    """全量覆写配置行 + 审计 eval.auto_config（detail {before, after}，生效值口径）。"""
    settings = settings or get_settings()
    before = load_eval_auto_config(session, settings)
    value = {
        "enabled": enabled,
        "synthPerSeed": synth_per_seed,
        "docSamplePerDept": doc_sample_per_dept,
        "weeklyMaxQuestions": weekly_max_questions,
        "regressionPp": regression_pp,
    }
    row = session.get(AppSetting, _EVAL_AUTO_KEY)
    if row is None:
        row = AppSetting(key=_EVAL_AUTO_KEY, value=value, updated_by=actor_id)
    else:
        row.value = value  # 重新赋值（非原地 mutate）：JSONB 变更检测依赖属性赋值
        row.updated_by = actor_id
    row.updated_at = clock.now()
    session.add(row)
    record_audit(
        session,
        actor_id,
        "eval.auto_config",
        "app_setting",
        None,
        {"before": asdict(before), "after": value},
    )
    session.commit()
