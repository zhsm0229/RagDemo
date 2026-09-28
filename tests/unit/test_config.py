import pytest

from app.core.config import Settings


def test_missing_required_field_raises(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    # pydantic ValidationError 中字段名为小写 jwt_secret，故用不区分大小写匹配
    with pytest.raises(Exception, match="(?i)JWT_SECRET"):
        Settings(_env_file=None)  # 缺必需配置必须 fail-fast（standards §6.4）


def test_defaults_load(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    s = Settings(_env_file=None)
    assert s.database_url.startswith("postgresql")
    assert s.access_token_expire_minutes == 480  # 8h
