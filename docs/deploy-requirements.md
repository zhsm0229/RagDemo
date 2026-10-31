# 部署需求文档（企业智能工作台 RAG 问答）

> 版本：2026-10-02。适用：试点/小规模商用单机部署。
> 上游规范：standards.md §12.2（部署形态）、§13.2（发布清单）；运维操作见 docs/runbook.md。
> 本文档回答三件事：**要什么硬件、选什么 LLM 轨道、怎么自动化部署**。数据来源除标注外均为本机实测。

---

## 1. 部署形态与组件清单

单机 Docker Compose，常驻容器：`app`（FastAPI+前端）、`pg`（pgvector:pg15）、`tei-embed`（BGE-M3 向量化）、`tei-rerank`（bge-reranker-v2-m3 重排）、`mineru`（PDF 解析）、`gotenberg`（老格式 doc/ppt/xls 自动转换，2026-10-06 新增，常驻 ~300-500MB 内存）；LLM 走云端 API（本地不跑大模型，见 §3）：

| 容器 | 镜像 | 职责 | 镜像/模型占用 |
|------|------|------|--------------|
| app | ragdemo-app（自建，330MB） | FastAPI 后端 + 前端 dist 托管（8000 端口） | — |
| pg | pgvector/pgvector:pg15（612MB） | 文档/向量/业务库 | 数据随业务增长（试点 <2GB/年） |
| tei-embed | TEI cpu-1.9.2（938MB） | BGE-M3 向量化 | 模型 4.3GB（挂载 data/tei-models） |
| tei-rerank | 同上 | bge-reranker-v2-m3 重排 | 模型 4.3GB（同上） |
| mineru | ragdemo-mineru（自建，**11.6GB**） | PDF 高精度解析（可按需启停） | 模型内置于镜像 |
| gotenberg | gotenberg/gotenberg（~1GB，2026-10-06 新增） | 老格式（doc/ppt/xls）LibreOffice 自动转换为 OOXML（F-1015） | — |

LLM：DeepSeek 云 API（`api.deepseek.com`，OpenAI 兼容）。**本地不部署大模型时的 GPU 需求为零**；本地化方案见 §3。

生产 mem_limit（docker-compose.prod.yml）：app 2g / pg 3g / tei-embed 3g / **tei-rerank 4.5g**（CPU candle 后端加载+预热峰值，对 §12.5 GPU 口径 1C3G 的偏差登记；GPU TEI 到位后调回）/ mineru 6g / gotenberg 1g。

## 2. 硬件需求（实测）

| 维度 | 最低 | 推荐 | 实测依据 |
|------|------|------|---------|
| 内存 | **12GB**（mineru 按需启停） | 24GB（全组件常驻） | 常驻合计 ≈10GB：app 0.15G + pg 0.06G + embed 2.8G + rerank 3.6G + mineru 3.4G；8G 实证三巨头互挤 OOM（2026-10-02 事故） |
| 磁盘 | 50GB | 200GB+ | 镜像 13.5GB + TEI 模型 8.6GB + 数据/备份；1TB 空间数年无忧 |
| CPU | 4 核（能跑） | **≥8 核**（重排多核并行，实测 400%+ CPU） | CPU 重排是问答延迟大头：top_k=8 时 15-60s/问（内存充足时） |
| GPU | 无（可选） | 入门卡 4-8GB 显存（T4/4060 级）跑 GPU 版 TEI | 重排 <1s/问（CPU 的 1/30），规范 §1.5 的 10s SLA 前提即 GPU TEI |
| 网络 | 外网可达 `api.deepseek.com`（云轨必需） | — | 出网范围见合规确认单 |
| 系统 | Linux 原生（Ubuntu 22.04+）优于 Windows+WSL2 | — | WSL2 多一层内存/IO 开销 |
| CPU 架构 | **linux/amd64（x86_64）** | — | 仅 amd64：TEI 官方镜像无 arm64 manifest，arm64 服务器（含国产化 ARM 平台）`docker compose pull` 会直接失败——需换 TEI 基镜像（临时方案见 deploy/mac-setup/README.md §7，正解=自构建 arm64 基镜像后钉版），见 2026-10-31 评审 P2 |

