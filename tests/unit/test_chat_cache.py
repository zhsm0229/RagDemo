r"""进程内缓存 / 每日限额 / 召回计数聚合器单元测试（Task 7，FakeClock 注入）。

断言清单来源 task-7-brief（①②③ 单元段；④ 真 pg 读回段见
tests/integration/test_hit_counter_flush.py，§15.4 写库必带读回）：
① ExactCache：make_key 精确格式 ``chat:{dept_id}:{question_normalized}``，同问题
  不同 dept 键不同；put→get 读回 entry 三字段相等；TTL 过期（clock 前拨 ttl+1s）
  get 返回 None 且条目被惰性清除；get 返回深拷贝，调用方改写不污染缓存。
② DailyCounter：连吃 limit 次后第 limit+1 次返回 False 且不计数（条目保持
  (当日, limit)）；跨日（clock 前拨 1 天）重置为可再消费；限额按 user_id 独立。
③ HitCounter：add 同 id 进程内累加；flush 一次 execute 携带全部参数组（一条 SQL
  executemany、参数绑定），按 id 升序携带 n 与注入时钟 now；提交成功才清空聚合，
  返回去重 chunk 数；空聚合 flush 返回 0 且不发 SQL；失败轮次聚合保留待重试。
④ 并发（评审修复轮）：GIL 切换间隔压至微秒级 + Barrier 对齐起跑的多线程
  stress——过期 get 无 KeyError 逃逸、同用户并发限额恰放行 limit 次、add 无丢
  更新；flush 的 commit 窗口内落下的 add 用确定性替身（commit() 里 add）复现，
  下一轮 flush 必须带上。
"""

import sys
import threading
from datetime import UTC, date, datetime

import pytest

from app.core.clock import FakeClock
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
TOMORROW = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)


def _clock() -> FakeClock:
    return FakeClock("2026-10-15T09:30:00+00:00")


@pytest.fixture()
def tiny_switch_interval():
    """把 GIL 切换间隔压到微秒级放大线程切换频率（stress 放大，把窗口型竞态逼出）。"""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


class _CaptureSession:
    """单元替身：记录 execute 调用与参数组，不做任何 IO（断言「一条 SQL」与绑定参数）。"""

    def __init__(self) -> None:
        self.statements: list[object] = []
        self.param_groups: list[object] = []
        self.committed = False

    def connection(self) -> "_CaptureSession":
        return self  # Core 通道复用本替身：execute/commit 记录照旧

    def execute(self, statement: object, params: object = None) -> None:
        self.statements.append(statement)
        self.param_groups.append(params)

    def commit(self) -> None:
        self.committed = True


class _ForbidSQLSession:
    """空聚合红线替身：flush 触碰任何会话方法即断言失败。"""

    def execute(self, statement: object, params: object = None) -> None:
        raise AssertionError("空聚合 flush 不得发起 SQL")

    def commit(self) -> None:
        raise AssertionError("空聚合 flush 不得提交")


class _ExplodingSession:
    """失败路径替身：execute 即抛，模拟 pg 不可用。"""

    def connection(self) -> "_ExplodingSession":
        return self

    def execute(self, statement: object, params: object = None) -> None:
        raise RuntimeError("pg 不可用")

    def commit(self) -> None:
        raise AssertionError("execute 已失败，不应走到提交")


class TestExactCacheKey:
    def test_make_key_exact_format(self) -> None:
        """make_key 精确等于 chat:{dept_id}:{规范化问题原文}。"""
        cache = ExactCache(_clock(), ttl_s=300)
        assert cache.make_key(3, "年假有几天") == "chat:3:年假有几天"

    def test_same_question_different_dept_produces_different_keys(self) -> None:
        """键碰撞用例：同问题不同部门，键必不同（部门隔离延伸到缓存键）。"""
        cache = ExactCache(_clock(), ttl_s=300)
        assert cache.make_key(1, "q") == "chat:1:q"
        assert cache.make_key(2, "q") == "chat:2:q"
        assert cache.make_key(1, "q") != cache.make_key(2, "q")

    def test_make_key_user_suffix_inserted_between_dept_and_question(self) -> None:
        """user_suffix 非空（§3.4 注入非空）：键插在部门与问题之间 chat:{dept}:u{id}:{q}。"""
        cache = ExactCache(_clock(), ttl_s=300)
        assert cache.make_key(3, "年假有几天", user_suffix="u7") == "chat:3:u7:年假有几天"

    def test_make_key_suffix_none_keeps_legacy_byte_identical(self) -> None:
        """user_suffix 缺省 None：键与历史形态逐字节相同（无记忆用户共享部门级缓存）。"""
        cache = ExactCache(_clock(), ttl_s=300)
        assert cache.make_key(3, "年假有几天") == cache.make_key(3, "年假有几天", user_suffix=None)
        assert cache.make_key(3, "年假有几天", user_suffix=None) == "chat:3:年假有几天"

    def test_make_key_user_suffix_separates_same_dept_users(self) -> None:
        """同部门两用户后缀不同：键必不同（带不同画像的两人对同题不互串答案）。"""
        cache = ExactCache(_clock(), ttl_s=300)
        assert cache.make_key(1, "q", user_suffix="u5") != cache.make_key(1, "q", user_suffix="u6")
        assert cache.make_key(1, "q", user_suffix="u5") != cache.make_key(1, "q")


