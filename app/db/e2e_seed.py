r"""E2E 造数种子（幂等 upsert）：给 Playwright L1 层提供固定凭据与确定性数据。

为什么不用 seed.py：主种子 admin 密码随机且首登强制改密（§5.2），自动化需要
可复现凭据。本模块两条路径的口径（2026-10-05 试点库被 E2E_WIPE 清空事故根治）：
- 硬复位路径（E2E_WIPE=1 TRUNCATE）只允许 E2E 隔离库（rag_e2e / *_e2e 后缀），
  main() 入口防呆强制，无豁免开关——豁免即事故重演；
- 无 wipe 的幂等 upsert 路径不受库名限制：startup-check.sh 在试点库播 e2e-*
  冒烟账号是有意设计（幂等、可重复装配、不清理业务数据）。

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
import sys

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


# E2E_WIPE 只允许打在 E2E 隔离库上（库名白名单 + *_e2e 后缀）；无豁免开关。
_E2E_DB_WHITELIST = frozenset({"rag_e2e"})


def _wipe_target_db() -> str:
    """从 DATABASE_URL 解析库名（末段路径，忽略查询串；rstrip 处理尾斜杠）。"""
    from app.core.config import get_settings

    url = get_settings().database_url.rstrip("/")
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def _assert_wipe_isolated() -> None:
    """E2E_WIPE 防呆：目标库不是 E2E 隔离库时非零退出（在触库之前拦截）。"""
    db = _wipe_target_db()
    if db not in _E2E_DB_WHITELIST and not db.endswith("_e2e"):
        print(
            f"[e2e-seed] 拒绝执行 E2E_WIPE：DATABASE_URL 指向「{db}」，不是 E2E 隔离库"
            f"（允许：rag_e2e 或 *_e2e 后缀）。隔离栈用法见 frontend/package.json 的 "
            f"e2e:up / e2e:down 与 deploy/e2e-visible.sh，禁止对试点/业务库跑硬复位。",
            file=sys.stderr,
        )
        sys.exit(2)


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


# 上传夹具清单（{stamp} 为每轮时间戳占位，函数内 format 填充）。用例映射：
# latest=DOC-01；dup 与 latest 字节同源（DOC-06 查重面）；doc13 v1/v2=DOC-13 版本递增；
# fail-redline=DOC-11 重试恒败（清洗后 <100 字触发 §10.3 红线）；docspec=documents.spec 专用。
_E2E_BODY: tuple[str, ...] = (
    "第四条 住宿费一线城市每晚六百元封顶，超标部分自理。",
    "第五条 报销须在出差结束后十个工作日内提交费用系统。",
    "第六条 城市间交通费凭票据实报销，鼓励乘坐公共交通工具。",
)
_FIXTURE_TEMPLATES: list[tuple[str, tuple[str, ...]]] = [
    ("e2e-upload-latest.docx", ("流水号 {stamp}。", *_E2E_BODY)),
    ("e2e-upload-dup.docx", ("流水号 {stamp}。", *_E2E_BODY)),  # 与 latest 字节同源
    (
        "e2e-doc13-v1.docx",
        (
            "考勤与休假制度 v1（流水号 {stamp}）。",
            "工作日上午九点至下午六点，午休一小时。",
            "年假需提前三个工作日在 OA 系统申请，由直属主管审批后报人事部备案。",
            "迟到早退月度累计三次以上者，取消当月全勤奖金并计入年度考核记录。",
        ),
    ),
    (
        "e2e-doc13-v2.docx",
        (
            "考勤与休假制度 v2（流水号 {stamp}）。",
            "工作日弹性打卡时段为八点半至九点半。",
            "年假需提前五个工作日在 OA 系统申请，超过五天需部门总监加签确认。",
            "旷工半天以上者扣发当日工资并全公司通报批评，情节严重者解除劳动合同。",
        ),
    ),
    ("e2e-fail-redline.docx", ("短正文",)),  # 清洗后 <100 字 → §10.3 红线失败（重试恒失败）
    (
        "e2e-upload-docspec.docx",
        (
            "DOC-01 专用流水号 {stamp}。",
            "第七条 本夹具仅供 documents.spec 的入库用例使用，内容与 latest 互异。",
        ),
    ),
    (
        # SEC-AGENT-01（agent-evolution #09）串库边界断言用：上传时归属客服部门、仅客服
        # 可见，稳定标记 SVCONLY-7f3a92 供 hr 侧 agent 运行"产出/事件流不含客服域语料"
        # 的取反断言（检索工具 dept 过滤在服务端强制，标记越界出现即回归）。
        "e2e-svc-only.docx",
        (
            (
                "客服专属话术标记 SVCONLY-7f3a92（流水号 {stamp}）。本夹具归属客服部门，"
                "仅客服成员可见，其他部门 agent 运行的产出与事件流不得包含本标记。"
            ),
            (
                "客户咨询退货时，客服先核对订单号与购买日期，七天无理由范围内直接登记退货工单，"
                "告知客户仓库三个工作日内完成验收、退款原路返回。"
            ),
        ),
    ),
]


def main() -> None:
    wipe = os.environ.get("E2E_WIPE") == "1"
    if wipe:
        assert set(_WIPE_TABLES.replace(",", " ").split()) <= _ALLOWED_WIPE, "TRUNCATE 清单越界"
        _assert_wipe_isolated()  # 先验库后动手：隔离库名不对即中止，绝不 TRUNCATE 业务库
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
    → 同轮不互撞。清单表驱动（findings C5：单函数 ≤50 行），用例映射见
    _FIXTURE_TEMPLATES 注释。
    """
    import time
    from pathlib import Path

    from docx import Document

    out_dir = Path("/srv/ragdemo/data/e2e")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 10 位秒级（<13 位）：13-19 位纯数字串会进银行卡敏感扫描并按 ~1/10 概率过
    # Luhn → 文档被判敏感命中 is_active=False 待确认 → 评测题来源匹配 422、setup
    # 随机挂（2026-10-11 第六轮根因；17 位毫秒串整串命中 _BANK_CARD_RE）
    stamp = str(int(time.time()))
    for name, paragraphs in _FIXTURE_TEMPLATES:
        doc = Document()
        doc.add_heading("员工差旅费报销小样", level=0)
        for para in paragraphs:
            doc.add_paragraph(para.format(stamp=stamp))
        doc.save(out_dir / name)
    print("[e2e-seed] 上传夹具已生成（7 份，内容互异）")


if __name__ == "__main__":
    main()
    _write_upload_fixture()
