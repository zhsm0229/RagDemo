r"""E2E 造数种子（幂等 upsert）：给 Playwright L1 层提供固定凭据与确定性数据。

为什么不用 seed.py：主种子 admin 密码随机且首登强制改密（§5.2），自动化需要
可复现凭据；本模块只服务 E2E 环境，严禁对任何真实库执行。

- e2e-admin：管理员（全员部门，免首登改密）——常规冒烟身份
- e2e-fresh：管理员，must_change_password=TRUE——AUTH-04 首登改密流专用
- e2e-hr / e2e-svc：人力/客服 member——串库双向与权限矩阵冒烟
- 一条种子问答对（问题与测试提问逐字一致）：TEST_MODE 下 StubEmbedder 按 sha256
  播种，同文本必得同向量 → 余弦 1.0 稳定命中 qa 直答（L1 无语义检索能力，
  语义检索验证归 L2 真实链路）

用法（app 容器内或本机 venv，DATABASE_URL 指向目标库）：
    python -m app.db.e2e_seed        # 口令取 E2E_PASSWORD，缺省 E2ePass#2026
"""

import os

from sqlalchemy import func, select, update

from app.core.security import hash_password
from app.db.base import SessionLocal
from app.db.models import Department, Document, QaPair, User

E2E_PASSWORD: str = os.environ.get("E2E_PASSWORD", "E2ePass#2026")


def _retire_previous_docs(session) -> None:
    """软删上一轮 E2E 上传的文档：释放 content_hash 唯一键（uq_doc_content_hash
    WHERE deleted_at IS NULL），否则同夹具重复上传被 F-1001 精确查重 409 秒拒。
    软删符合两段式口径（§4.6），物理清理归保留策略。"""
    session.execute(
        update(Document)
        .where(Document.title.like(r"e2e\_%", escape="\\"))
        .values(is_active=False, deleted_at=func.now())
    )


def _upsert_user(
    session, username: str, role: str, dept_code: str, *, must_change: bool
) -> None:
    dept_id = session.scalars(
        select(Department.id).where(Department.code == dept_code)
    ).first()
    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, dept_id=dept_id, role=role)
        session.add(user)
    user.password_hash = hash_password(E2E_PASSWORD)
    user.must_change_password = must_change
    user.is_active = True


def _upsert_seed_qa(session) -> None:
    """种子问答对：问题逐字等于 e2e 用例提问（stub 同文本同向量，qa 直答稳定命中）。"""
    from app.core.llm import StubEmbedder

    question = "报销的时限是多少天"
    answer = "出差结束后十个工作日内，在费用系统提交报销单。"
    if session.scalars(select(QaPair).where(QaPair.question == question)).first():
        return
    vector = StubEmbedder().embed([question])[0]
    all_dept = session.scalars(select(Department.id).where(Department.code == "all")).first()
    session.add(
        QaPair(
            dept_id=all_dept,
            question=question,
            answer=answer,
            embedding=vector,
            created_by=_admin_id(session),
        )
    )


def _admin_id(session) -> int:
    return session.scalars(select(User.id).where(User.username == "e2e-admin")).first()


def main() -> None:
    with SessionLocal() as session, session.begin():
        _retire_previous_docs(session)
        _upsert_user(session, "e2e-admin", "admin", "all", must_change=False)
        _upsert_user(session, "e2e-fresh", "admin", "all", must_change=True)
        _upsert_user(session, "e2e-hr", "member", "hr", must_change=False)
        _upsert_user(session, "e2e-svc", "member", "service", must_change=False)
        session.flush()  # 取 e2e-admin id 供种子问答对 created_by
        _upsert_seed_qa(session)
    print(f"[e2e-seed] ok（口令长度 {len(E2E_PASSWORD)}，4 账号 + 1 问答对就绪）")


if __name__ == "__main__":
    main()
