r"""阈值扫描向导（W4 Task 5，F-1012 简化版）API 测试。

brief 钉死口径的全量钉：合成 done 明细（拒答题 2：refuse 题全档判拒 + cross_reject
漏判题、qa 题 1、rag 题 2 含不同 doc 分、pending 1、error 1）→ 七档拒答曲线逐档
手工核对（accuracy 含人工半分权重：0.40 档 (1+0+1+0.5)/4=0.625）、方向 sanity
（阈值升 → 拒答率不降）、qa 题档位敏感（0.88 分 0.85 档仍命中 0.90 档脱落）、
refuse 题全档拒答（0.40 档 refuseRate=0.25 只可能来自它）、跳过计数与口径局限
注明、推荐值并列档取更低（0.60/0.65 并列 0.75 → 取 0.60）、结果存 detail
读回且重复扫描覆盖哨兵、running 422/3004、全 pending 422 指名无可重放题目、
审计 eval.threshold_scan 读回、寻址 404 与 member 403。

造数：直插合成 EvalRun.detail（扫描是纯 detail 重放器，不经引擎——分数须精确
可控才能逐档手工核对；引擎跑出的分数依赖 Stub 向量无法钉死档位边界）。
认证辅助复用 test_admin_eval_import（同目录 import，不另造）。
"""

from decimal import Decimal

from sqlalchemy import select
from test_admin_eval_import import admin_login, login, make_user

from app.core.errors import ERR_EVAL_STATE, ERR_FORBIDDEN, ERR_NOT_FOUND
from app.db.models import AuditLog, EvalRun

# ---------------------------------------------------------------------------
# 造数助手：合成 9 键逐题明细 + done/running run 行（扫描只读 detail）
# ---------------------------------------------------------------------------


def _doc_citation(chunk_id: int, score: float) -> dict:
    """SSE DTO 形态的 doc 引用（eval_engine detail.citations 同构，7 键全）。"""
    return {
        "n": 1,
        "refType": "doc",
        "refId": chunk_id,
        "title": "客服手册 3.2 节",
        "page": 3,
        "titlePath": "客服手册/密码重置",
        "score": score,
    }


def _qa_citation(qa_id: int, score: float) -> dict:
    """SSE DTO 形态的 qa 引用（chat._qa_plan 同构：page=None、titlePath 空）。"""
    return {
        "n": 1,
        "refType": "qa",
        "refId": qa_id,
        "title": "如何重置密码？",
        "page": None,
        "titlePath": "",
        "score": score,
    }


def _item(question_id: int, **overrides) -> dict:
    """detail 逐题 9 键基础形态（eval_engine._run_one 同构），按需覆写。"""
    item: dict = {
        "questionId": question_id,
        "deptId": 2,
        "qtype": "factual",
        "expected": "标准答案",
        "answerType": "",
        "answer": "答案正文",
        "citations": [],
        "autoJudgment": "pending",
        "error": None,
    }
    item.update(overrides)
    return item


def _scan_items() -> list[dict]:
    """七题合成明细（questionId 1-7）：
    1 refuse 题拒答成功（auto correct，citations 空）；2 cross_reject 漏判
    （doc 0.58 ≥ 配置拒答阈 0.45 → 已回答，auto wrong）；3/4 rag 题人工判
    correct/partial（doc 0.67/0.48）；5 qa 题人工判 correct（qa 分 0.88）；
    6 pending；7 error。"""
    return [
        _item(
            1,
            qtype="cross_reject",
            expected="[拒答]",
            answerType="refuse",
            answer="知识库中未找到相关内容",
            autoJudgment="correct",
        ),
        _item(
            2,
            qtype="cross_reject",
            expected="[拒答]",
            answerType="rag",
            citations=[_doc_citation(101, 0.58)],
            autoJudgment="wrong",
        ),
        _item(
            3,
            answerType="rag",
            citations=[_doc_citation(102, 0.67)],
            autoJudgment="pending",
            judgment="correct",
        ),
        _item(
            4,
            qtype="multi_hop",
            answerType="rag",
            citations=[_doc_citation(103, 0.48)],
            autoJudgment="pending",
            judgment="partial",
        ),
        _item(
            5,
            answerType="qa",
            citations=[_qa_citation(201, 0.88)],
            autoJudgment="pending",
            judgment="correct",
        ),
        _item(6, answerType="rag", citations=[_doc_citation(104, 0.90)]),
        _item(7, answerType="", answer="", error="上游超时"),
    ]


