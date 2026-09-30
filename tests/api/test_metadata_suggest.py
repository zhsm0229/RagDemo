r"""元数据自动建议测试（F-1008）：文件名 `部门_主题_日期.ext` → 上传表单预填建议。

覆盖：解析纯函数表驱动——规范命名（YYYYMMDD 与 YYYY-MM-DD 两种白名单日期、中文
部门别名、全员部门、多段主题含下划线）给出建议，命名不符一律 None 不产出错误
建议（无结构 / 部门枚举外 / 日期格式非法 / 缺前导零 / 不存在的日期）；端点
（admin-only）multipart 上传假文件——实现只读文件名不读文件内容（字节占位即可，
验收口径"20 个规范命名 ≥18 正确"的主要面在文件名规则，内容提取日期属超范围
工程）；命名不符返回空建议 {}；member 403（AdminUser 门 = §F-1008 防越权注入）。
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_FORBIDDEN
from app.db.models import Department, User
from app.services.metadata_suggest import SuggestedMeta, parse_filename_metadata
from tests.conftest import login_as

# ---------- 构造与登录辅助（口径同 test_zip_import.py） ----------


def make_user(session: Session, username: str, password: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def admin_login(client: TestClient, session: Session) -> None:
    """创建并登录 admin（cookie 由 client 保持）。"""
    make_user(session, "msadmin", "right-one", role="admin")
    assert login_as(client, "msadmin", "right-one").status_code == 200


def post_suggest(client: TestClient, filename: str):
    """端点统一入口：假文件只携文件名，字节占位（实现不读内容）。"""
    return client.post(
        "/api/documents/metadata-suggest",
        files={"file": (filename, b"bytes-not-read", "application/octet-stream")},
    )


# ---------- 解析纯函数：表驱动（规范命名 ≥18/20 验收口径抽样） ----------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # 规范命名：编码部门 + YYYYMMDD
        ("service_员工手册_20260901.docx", SuggestedMeta("service", "员工手册", date(2026, 9, 1))),
        # 规范命名：编码部门 + YYYY-MM-DD
        ("hr_考勤制度_2026-09-15.docx", SuggestedMeta("hr", "考勤制度", date(2026, 9, 15))),
        # 中文部门别名映射为编码；pdf 扩展名不受限
        ("客服_客服话术_20260131.pdf", SuggestedMeta("service", "客服话术", date(2026, 1, 31))),
        ("全员_安全须知_20261231.docx", SuggestedMeta("all", "安全须知", date(2026, 12, 31))),
        # 多段主题：下划线归主题，不误拆
        (
            "service_员工_手册_v2_20260901.docx",
            SuggestedMeta("service", "员工_手册_v2", date(2026, 9, 1)),
        ),
        # ---- 命名不符：一律 None（不产出错误建议）----
        ("通知.docx", None),  # 无 部门_主题_日期 结构
        ("legal_制度_20260901.docx", None),  # 部门枚举外
        ("hr_薪酬制度_2026年9月.docx", None),  # 日期格式不在白名单
        ("service_手册_2026-9-1.docx", None),  # 缺前导零
        ("hr_薪酬_20261332.docx", None),  # 日期不存在（13 月）
        ("service_手册_20260230.docx", None),  # 日期不存在（2 月 30 日）
        ("人力_制度_无日期.docx", None),  # 末段不是日期
    ],
)
def test_parse_filename_metadata_table(filename: str, expected: SuggestedMeta | None) -> None:
    """表驱动：规范命名给出（部门码, 主题, 日期）建议，命名不符一律 None。"""
    assert parse_filename_metadata(filename) == expected


# ---------- 端点：admin-only，multipart 假文件，断言建议 JSON ----------


def test_suggest_prefills_from_regular_filename(client, session) -> None:
    """规范命名文件名：200 返回三项建议（camelCase，日期规范为 ISO 格式）。"""
    admin_login(client, session)
    resp = post_suggest(client, "service_员工手册_20260901.docx")
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "suggestedDeptCode": "service",
        "suggestedTitle": "员工手册",
        "suggestedEffectiveDate": "2026-09-01",
    }


def test_suggest_maps_chinese_dept_name(client, session) -> None:
    """中文部门名文件名：建议库内部门编码 service（中文名与编码均收）。"""
    admin_login(client, session)
    resp = post_suggest(client, "客服_客服话术_20260131.pdf")
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "suggestedDeptCode": "service",
        "suggestedTitle": "客服话术",
        "suggestedEffectiveDate": "2026-01-31",
    }


def test_suggest_returns_empty_suggestion_on_bad_naming(client, session) -> None:
    """命名不符：200 空建议 {}（不报错、不产出错误建议，表单留空待管理员补录）。"""
    admin_login(client, session)
    resp = post_suggest(client, "hr_薪酬制度_2026年9月.docx")
    assert resp.status_code == 200
    assert resp.json()["data"] == {}


def test_suggest_member_forbidden(client, session) -> None:
    """member 调用建议端点：403/2004（AdminUser 门，§F-1008 防越权注入）。"""
    make_user(session, "msmember", "right-one", role="member")
    assert login_as(client, "msmember", "right-one").status_code == 200
    resp = post_suggest(client, "service_手册_20260901.docx")
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