class TestExactCacheRoundTrip:
    def test_put_then_get_returns_entry_fields(self) -> None:
        """put→get 读回 entry 三字段逐项相等（answer / citations / answer_type）。"""
        cache = ExactCache(_clock(), ttl_s=300)
        entry = {
            "answer": "年假 5 天",
            "citations": [{"chunk_id": 7, "page_no": 3}],
            "answer_type": "doc",
        }
        cache.put(cache.make_key(1, "年假"), entry)
        assert cache.get(cache.make_key(1, "年假")) == {
            "answer": "年假 5 天",
            "citations": [{"chunk_id": 7, "page_no": 3}],
            "answer_type": "doc",
        }

    def test_mutating_returned_entry_does_not_corrupt_cache(self) -> None:
        """get 返回深拷贝：调用方原地改写（含嵌套列表）不影响缓存内条目。"""
        cache = ExactCache(_clock(), ttl_s=300)
        key = cache.make_key(1, "q")
        cache.put(key, {"answer": "a", "citations": [1], "answer_type": "doc"})
        returned = cache.get(key)
        assert returned is not None
        returned["citations"].append("污染")
        returned["answer"] = "改写"
        assert cache.get(key) == {"answer": "a", "citations": [1], "answer_type": "doc"}

    def test_expired_get_returns_none_and_lazy_evicts(self) -> None:
        """TTL 过期（前拨 ttl+1s）：get 返回 None，且过期条目在本次 get 中被惰性清除。"""
        clock = _clock()
        cache = ExactCache(clock, ttl_s=300)
        key = cache.make_key(1, "q")
        cache.put(key, {"answer": "a", "citations": [], "answer_type": "doc"})
        clock.advance(seconds=301)
        assert cache.get(key) is None
        assert cache._entries == {}  # 惰性清除：过期条目不在缓存中滞留

    def test_entry_within_ttl_still_hit(self) -> None:
        """TTL 边界内（前拨 ttl-1s）仍命中，读回原文。"""
        clock = _clock()
        cache = ExactCache(clock, ttl_s=300)
        key = cache.make_key(1, "q")
        cache.put(key, {"answer": "a", "citations": [], "answer_type": "doc"})
        clock.advance(seconds=299)
        assert cache.get(key) == {"answer": "a", "citations": [], "answer_type": "doc"}


class TestExactCacheClear:
    def test_clear_drops_all_entries_within_ttl(self) -> None:
        """clear 整清（文档 retry/confirm 失效接线，W3 Task 1）：TTL 内条目一并丢弃，
        get 立即未命中——文档状态变化改变可检索事实，失效不等 TTL 自然过期。"""
        cache = ExactCache(_clock(), ttl_s=300)
        entry = {"answer": "a", "citations": [], "answer_type": "doc"}
        cache.put(cache.make_key(1, "年假"), entry)
        cache.put(cache.make_key(2, "病假"), entry)

        cache.clear()

        assert cache._entries == {}
        assert cache.get(cache.make_key(1, "年假")) is None
        assert cache.get(cache.make_key(2, "病假")) is None

    def test_clear_then_put_roundtrip_again(self) -> None:
        """clear 后缓存可继续 put→get 读回新条目（失效是清空而非销毁）。"""
        cache = ExactCache(_clock(), ttl_s=300)
        cache.put(cache.make_key(1, "q"), {"answer": "a", "citations": [], "answer_type": "doc"})
        cache.clear()

        cache.put(cache.make_key(1, "q"), {"answer": "新", "citations": [], "answer_type": "doc"})

        assert cache.get(cache.make_key(1, "q")) == {
            "answer": "新",
            "citations": [],
            "answer_type": "doc",
        }