def _make_run(session, items: list[dict], *, status: str = "done") -> int:
    """合成 run 行并落库（挂在测试事务上）：config 快照的阈值与明细分数自洽
    （0.58/0.62 ≥ 0.45 已回答、0.48 例外走人工、0.88 ≥ 0.85 qa 命中）。"""
    run = EvalRun(
        name="阈值扫描合成任务",
        config={"refuseThreshold": 0.45, "qaThreshold": 0.85},
        status=status,
        total=len(items),
        correct=Decimal("3.5"),
        accuracy=Decimal("0.5"),
        detail={"items": items, "pendingCount": 2},
        triggered_by=1,
    )
    session.add(run)
    session.flush()
    return run.id


def _scan(client, run_id: int):  # type: ignore[no-untyped-def]
    return client.post(f"/api/admin/eval-runs/{run_id}/threshold-scan")


# 七档手工核对基准：分母 4 = refuse/rag 参与者（qa 题、pending、error 均不入）。
# 逐题权重：1=refuse 拒答题全档 1；2=cross_reject 漏判 0.58 分（≥0.60 翻正）；
# 3=rag 人工 correct 0.67 分（0.70 档被误拒）；4=rag 人工 partial 0.48 分
# （0.50 档起被误拒）。0.70 档全员判拒 → accuracy 回落 0.5、refuseRate 达 1.0。
_EXPECTED_REFUSE_CURVE = [
    {"threshold": 0.4, "accuracy": 0.625, "refuseRate": 0.25},
    {"threshold": 0.45, "accuracy": 0.625, "refuseRate": 0.25},
    {"threshold": 0.5, "accuracy": 0.5, "refuseRate": 0.5},
    {"threshold": 0.55, "accuracy": 0.5, "refuseRate": 0.5},
    {"threshold": 0.6, "accuracy": 0.75, "refuseRate": 0.75},
    {"threshold": 0.65, "accuracy": 0.75, "refuseRate": 0.75},
    {"threshold": 0.7, "accuracy": 0.5, "refuseRate": 1.0},
]
# qa 题 0.88 分：0.75/0.80/0.85 档仍命中（照走 qa 通道），0.90/0.95 档脱落。
_EXPECTED_QA_CURVE = [
    {"threshold": 0.75, "hitRate": 1.0},
    {"threshold": 0.8, "hitRate": 1.0},
    {"threshold": 0.85, "hitRate": 1.0},
    {"threshold": 0.9, "hitRate": 0.0},
    {"threshold": 0.95, "hitRate": 0.0},
]


# ---------------------------------------------------------------------------
# ① 七档拒答曲线 + 五档 qa 曲线：档位数与逐档手工核对（含人工半分权重）
# ---------------------------------------------------------------------------


