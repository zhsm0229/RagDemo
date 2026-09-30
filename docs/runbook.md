# 运维手册（Runbook）

> 状态：W3 Task 13 产出骨架，W5 Task 6 定稿备份/恢复章节（第 3 节，§18）；W5 Task 8 增串库双向演练章节（第 5 节，T9 执行）。**T9 已于 2026-09-30 完成恢复演练与串库双向走查，实测数据已回填 3.4 / 3.5 / 5.5，串库判据按实测结论修正（见 5.3/5.4/5.5）**。其余章节随里程碑增量补全（§18.4：启动停止 / 备份恢复 / 常见故障 / 合规轨道切换 / 端口核查）。
> 部署形态：单机 Docker Compose，4 核 16G VM（§12.5）。所有命令在仓库根目录执行（Windows Git Bash 或部署机 bash）。

## 1. 主栈启停

```bash
# 启动
docker compose up -d

# 状态与健康检查（healthcheck 就绪应为 healthy）
docker compose ps
docker inspect -f '{{.Name}} {{.State.Health.Status}}' ragdemo-pg

# 停止（保留数据卷）
docker compose down
```

- 主栈容器清单随里程碑并入：当前为 `pg` / `pg-test`（W3 阶段）；`app` / `mineru` / `tei-embed` / `tei-rerank` 后续并入，并入后 app 健康检查增加 `curl -f http://127.0.0.1/healthz`（发布端口仅 80，§12.2）。
- **禁止**对主栈使用 `down -v`（会删除 `pgdata` 业务数据）；数据清理只允许在明确授权的重建流程中执行。

## 2. RAGFlow 基线环境

### 2.1 定位与合规红线

- 用途：检索准确率评测的 RAGFlow 对照基线（project-brief 决策 11/15），**仅内测期存在，不进生产栈**（§12.2）。配置文件：`deploy/docker-compose.ragflow.yml`（独立 project `ragflow-baseline`，网络/卷与主栈完全隔离）。
- **§7.6 红线：禁止为 RAGFlow 配置任何云 API key 灌真实语料**（否则构成第二条内容出网通道）。只允许 full 镜像内置本地 embedding，或在基线界面配置内网模型端点；语料只用脱敏/合成数据。发现配置了云 key 立即下线整改。
- 凭据：compose 内置默认值仅供首次拉起；部署机建议经 `deploy/.env`（git 已忽略）覆盖 `RAGFLOW_MYSQL_PASSWORD` / `RAGFLOW_MINIO_USER` / `RAGFLOW_MINIO_PASSWORD` / `RAGFLOW_REDIS_PASSWORD`。

### 2.2 启动（错峰顺序）

基线资源上限约 11.75G / 4.5C，主栈满配上限 10C17G（§12.5 预算值，主栈 compose limits 待后续任务落地），主机仅 4 核 16G——**两栈禁止同时满载（§12.5）**，顺序如下：

```bash
# 1. 先确认主栈无重负载：无解析任务（mineru 空闲）、无批量上传/评测在跑；
#    必要时先停主栈（至少停 mineru/tei 等重负载服务）
docker compose down

# 2. 起基线（首次启动拉镜像 + 初始化较慢，属预期）
docker compose -f deploy/docker-compose.ragflow.yml up -d

# 3. 等待五服务全部 healthy（es01 首次初始化最慢）
docker compose -f deploy/docker-compose.ragflow.yml ps

# 4. 浏览器访问 Web UI：http://<vm-ip>:9380
```

### 2.3 停止（错峰恢复主栈）

```bash
# 停基线（保留 baseline-* 卷，下次启动语料仍在）
docker compose -f deploy/docker-compose.ragflow.yml down

# 确认基线全部退出后再恢复主栈
docker compose ps   # 应为空
docker compose up -d
```

### 2.4 资源冲突观察点（合计上限 vs 4 核 16G）