class TestDailyCounter:
    def test_allows_limit_then_refuses_without_counting(self) -> None:
        """连吃 limit 次全 True；第 limit+1 次返回 False 且不计数（条目停在 (当日, limit)）。"""
        clock = _clock()
        counter = DailyCounter(clock)
        results = [counter.try_consume(42, limit=3) for _ in range(3)]
        assert results == [True, True, True]
        assert counter.try_consume(42, limit=3) is False
        assert counter._usage[42] == (date(2026, 10, 15), 3)  # 拒绝的这次没有 +1

    def test_refusal_repeats_but_never_recovers_same_day(self) -> None:
        """达限后同日继续询问一律 False（不因拒绝而放行）。"""
        counter = DailyCounter(_clock())
        for _ in range(2):
            counter.try_consume(7, limit=2)
        assert counter.try_consume(7, limit=2) is False
        assert counter.try_consume(7, limit=2) is False

    def test_cross_day_reset_allows_consumption_again(self) -> None:
        """跨日（clock 前拨 1 天）重置：昨日达限用户当日又可消费 limit 次。"""
        clock = _clock()
        counter = DailyCounter(clock)
        for _ in range(3):
            assert counter.try_consume(42, limit=3) is True
        assert counter.try_consume(42, limit=3) is False
        clock.advance(days=1)
        assert clock.now() == TOMORROW
        assert [counter.try_consume(42, limit=3) for _ in range(3)] == [True, True, True]
        assert counter.try_consume(42, limit=3) is False

    def test_quota_is_per_user(self) -> None:
        """限额按 user_id 独立：用户 A 达限不影响用户 B 首次消费。"""
        counter = DailyCounter(_clock())
        assert counter.try_consume(1, limit=1) is True
        assert counter.try_consume(1, limit=1) is False
        assert counter.try_consume(2, limit=1) is True


class TestHitCounterAggregate:
    def test_add_merges_duplicate_ids_and_flush_single_statement(self) -> None:
        """add([1,1,2]) 同 id 进程内累加；flush 一次 execute（一条 SQL）按 id 升序
        executemany，参数组携带 (id, n, 注入时钟 now)，返回去重 chunk 数 2。"""
        clock = _clock()
        counter = HitCounter(clock)
        counter.add([1, 1, 2])
        db = _CaptureSession()
        flushed = counter.flush(db)
        assert flushed == 2
        assert len(db.statements) == 1  # 一条 UPDATE 语句完成全部累加
        assert db.param_groups[0] == [
            {"chunk_id": 1, "n": 2, "now": NOW},
            {"chunk_id": 2, "n": 1, "now": NOW},
        ]
        assert db.committed is True

    def test_flush_clears_aggregation_after_success(self) -> None:
        """提交成功后聚合清空：再次 flush 返回 0 且不发 SQL（空聚合红线）。"""
        counter = HitCounter(_clock())
        counter.add([5])
        assert counter.flush(_CaptureSession()) == 1
        assert counter.flush(_ForbidSQLSession()) == 0

    def test_empty_flush_returns_zero_without_sql(self) -> None:
        """空聚合 flush：直接返回 0，不发起任何 SQL、不提交。"""
        assert HitCounter(_clock()).flush(_ForbidSQLSession()) == 0

    def test_failed_flush_keeps_aggregation_for_retry(self) -> None:
        """execute 失败原样上抛（吞异常是调度任务体的职责），聚合保留待下轮重试。"""
        counter = HitCounter(_clock())
        counter.add([3, 3])
        with pytest.raises(RuntimeError, match="pg 不可用"):
            counter.flush(_ExplodingSession())
        db = _CaptureSession()
        assert counter.flush(db) == 1
        assert db.param_groups[0] == [{"chunk_id": 3, "n": 2, "now": NOW}]  # 计数未丢失


class TestExactCacheConcurrency:
    def test_concurrent_expired_get_no_keyerror_escape(self, tiny_switch_interval) -> None:
        """评审竞态①：过期惰性清除并发 get——双线程同时 del 抛未捕获 KeyError（请求侧
        500）。16 线程 × 150 轮双 Barrier 对齐（每轮先 put 后前拨 ttl+1s，等全部
        get 完才进下一轮）：无异常逃逸、每轮 16 个结果全为 None。"""
        clock = _clock()
        cache = ExactCache(clock, ttl_s=300)
        key = "chat:1:q"
        workers, rounds = 16, 150
        errors: list[BaseException] = []
        results: list[dict | None] = []
        collect = threading.Lock()
        gate = threading.Barrier(workers + 1)
        done = threading.Barrier(workers + 1)

        def worker() -> None:
            for _ in range(rounds):
                gate.wait()
                try:
                    got = cache.get(key)
                except Exception as exc:  # noqa: BLE001 — 竞态逃逸检测：任何异常都要记下断言
                    with collect:
                        errors.append(exc)
                    got = None
                with collect:
                    results.append(got)
                done.wait()

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for _ in range(rounds):
            cache.put(key, {"answer": "a", "citations": [], "answer_type": "doc"})
            clock.advance(seconds=301)
            gate.wait()
            done.wait()
        for thread in threads:
            thread.join()
        assert errors == []
        assert results == [None] * (workers * rounds)


