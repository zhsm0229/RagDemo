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
    mineru_base_url: str = ""  # 空=pypdf 降级路径
    mineru_timeout_s: int = 300
    test_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    # 缓存契约：lru_cache 首次调用即钉死当时的 env/.env 读数。测试中修改
    # DATABASE_URL 等环境变量后，必须先 get_settings.cache_clear() 再断言/使用，
    # 否则会拿到旧缓存（见 tests/conftest.py pg fixture 的显式清除与还原）。
    return Settings()
