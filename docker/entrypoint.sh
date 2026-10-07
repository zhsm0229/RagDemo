#!/bin/sh
# 容器入口（§12.2-3）：先迁移后服务。迁移失败即退出（fail-closed，不留旧结构对外服务）；
# alembic 幂等，已到 head 时为 no-op。迁移仅追加由评审保证（§4.5），此处只升不降。
set -e
echo "[entrypoint] alembic upgrade head"
alembic upgrade head
# 自愈（2026-10-02 事故）：真实模式解析走容器内 BackgroundTasks，不持久化——容器重启
# 后队列丢失，遗留 parsing 会永久卡住且 UI 无重试入口。启动时统一转 failed+原因，
# 管理员即可在列表点重试（retry 走新任务，幂等）。
# 前提：本容器独占其数据库（单机 compose 一库一 app；E2E 隔离库 pg-e2e/rag_e2e 落地后，
# 共库跑套件的事故形态已消除）——若未来多实例共享同一库，本段会把其他实例正在跑的
# parsing/running 任务误标 failed，届时须按实例归属加过滤再执行。
python - <<'PY'
from app.db.base import SessionLocal
from app.db.models import Document, EvalRun
s = SessionLocal()
n = s.query(Document).filter(Document.status == 'parsing').update(
    {Document.status: 'failed', Document.parse_error: '服务重启导致解析中断，请在列表点重试',
     Document.parse_error_detail: '容器重启：BackgroundTasks 解析队列不持久化，任务随旧进程丢失'},
    synchronize_session=False,
)
# 同类缺口（2026-10-02 统一排查）：评测跑分同为进程内任务，重启后 running 永久卡死、
# 前端 10s 轮询永不停。已逐题 commit 的明细保留，状态收敛为 failed 供重新发起。
m = s.query(EvalRun).filter(EvalRun.status == 'running').update(
    {EvalRun.status: 'failed'}, synchronize_session=False,
)
s.commit()
s.close()
print(f'[entrypoint] 自愈：{n} 个中断解析任务、{m} 个中断评测任务已转为可重试')
PY
exec "$@"
