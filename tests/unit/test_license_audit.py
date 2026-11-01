r"""许可清单守账测试（工单 31，法律意见证据链自动化保鲜）。

对账对象 = `.scratch/commercial-readiness/design/10-license-compliance-tech.md`
逐组件许可清单（工单 10 实证产物）。机制：读本项目 pyproject 运行时依赖组（与
Dockerfile `pip install .` 同口径——dev-only 依赖如 fpdf2 不随镜像分发、不入对账），
逐包查安装 metadata 的 License-Expression/License 字段实测，断言与登记清单一致——
依赖升降级导致许可漂移时本测试红，法律顾问每次只审 diff 而非重审全量。

边界声明：本测试只守"技术事实清单不漂"，**不构成法律意见**——AGPL/LGPL 义务边界的
法律解释、商用分发合同条款，均须外部法律顾问确认（工单 10 外部项，验收路径不变）。
"""

from importlib import metadata

import packaging.requirements

# 登记清单的机器可读对账面（与 design/10 §一"app 镜像 Python 依赖"行逐包一致，
# 2026-11-01 实证口径；清单行更新时本表同步——漂移双向对账）。
EXPECTED = {
    "fastapi": "MIT",
    "uvicorn": "BSD-3-Clause",
    "gunicorn": "MIT",
    "sqlalchemy": "MIT",
    # ⚠️ 关键行（工单 10 发现）：LGPL——随容器分发须提供源码获取方式，钉版+源码链接即满足
    "psycopg": "LGPL-3.0-only",
    "alembic": "MIT",
    "bcrypt": "Apache-2.0",
    "pyjwt": "MIT",
    "python-multipart": "Apache-2.0",
    "python-docx": "MIT",
    "pypdf": "BSD-3-Clause",
    "openpyxl": "MIT",
    "python-pptx": "MIT",
    "pydantic-settings": "MIT",
    "apscheduler": "MIT",
    "httpx": "BSD-3-Clause",
    "deepagents": "MIT",
    "langgraph": "MIT",
    "langchain-core": "MIT",
    "ag-ui-protocol": "MIT",
}


def _runtime_deps() -> list[str]:
    """运行时依赖名清单（requires() 区分 extra：dev 组带 extra == 'dev' 标记排除）。"""
    reqs = metadata.requires("ragdemo") or []
    return [packaging.requirements.Requirement(r).name for r in reqs if "extra ==" not in r]


def _license_of(dist: str) -> str:
    m = metadata.metadata(dist)
    return (m.get("License-Expression") or m.get("License") or "").strip()


def test_运行时依赖许可与登记清单逐包一致() -> None:
    """运行时依赖实测许可 == 登记清单：漂移即红（diff 即顾问审阅面）。"""
    runtime = _runtime_deps()
    assert sorted(runtime) == sorted(EXPECTED), (
        "pyproject 运行时依赖与登记清单集合不一致——"
        "新增/移除依赖须同步 design/10 清单与本表（漂移双向对账）"
    )
    drifted = {
        name: {"登记": lic, "实测": actual}
        for name, lic in EXPECTED.items()
        if (actual := _license_of(name)) != lic
    }
    assert not drifted, f"许可漂移（须更新 design/10 清单并知会法律顾问）：{drifted}"


def test_开发态依赖不入运行时分发面() -> None:
    """fpdf2（LGPL）只用于测试造夹具、属 dev 组——不入运行时依赖集合
    （design/10 §一"仅开发态"行的机器可读钉死：勿将 LGPL dev 依赖误入分发面）。"""
    assert "fpdf2" not in _runtime_deps()


def test_nomineru降级档compose不含mineru且app强制置空() -> None:
    """无 MinerU 交付形态（design/10 §三降级档）的配置面守账：mineru 划入 full
    profile 缺省不起、app 的 MINERU_BASE_URL 强制置空（environment 优先级高于 .env）。"""
    import yaml

    with open("deploy/docker-compose.nomineru.yml", encoding="utf-8") as f:
        override = yaml.safe_load(f)
    services = override.get("services", {})
    if "mineru" in services:
        # 覆盖文件里的 mineru 只允许以 profile 方式缺省不起
        assert "full" in services["mineru"].get("profiles", []), (
            "nomineru 档 mineru 必须挂 profile 缺省不起"
        )
    app_env = services["app"].get("environment", {})
    assert app_env.get("MINERU_BASE_URL", "") == "", (
        "nomineru 档 app 的 MINERU_BASE_URL 必须强制置空（pypdf 降级前提）"
    )
