r"""进程内缓存 / 每日限额 / 召回计数聚合器单元测试（Task 7，FakeClock 注入）。

断言清单来源 task-7-brief（①②③ 单元段；④ 真 pg 读回段见
tests/integration/test_chat_cache.py，§15.4 写库必带读回）：
① ExactCache：make_key 精确格式 ``chat:{dept_id}:{question_normalized}``，同问题
  不同 dept 键不同；put→get 读回 entry 三字段相等；TTL 过期（clock 前拨 ttl+1s）
  get 返回 None 且条目被惰性清除；get 返回浅拷贝，调用方改写不污染缓存。
② DailyCounter：连吃 limit 次后第 limit+1 次返回 False 且不计数（条目保持
  (当日, limit)）；跨日（clock 前拨 1 天）重置为可再消费；限额按 user_id 独立。
③ HitCounter：add 同 id 进程内累加；flush 一次 execute 携带全部参数组（一条 SQL
  executemany、参数绑定），按 id 升序携带 n 与注入时钟 now；提交成功才清空聚合，
  返回去重 chunk 数；空聚合 flush 返回 0 且不发 SQL；失败轮次聚合保留待重试。
"""

from datetime import UTC, date, datetime

import pytest

from app.core.clock import FakeClock
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
TOMORROW = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)


def _clock() -> FakeClock:
    return FakeClock("2026-10-15T09:30:00+00:00")


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
        """get 返回浅拷贝：调用方原地改写（含嵌套列表）不影响缓存内条目。"""
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
