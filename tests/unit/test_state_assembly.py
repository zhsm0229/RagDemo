r"""进程内状态装配测试（工单 19 设计 §7.3，不触库）：StateBundle 工厂按
Settings.state_backend 选择 memory/pg 实现，test_mode 恒 memory（conftest reset()
测试基建建立在进程内语义上，设计 §8 裁决 3）；路由接线三处显式传参断言 bundle
实例被触达（login 经 limiter / logout、get_current_user 经 blacklist）。

隔离口径：本文件零 DB 访问——pg 分支仅构造实现类（引擎惰性不建连）；路由接线
以 spy/自建实例断言传参路径，不走 lifespan。
"""

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.api import auth as auth_router
from app.api import deps as api_deps
from app.core.clock import SystemClock
from app.core.config import Settings, get_settings
from app.core.errors import ERR_RATE_LIMITED, ApiError
from app.core.security import JtiBlacklist, create_token, decode_token
from app.db.models import User
from app.main import StateBundle, _build_chat_deps, _build_state
from app.services.auth import SlidingWindowLimiter
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter


def _settings(**overrides: object) -> Settings:
    return Settings(jwt_secret="state-assembly-test", **overrides)  # type: ignore[arg-type]


def _bundle(**overrides: object) -> StateBundle:
    """手搓 StateBundle：可注入 spy 替身（路由接线断言触达用）。"""
    clock = SystemClock()
    fields: dict[str, object] = {
        "cache": ExactCache(clock, 300),
        "counter": DailyCounter(clock),
        "hit_counter": HitCounter(clock),
        "limiter": SlidingWindowLimiter(60, 10),
        "blacklist": JtiBlacklist(),
    }
    fields.update(overrides)
    return StateBundle(**fields)  # type: ignore[arg-type]


def _request(app: FastAPI, cookie: str | None = None) -> Request:
    headers = [(b"cookie", f"jwt={cookie}".encode())] if cookie else []
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/auth",
            "headers": headers,
            "query_string": b"",
        }
    )


# ---------- _build_state 后端选择 ----------


def test_缺省装配五件均memory类() -> None:
    """state_backend 缺省 memory：五件均进程内实现（limiter/blacklist 复用模块级
    单例——conftest 的 reset() 契约建立在同一对象上，测试隔离零改动）。"""
    from app.core.security import default_blacklist
    from app.services.auth import default_limiter

    state = _build_state(_settings())
    assert type(state.cache) is ExactCache
    assert type(state.counter) is DailyCounter
    assert type(state.hit_counter) is HitCounter
    assert state.limiter is default_limiter
    assert state.blacklist is default_blacklist


def test_state_backend_pg装配五件均Pg类() -> None:
    from app.services.state_pg import PgDailyCounter, PgExactCache, PgHitCounter
    from app.services.state_pg_auth import PgJtiBlacklist, PgSlidingWindowLimiter

    state = _build_state(_settings(state_backend="pg"))
    assert type(state.cache) is PgExactCache
    assert type(state.counter) is PgDailyCounter
    assert type(state.hit_counter) is PgHitCounter
    assert type(state.limiter) is PgSlidingWindowLimiter
    assert type(state.blacklist) is PgJtiBlacklist


def test_test_mode强制memory_env注pg也memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """test_mode=True 时无视 state_backend 强制 memory（构造参数与环境变量两形态）。"""
    forced = _build_state(_settings(test_mode=True, state_backend="pg"))
    assert type(forced.cache) is ExactCache

    monkeypatch.setenv("STATE_BACKEND", "pg")
    via_env = _build_state(_settings(test_mode=True))
    assert type(via_env.cache) is ExactCache
    assert type(via_env.limiter) is SlidingWindowLimiter


# ---------- ChatDeps 复用传入 state ----------


def test_build_chat_deps复用传入state实例() -> None:
    """_build_chat_deps(settings, state) 三字段 is 同一实例（装配唯一点，不二次构建）。"""
    pg_state = _build_state(_settings(state_backend="pg"))
    deps = _build_chat_deps(_settings(test_mode=True), pg_state)
    assert deps.cache is pg_state.cache
    assert deps.counter is pg_state.counter
    assert deps.hit_counter is pg_state.hit_counter


def test_build_chat_deps缺省自建state() -> None:
    """state 缺省自建：scheduler 周评测与启动补偿调用点零改动的契约面。"""
    deps = _build_chat_deps(_settings(test_mode=True))
    assert type(deps.cache) is ExactCache


# ---------- 路由接线（显式传参，消除缺省解析的隐式全局） ----------


def test_login路由经bundle_limiter() -> None:
    """login 路由把 bundle.limiter 传给服务层：spy 返回 False → 429 短路
    （限流门在查库之前，db 形参传 None 即可证明触达）。"""
    touched: list[str] = []

    class _SpyLimiter:
        def hit(self, key: str) -> bool:
            touched.append(key)
            return False

        def reset(self) -> None:
            return None

    app = FastAPI()
    app.state.state_bundle = _bundle(limiter=_SpyLimiter())
    request = _request(app)
    body = auth_router.LoginRequest(username="alice", password="password-123")
    with pytest.raises(ApiError) as exc_info:
        auth_router.login(body, request, None)  # type: ignore[arg-type]
    assert exc_info.value.code == ERR_RATE_LIMITED
    assert exc_info.value.http_status == 429
    assert touched == ["None|alice"]  # ip 缺省 None → 键 "None|alice"


def test_logout路由经bundle_blacklist() -> None:
    """logout 路由把 bundle.blacklist 传给服务层：有效令牌登出后 jti 记入该实例。"""
    blacklist = JtiBlacklist()
    app = FastAPI()
    app.state.state_bundle = _bundle(blacklist=blacklist)
    now = SystemClock().now()
    token = create_token(7, "member", 2, 30, now, get_settings().jwt_secret)
    claims = decode_token(token, get_settings().jwt_secret, blacklist)
    assert claims is not None  # 前置：令牌本身有效

    auth_router.logout(_request(app, cookie=token))

    assert blacklist.revoked(claims.jti) is True


def test_get_current_user经bundle_blacklist(monkeypatch: pytest.MonkeyPatch) -> None:
    """deps.get_current_user 把 bundle.blacklist 传给 decode_token：spy 记录 revoked
    查询（get_user_by_id 打桩返回在册用户，零 DB）。"""
    revoked_calls: list[str] = []

    class _SpyBlacklist:
        def revoke(self, jti: str, exp: float) -> None:
            raise AssertionError("get_current_user 不得 revoke")

        def revoked(self, jti: str) -> bool:
            revoked_calls.append(jti)
            return False

        def reset(self) -> None:
            return None

    app = FastAPI()
    app.state.state_bundle = _bundle(blacklist=_SpyBlacklist())
    now = SystemClock().now()
    token = create_token(7, "member", 2, 30, now, get_settings().jwt_secret)
    monkeypatch.setattr(
        api_deps,
        "get_user_by_id",
        lambda db, user_id: User(
            id=user_id, username="alice", role="member", dept_id=2, is_active=True
        ),
    )
    user = api_deps.get_current_user(_request(app, cookie=token), None)  # type: ignore[arg-type]
    assert user.id == 7
    assert len(revoked_calls) == 1  # decode_token 经 bundle.blacklist 查询过黑名单