class TestDailyCounterConcurrency:
    def test_concurrent_try_consume_never_exceeds_limit(self, tiny_switch_interval) -> None:
        """评审竞态②：try_consume 的 check-then-act 并发——同用户并发提问可超限。
        8 线程 × 25 次（合计 200 次请求）limit=50：放行恰 50 次、拒绝恰 150 次。"""
        counter = DailyCounter(_clock())
        limit, workers, calls = 50, 8, 25
        gate = threading.Barrier(workers)
        results: list[bool] = []
        collect = threading.Lock()

        def worker() -> None:
            gate.wait()
            local = [counter.try_consume(42, limit=limit) for _ in range(calls)]
            with collect:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results.count(True) == limit
        assert results.count(False) == workers * calls - limit


class TestHitCounterConcurrency:
    def test_concurrent_add_no_lost_updates(self, tiny_switch_interval) -> None:
        """评审竞态③a：add 的读-改-写并发丢更新。8 线程 × 200 次 add([7])：flush
        读回 n == 1600（仍是一条 UPDATE、参数绑定）。"""
        counter = HitCounter(_clock())
        workers, rounds = 8, 200
        gate = threading.Barrier(workers)

        def worker() -> None:
            gate.wait()
            for _ in range(rounds):
                counter.add([7])

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        db = _CaptureSession()
        assert counter.flush(db) == 1
        assert db.param_groups[0] == [{"chunk_id": 7, "n": workers * rounds, "now": NOW}]
        assert db.committed is True

    def test_flush_preserves_adds_landed_during_commit_window(self) -> None:
        """评审竞态③b（确定性复现，无需真并发）：flush 的 commit 窗口内请求线程
        add 的计数不得被 clear 静默丢弃——替身 session 在 commit() 里模拟一次并发
        add([9])，下一轮 flush 必须把 [9] 带上。"""
        counter = HitCounter(_clock())
        counter.add([7])

        class _AddDuringCommitSession:
            """execute 照常；commit() 触发时模拟另一线程恰好完成一次 add([9])。"""

            def connection(self) -> "_AddDuringCommitSession":
                return self

            def execute(self, statement: object, params: object = None) -> None:
                pass

            def commit(self) -> None:
                counter.add([9])

        assert counter.flush(_AddDuringCommitSession()) == 1
        db = _CaptureSession()
        assert counter.flush(db) == 1
        assert db.param_groups[0] == [{"chunk_id": 9, "n": 1, "now": NOW}]


# ---------------------------------------------------------------------------
# 按 key 精准失效（工单 23）：clear_by_ref 替代整清——变更只淘汰引用该资源的条目
# ---------------------------------------------------------------------------


def test_clear_by_ref_只删引用目标资源的条目() -> None:
    """两条缓存分别引用 doc 7 与 doc 9：clear_by_ref("doc", 7) 只删前者，
    doc 9 条目保留（仍可命中）——文档变更不再拖累全缓存命中率。"""
    cache = ExactCache(_clock(), ttl_s=300)
    cache.put("k:doc7", {"answer": "A", "citations": [{"refType": "doc", "refId": 7}]})
    cache.put("k:doc9", {"answer": "B", "citations": [{"refType": "doc", "refId": 9}]})

    removed = cache.clear_by_ref("doc", 7)

    assert removed == 1
    assert cache.get("k:doc7") is None
    assert cache.get("k:doc9") == {"answer": "B", "citations": [{"refType": "doc", "refId": 9}]}


def test_clear_by_ref_同条目多引用任一命中即删() -> None:
    """一条回答引用多资源（doc 7 + qa 3）：按任一被变更资源失效——引用关系是集合。"""
    cache = ExactCache(_clock(), ttl_s=300)
    cache.put(
        "k:mix",
        {"citations": [{"refType": "doc", "refId": 7}, {"refType": "qa", "refId": 3}]},
    )
    assert cache.clear_by_ref("qa", 3) == 1
    assert cache.get("k:mix") is None


def test_clear_by_ref_无citations条目保守删除() -> None:
    """citations 缺席/空（形态漂移兜底）：保守删除（fail-safe）——宁可多失效不留旧答。"""
    cache = ExactCache(_clock(), ttl_s=300)
    cache.put("k:odd", {"answer": "X"})
    assert cache.clear_by_ref("doc", 7) == 1
    assert cache.get("k:odd") is None


def test_clear_by_ref_无命中返回零且不影响其余() -> None:
    cache = ExactCache(_clock(), ttl_s=300)
    cache.put("k:doc9", {"citations": [{"refType": "doc", "refId": 9}]})
    assert cache.clear_by_ref("doc", 999) == 0
    assert cache.get("k:doc9") is not None
