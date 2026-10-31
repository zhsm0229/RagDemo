# 工单 20 设计：调度器独立单副本 + gunicorn 多副本

> 版本 v1（评审基线）｜依赖：工单 19（状态外置后副本间状态才一致）
> 依据：issues/20-scheduler-split-and-scale.md；现状单实例语义见 scheduler.py:169、
> deploy-requirements.md §7 条目 1（:161）

## 1. 现状锚点（已核实）
- build_scheduler 末尾唯一启动闸：`if not settings.test_mode: scheduler.start()`
  （app/services/scheduler.py:238-239）。
- 装配：main.lifespan 建 deps→scheduler→add_hit_flush_job（main.py:203-205，partial 绑定
  hit_counter 实例）；catchup 后台线程（main.py:210-212）与 scheduler.py:341-358 的
  _run_eval_auto_job 各自延迟 import _build_chat_deps 独立建 deps——19 落地后这些副本
  天然共享 PG 态，本工单不再改它们。
- 部署：Dockerfile CMD uvicorn 单进程（Dockerfile:42）；entrypoint 先迁移后自愈（docker/
  entrypoint.sh:5-6、13-30），其注释自知多实例互杀风险（:10-12）；compose app 固定
  container_name（docker-compose.yml:116）。
- healthz/SSE/静态托管无状态；唯一有状态点已由工单 19 外置。

## 2. SCHEDULER_ENABLED 开关
Settings 增：`scheduler_enabled: bool = True`（缺省 true=现形态零变化）。
build_scheduler 启动闸改为（scheduler.py:238-239）：
```python
if not settings.test_mode and settings.scheduler_enabled:
    scheduler.start()
```
test_mode 短路顺序不变（测试进程永不起后台线程）；开关只影响 start，任务装配恒完整
（便于单测断言 job 注册表）。

## 3. advisory lock 抢主（双保险，防"多容器都开了开关"的配置事故）
### 3.1 机制
- 锁键：`hashtext('ragdemo:scheduler:leader')`，PG 会话级 `pg_try_advisory_lock`。
- 连接持有方式（关键裁决）：**独立 NullPool 引擎专持锁连接**——
```python
# scheduler.py 新增模块级
_LEADER_KEY = "ragdemo:scheduler:leader"
_leader_engine = None

def try_acquire_leader(settings: Settings) -> bool:
    """进程级抢主：pg_try_advisory_lock(hashtext(key))，持锁连接随进程生命周期。"""
    global _leader_engine
    _leader_engine = create_engine(settings.database_url, poolclass=NullPool)
    conn = _leader_engine.connect()
    got = conn.exec_driver_sql("SELECT pg_try_advisory_lock(hashtext(%s))", (_LEADER_KEY,)).scalar()
    if not got:
        conn.close()
        return False
    return True   # 连接保持打开：会话级锁随连接存活；进程退出 TCP 断开 PG 自动释放
```
  **为何不用 SessionLocal 池连接**：池连接会被 recycle/归还，锁随连接回收静默丢失 →
  出现"第二个调度器"。NullPool 独立连接生命周期=进程，锁不可漂移。
- 闸的位置：build_scheduler 内（保持签名与返回类型，tests 不破坏）：
```python
is_leader = False
if not settings.test_mode and settings.scheduler_enabled:
    try:
        is_leader = try_acquire_leader(settings)
    except Exception:
        logger.exception("调度主锁获取失败（本进程不启动调度器，misfire+catchup 兜底）")
    if is_leader:
        scheduler.start()
scheduler.is_leader = is_leader   # BackgroundScheduler 对象可挂属性，供 lifespan 读
```
- 未抢到行为：info 日志"调度主锁已被其他实例持有，本进程不启动调度"；scheduler 返回但
  running=False，add_hit_flush_job 挂上的任务永不触发（未 start）。
- 释放：lifespan finally（main.py:215-217）shutdown 后调 `release_leader()`——关 NullPool
  连接并 dispose 引擎；异常吞掉留日志（进程退出兜底释放）。
### 3.2 与 catchup 线程的关系（裁决：catchup 只在 leader 跑）
main.py:210-212 改为：
```python
if getattr(scheduler, "is_leader", False):
    threading.Thread(target=_startup_catchup, args=(settings,), daemon=True, name="eval-catchup").start()
```
理由：catchup 是分钟-小时级 LLM 任务（main.py:208 注释），N worker 各跑一遍 = N 倍 LLM
账单；catchup 与 cron 靠"同周幂等"互斥（deploy-requirements §7.3），幂等但烧钱。锁是
天然的单写者信号，顺带收敛 catchup 归属，不加第二个锁键。