| 观察点 | 命令 | 预警线 |
|---|---|---|
| 内存 | `free -h` | available < 2G 预警，< 1G 立即停一栈 |
| 容器资源 | `docker stats --no-stream` | 任一容器贴 memory limit（被 limit OOM 会频繁重启） |
| 系统 OOM | `sudo dmesg -T \| grep -i "out of memory"` | 出现 kill 记录即资源超卖，回去查错峰纪律 |
| ES 磁盘水位 | 基线界面 / `docker logs baseline-es01` | 5gb/3gb/2gb 水位告警（已内置） |
| 宿主磁盘 | `df -h` | es/mysql/minio 卷增长，剩余 < 20% 预警 |

基线重负载 = 批量导入解析 + embedding 生成，为最坏负载；评测取数期间保持主栈停止，跑完按 2.3 交还资源。

### 2.5 数据清理

```bash
# 基线语料整体作废：删除全部 baseline-* 卷（不影响主栈）
docker compose -f deploy/docker-compose.ragflow.yml down -v

# 只重置单个数据域（先 down 再删对应卷）
docker volume rm baseline-es-data
```

- 卷以 `baseline-` 前缀与主栈（`ragdemo_pgdata` 等）隔离；**清理命令只允许作用于 baseline 前缀卷**。
- 基线是一次性对照数据，不做备份（见第 3 节），销毁后重新导入脱敏语料即可。

### 2.6 镜像 digest 核对登记表（§12.8 风格）

部署机首跑时逐条核对登记（命令见下），**双人复核**后本表方可视为有效；换 tag 属变更，须重新登记：

```bash
docker pull <镜像:tag>
docker inspect --format '{{index .RepoDigests 0}}' <镜像:tag>
```

| 镜像 | tag | digest (sha256) | 登记日期 | 复核人 A | 复核人 B |
|---|---|---|---|---|---|
| infiniflow/ragflow | v0.19.1 | 待部署机登记 | - | - | - |
| docker.elastic.co/elasticsearch/elasticsearch | 8.11.3 | 待部署机登记 | - | - | - |
| mysql | 8.0.39 | 待部署机登记 | - | - | - |
| quay.io/minio/minio | RELEASE.2023-12-20T01-00-02Z | 待部署机登记 | - | - | - |
| valkey/valkey | 8.1.3 | 待部署机登记 | - | - | - |

注：① `infiniflow/ragflow:v0.19.1` 为 full 变体，内置 embedding 模型工件同受 §12.8 约束（模型 hash 复核在镜像内进行，登记表可加行）；② tag 全部钉死、禁 `latest`（§12.1）。

登记后闭环（§12.8「含模型的镜像以 digest 引用」）：在部署机 `deploy/.env`（git 已忽略）把 ragflow 运行引用切到 digest，并重建容器生效——

```bash
# deploy/.env
RAGFLOW_IMAGE=infiniflow/ragflow@sha256:<登记值>

# 重建生效（数据卷不受影响）
docker compose -f deploy/docker-compose.ragflow.yml up -d
```

compose 中 `${RAGFLOW_IMAGE:-infiniflow/ragflow:v0.19.1}` 的 tag 默认值保留作可读回退，部署机以 `.env` 的 digest 引用为准。

## 3. 备份 / 恢复（§18，W5 T6 定稿）

- 规范指针：standards §18——PostgreSQL 每日 `pg_dump` 全量保留 7 天（备份目录权限 0700）、上传目录每日备份、恢复目标 **RPO ≤ 24h / RTO ≤ 2h**（§18.3）、`.env` 凭据副本托管密码管理器。
- **安全须知（§18.1）：备份含全部问答与文档内容，访问权限同生产库**——备份只落独立目录（本方案 `./backups`，脚本已 `chmod 700`），禁止放入 Web 可访问路径或全员可见共享位置；`.env`（含密钥）不在备份内，副本托管密码管理器。
- 备份范围 = 主库 `rag`（容器 `ragdemo-pg`）+ 上传目录 `./data/uploads`（`STORAGE_DIR` 默认值）。`ragdemo-pg-test`（`rag_test`，5434 端口）是独立测试实例，**不在备份范围**；RAGFlow 基线环境不做备份（一次性对照数据，销毁重建，见 2.5）。
- 备份统一走 `deploy/backup.sh`（顶部参数化区可按环境覆盖容器名/库名/目录/保留期），每步带 `[backup]` 时间戳日志，任一步失败非零退出。

