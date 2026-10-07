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
    embed_batch_chars: int = 1200  # 入库嵌入分批的字符预算/批：TEI --max-batch-tokens 2048
    # （防 OOM 特意调低，见 runbook）下单请求超限即 422——2026-10-02 大文档全量单请求全败根因；
    # BGE tokenizer 中文≈1字1token，1200 留 40% 余量。0=不分批（仅测试用）
    rerank_base_url: str = "http://localhost:8082"  # TEI rerank（bge-reranker-v2-m3）
    rerank_timeout_s: int = 10
    mineru_base_url: str = ""  # 空=pypdf 降级路径
    mineru_timeout_s: int = 300
    convert_base_url: str = ""  # Gotenberg 老格式转换服务（doc/ppt/xls→OOXML）；空=老格式拒收
    convert_timeout_s: int = 120
    image_ocr_enabled: bool = (
        False  # 内嵌图片 OCR（F-1017）：抽取图片→MinerU→文字块；关闭=行为与现状一致
    )
    llm_base_url: str = ""  # OpenAI 兼容端点；非 test_mode 必填，为空装配期 fail-fast（§6.4）
    llm_api_key: str = ""  # 云端点鉴权密钥；严禁提交真实密钥
    llm_model: str = ""  # 对话模型名（如 deepseek-flash）
    llm_effort: str = "low"  # 推理力度（deepseek-flash: low/high/max）：RAG 证据型问答
    # 正文优先，默认 low——high 会把 max_tokens 全烧在 reasoning 上致正文为空（2026-10-01 事故）
    llm_timeout_s: int = 60
    chat_max_tokens: int = 4096  # 回答生成 max_tokens 上限
    prompt_version: str = "v1"  # 提示词包版本（对应 prompts/ 头注释 # version）
    daily_question_limit: int = 200  # 单用户每日提问上限
    qa_threshold: float = 0.85  # 问答对准入阈值（embedding 余弦相似度 ≥ 此值才走问答对直答；不得与 rerank 分混用，§4.4）
    refuse_threshold: float = 0.55  # 拒答阈值；两套阈值不得混用（standards §4.4）
    cache_ttl_s: int = 300  # 问答结果缓存 TTL（秒）
    retriever_top_k: int = 20  # 向量检索候选数 top_k
    rerank_top_n: int = 5  # 重排后保留条数 top_n
    chunk_dedup_sim: float = 0.98  # 检索结果去重相似度阈值
    hnsw_ef_search: int = 40  # pgvector HNSW 查询 ef_search
    message_retention_days: int = 90  # 提问记录保留天数，超过即归档停用（两段式第一阶段，§7.3）
    message_cleanup_cooldown_days: int = 30  # 归档冷却天数，期满物理清理（两段式第二阶段，§4.6）
    # ---- 长期记忆（F-210，agent-evolution §三）：两项为代码缺省，非部署必填项，不进 .env.example ----
    memory_max_per_user: int = (
        30  # 单用户记忆条数上限（§3.2）：写入超限淘汰最旧（is_active=false 软删，不物理删）
    )
    memory_inject_limit: int = (
        10  # 问答注入上限（§3.3）：取 is_active 最近 ≤N 条时间倒序并入 <user_memory> 标签
    )
    # ---- Agent 演进模型档位（F-211，工单 #04）：三项留空即复用主问答通道（同档位），不进 .env.example ----
    agent_model_base_url: str = (
        ""  # Agent 模型 OpenAI 兼容端点；留空复用 llm_base_url（升档位时才单独配）
    )
    agent_model_api_key: str = ""  # Agent 模型密钥；留空复用 llm_api_key；严禁提交真实密钥
    agent_model_model: str = ""  # Agent 模型名；留空复用 llm_model
    agent_model_timeout_s: int = 60  # Agent 模型调用超时（chat 纪律与主通道一致，standards §1.4-5）
    agent_model_react_fallback: bool = (
        True  # 模型无原生 tool-calling 时启用 ReAct 文本协议兜底解析（app/core/agent_model.py）
    )
    test_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    # 缓存契约：lru_cache 首次调用即钉死当时的 env/.env 读数。测试中修改
    # DATABASE_URL 等环境变量后，必须先 get_settings.cache_clear() 再断言/使用，
    # 否则会拿到旧缓存（见 tests/conftest.py pg fixture 的显式清除与还原）。
    return Settings()