## 4. gunicorn 引入
- pyproject dependencies 增 `gunicorn`（用途：多 worker 进程模型，SSE 兼容 uvicorn worker）。
- Dockerfile:42 改 shell 形式（env 展开需经 shell）：
```dockerfile
CMD gunicorn app.main:app --workers ${WEB_CONCURRENCY:-1} \
    --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 \
    --timeout 0 --graceful-timeout 60 --access-logfile -
```
  - `--timeout 0`：gunicorn watchdog 对长 SSE 流有误杀面，async worker 自有心跳，0 关闭并
    由 compose healthcheck（docker-compose.yml:140-145）兜底挂死检出。
  - WEB_CONCURRENCY 缺省 1 ≈ 现单进程 uvicorn 形态（worker 类同为 uvicorn），兼容零漂移。
- 序列：entrypoint（迁移→自愈→exec gunicorn master）→ master fork N worker → 各 worker 独立
  跑 lifespan（各自建 deps、各自试抢主锁）→ 恰一 worker 持锁调度。迁移与自愈每容器一次
  （entrypoint 在 master 之前），不随 worker 数放大。

## 5. 自愈段 gate（裁决：SCHEDULER_ENABLED 副本才跑自愈）
entrypoint.sh:13 的自愈 heredoc 外包：
```sh
if [ "${SCHEDULER_ENABLED:-true}" = "true" ]; then
  python - <<'PY' ... PY
fi
```
**理由**：自愈把 parsing/running 一律置 failed（entrypoint.sh:17-26），多副本共库时 app 副本
启动自愈会误杀**其他副本正在跑**的任务——entrypoint.sh:10-12 注释已自知此前提（"本容器
独占其数据库"）。scale 拓扑下 scheduler 副本不对外服务、单副本，是最接近"独占"前提的
写者；app 副本（SCHEDULER_ENABLED=false）跳过自愈。**残余风险（明示）**：app 副本在途
解析恰逢 scheduler 副本重启 → 误标 failed，管理员可重试，量级可接受；按实例归属加过滤
（instance_id 列）留触发线后置，不提前建设。

## 6. compose 覆盖文件 deploy/docker-compose.scale.yml 草案
```yaml
# 最小商用拓扑覆盖（依赖工单 19/20 已合入；未开 STATE_BACKEND=pg 禁用本文件——
# 多副本 + 进程内状态 = 状态分裂，顶部注释 fail-fast 声明）
services:
  app:
    container_name: !reset null        # compose ≥ v2.24 支持清除基文件固定名（ replicas 前提）
    ports: ["8000-8003:8000"]          # 副本占连续宿主端口；统一入口/LB 留触发线，不提前建
    environment:
      SCHEDULER_ENABLED: "false"       # api 副本不跑调度不自愈
      STATE_BACKEND: "pg"              # 工单 19 的开关（本文件的前置条件）
      WEB_CONCURRENCY: "2"
    deploy:
      replicas: 2
  scheduler:                            # 同镜像同 CMD：调度在 lifespan 内随开关/抢主启停
    build: { context: ., dockerfile: Dockerfile }
    environment:
      SCHEDULER_ENABLED: "true"
      STATE_BACKEND: "pg"
      DATABASE_URL: postgresql+psycopg://rag:${POSTGRES_PASSWORD:-rag}@pg:5432/rag
      EMBED_BASE_URL: http://tei-embed:80
      RERANK_BASE_URL: http://tei-rerank:80
    env_file: [.env]
    depends_on:
      app: { condition: service_healthy }   # 迁移由 app 先行，scheduler 晚起避免 alembic 竞态
    healthcheck:                             # 复用 app 同款（healthz 无状态，两副本语义一致）
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status == 200 else 1)"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 60s
    restart: unless-stopped
```
要点：scheduler 不发布端口（healthz 仅容器内网消费）；部署机 compose < v2.24 时退化为
独立 project 文件（`docker compose -p ragdemo-scale -f ...`，app 定义整体复制一份去掉
container_name）——二选一在实施 PR 里按部署机实测钉死并记录 runbook。