### 3.1 每日自动备份（调度示例）

Windows（装有 Git Bash 的开发/演示机；在 cmd 执行，Git Bash 与仓库路径按实际调整）：

```bat
schtasks /Create /TN "ragdemo-daily-backup" /SC DAILY /ST 02:30 /TR "\"C:\Program Files\Git\bin\bash.exe\" -c 'cd /d/Project/ZCodeProject/RagDemo && mkdir -p backups && ./deploy/backup.sh >> backups/backup-cron.log 2>&1'"
```

Linux（部署机；`crontab -e` 追加一行，仓库路径按实际调整）：

```bash
30 2 * * * cd /opt/ragdemo && mkdir -p backups && ./deploy/backup.sh >> backups/backup-cron.log 2>&1
```

- 两版均为每日 02:30 执行，日志追加到 `backups/backup-cron.log`（backup.sh 的保留期清理只删 `*.gz`，该日志需手工定期清理）。
- 前置：`ragdemo-pg` 在运行且 healthy（`docker inspect -f '{{.State.Health.Status}}' ragdemo-pg`）；失败先查上述日志，再手工按 3.2 复现。
- 登记核查：Windows `schtasks /Query /TN "ragdemo-daily-backup"`；Linux `crontab -l`。

### 3.2 手动备份

```bash
bash deploy/backup.sh
```

- 产物：`backups/rag-YYYYMMDD-HHMMSS.sql.gz`（主库全量 dump，gzip 在宿主侧管道完成）+ `backups/uploads-YYYYMMDD-HHMMSS.tar.gz`（上传目录归档；目录不存在时警告跳过）；结束自动清理超过 7 天的 `backups/*.gz`。
- 备份目录 `backups/` 已加入 `.gitignore`（备份含全部问答与文档内容，禁止入库，§18.1），勿移出该忽略项。
- 备份后速检：`ls -lh backups/`——当日 `.sql.gz` 必须非 0 字节；**没做过恢复读回验证的备份不算可用备份**——至少在每次演练时按 3.3(a) 验证一轮。

### 3.3 恢复步骤（两种口径，动手前先确认口径）

两种口径共用前置：`ragdemo-pg` 在运行；先定位目标备份文件（默认取最新一份，也可手工指定）：

```bash
BK="$(ls -t backups/rag-*.sql.gz | head -n1)"     # 主库备份，如 backups/rag-20260930-023000.sql.gz
BU="$(ls -t backups/uploads-*.tar.gz | head -n1)" # 上传目录备份
echo "BK=$BK BU=$BU"
```

#### (a) 恢复到新建验证库（演练 / 校验备份可用性，不影响生产数据）

```bash
# 1. 建验证库（幂等：先删同名残留）
docker exec ragdemo-pg psql -U rag -c "DROP DATABASE IF EXISTS rag_restore"
docker exec ragdemo-pg psql -U rag -c "CREATE DATABASE rag_restore"

# 2. 恢复（ON_ERROR_STOP=1：任一 SQL 失败即非零退出，不得静默半成功）
gunzip -c "$BK" | docker exec -i ragdemo-pg psql -v ON_ERROR_STOP=1 -U rag -d rag_restore

# 3. 读回核对：表清单应与生产库一致，核心表行数应与备份时点一致
docker exec ragdemo-pg psql -U rag -d rag_restore -c "\dt"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM qa_pairs"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM messages"

# 4. 与备份时点记录的行数比对（三表同第 3 步口径；演练当天如有新增写入，恢复库行数 ≤ 生产库属正常，差值 = 备份后增量）
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM qa_pairs"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM messages"

# 5. 演练结束清理验证库
docker exec ragdemo-pg psql -U rag -c "DROP DATABASE rag_restore"
```

