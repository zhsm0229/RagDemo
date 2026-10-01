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

from sqlalchemy import func, select, text, update

from app.core.security import hash_password
from app.db.base import SessionLocal
from app.db.models import Department, Document, QaPair, User

E2E_PASSWORD: str = os.environ.get("E2E_PASSWORD", "E2ePass#2026")


def _reset_lock_state(session) -> None:
    """复位 E2E 账号锁定态：AUTH-02/03 会把账号打出 failed_attempts/locked_until，
    不复位则后续套件的 setup 登录连锁 401。锁定审计记录保留（不回滚审计）。"""
    session.execute(
        update(User).where(User.username.like("e2e-%")).values(failed_attempts=0, locked_until=None)
    )


def _retire_previous_docs(session) -> None:
    """软删上一轮 E2E 上传的文档：释放 content_hash 唯一键（uq_doc_content_hash
    WHERE deleted_at IS NULL），否则同夹具重复上传被 F-1001 精确查重 409 秒拒。
    软删符合两段式口径（§4.6），物理清理归保留策略。"""
    session.execute(
        update(Document)
        .where(Document.title.like(r"e2e\_%", escape="\\"))
        .values(is_active=False, deleted_at=func.now())
    )


def _upsert_user(session, username: str, role: str, dept_code: str, *, must_change: bool) -> None:
    dept_id = session.scalars(select(Department.id).where(Department.code == dept_code)).first()
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


# 套级硬复位开关：E2E_WIPE=1 时清空业务表（文档/块/问答对/会话/消息/审计/评测），
# 杜绝跨轮 content_hash 占用与状态污染；账号与部门保留。
_WIPE_TABLES = (
    "documents, chunks, qa_pairs, conversations, messages, "
    "audit_logs, eval_questions, eval_runs RESTART IDENTITY CASCADE"
)
# 红线 2 豁免声明：TRUNCATE 的表名清单是标识符，无法走参数绑定；此处以白名单
# 交叉验证防拼接面失控（新增表必须显式进 _ALLOWED_WIPE，清单外字面量即 AssertionError）。
_ALLOWED_WIPE = frozenset(
    {
        "documents",
        "chunks",
        "qa_pairs",
        "conversations",
        "messages",
        "audit_logs",
        "eval_questions",
        "eval_runs",
        "RESTART",
        "IDENTITY",
        "CASCADE",
    }
)


def main() -> None:
    wipe = os.environ.get("E2E_WIPE") == "1"
    if wipe:
        assert set(_WIPE_TABLES.replace(",", " ").split()) <= _ALLOWED_WIPE, "TRUNCATE 清单越界"
    with SessionLocal() as session, session.begin():
        if wipe:
            session.execute(text(f"TRUNCATE {_WIPE_TABLES}"))
        _reset_lock_state(session)
        _retire_previous_docs(session)
        _upsert_user(session, "e2e-admin", "admin", "all", must_change=False)
        _upsert_user(session, "e2e-fresh", "admin", "all", must_change=True)
        _upsert_user(session, "e2e-hr", "member", "hr", must_change=False)
        _upsert_user(session, "e2e-svc", "member", "service", must_change=False)
        _upsert_user(session, "e2e-lock", "member", "hr", must_change=False)
        session.flush()  # 取 e2e-admin id 供种子问答对 created_by
        _upsert_seed_qa(session)
    print(f"[e2e-seed] ok（口令长度 {len(E2E_PASSWORD)}，4 账号 + 1 问答对就绪）")


def _write_upload_fixture() -> None:
    """按用例生成内容互异的上传夹具到上传卷（宿主 = ./data/uploads/e2e/）。

    每轮内嵌时间戳 → 与历史轮次内容不同（F-1001 不误伤）；套件内各用例内容互异
    → 同轮不互撞。映射：latest=DOC-01；latest 复传=DOC-06 查重；doc13v1/v2=DOC-13
    版本递增；fail-redline=DOC-11 重试（正文 <100 字触发 §10.3 红线，重试恒失败）。
    """
    import time
    from pathlib import Path

    from docx import Document

    out_dir = Path("/srv/ragdemo/data/e2e")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(time.time()).replace(".", "")

    def make(name: str, paragraphs: list[str]) -> None:
        doc = Document()
        doc.add_heading("员工差旅费报销小样", level=0)
        for para in paragraphs:
            doc.add_paragraph(para)
        doc.save(out_dir / name)

    body = [
        "第四条 住宿费一线城市每晚六百元封顶，超标部分自理。",
        "第五条 报销须在出差结束后十个工作日内提交费用系统。",
        "第六条 城市间交通费凭票据实报销，鼓励乘坐公共交通工具。",
    ]
    make("e2e-upload-latest.docx", [f"流水号 {stamp}。", *body])
    make("e2e-upload-dup.docx", [f"流水号 {stamp}。", *body])  # 与 latest 字节同源
    make(
        "e2e-doc13-v1.docx",
        [
            f"考勤与休假制度 v1（流水号 {stamp}）。",
            "工作日上午九点至下午六点，午休一小时。",
            "年假需提前三个工作日在 OA 系统申请，由直属主管审批。",
        ],
    )
    make(
        "e2e-doc13-v2.docx",
        [
            f"考勤与休假制度 v2（流水号 {stamp}）。",
            "工作日弹性打卡时段为八点半至九点半。",
            "年假需提前五个工作日在 OA 系统申请，超过五天需部门总监加签。",
        ],
    )
    make("e2e-fail-redline.docx", ["短正文"])  # 清洗后 <100 字 → §10.3 红线失败（重试恒失败）
    print("[e2e-seed] 上传夹具已生成（5 份，内容互异）")


if __name__ == "__main__":
    main()
    _write_upload_fixture()
