# 工单 10（许可合规）：技术合规梳理与无 MinerU 降级交付档

> 2026-11-01 ｜ 开发工程师 A（agent 可执行部分；法律意见本身归外部法律顾问）。
> **声明：本文档仅汇总技术事实，不构成法律意见**——AGPL 义务边界的法律解释、商用分发合同条款，均须外部法律顾问确认后定稿。
> 核实方法（2026-11-01 实证）：Python 包读容器/venv 内 `importlib.metadata`（License-Expression 字段）；TEI/Gotenberg/pgvector 读 GitHub 仓库许可（API + LICENSE 原文）；模型权重读 HuggingFace API cardData/tags；前端包读 node_modules LICENSE 文件。

## 一、逐组件许可清单（商用私有化交付的运行时分发面）

| 组件（版本） | 许可类型 | 实证来源 | 商用分发义务 | 结论 |
|---|---|---|---|---|
| MinerU 2.5.4（自建镜像，`docker/mineru-cpu.Dockerfile` 钉 `mineru[core]==2.5.4`） | **AGPL-3.0** | 容器内 metadata('mineru').License | 强 copyleft：分发镜像须向接收方提供对应源码（构建 Dockerfile + 依赖钉版清单即满足书面源码路径）；不得剥离许可与版权声明；客户再分发或修改后对外提供网络服务时，义务随其转移 | 独立容器 HTTP 形态不传染 app 自有代码（§二）；客户不接受 AGPL 时走 §三降级档 |
| TEI text-embeddings-inference cpu-1.9.2 | Apache-2.0 | GitHub 仓库 spdx=apache-2.0 | 保留 LICENSE/NOTICE；商标不随许可授权 | 低风险 |
| BAAI/bge-m3 权重（tei-embed） | MIT | HF API cardData/tags=mit | 随模型目录保留许可与署名 | 低风险 |
| BAAI/bge-reranker-v2-m3 权重（tei-rerank） | Apache-2.0 | HF API cardData/tags=apache-2.0 | 同上 | 低风险 |
| pgvector/pgvector:pg15（含 PostgreSQL 本体） | PostgreSQL Licence（BSD 族） | pgvector 仓库 LICENSE 原文（GitHub spdx 显示 NOASSERTION/"Other"，文本即 PostgreSQL Licence）；PostgreSQL 本体同许可 | 保留版权与许可声明 | 低风险 |
| Gotenberg 8（gotenberg/gotenberg） | MIT | GitHub 仓库 spdx=MIT | 保留声明 | 低风险 |
| app 镜像 Python 依赖（fastapi/gunicorn/sqlalchemy/alembic/PyJWT/pydantic-settings/apscheduler/python-docx/python-pptx/openpyxl/deepagents/langgraph/langchain-core/ag-ui-protocol=MIT；pypdf/httpx/uvicorn=BSD-3；bcrypt/python-multipart=Apache-2.0；均 License-Expression 实证） | 宽松许可为主 | pip metadata | 随交付包附第三方声明清单即可 | ⚠️ **psycopg 3.3.6 = LGPL-3.0-only**：随容器分发须提供该库源码获取方式（LGPL §4/§6，含反编译例外）；钉版 + 源码链接即满足，登记即可 |
| 前端 element-plus 2.14.6 / @element-plus/icons-vue 2.3.2 / vue 3.5.43 | MIT | node_modules LICENSE 文件 | 保留声明 | 低风险 |
| 仅开发态（不随镜像分发）：fpdf2 2.9.0（**LGPL-3.0-only**）、pytest 等 | — | `Dockerfile` 仅 `pip install .`（不含 dev 依赖组）；fpdf2 只用于测试造 pdf 夹具 | 不分发即无分发义务 | 无风险；勿将 fpdf2 加入运行时依赖 |
| DeepSeek 云 API | 服务条款（非开源许可） | — | 数据出网/个人信息合规 | 归工单 11，本单不评估 |

## 二、AGPL（MinerU）义务边界的技术事实

1. **调用形态**：`app/rag/parser_mineru.py` 经 HTTP `POST /file_parse` 调独立容器（compose 服务 `mineru`），与 app 无 import、无进程内链接、无代码混合。按 FSF 对 AGPL §13 网络条款的公开解释口径：义务触发于"你修改的版本向公众提供网络交互"，未修改的 MinerU 以独立进程被网络调用，一般不使调用方代码卷入 copyleft——"独立程序 vs 衍生作品"的最终判定属法律意见，此处仅记录技术事实。
2. **自建 Dockerfile 的"修改版"争议点**：`docker/mineru-cpu.Dockerfile` 只做安装编排（`python:3.11-slim` + pip 装 `mineru[core]==2.5.4` + arm64 特例钉 `torch==2.6.0+cpu`、amd64 `2.5.1+cpu`、阿里镜像源、分两层缓存），未 patch MinerU 任何源文件——属"聚合（aggregate）"而非对 MinerU 本体的修改。即便按最保守口径，义务也仅是向镜像接收方提供 Dockerfile 与依赖获取清单（该 Dockerfile 本身已随仓库分发）。
3. **向客户分发注意事项**：(a) 含 mineru 镜像交付时，随包提供对应源码路径（Dockerfile + pip 依赖钉版清单）；(b) 不剥离镜像内版权/许可文件；(c) 合同告知客户——其再分发镜像、或修改 MinerU 后对第三方提供网络服务，AGPL 义务由其承担；(d) 客户书面拒绝 AGPL → 按 §三降级档交付（`Parser` 六接口之一，`mineru_base_url=""` 即整体切到 pypdf，零代码改动）。