**Mini 主机 24G 内存 + 1TB 磁盘：可正常运行**，内存磁盘均远超需求；唯一要认真选的是 **CPU 型号**（N100 类 4 核小主机重排延迟 30-90s；7840HS/i5-1240P 级 8 核 15-40s；要商用级 10s 内响应须加 GPU TEI）。

## 3. LLM 轨道

### 3.1 云轨（默认，当前配置）

`.env` 三键：`LLM_BASE_URL=https://api.deepseek.com/v1`、`LLM_API_KEY=<密钥>`、`LLM_MODEL=deepseek-flash`、`LLM_EFFORT=low`（high 会把 max_tokens 烧在推理上致正文为空——2026-10-01 事故）。优点：质量最优、零算力占用；代价：出网（合规已确认）、按量计费、依赖外网。

### 3.2 本地大模型轨（数据不出内网时）

接入点：任何 **OpenAI 兼容端点**（app 侧只改 `.env` 三键，代码零改动；`LLM_EFFORT` 为 DeepSeek 专属参数，本地端点缺席自动不下发）。

**推理服务选型**：

| 服务 | 适用 | 说明 |
|------|------|------|
| **Ollama**（推荐） | 迷你主机 / CPU / 单卡，运维最轻 | OpenAI 兼容 `/v1`，模型一条命令拉取 |
| vLLM | 有 GPU 且要求并发吞吐 | OpenAI 原生，显存管理重，企业级 |
| Xinference / LocalAI | 多模型统一管理 | 按需 |

**模型选型**（中文 RAG 回答生成场景；均为 Q4 量化）：

| 档位 | 模型 | 体积 | 运行资源 | 预期生成速度 |
|------|------|------|---------|-------------|
| **首选** | `qwen2.5:7b-instruct-q4_K_M` | ~4.7GB | 显存 ~6GB 或内存 ~6GB | GPU 30-60 tok/s（单答 5-15s）；纯 CPU 8 核 5-15 tok/s（单答 30-90s） |
| 备选 | `glm4:9b-chat` | ~5.5GB | 同上略高 | 中文对话表现好 |
| 进阶（仅 GPU） | `qwen2.5:14b-instruct-q4_K_M` | ~9GB | 显存 ~12GB | 质量接近云轨 |
| **不推荐** | DeepSeek-R1 蒸馏 / Qwen3 thinking 系 | — | — | reasoning 输出与管线空正文防护冲突，RAG 证据型回答不需要深度推理；确需 Qwen3 须关闭思考模式 |

**已知坑（必读）**：
1. **Ollama `num_ctx` 默认 4096，必须调 ≥8192**（RAG 上下文 3k+ tokens，默认值会截断证据导致引用丢失）——`OLLAMA_CONTEXT_LENGTH=8192` 环境变量或 Modelfile 参数。
2. 质量预期：7B 本地模型在复杂归纳/多跳问题上**弱于 deepseek-flash**，试点可接受，正式商用建议云轨或 14B+。
3. 本地轨资源增量：内存 +6GB（CPU 推理）或 GPU 显存 6-12GB；磁盘 +5-10GB。

**切换步骤**（云轨 ↔ 本地轨）：

```bash
# 1. 部署机上安装 Ollama 并拉模型
curl -fsSL https://ollama.com/install.sh | sh
OLLAMA_CONTEXT_LENGTH=8192 ollama serve            # systemd 部署则写进 service 环境
ollama pull qwen2.5:7b-instruct-q4_K_M
# 2. .env 修改三键
#    LLM_BASE_URL=http://<ollama主机>:11434/v1
#    LLM_API_KEY=ollama            （占位即可，本地无鉴权）
#    LLM_MODEL=qwen2.5:7b-instruct-q4_K_M
# 3. 重建 app 使环境变量生效
docker compose up -d --force-recreate app
# 4. 验证：服务健康卡 llm 行=正常（探活打 /models）；发一问核对回答与引用
```

## 4. 自动化部署

### 4.1 一键前置体检（机器判定就绪性，FAIL 即中止）

```bash
bash deploy/bootstrap.sh --check-only   # 干跑：只体检不装配
```

体检项（PASS/WARN/FAIL，任何 FAIL 中止并给修复指引）：

