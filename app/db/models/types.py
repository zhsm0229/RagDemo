"""共享列类型与小工具：Vector（pgvector 列类型）与 _ts（带时区时间戳列工厂）。

拆分自原 app/db/models.py（>300 行红线，standards §1.4-4）：各域模型文件从本模块
取公共件；__init__.py 统一 re-export，`from app.db.models import X` 导入路径不变
（alembic/env.py 与 T4 零差异测试依赖）。
"""

from sqlalchemy import DateTime
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType):
    """pgvector 的 vector(n) 列类型（仅 DDL 声明与映射用）。"""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self) -> str:
        return f"vector({self.dim})"


def _ts() -> DateTime:
    return DateTime(timezone=True)