uploads 侧核对：`tar -tzf "$BU" | head` 能列出 `uploads/...` 文件即归档完好；正式演练时抽一个文件解出比对内容。

#### (b) 覆盖生产库（灾难恢复；先停写、后覆盖、再冒烟）

> **【危险】覆盖生产库前必须确认已有一次成功备份——本节会先 DROP 生产库 `rag`。没有非 0 字节的 `rag-*.sql.gz` 时禁止执行本节，先走 3.3(a) 验证库口径排查备份可用性。**

```bash
# 0. 前置检查：目标备份存在且非 0 字节；停机窗口与相关方已确认
ls -lh backups/rag-*.sql.gz

# 1. 停写入方（app 容器并入主栈后生效；并入前确认无业务写入在跑）
docker compose stop app

# 2. 重建生产库（连 postgres 维护库执行；WITH (FORCE) 断开残留连接，PG15 支持）
docker exec ragdemo-pg psql -U rag -d postgres -c "DROP DATABASE IF EXISTS rag WITH (FORCE)"
docker exec ragdemo-pg psql -U rag -d postgres -c "CREATE DATABASE rag"

# 3. 恢复主库（ON_ERROR_STOP=1：任一 SQL 失败即非零退出——灾难恢复的"成功"信号必须可靠，不得静默半成功）
gunzip -c "$BK" | docker exec -i ragdemo-pg psql -v ON_ERROR_STOP=1 -U rag -d rag

# 4. 恢复上传目录（归档内含 uploads/ 一级目录，解到其父目录 ./data）
mkdir -p ./data
tar -xzf "$BU" -C ./data

# 5. 起服务 + 冒烟
docker compose up -d
docker compose ps   # 应全部 healthy
# 冒烟：登录工作台发起一次问答，确认回答带可核对引用；app 并入后增加 curl -f http://127.0.0.1/healthz（§12.2）

# 6. 读回核对（对 -d rag 执行，行数与备份时点一致）
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM qa_pairs"
```

- 红线（与第 1 节一致）：本节 `DROP`/`CREATE` 只针对库 `rag`，禁止波及 `rag_test`；全程禁止 `docker compose down -v`（会删 `pgdata` 业务数据卷）。

### 3.4 演练记录表（§18.3，实测回填）

| 日期 | 类型 | 备份大小 | 恢复耗时 | RTO | RPO | 执行人 | 备注 |
|---|---|---|---|---|---|---|---|
| （示例）2026-09-30 | 验证库恢复 | 12 MB | 8 分钟 | 12 分钟 | 20 小时 | 张三 | 示例行：RTO=恢复+冒烟端到端时长，RPO=距上次成功备份的数据窗口；正式记录自 W5 T9 演练起填 |
| 2026-09-30 | 验证库恢复 | 42 KB（sql.gz）+ 36 KB（uploads） | 36 秒 | ≈4 分钟 | ≈0（演练口径） | T9 测试者（AI） | W5 T9 首次实测：读回断言三表行数/关键行/alembic_version(0003) 与备份时点一致、上传归档字节比对一致、恢复库冒烟（登录/文档列表/问答）通过；RTO=备份定位→恢复→读回→起服务→冒烟端到端，RPO 以灾前即时备份演示（数据窗口 0），声明口径 ≤24h 见 3.5 |

- 类型口径：`验证库恢复` = 3.3(a)，常规演练；`生产覆盖演练` = 3.3(b)，需审批与停机窗口。

### 3.5 RPO / RTO 目标声明（§18.3）

- **RPO ≤ 24 小时**：由每日 02:30 全量备份决定，最大数据丢失窗口 ≈ 24 小时（上次成功备份到故障点之间新增的数据）。
- **RTO ≤ 2 小时**：定位备份 → 3.3 恢复 → 起服务 → 冒烟核对的端到端时长上限。
- 实测值（W5 T9，2026-09-30，验证库口径）：**RTO ≈ 4 分钟**（其中恢复段 36 秒、读回断言与冒烟约 3 分钟），远低于 2 小时目标；RPO 演示窗口 ≈ 0（灾前即时备份），声明口径 ≤24h 由每日调度保证。正式记录见 3.4。

