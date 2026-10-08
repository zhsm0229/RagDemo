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


def test_w2_qa_chain_defaults(monkeypatch):
    """W2 问答链路新键缺省值：与 task-2-brief 接口表逐字一致（W3 Task 1 起不含
    generation_temperature——I-2 修复后 manifest.temperature 为唯一生效源）。"""
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    s = Settings(_env_file=None)
    assert s.rerank_base_url == "http://localhost:8082"
    # 120：CPU TEI 实测口径（20 条 OCR 长块稳定超 10s，模板生成 400 实证后上调；metrics-balance 前波变更，断言随默认值演进）
    assert s.rerank_timeout_s == 120
    assert s.llm_base_url == ""
    assert s.llm_api_key == ""
    assert s.llm_model == ""
    assert s.llm_timeout_s == 60
    assert s.chat_max_tokens == 4096
    assert s.daily_question_limit == 200
    assert s.qa_threshold == 0.85
    assert s.refuse_threshold == 0.55
    assert s.cache_ttl_s == 300
    assert s.retriever_top_k == 20
    assert s.rerank_top_n == 5
    assert s.chunk_dedup_sim == 0.98
    assert s.hnsw_ef_search == 40
    assert s.prompt_version == "v1"


def test_generation_temperature_dropped_and_env_key_ignored(monkeypatch):
    """W3 Task 1 Step 5：死配置 generation_temperature 已删除（I-2 修复后
    manifest.temperature 为唯一生效源，字段无消费者）；.env 残留
    GENERATION_TEMPERATURE 键经 extra="ignore" 不破坏加载。"""
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("GENERATION_TEMPERATURE", "0.7")

    s = Settings(_env_file=None)

    assert not hasattr(s, "generation_temperature")
