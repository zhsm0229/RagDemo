from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5433/rag"
    jwt_secret: str  # 必需，缺失拒启
    access_token_expire_minutes: int = 480
    storage_dir: str = "./data/uploads"
    embed_base_url: str = "http://localhost:8081"  # TEI
    embed_timeout_s: int = (
        60  # embed 超时（2026-11-01 由 10 上调，known-issues 开放-1 同款豁免）：10s 按
        # 单条查询向量化设的纪律值，但入库批量（分批 ≤256 条）在 CPU TEI 上队列堆积
        # 实测单批 4-8s+排队可超 10s（三份模板草稿批量入库 ReadTimeout 事故）；查询期
        # 单条 <1s 不受影响，TEI 故障时失败变慢由熔断/重试兜底。GPU 后回调（同 rerank）
    )
    embed_batch_chars: int = 1200  # 入库嵌入分批的字符预算/批：TEI --max-batch-tokens 2048
    # （防 OOM 特意调低，见 runbook）下单请求超限即 422——2026-10-02 大文档全量单请求全败根因；
    # BGE tokenizer 中文≈1字1token，1200 留 40% 余量。0=不分批（仅测试用）
    rerank_base_url: str = "http://localhost:8082"  # TEI rerank（bge-reranker-v2-m3）
    rerank_timeout_s: int = 120  # CPU TEI 实测 20 条×190 字 ≈ 110s（推理 ~30s + 排队，
    # known-issues 开放-1 算力不足）：10s 纪律值在该部署形态下不可达，超时即全军覆没；
    # 部署机换 GPU 后应收敛回纪律值并复测
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
    fallback_refuse_threshold: float = (
        0.63  # 降级态（rerank 故障/熔断 → 向量序兜底）拒答阈值，与 refuse_threshold
        # 分轨不互比（§4.4）：取开放-2 对照命中带 0.63-0.71 下沿为保守下限，实测后按
        # 降级态负例校准（retrieval-policy §3/§11 裁决 7）
    )
    rerank_breaker_enabled: bool = (
        True  # rerank 熔断总闸（retrieval-policy §3）：手动关闭恒 closed 不记失败
    )
    cache_ttl_s: int = 300  # 问答结果缓存 TTL（秒）
    state_backend: Literal["memory", "pg"] = (
        "memory"  # 进程内状态后端（工单 19）：memory=进程内字典（单实例，缺省）/
        # pg=外置三张 state_* 表（问答缓存/日限额/召回暂存/登录限流/登出黑名单，
        # 跨实例共享，工单 20 多副本前置）。test_mode 恒 memory（测试 reset() 契约
        # 建立在进程内语义上）。【部署可选：缺省 memory】
    )
    db_pool_size: int = (
        5  # SQLAlchemy 连接池常驻连接数（评审 P2：缺省维持现值零变化；多副本/高并发
        # 部署按并发档位调大——上限=同步端点线程池，配合 max_overflow 突发缓冲）
    )
    db_max_overflow: int = (
        10  # 连接池突发溢出上限（超出 pool_size 的临时连接数）；压测后与 pool_size 一并标定
    )
    retriever_top_k: int = 20  # 向量检索候选数 top_k
    rerank_top_n: int = 5  # 重排后保留条数 top_n
    chunk_dedup_sim: float = 0.98  # 检索结果去重相似度阈值
    hnsw_ef_search: int = 40  # pgvector HNSW 查询 ef_search
    compute_tier: str = (
        "cpu_constrained"  # 算力档位（retrieval-policy §2）：cpu_constrained（本机 CPU
        # TEI 缺省）/ gpu（Mac Mini 部署档）——multi_query 等 A 类重手段的解锁闸
        # （spec §1），消费方 app/rag/retrieval_policy.resolve_policy；代码缺省即现状，
        # 与 agent_model_* 同款不进 .env.example 部署必填清单
    )
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
    agent_model_timeout_s: int = 300  # Agent 模型单次调用超时：非流式等全量补全（模板生成
    # 一轮实测 2-5 分钟），不适用流式 chat 的 60s 首字纪律（standards §1.4-5 的流式口径）；
    # 超时后由编排层按 5001 收口，前端看门狗（150s 停滞/600s 总）宽于该值
    agent_model_react_fallback: bool = (
        True  # 模型无原生 tool-calling 时启用 ReAct 文本协议兜底解析（app/core/agent_model.py）
    )
    agent_run_stale_minutes: int = (
        45  # agent 运行幽灵清扫阈值（分钟，A4）：running 且 started_at 超过此时长的
        # agent_runs 视为进程中断遗留的幽灵行，由定时任务置 failed（error=运行超时被清扫）。
        # 须 ≥ 长任务合法时长（agent_model_timeout_s=300s/轮 × 多轮迭代 + 工具重试），
        # 15min 会把在途的模板生成误杀（2026-10-07 实测教训）
    )
    eval_run_stale_minutes: int = (
        120  # 评测 run 幽灵清扫阈值（分钟，商用就绪工单 06）：running 且 created_at
        # 超过此时长的 eval_runs 视为崩溃遗留幽灵行，定时任务置 failed——running 占住
        # [自动] 幂等键会让本周三视角跑分静默缺失。≥ 跑分合法时长（题量 × 单题
        # rerank+LLM 最长链路），部署机按题量调大
    )
    # ---- 评测集自动更新（评测自动闭环）：总闸缺省关=行为与现状一致；预算项运行时 ----
    # 读取（改配置无需重装调度器），纯内部阈值不进 .env.example（同 agent_model_* 口径）
    eval_auto_enabled: bool = (
        False  # 评测自动闭环总闸：难例合成流水（周日 04:00）与三视角自动跑分（周日
        # 05:00）的启停开关；部署稳定后经 .env 覆盖开启【部署可选：缺省 false】
    )
    eval_auto_synth_per_seed: int = 2  # 每个难例种子最多合成的变体题数（LLM 预算闸一）
    eval_auto_doc_sample_per_dept: int = 20  # 文档造题通道每部门每周抽样知识块数（预算闸二）
    eval_auto_weekly_max_questions: int = 100  # 每周自动新增评测题总量上限（预算闸三）
    eval_auto_regression_pp: float = (
        5.0  # 退化告警阈值（百分点）：同口径相邻两轮 accuracy 降幅超过该值即在报告
        # detail 存 regression 标记、健康卡标红
    )
    # ---- 分级限流（工单 21）：总闸缺省关=行为与现状一致（灰度由部署决策）；纯内部 ----
    # 阈值不进 .env.example（同 agent_model_* 口径），调整走评审
    rate_limiter_enabled: bool = (
        False  # 令牌桶分级限流总闸（三层限流第 2 层）：chat/agent 两入口秒级平滑
        # 限速；开启需单实例/PG 状态外置口径见 deploy-requirements §7【部署可选：缺省 false】
    )
    rate_chat_per_min: int = 30  # 问答平滑速率（次/分钟/用户）
    rate_chat_burst: int = 10  # 问答突发容量（令牌桶深度）
    rate_agent_per_min: int = 10  # agent 平滑速率（长任务入口更紧）
    rate_agent_burst: int = 4  # agent 突发容量
    # ---- 调度器独立（工单 20）：缺省 true=现形态零变化；scale 拓扑下 api 副本置 false ----
    scheduler_enabled: bool = (
        True  # 定时任务与启动补偿的启停开关（build_scheduler 启动闸 + catchup
        # leader-gate + entrypoint 自愈段共用）：多副本部署时仅调度副本开启，
        # api 副本置 false【部署可选：缺省 true】
    )
    test_mode: bool = False
    static_dir: str = (
        ""  # 前端 dist 托管目录（§12.2 生产仅 app 发布 80 端口）：空=不挂载（本机 dev/
        # CI test_mode）；容器经 ENV 注入 /srv/ragdemo/static。原 os.getenv 直读不进
        # .env 解析（评审 P2 形态差异），并入 Settings 后一处读法
    )


@lru_cache
def get_settings() -> Settings:
    # 缓存契约：lru_cache 首次调用即钉死当时的 env/.env 读数。测试中修改
    # DATABASE_URL 等环境变量后，必须先 get_settings.cache_clear() 再断言/使用，
    # 否则会拿到旧缓存（见 tests/conftest.py pg fixture 的显式清除与还原）。
    return Settings()