## 4. 端口暴露核查清单（§12.2，每次发布 / 基线启停后执行）

规则基线：**主栈仅 app 发布 80；RAGFlow 基线仅发布 9380；其余一切端口仅 compose 内网互联，禁止 `ports` 发布。**

```bash
# 1. 容器发布端口核查：输出中只允许出现 0.0.0.0:80(app) 与 0.0.0.0:9380(baseline-ragflow)
docker ps --format '{{.Names}}\t{{.Ports}}'

# 2. 宿主监听核查：比对无未登记监听
ss -tlnp   # 或 netstat -tlnp
```

- [ ] `docker ps` 发布端口仅为 80（主栈 app）与 9380（基线），无其他 `0.0.0.0:` 映射
- [ ] 主栈 pg 的 5433/5434 属**开发期临时发布**（本机开发用），生产收口前必须收回为 compose 内网（§12.2）
- [ ] es01/mysql/minio/valkey（9200/3306/9000/9001/6379）无任何宿主发布
- [ ] 防火墙/安全组仅放行 80、9380（22 运维端口按 IT 策略另行管理）
- [ ] 核查结果异常时：先 `docker compose down` 对应栈收敛端口，再排查是谁改了 compose

## 5. 串库双向演练（W5 T8 产出流程，T9 执行）

目的：双向走查串库拒答——客服视角问人力题、人力视角问客服题都必须拒答（检索边界恒 [视角部门, 全员]，F-502；产品红线"知识不足必须拒答"的部门维度）；同时用全员对照题防"全部拒答也全绿"的假阴性。题库由 `tests/fixtures/tools/make_crosstalk_suite.py` 生成，共 24 行：人力制度类串库拒答题 10 条 + 客服售后类串库拒答题 10 条 + 全员对照事实题 4 条，题目均以"演练用合成题"标注（与真实题库区分，演练后可按此前缀识别清理）。

### 5.1 前置（每次演练执行）

```bash
# 0. 主栈已启动（第 1 节）；测试/演示环境（test_mode，stub LLM）
#    题库生成器 import 后端常量，须在项目 venv 内执行（make install / uv sync --extra dev）

# 1. 生成题库（生成物不入库，放临时目录；可重复运行，覆盖输出）
python tests/fixtures/tools/make_crosstalk_suite.py -o /tmp/crosstalk.xlsx

# 2. 生成对照语料（4 条对照题的期望答案取自该文档固定文本）
python tests/fixtures/tools/make_sample_docx.py   # 写出 tests/fixtures/samples/sample.docx
```

- **上传对照语料**：管理后台 → 知识库·文档管理 → ＋上传文档，把对照语料上传为**全员**部门文档（标题如"串库演练对照语料"），等状态 ready。语料缺失或内容改动 = 对照题必假阴性（命中不了期望关键词）。
  **勘误（T9 实测）**：`sample.docx` 有效正文 < 100 字符，直传会被解析质量闸拒收（failed，"疑似扫描件或空文档"）——上传前需在保留四条对照关键词句（本手册适用于全体员工／总部及各分支机构／凭发票据实报销／每周一至周五）逐字不变的前提下增补正文（T9 演练用增补版 214 清洗字符通过；根修复——扩充 make_sample_docx 并同步 test_parser 结构断言——留待后续任务）。
- 题库无单题删除接口，重复导入会累积同题并进入后续所有跑分：重复演练前由运维清理 `eval_questions`（软删 `is_active` 或清表，经 DBA 确认）再重新导入；演练断言按"题面部门 + 题型"分组核对，不依赖 run 总数恰为 24。

### 5.2 导入题库

管理后台 → 评测管理 → "评测题库"页签 → **Excel 导入题目** 上传 crosstalk.xlsx（或 `POST /api/admin/eval-questions/import`），成功返回 `imported: 24`。行级错误整批退回（422）——生成器产物若报错说明后端模板已漂移，先跑 `pytest tests/unit/test_crosstalk_suite.py` 定位。

