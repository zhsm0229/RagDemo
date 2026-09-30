r"""无鉴权存活探针（§12.2-3 healthcheck 前置）：进程 + 数据库连通二合一。

与管理面 /api/admin/health 的区别：本探针无鉴权、零业务语义、只答存活——
供容器 healthcheck 与发布脚本"等 healthz"使用，不暴露版本/统计等信息面。
响应故意不走统一信封（探针消费方是 docker/curl，不是前端 client）。
DB 不可达时 SELECT 1 抛异常 → 500 → 容器标记 unhealthy（fail-closed）。
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import DbSession

router = APIRouter(tags=["healthz"])


def _db_ping(db: DbSession) -> Iterator[Session]:
    db.execute(text("SELECT 1"))
    yield db


_DbChecked = Annotated[Session, Depends(_db_ping)]


@router.get("/healthz")
def healthz(_db: _DbChecked) -> dict[str, str]:
    return {"status": "ok"}
