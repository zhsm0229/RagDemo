r"""依赖服务探活（服务健康卡，GET /api/admin/services/health 的业务层）。

2026-10-02 试点背景：app 容器自身 healthy 但 TEI/LLM 挂掉时问答全挂，管理页却
完全不可见（healthcheck 只管 app 进程）。本模块对问答链路各依赖发轻量探活，
给管理页只读展示；**不做任何启停动作**（服务生命周期归 compose/runbook，
2026-10-02 评估结论：web 触达容器编排=攻击面质变，见 deploy/recover.sh）。

口径：
- pg：注入 session 上 SELECT 1（走连接池，最能代表"应用→库"这条真实路径）。
- tei-embed / tei-rerank / llm：对各自 base_url 发 GET（TEI /health、OpenAI 兼容
  /models），超时 2.5s，asyncio.gather 并发，总耗时=最慢者。
- TEST_MODE：embed/rerank/llm 走 Stub 三件套（无网络依赖）→ 直接标 up 并注明
  "TEST_MODE 桩"，CI 无 TEI/外网也全绿；mineru 是真实解析服务不短路。
- mineru_base_url 空 = pypdf 降级路径（§10.3），不是故障 → not_configured；
  已配置时探 GET /file_parse，405（方法不允许=服务在位且路由已挂载）视为 up——
  MinerU 无自有 /health（容器实测），探 /docs 会与 FastAPI docs 开关耦合误报（I15）。
- overall 三态：down=问答链路（pg/embed/rerank/llm）任一不可用；degraded=仅
  mineru 不可用（问答正常、文档解析降级）；up=全绿。
"""

import asyncio
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings

_PROBE_TIMEOUT_S = 2.5  # 内网依赖（TEI/MinerU）
_LLM_PROBE_TIMEOUT_S = 5.0  # LLM 为外网 API：握手常超 2.5s，放宽避免把偶发慢误报为 down

# overall 判定中视为"问答链路"的组件；mineru 不在内（down 只降级为 degraded）
_CORE_KEYS = frozenset({"pg", "embed", "rerank", "llm"})


@dataclass(frozen=True)
class ServiceHealth:
    """单个依赖的探活结果；detail 为空表示正常，否则为给管理员看的中文原因摘要。"""

    key: str
    label: str
    status: str  # up | down | not_configured
    latency_ms: int
    detail: str


@dataclass(frozen=True)
class ServicesHealthReport:
    services: tuple[ServiceHealth, ...]

    @property
    def overall(self) -> str:
        statuses = {s.key: s.status for s in self.services}
        if any(statuses.get(k) == "down" for k in _CORE_KEYS):
            return "down"
        if any(s.status == "down" for s in self.services):
            return "degraded"
        return "up"


def _brief(exc: Exception) -> str:
    """异常摘要：类型名 + 首行消息（截 80 字），供健康卡 detail 展示。"""
    message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"[:80]


def _check_pg(db: Session) -> ServiceHealth:
    started = time.monotonic()
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 —— 探活必须吞一切异常转为 down 状态
        return ServiceHealth("pg", "PostgreSQL", "down", _elapsed(started), _brief(exc))
    return ServiceHealth("pg", "PostgreSQL", "up", _elapsed(started), "")


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def _probe_http(
    key: str,
    label: str,
    base_url: str,
    path: str,
    headers: dict | None = None,
    timeout_s: float = _PROBE_TIMEOUT_S,
    extra_ok: frozenset[int] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ServiceHealth:
    """GET {base_url}{path} 轻探活：2xx=up；4xx/5xx/连接错误/超时=down（detail 摘要）。

    4xx（如 LLM /models 的 401）同样算 down——网络在位但鉴权/配置错，回答必然失败，
    这正是健康卡要暴露的。extra_ok：额外视为 up 的状态码白名单（mineru 用 405，见
    check_services_health 内注释）；transport 供测试注入 httpx.MockTransport。"""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            resp = await client.get(f"{base_url}{path}", headers=headers or {})
    except Exception as exc:  # noqa: BLE001 —— 同上，探活吞异常转 down
        return ServiceHealth(key, label, "down", _elapsed(started), _brief(exc))
    ok = resp.status_code < 300 or (extra_ok is not None and resp.status_code in extra_ok)
    status = "up" if ok else "down"
    detail = "" if status == "up" else f"HTTP {resp.status_code}"
    return ServiceHealth(key, label, status, _elapsed(started), detail)


async def check_services_health(db: Session) -> ServicesHealthReport:
    """探活全部依赖并聚合。HTTP 探活并发执行；pg 同步（连接池内 SELECT 1，毫秒级）。"""
    settings = get_settings()
    services: list[ServiceHealth] = [_check_pg(db)]

    if settings.test_mode:
        # Stub 三件套无网络依赖：不探活直接 up，注明口径（CI 无 TEI/外网也全绿）
        stub_detail = "TEST_MODE 桩，未探活"
        services += [
            ServiceHealth("embed", "向量化 (TEI)", "up", 0, stub_detail),
            ServiceHealth("rerank", "重排 (TEI)", "up", 0, stub_detail),
            ServiceHealth("llm", "大模型 (LLM)", "up", 0, stub_detail),
        ]
    else:
        services += list(
            await asyncio.gather(
                _probe_http("embed", "向量化 (TEI)", settings.embed_base_url, "/health"),
                _probe_http("rerank", "重排 (TEI)", settings.rerank_base_url, "/health"),
                _probe_http(
                    "llm",
                    "大模型 (LLM)",
                    settings.llm_base_url,
                    "/models",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    timeout_s=_LLM_PROBE_TIMEOUT_S,
                ),
            )
        )

    if settings.mineru_base_url:
        # I15：探稳定路径而非 /docs。MinerU 2.5.4 容器实测（2026-10-06）无自有 /health
        # （/health、/ 均为 404），唯一路由 POST /file_parse——GET 探针得 405=服务在位
        # 且路由已挂载，视为 up。原探 /docs 与 FastAPI docs 开关耦合：生产关闭 docs
        # （常见加固项）时健康卡误报 down→degraded
        services.append(
            await _probe_http(
                "mineru",
                "文档解析 (MinerU)",
                settings.mineru_base_url,
                "/file_parse",
                extra_ok=frozenset({405}),
            )
        )
    else:
        services.append(
            ServiceHealth(
                "mineru", "文档解析 (MinerU)", "not_configured", 0, "未配置，走 pypdf 降级解析"
            )
        )

    # gotenberg（F-1015 老格式转换）：与 mineru 同语义——down 仅降级（问答不受影响）；
    # convert_base_url 空=未配置（老格式上传将被拒收并提示另存）
    if settings.convert_base_url:
        services.append(
            await _probe_http(
                "gotenberg", "老格式转换 (Gotenberg)", settings.convert_base_url, "/health"
            )
        )
    else:
        services.append(
            ServiceHealth(
                "gotenberg",
                "老格式转换 (Gotenberg)",
                "not_configured",
                0,
                "未配置，老格式上传将被拒收",
            )
        )

    return ServicesHealthReport(tuple(services))