### 5.3 客服视角跑分

1. "跑分记录"页签 → **发起跑分**：任务名称 `串库演练-客服视角-<日期>`，视角部门=客服（`perspectiveDeptCode: "service"`）。
2. 轮询 `GET /api/admin/eval-runs/{id}`（前端每 10 秒自动刷新）至 `status=done`，逐题核对 detail。**判据分两种 stub 场景（T9 实测修正——stub rerank 分为伪随机值，串库题在边界内有语料时会以占位文案作答，"10/10 拒答"不是 stub 语料在库场景的有效断言）**：
   - **场景一：边界内有语料（对照语料已上传，常规形态）**——断言三条：①**引用零越界**：全部题的 citations 不含越界部门（客服视角下无任何 `refType=doc` 引用指向人力部门块，反之亦然；可用 chunk→dept 对照 SQL 机检）——这是 stub 模式下串库边界的**结构性断言**（F-502 部门过滤，漏一条即串库漏洞，排查 `app/services/retrieval.py` 的 dept 边界）；②**对照题命中**：题面部门=全员的 4 条 answerType ∈ rag/qa（非拒答）→ "评测题库"录分入口人工评判 correct（`POST /api/admin/eval-runs/{id}/judgments`；`autoJudgment=pending` 属预期：非拒答题不自动判，判定协议第 3 条）；③题面部门=本视角的 10 条不要求拒答（本视角内可答）。
   - **场景二：边界内无任何语料（空范围，机制验证形态；T9 于隔离库 rag_test_b 实测）**——人力题 10 条（cross_reject）`autoJudgment` 全部 correct（回答逐字等于拒答话术"知识库中未找到相关内容"），**串库组拒答 10/10**；对照组 4 条同样拒答（pending，无需录分）。此场景验证拒答机制 + 自动判定协议本身。
   - 题面部门=客服的 10 条：库内无客服语料时同样拒答、auto correct 属预期（stub 演练形态）；部署机若已灌客服语料，命中回答记 auto wrong 属预期（本视角内应答），人工评判 correct 覆写即可。
3. 人工录分后点**报告**核对分组口径（byDept / byQtype），报告页与 `GET /api/admin/eval-runs/{id}/report` 响应一并归档。

### 5.4 人力视角跑分（对称）

重复 5.3：任务名称 `串库演练-人力视角-<日期>`，视角部门=人力（`perspectiveDeptCode: "hr"`）。断言对称——**场景一**：引用零越界（无任何 service 部门块出现在 citations）+ 对照组 4 条 rag/qa 后人工 correct；**场景二（空范围）**：题面部门=客服的 10 条 100% 拒答。两轮双向场景均绿 = 双向隔离成立。

### 5.5 存档与登记

- 跑分结果天然存档于 `eval_runs`：收口即锁定 config 快照 9 字段（model / promptVersion / qaThreshold / refuseThreshold / efSearch / topK / topN / temperature / manifestVersion），防跨跑对比被阈值调整污染；审计 `eval.run` / `eval.judge` 留痕（§23）。
- **stub 模式口径（T9 实测修订）**：检索部门过滤的结构性保证已验证——边界内语料齐备时双向引用零越界（Run A，dev 库 run 1/2）；空范围时串库组双向 10/10 拒答（Run B，隔离库 rag_test_b run 1/2）。**stub rerank 分为伪随机值，"越视角题 100% 拒答"在边界内有语料时不可作 stub 判据**（Run A 实测 0/20 拒答、以占位文案作答 auto wrong 属预期）；**真实阈值下的拒答边界须在部署机以真实 embedding + 真实语料复测**（届时人力/客服语料都在库，断言恢复为：越视角组 100% 拒答、对照组全中）。
- **真实模式复测口径（2026-09-30 部署验收实测，runbook §5.5 预设与实际差异勘误）**：试点库实际构成 = 客服手册 + 全员手册（同内容双部门发布）+ 全员对照语料，**hr 语料缺位**——"人力/客服语料都在库"预设不成立，越界断言按机检 chunk→dept 仍可执行（界=[视角部门, 全员]）。实测（真实 TEI bge-m3 + DeepSeek + refuseThreshold=0.55）：**越界引用 0/19** ✓；越视角组拒答 **7/9**（2 例漏拒：rerank 0.6509/0.6578，撞上全员手册「考勤与休假」章——hr 主题语义天然重叠）；对照组实答 2/2 内容正确。**漏拒带（0.65+）与对照命中带（0.63-0.71）重叠，单一 refuseThreshold 无法同时满足"漏拒=0"与"对照全中"**——阈值维持 0.55 不动，漏拒治理走语料侧（hr 主题章节从全员/客服手册剥离或 qa 优先命中），详见 `.superpowers/sdd/2026-09-30-w5-hardening/deploy-acceptance-report.md` §5.2。运维注意：真实跑分对 rerank 吞吐敏感，部署机 `.env` 已调高 TEI 超时（embed 60s / rerank 120s），rerank 批量负载饱和时整题会以 infra error 收口（detail.error 留痕，autoJudgment=pending）——跑分窗口应避开批量解析/embedding 作业。
- 结果登记表（一行 = 一次跑分；T9 演练已填，库=主库 rag 除注明外）：