def test_七档拒答曲线与五档qa曲线逐档手工核对(client, session):
    """① 拒答曲线 7 档精确值：0.40 档 (1+0+1+0.5)/4=0.625（人工 partial 半分
    权重进 accuracy）、0.60 档起漏判串库题翻正至 0.75；qa 曲线 5 档精确值。"""
    admin_login(client, session)
    run_id = _make_run(session, _scan_items())

    resp = _scan(client, run_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["message"] == "ok"
    data = body["data"]
    assert data["refuseCurve"] == _EXPECTED_REFUSE_CURVE
    assert data["qaCurve"] == _EXPECTED_QA_CURVE


# ---------------------------------------------------------------------------
# ② 方向 sanity：阈值升 → 拒答率不降
# ---------------------------------------------------------------------------


def test_拒答率随阈值升不降_曲线方向sanity(client, session):
    """② score < 阈值判拒的口径下，档位升高可判拒的题只增不减：refuseRate
    序列单调不降（qa 曲线反向单调不升同理）。"""
    admin_login(client, session)
    run_id = _make_run(session, _scan_items())

    data = _scan(client, run_id).json()["data"]

    refuse_rates = [entry["refuseRate"] for entry in data["refuseCurve"]]
    assert refuse_rates == sorted(refuse_rates)
    hit_rates = [entry["hitRate"] for entry in data["qaCurve"]]
    assert hit_rates == sorted(hit_rates, reverse=True)


# ---------------------------------------------------------------------------
# ③ refuse 题全档判拒 + qa 题不入拒答曲线：跳过计数与口径局限注明
# ---------------------------------------------------------------------------


def test_refuse题全档判拒_qa题跳过计数与局限注明(client, session):
    """③ 最低档 0.40 的 refuseRate=0.25 只可能来自 citations 为空的 refuse 题
    （全档判拒）；scope 计数钉死分区：refuse 4（含拒答成功 1）、qa 1（不入拒答
    曲线，故 0.40 档分母是 4 非 5）、pending 1、error 1，note 注明 qa 曲线无
    rag 题反证数据的口径局限。"""
    admin_login(client, session)
    run_id = _make_run(session, _scan_items())

    data = _scan(client, run_id).json()["data"]

    assert data["refuseCurve"][0] == {"threshold": 0.4, "accuracy": 0.625, "refuseRate": 0.25}
    assert data["scope"] == {
        "refuseTotal": 4,
        "qaTotal": 1,
        "pendingSkipped": 1,
        "errorSkipped": 1,
        "note": "qa 曲线仅统计 qa 题各档位仍命中的比例（档位敏感度），不含 rag 题"
        "反证数据：detail 快照未存 rag 题曾见的 qa 匹配分，无法重放'降档会误入"
        " qa 通道'的风险。",
    }


# ---------------------------------------------------------------------------
# ④ 推荐值：accuracy/hitRate 最高档，并列取更低阈值（更倾向回答）
# ---------------------------------------------------------------------------


def test_推荐值并列档取更低阈值(client, session):
    """④ 拒答曲线 0.60/0.65 两档 accuracy 并列 0.75 → 推荐 0.60（同等准确度
    更倾向回答）；qa 曲线 0.75/0.80/0.85 三档 hitRate 并列 1.0 → 推荐 0.75。"""
    admin_login(client, session)
    run_id = _make_run(session, _scan_items())

    data = _scan(client, run_id).json()["data"]

    assert data["recommended"] == {"refuseThreshold": 0.6, "qaThreshold": 0.75}


# ---------------------------------------------------------------------------
# ④-补 全拒答档不参与推荐（评审修复轮 1 复现钉）：refuseRate=1.0 的档凭严格
# 最高 accuracy 胜出会把推荐推向"全部拒答"——恰是本功能要解决的串库场景
# ---------------------------------------------------------------------------


def test_全拒答档不参与推荐_串库场景复现(client, session):
    """评审构造例：3 条拒答期望 refuse 题 + 1 条 cross_reject 漏判（doc 0.68）。
    0.40-0.65 档 accuracy=0.75；0.70 档漏判题也被拒 → accuracy=1.0 严格最高、
    refuseRate=1.0——守卫前推荐 0.70（全部拒答档），守卫后排除该档、0.40-0.65
    并列 0.75 取更低 → 0.40。"""
    admin_login(client, session)
    items = [
        _item(
            qid,
            qtype="cross_reject",
            expected="[拒答]",
            answerType="refuse",
            answer="知识库中未找到相关内容",
            autoJudgment="correct",
        )
        for qid in (21, 22, 23)
    ] + [
        _item(
            24,
            qtype="cross_reject",
            expected="[拒答]",
            answerType="rag",
            citations=[_doc_citation(301, 0.68)],
            autoJudgment="wrong",
        )
    ]
    run_id = _make_run(session, items)

    data = _scan(client, run_id).json()["data"]

    top_tier = data["refuseCurve"][-1]  # 前置自证：0.70 档确为严格最高 accuracy
    assert top_tier == {"threshold": 0.7, "accuracy": 1.0, "refuseRate": 1.0}
    assert data["recommended"]["refuseThreshold"] != 0.7  # 全拒答档不得胜出
    assert data["recommended"]["refuseThreshold"] == 0.4  # 可回答档并列取更低


def test_全档全拒答时守卫不误伤_仍返回最低档(client, session):
    """守卫边界：参与者全为 refuse 题（每档 refuseRate=1.0）——排除后无候选，
    回退全档参与（"无可回答档"语义，模块 docstring 注明），accuracy 全档并列
    1.0 取更低 → 0.40，不得返回 None 或空档崩溃。"""
    admin_login(client, session)
    items = [
        _item(
            qid,
            qtype="cross_reject",
            expected="[拒答]",
            answerType="refuse",
            answer="知识库中未找到相关内容",
            autoJudgment="correct",
        )
        for qid in (31, 32)
    ]
    run_id = _make_run(session, items)

    data = _scan(client, run_id).json()["data"]

    assert all(entry["refuseRate"] == 1.0 for entry in data["refuseCurve"])
    assert data["recommended"] == {"refuseThreshold": 0.4, "qaThreshold": None}


# ---------------------------------------------------------------------------
# ⑤ 结果存 detail.thresholdScan 读回 + 重复扫描覆盖旧结果
# ---------------------------------------------------------------------------


def test_结果存入detail读回且重复扫描覆盖(client, session):
    """⑤ F-1012 可追溯：扫描结果整体存入 detail.thresholdScan，GET 详情读回与
    响应 data 一致（含 scannedAt）；预置哨兵旧结果再扫 → 被新结果覆盖。"""
    admin_login(client, session)
    run_id = _make_run(session, _scan_items())

    data = _scan(client, run_id).json()["data"]
    assert data["scannedAt"] == "2026-10-01T08:00:00+00:00"  # FakeClock 固定时刻

    detail = client.get(f"/api/admin/eval-runs/{run_id}").json()["data"]["detail"]
    assert detail["thresholdScan"] == data

    run = session.get(EvalRun, run_id)
    run.detail = {**detail, "thresholdScan": {"stale": True}}
    session.flush()
    data_again = _scan(client, run_id).json()["data"]
    reread = client.get(f"/api/admin/eval-runs/{run_id}").json()["data"]["detail"]
    assert reread["thresholdScan"] == data_again
    assert "stale" not in reread["thresholdScan"]


# ---------------------------------------------------------------------------
# ⑥ 状态闸：running 422/3004；全 pending/error 无可重放题目 422 指名
# ---------------------------------------------------------------------------


def test_running状态与全pending明细扫描422且零审计(client, session):
    """⑥ running 扫描 422/3004（后台正逐题写 detail，读改写互覆，与录分同闸）；
    done 但明细全 pending/error → 422/3004 指名"无可重放题目"；两分支均零审计。"""
    admin_login(client, session)
    running_id = _make_run(session, _scan_items()[:1], status="running")
    all_pending_id = _make_run(session, [_item(11), _item(12, error="上游超时")])

    running = _scan(client, running_id)
    assert running.status_code == 422
    assert running.json() == {
        "code": ERR_EVAL_STATE,
        "message": "评测任务未完成，暂不能扫描阈值",
        "data": None,
    }

    no_replay = _scan(client, all_pending_id)
    assert no_replay.status_code == 422
    assert no_replay.json()["code"] == ERR_EVAL_STATE
    assert "无可重放题目" in no_replay.json()["message"]

    assert (
        session.scalars(select(AuditLog).where(AuditLog.action == "eval.threshold_scan")).all()
        == []
    )


# ---------------------------------------------------------------------------
# ⑦ 审计 eval.threshold_scan 读回
# ---------------------------------------------------------------------------


def test_扫描审计读回(client, session):
    """⑦ 审计 eval.threshold_scan {runId}：object_type=eval_run、object_id=run
    id、actor=扫描 admin，与 detail 写入同事务落库。"""
    admin_login(client, session)
    run_id = _make_run(session, _scan_items())

    assert _scan(client, run_id).status_code == 200

    session.expire_all()
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "eval.threshold_scan"))
    assert audit is not None
    assert audit.object_type == "eval_run"
    assert audit.object_id == run_id
    assert audit.detail == {"runId": run_id}


# ---------------------------------------------------------------------------
# ⑧ 寻址 404 + member 403
# ---------------------------------------------------------------------------


def test_寻址不存在404_成员角色403(client, session):
    """⑧ 不存在的 run 404/3003；member 扫描 403/2004（AdminUser 门）。"""
    admin_login(client, session)
    missing = _scan(client, 999999)
    assert missing.status_code == 404
    assert missing.json()["code"] == ERR_NOT_FOUND
    assert missing.json()["message"] == "评测任务不存在"

    make_user(session, "evalmember", "right-one", role="member")
    assert login(client, "evalmember", "right-one").status_code == 200
    forbidden = _scan(client, 1)
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == ERR_FORBIDDEN
