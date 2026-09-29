"""共享列类型与小工具：Vector（pgvector 列类型）与 _ts（带时区时间戳列工厂）。

拆分自原 app/db/models.py（>300 行红线，standards §1.4-4）：各域模型文件从本模块
取公共件；__init__.py 统一 re-export，`from app.db.models import X` 导入路径不变
（alembic/env.py 与 T4 零差异测试依赖）。
"""

from collections.abc import Sequence

from sqlalchemy import DateTime, cast, literal
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType):
    """pgvector 的 vector(n) 列类型：DDL 声明、映射与查询表达式（余弦距离）用。"""

    cache_ok = True

    class Comparator(UserDefinedType.Comparator):
        """表达式级比较器：``列.cosine_distance(vec)`` → ``列 <=> CAST(:p AS vector(n))``。

        绑定参数必须显式 CAST 成 vector：psycopg 将 list[float] 以 double precision[]
        类型发送，不 CAST 时 PG 报「vector <=> double precision[] 无此运算符」
        （pgvector 0.8/pg-test 实证）；CAST 后经 real[]→vector 赋形转换可命中。
        """

        def cosine_distance(self, other: Sequence[float]) -> ColumnElement[float]:
            return self.op("<=>")(cast(literal(other), self.expr.type))

    comparator_factory = Comparator

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self) -> str:
        return f"vector({self.dim})"


def _ts() -> DateTime:
    return DateTime(timezone=True)