| 日期 | run ID | 视角 | 串库组拒答 | 对照组人工判 | accuracy | 执行人 | 备注 |
|---|---|---|---|---|---|---|---|
| （示例）2026-09-30 | 12 | 客服 | 人力题 10/10 | 4/4 correct | 1.0000 | 张三 | 示例行：accuracy 取录分收口后 summary；串库组按题面部门核对 |
| 2026-09-30 | 1 | 客服 | 人力题 0/10（stub 占位作答，预期内） | 4/4 correct（answerType 均 rag） | 0.1667 | T9 测试者（AI） | Run A 场景一：对照+诱饵语料在库；引用零越界 ✓（机检 0 条 hr 块）；config 快照 9 字段落档 |
| 2026-09-30 | 2 | 人力 | 客服题 0/10（stub 占位作答，预期内） | 4/4 correct（answerType 均 rag） | 0.1667 | T9 测试者（AI） | Run A 对称：引用零越界 ✓（0 条 service 块）；byDept: hr 0 / service 0 / all 1.0 |
| 2026-09-30 | 1（rag_test_b） | 客服 | 人力题 10/10 | 对照组拒答挂人工（空范围无对照语料，不录分） | 0.8333 | T9 测试者（AI） | Run B 场景二：空范围机制验证（隔离库，演练后已清理数据） |
| 2026-09-30 | 2（rag_test_b） | 人力 | 客服题 10/10 | 对照组拒答挂人工（同上） | 0.8333 | T9 测试者（AI） | Run B 对称：byQtype cross_reject accuracy 1.0 |
| 2026-09-30 | 1 | 客服 | 人力题 7/9 拒答（2 漏拒 rerank 0.6509/0.6578；另 1 题 rerank 超时 infra err） | 对照 2/2 录分 correct（另 2 题 rerank 超时挂人工） | 0.7500 | 部署验收（AI） | 真实 embedding 复测：越界引用 0/19 机检 ✓、config 快照 9 字段落档；漏拒根因=全员手册「考勤与休假」语义重叠，阈值/语料治理结论见验收报告 §5.2；执行窗口撞 rerank 批量负载 |
| 2026-09-30 | 3 | 人力 | 客服题实得 2/2 拒答（q11/q18，auto correct）；其余 22 题全部 rerank 超时 infra err（21:09-22:44 执行窗口与用户批量作业撞车，TEI rerank 队列 8-12 深） | 对照组 0/4 实答（全 infra err，无可录分） | 0.0833（失真：infra err 记 pending 计 0） | 部署验收（AI） | hr 视角对称矩阵被负载摧毁，**有效样本 2/24、方向正确但不足判据，留负载解除后复测**；accuracy 0.0833 不具评价意义；复测口径：避开批量解析/embedding 窗口（见 §5.5 真实模式复测口径注） |
