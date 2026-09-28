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
    return Settings()