| # | 检查项 | 阈值 | 依据 |
|---|--------|------|------|
| 1 | Docker daemon | 运行中 | — |
| 2 | 内存（Docker 可分配） | <11G FAIL；11-22G WARN（mineru 按需）；≥22G 全常驻。阈值唯一常量 **MEM_FULL_MB=22528（22G，以代码为准）**，bootstrap.sh / deploy.sh 同源引用，改阈值须含本表四处一并改 | 实测常驻 ≈10GB；MB 精度防整数截断误报 |
| 3 | 磁盘（Docker 数据根） | <50G FAIL；<100G WARN | 基础占用 ≈25GB |
| 4 | CPU 核数 | <8 WARN | 重排延迟翻倍 |
| 5 | 外网 api.deepseek.com | 不可达 WARN（本地轨可忽略） | 云轨必需 |
| 6 | `.env` 必填键 | DATABASE_URL/JWT_SECRET/LLM_BASE_URL/LLM_MODEL 非空（EMBED/RERANK/MINERU/PASSWORD 由 compose 注入，不要求在 .env） | .env.example §逐项 |
| 7 | 安全红线 | JWT_SECRET ≥32 字且非 example 占位；POSTGRES_PASSWORD 非缺省 rag；prod 模式 TEST_MODE 必须 false | 密钥留占位值上线=事故 |
| 8 | TEI 模型 | bge-m3 / bge-reranker 目录各 ≥1GB | 缺文件 TEI 起不来 |
| 9 | mineru 镜像 | 缺失 WARN（pypdf 降级知情）；prod 口径查 `${MINERU_IMAGE:-mineru-cpu:2.5.4}`，执行时由 deploy.sh 第 0 步校验——仅内存 ≥22G（mineru 常驻档）缺失才中止并给导入指引，低配档延后拉起仅提示（§4.3 分档） | 功能降级要知情 |
| 10 | gotenberg 镜像 | 缺失 WARN（仅影响 doc/ppt/xls 老格式转换，问答不受影响） | 功能降级要知情 |

### 4.2 试点机一键装配

```bash
bash deploy/bootstrap.sh        # 体检通过后自动：错峰拉起 pg→embed→rerank→app + 冒烟提问
# 内存 ≥22G 时 mineru 自动常驻；否则按需 bash deploy/recover.sh mineru
```

装配复用 `deploy/startup-check.sh`（错峰启动防 OOM 互挤 + 等待健康 + 种子冒烟账号 `app.db.e2e_seed`（e2e-admin / E2ePass#2026）+ 冒烟提问），最后打印验收三步（健康卡 / 上传就绪 / 问答带引用）。

### 4.3 生产机发布（digest 模式）

```bash
bash deploy/bootstrap.sh --mode prod --check-only   # 生产口径体检（另查 .env.prod、TEST_MODE 必须 false）
./deploy.sh <ghcr.io/.../ragdemo@sha256:...>        # 拉镜像→按内存分档错峰起栈（entrypoint 自动 alembic 迁移）→健康→冒烟
./rollback.sh <上一版 digest>                        # 回滚
UPLOADS_VOLUME=deploy_uploads ./deploy/backup.sh    # 定时备份（pg_dump + uploads 卷，runbook §3.6）
```

- deploy.sh 起栈与 §4.4 手动口径同源：错峰 pg→tei-embed→tei-rerank→（mineru）→gotenberg→app，每步等 healthy，**严禁一次性全量 up**；mineru 按 Docker 可分配内存分档（≥22G 随栈常驻，阈值与 §4.1 体检表同源 22528MB；<22G 不随栈拉起，发布完成时打印按需启停指引——`docker compose -f deploy/docker-compose.prod.yml up -d mineru` / 用后 `docker stop ragdemo-mineru`），mineru 镜像校验仅在常驻档硬中止。
- `.env.prod` 逐字段清单（含义/必填性/生产取值示例，含 TEI/MinerU 必须填 compose 服务名 DNS 如 `http://tei-embed:80`）见 **runbook §9**。
- prod uploads 在 named volume（`deploy_uploads`），备份/恢复走卷口径（runbook §3.6），`backup.sh` 传 `UPLOADS_VOLUME` 即启用；产物 `.part` 原子改名 + `.sha256` 速检（runbook §3.2）。

### 4.4 手动步骤对照（供审计/灾备，自动化不可用时的兜底）