## 7. 测试清单（先红）
- tests/unit/test_scheduler_flag.py：
  - "开关缺省 true：非 test_mode 下 build_scheduler 启动（现状回归）"
  - "scheduler_enabled=false：任务装配完整但 running=False、is_leader=False"
  - "test_mode=true：恒不 start（既有语义回归）"
- tests/integration/test_scheduler_leader.py（真库）：
  - "两进程先后 try_acquire_leader：恰一 True 一 False"（两个 NullPool 引擎模拟）
  - "持锁连接关闭后锁释放：第二进程可再抢"
  - "build_scheduler 抢锁失败不 start 且 is_leader=False"
- tests/api/test_lifespan_catchup.py：
  - "非 leader 不起 catchup 线程；leader 恰起一条"（monkeypatch Thread 记录 target）
- 部署验证（非 pytest，进 runbook 检查单）：scale 拓扑双 app 副本 + scheduler 副本起，
  日志恰一份"调度启动"，副本日志各一份"主锁被持有"。

## 8. 文档更新位清单
- docs/deploy-requirements.md §7 条目 1（:161）改写要点：单实例约束解除为"SCHEDULER_ENABLED
  副本分工 + STATE_BACKEND=pg"；补"自愈仅 scheduler 副本执行及其残余风险"一句。
- §7 条目 2（:162）：重启语义改为"memory 后端维持原表述；pg 后端限流/黑名单/缓存跨重启存续"。
- scheduler.py:168-169 docstring："生产按单实例语义"句改为"开关+advisory lock 抢主，多副本
  恰一 leader"（含锁键字面量，便于运维 pg_locks 排查）。
- .env.example：SCHEDULER_ENABLED（部署可选，缺省 true）、WEB_CONCURRENCY（部署可选，
  缺省 1）；STATE_BACKEND 已由工单 19 登记，此处只加指针注释。
- docs/runbook.md：新部署拓扑小节（scale 覆盖文件用法、健康检查口径、锁排查 SQL
  `SELECT * FROM pg_locks WHERE locktype='advisory'`）。
- AGENTS.md"当前状态"节：收口时更新（唯一高频改写段）。

## 9. 裁决点
1. **抢主锁放 build_scheduler 内而非 lifespan**：保持函数签名/返回类型，单测与既有调用点
   零破坏；lifespan 只消费 is_leader 属性，装配与选举同处一函数便于审计。
2. **锁连接用独立 NullPool 引擎**：池连接 recycle 会静默丢锁（最危险的失败形态=双调度器），
   NullPool 生命周期=进程，宁可多一条常驻连接（1 条）换确定性。
3. **catchup 跟随 leader**：幂等但烧 LLM 预算，锁是现成的单写者信号，不为它造第二把锁。
4. **自愈 gate 用 SCHEDULER_ENABLED 而非 leader 锁**：entrypoint 在 lifespan 之前运行，彼时
   无锁可查；拓扑级单写者（开关）已满足前提，残余风险量化后可接受（§5）。
5. **scheduler 副本复用完整 app 镜像与 CMD**：零新增进程类型代码、healthz/迁移/依赖全部
   复用；代价是 scheduler 副本也装配 API（不发布端口），换来的简单性值得。

## 10. 评审修订（2026-10-31，测试工程师 QC R4-R6）

- **R4 test_scheduler.py:219 必破（修正"零破坏"声明）**：`test_build_scheduler_starts_in_normal_mode`（test_mode=False 直调 build_scheduler）在新闸下会真连库抢锁——修正方案：该用例 monkeypatch `try_acquire_leader` 恒 True（隔离选举与启动语义）；另补"try_acquire_leader 失败→不 start"由 test_scheduler_leader.py 真库用例承载。设计 §1"既有零破坏"改为"一处用例按本方案适配"。
- **R5 api 测试 lifespan 影响评估（补入 §7）**：tests/api 全部 client fixture 以 test_mode=True 起 TestClient——build_scheduler 在 test_mode 短路，**不触抢锁不连库**，无基建影响；tests/integration 无起 app 的非 test_mode 用例（grep 核实）——结论：抢锁仅在显式构造的非 test_mode 场景触发，全部有 monkeypatch 或真库专项用例覆盖。
- **R6 票面"workers=2 下 PG 正确"验证归宿（补入 §7）**：该验收项不在 pytest 范围——归部署机复测窗口（工单 13 部署验证检查单），本工单交付 compose.scale.yml 与 runbook 检查单（双副本日志恰一份"调度启动"+state_counters 共享读回），设计补注验证归属。
