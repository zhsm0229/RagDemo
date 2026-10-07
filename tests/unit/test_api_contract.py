r"""API 契约对账测试（2026-10-06 工单 06，findings I10）：docs/api.md 基线与 FastAPI 实际路由一致。

端点计数此前靠手记，四方漂移（AGENTS 47 / api.md 头部 51 / 小节枚举 55 / 代码实测 56），
本测试把对账脚本化，三向守护：
1. 解析 api.md 头部"端点总数：N"标记行，与实现导出的端点数断言相等——文档漏改即红；
2. api.md 章节标题声明的每个端点（METHOD + 路径）必须真实存在于实现——文档不写幽灵端点
   （方法/路径写错同判，路径参数名差异如 {id} vs {doc_id} 归一化后比对）；
3. 实现中除 ops 探针 `GET /healthz`（docker healthcheck 消费，头部行注明、不设业务章节）
   外的端点必须在 api.md 有对应章节——代码新增端点漏写文档同判红。

实现侧取数走 `create_app().openapi()`：本版 FastAPI 的 include_router 在 app.routes 里
是惰性 _IncludedRouter 包装，直接遍历 app.routes 拿不到 APIRoute；openapi() 触发展开，
且内建文档路由（/docs 等）与 SPA catch-all（include_in_schema=False）天然不进 schema，
计数即业务端点数（METHOD+path 为一个端点）。纯路由内省，不触库、不依赖 pg/session。
"""

import re
from pathlib import Path

from app.main import create_app

API_MD = Path(__file__).resolve().parents[2] / "docs" / "api.md"
_METHODS = {"get", "post", "put", "patch", "delete"}
# 章节标题形如 "### 2.10c DELETE /api/documents/{id} — 文档删除（…）"；域级小节
# （如 "### 4.1 问答对管理（…）"）与后台任务节无 METHOD，不匹配即跳过
_SECTION_ENDPOINT = re.compile(
    r"^#{3,4}\s+\S+\s+(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)", re.MULTILINE
)
# 唯一豁免章节的端点：ops 探针（见模块 docstring 第 3 条）
_SECTION_EXEMPT = {"GET /healthz"}


def _normalize(path: str) -> str:
    """路径参数名归一化：文档 {id} 与代码 {doc_id} 视为同一路径。"""
    return re.sub(r"\{[^}]+\}", "{}", path)


def _actual_endpoints() -> set[str]:
    """从 OpenAPI schema 导出实际端点（METHOD + 归一化路径）。"""
    schema = create_app().openapi()
    return {
        f"{method.upper()} {_normalize(path)}"
        for path, ops in schema["paths"].items()
        for method in ops
        if method in _METHODS
    }


def _documented_endpoints() -> set[str]:
    """从 api.md 章节标题解析已文档化端点（METHOD + 归一化路径）。"""
    text = API_MD.read_text(encoding="utf-8")
    return {f"{method} {_normalize(path)}" for method, path in _SECTION_ENDPOINT.findall(text)}


def test_api_md端点总数标记行与FastAPI实际端点数一致() -> None:
    marker = re.search(r"端点总数：(\d+)", API_MD.read_text(encoding="utf-8"))
    assert marker, 'docs/api.md 头部缺"端点总数：N"标记行，契约基线不可对账'
    actual = _actual_endpoints()
    assert len(actual) == int(marker.group(1)), (
        f"端点数漂移：实现 {len(actual)} 个，api.md 声明 {marker.group(1)} 个——"
        "新增/删除端点后未同步文档基线，请更新 api.md 头部标记行、小节枚举与 AGENTS.md 当前状态段"
    )


def test_api_md章节声明的端点全部真实存在() -> None:
    ghost = _documented_endpoints() - _actual_endpoints()
    assert not ghost, f"api.md 声明了实现中不存在的端点（方法或路径不符）：{sorted(ghost)}"


def test_实现中的端点除healthz探针外全部有api_md章节() -> None:
    undocumented = _actual_endpoints() - _documented_endpoints() - _SECTION_EXEMPT
    assert not undocumented, (
        f"实现新增端点未写入 api.md 章节（仅更新头部计数不够）：{sorted(undocumented)}"
    )