## 三、无 MinerU 降级交付档（工单 10 已落地）

### 3.1 实现
- 既有机制：dev `docker-compose.yml` 的 mineru 带 `profiles: ["parser"]`，缺省 `up` 本就不含；
- 新增 `deploy/docker-compose.nomineru.yml`（叠加 dev 或 prod compose）：mineru 划入 `profiles: ["full"]`（缺省不起），`app.environment.MINERU_BASE_URL=""`（compose 合并中 environment 优先于 env_file，压过 `.env`/`.env.prod` 同名项 → `get_parser` 对 pdf 一律选 `PdfFallbackParser`）。

### 3.2 验证证据（2026-11-01 本机，Docker Compose v5.4.0）
1. compose 合并：dev 默认服务集不含 mineru，`config` 渲染 app `MINERU_BASE_URL: ""`（`.env` 中为 `http://localhost:8090`，被覆盖），`--profile parser` 仍可显式拉起 mineru（dev 工作流不回退）；prod 在 /tmp 隔离目录以 dummy 凭据验证（本机无 `deploy/.env.prod`，不动真实密钥）——基线服务集含 mineru，叠加覆盖后为 0，app `MINERU_BASE_URL: ""`。
2. 解析降级路径 pytest 实测（3 passed，库为 conftest 隔离 pg-test localhost:5434/rag_test）：
   - `tests/unit/test_parser.py::test_pdf_fallback_pages_and_engine`（pypdf 分页与引擎标注）
   - `tests/integration/test_ingest.py::test_pdf_without_mineru_uses_pypdf_engine`（配置路径：`mineru_base_url=""` → `parse_engine="pypdf"`、无降级标注、页码入库）
   - `tests/integration/test_ingest.py::test_mineru_outage_degrades_to_pypdf_annotated`（故障路径：UpstreamError 自动降级 +「降级解析」标注 + 审计 `degraded=true`）
   - 注：原计划另起 e2e 隔离栈实测，因用户并行会话正占用共享 compose 项目（app 容器被反复重建），为不干扰在途工作改用任务预批的单测证据路径 + 上述双栈 config 合并验证。

### 3.3 功能取舍清单（对照 `app/rag/parser.py` 选择树与 `ingest_pipeline._parse_with_fallback` 路由）

| 能力 | 有 MinerU | 降级档行为 |
|---|---|---|
| pdf 文字版 | 文字层预判命中直接走 pypdf（2026-10-05 试点裁定：文字版不进 MinerU） | 同左，**无损** |
| pdf 扫描件 | MinerU OCR | pypdf 提不出文字 → 质量红线 422「疑似扫描件或空文档，有效内容不足」，**如实拒收不造文** |
| 表格/版面结构 | 版面分析还原表格与阅读序 | 纯文本流：表格摊平、双栏串行（golden 夹具 `hr_制度_双栏_v1.pdf` 即该场景），引用页码仍可用 |
| doc/ppt/xls 老格式 | Gotenberg 转 PDF 后同 pdf 路由 | 文字版可用；转换产物为扫描版时同上受限 |
| 图片 OCR（F-1017） | MinerU 视觉 OCR | 交付时须设 `IMAGE_OCR_ENABLED=false`；若误开则逐图降级跳过并计数标注（不阻塞入库，`ingest_images.py` 既有降级路径） |
| 入库状态口径 | `parse_engine="mineru"` | `parse_engine="pypdf"`；仅"故障降级"打「降级解析」标注，配置缺省路径不打标（见 3.2 测试断言） |

### 3.4 运维口径
起法（仓库根执行，二选一）：dev `docker compose -f docker-compose.yml -f deploy/docker-compose.nomineru.yml up -d`；prod `docker compose -f deploy/docker-compose.prod.yml -f deploy/docker-compose.nomineru.yml up -d`。降级档**不适用**：deploy.sh 内存分档中的 mineru 常驻分支、runbook §6 的 mineru 镜像自建/hash 登记、`deploy/recover.sh mineru` 按需拉起。验收口径见 deploy-requirements §8。

## 四、遗留与外部项
1. 正式法律意见：AGPL 边界解释、LGPL 满足方式表述、交付合同条款——外部法律顾问（工单 10 外部项）。
2. DeepSeek 服务条款与数据出网合规——工单 11。
3. 建议（未落地）：交付包附 THIRD-PARTY-NOTICES（组件+版本+许可+源码链接，可由 uv.lock 与 node_modules 机械生成，作为交付物生成脚本的后置项）。
