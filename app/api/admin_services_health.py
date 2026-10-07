r"""服务健康卡路由：GET /api/admin/services/health（2026-10-02 试点反馈新增）。

只读探活聚合，过 AdminUser 门（401/2003、403/2004），不写库不留审计。
独立成文件而非并入 admin_overview.py：后者 142 行，探活域含 DTO 组装后
两文件都在 300 行红线内，且探活端点是 async（概览域其余为同步聚合）。

**不做启停动作**：2026-10-02 评估结论——web 进程触达容器编排需暴露 docker
控制面，管理员账号泄露即升级为宿主机沦陷；恢复动作归 deploy/recover.sh。
"""

from fastapi import APIRouter

from app.api.deps import AdminUser, DbSession
from app.services.services_health import ServiceHealth, check_services_health

router = APIRouter(prefix="/admin", tags=["admin"])


def _service_payload(svc: ServiceHealth) -> dict:
    return {
        "key": svc.key,
        "label": svc.label,
        "status": svc.status,
        "latencyMs": svc.latency_ms,
        "detail": svc.detail,
    }


@router.get("/services/health")
async def admin_services_health(user: AdminUser, db: DbSession) -> dict:
    """各依赖探活：pg（SELECT 1）、tei-embed/rerank（/health）、llm（/models）、
    mineru（/docs；未配置为 not_configured 不计故障）。overall：up / degraded
    （仅 mineru 不可用，问答不受影响）/ down（问答链路任一不可用）。"""
    report = await check_services_health(db)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "overall": report.overall,
            "services": [_service_payload(svc) for svc in report.services],
        },
    }
