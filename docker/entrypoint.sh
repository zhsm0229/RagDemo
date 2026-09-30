#!/bin/sh
# 容器入口（§12.2-3）：先迁移后服务。迁移失败即退出（fail-closed，不留旧结构对外服务）；
# alembic 幂等，已到 head 时为 no-op。迁移仅追加由评审保证（§4.5），此处只升不降。
set -e
echo "[entrypoint] alembic upgrade head"
alembic upgrade head
exec "$@"
