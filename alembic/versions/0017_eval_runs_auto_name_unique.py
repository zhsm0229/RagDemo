"""0017: eval_runs 建 [自动] 前缀部分唯一索引（商用就绪工单 05，2026-10-31 评审 P1-13）。

动机：周评测自动跑分（F-1019 06 票）的幂等判定 `_run_exists` 是查-插两段式，多
worker/双容器部署下两进程同时通过检查会双建三视角 run——双份全量跑分 LLM 成本 +
同名 run 污染环比取数。单进程部署未触发，私有化扩容即触雷。

口径：部分唯一索引只约束 `name LIKE '[自动]%'` 的行——人工 run 同名不受限（运营
自查范畴，历史上存在同名人工 run）。并发兜底由数据库约束完成，`run_weekly_eval`
插入处捕获 IntegrityError 幂等跳过（记日志非报错）。

回滚：drop 该索引即可（downgrade），不触碰数据。
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_eval_runs_auto_name",
        "eval_runs",
        ["name"],
        unique=True,
        postgresql_where=text("name LIKE '[自动]%'"),
    )
    # 索引业务含义：[自动] 前缀跑分名称部分唯一——同周同视角自动 run 恒唯一
    # （防并发双跑），人工 run 命名不受限（注释落在 docstring 与模块级常量，PG
    # 索引注释经 COMMENT ON 需 DDL 文本拼接，按 SQL 参数绑定红线不采用）


def downgrade() -> None:
    op.drop_index("uq_eval_runs_auto_name", table_name="eval_runs")
