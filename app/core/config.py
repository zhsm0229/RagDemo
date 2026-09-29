from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5433/rag"
    jwt_secret: str  # 必需，缺失拒启
    access_token_expire_minutes: int = 480
    storage_dir: str = "./data/uploads"
    embed_base_url: str = "http://localhost:8081"  # TEI
    embed_timeout_s: int = 10
    rerank_base_url: str = "http://localhost:8082"  # TEI rerank（bge-reranker-v2-m3）
    rerank_timeout_s: int = 10
    mineru_base_url: str = ""  # 空=pypdf 降级路径
    mineru_timeout_s: int = 300
    llm_base_url: str = ""  # OpenAI 兼容端点；非 test_mode 必填，为空装配期 fail-fast（§6.4）
    llm_api_key: str = ""  # 云端点鉴权密钥；严禁提交真实密钥
    llm_model: str = ""  # 对话模型名（如 glm-4）
    llm_timeout_s: int = 60
    chat_max_tokens: int = 4096  # 回答生成 max_tokens 上限
    generation_temperature: float = 0.2  # 与 prompts/manifest.json 同步版本化（§17.4）
    prompt_version: str = "v1"  # 提示词包版本（对应 prompts/ 头注释 # version）
    daily_question_limit: int = 200  # 单用户每日提问上限
    qa_threshold: float = 0.85  # 问答对准入阈值（embedding 余弦相似度 ≥ 此值才走问答对直答；不得与 rerank 分混用，§4.4）
    refuse_threshold: float = 0.55  # 拒答阈值；两套阈值不得混用（standards §4.4）
    cache_ttl_s: int = 300  # 问答结果缓存 TTL（秒）
    retriever_top_k: int = 20  # 向量检索候选数 top_k
    rerank_top_n: int = 5  # 重排后保留条数 top_n
    chunk_dedup_sim: float = 0.98  # 检索结果去重相似度阈值
    hnsw_ef_search: int = 40  # pgvector HNSW 查询 ef_search
    test_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    # 缓存契约：lru_cache 首次调用即钉死当时的 env/.env 读数。测试中修改
    # DATABASE_URL 等环境变量后，必须先 get_settings.cache_clear() 再断言/使用，
    # 否则会拿到旧缓存（见 tests/conftest.py pg fixture 的显式清除与还原）。
    return Settings()