1. 装 Docker → 2. 复制 `.env.example` 为 `.env` 填必填项 → 3. `docker/download_models.py` 预下 TEI 模型（`HF_HUB_DISABLE_XET=1`）→ 4. 导入 mineru 镜像 → 5. 按顺序 `docker compose up -d pg` → 等 healthy → `tei-embed` → 等 → `tei-rerank` → `app`（每步等 healthy，严禁一次性全量 up——内存互挤）→ 6. `curl /healthz` → 7. 建管理员账号 → 8. 上传文档冒烟。

## 5. 部署后验收清单

| # | 验收项 | 通过标准 |
|---|--------|---------|
| 1 | 服务健康卡 | 管理员 → 运营概览：pg/embed/rerank/llm 四行"正常"；mineru 按需态允许"不可用"黄灯 |
| 2 | 知识入库 | 上传 docx → 状态"就绪" |
| 3 | 问答 | 流式回答带 `[1]` 角标引用；无关问题拒答"知识库中未找到相关内容" |
| 4 | 缓存头 | `curl -sI http://localhost/ \| grep -i cache-control` 返回 no-cache（prod 唯一对外端口 80，与 deploy.sh/rollback.sh 探活同口径） |
| 5 | 手动验收 | 按 docs/manual-test-guide.md 全量用例 |

## 6. 风险与回滚

| 风险 | 缓解 |
|------|------|
| 内存不足 OOM | bootstrap 体检硬门槛；运行期 `recover.sh` 一键恢复，startup-check 错峰装配 |
| LLM 云轨不可达/超支 | 健康卡 llm 行实时暴露；本地轨切换步骤 §3.2（分钟级） |
| 版本回退 | rollback.sh（digest 清单在 deploy/releases.md）；迁移仅追加可安全降级应用版本 |
| 数据丢失 | backup.sh 定时备份 + 周期恢复演练（deploy/verify-restore.sh，runbook §3.4） |
| 演示模式误上线 | bootstrap prod 体检 TEST_MODE=false 硬校验；帮助页 Q3 教用户辨别 |

## 7. 部署形态边界与已知限制（验收单附件，2026-10-31 工单 12）

以下为**符合设计的已知行为**，签约与验收时按本节对齐，避免上线后按超出形态的预期扯皮：

1. **单实例约束**：应用进程内持有调度器、登录限流窗口、登出黑名单、精确缓存（进程内单例）——多 worker/双容器部署会导致调度重复触发与状态分裂；扩容路径见评审报告 §6 阶段二（状态外置→调度器独立→加副本）。**状态外置已具备**（2026-10-31 工单 19）：`STATE_BACKEND=pg` 可把登录限流/登出黑名单/精确缓存/日限额/召回暂存五件切到三张 `state_*` 表（跨实例共享，test_mode 恒 memory），调度器外置属工单 20。
2. **重启语义**：进程重启即清空登录限流窗口/登出黑名单/缓存（JWT 本体无状态不受影响）；`STATE_BACKEND=pg` 时五件状态落库、重启不丢（易失状态随备份卷留存，runbook 备份范围补充中）。
3. **调度任务补偿**：停机跨过周日 04:00/05:00（UTC）触发窗口，重启后由启动补偿自动补跑当周任务（eval_auto_synth_week 标记 + [自动] run 在册判定，同周互斥不重复）；运行中错过 ≤1h 由 misfire_grace_time 兜底。
4. **CPU 档性能上限**：无 GPU 时 rerank 15-60s/问（超时豁免 120s，known-issues 开放-1）；要商用级 10s 响应须配 GPU 版 TEI（§2 硬件表）。交付口径按 x86+CPU TEI 的实测数字对齐，不按 GPU 口径承诺。
5. **架构支持**：仅 linux/amd64（§2 系统行）。
6. **评测自动闭环**：总闸 EVAL_AUTO_ENABLED 缺省关（开启属部署决策）；开启后周日自动合成与跑分的预算/退化阈值经管理端"自动评测设置"在线调整。

---

*文档对应版本：2026-10-02 构建（§7 补充于 2026-10-31）；数据来源为本机 Docker 实测（镜像大小/内存占用/重排延迟）。硬件或模型变更时同步修订。*
