# P0 用例 → 自动化层映射矩阵（测试 B 交付物，W6 前滚动维护）

> 三层口径：**API**（pytest tests/api，后端契约）/ **E2E-L1**（Playwright，TEST_MODE stub，
> 每 MR）/ **E2E-L2**（Playwright @real，TEI+LLM 真链路，部署机每日/里程碑）/ **手工**。
> 本矩阵只管 P0（50 条）；P1 里程碑全量回归、P2 手工（test-cases.md §自动化归属）。

## 已落地的 E2E 用例（frontend/e2e/specs/）

| spec | 用例 | test-cases 编号 | 层 | 断言要点 |
|------|------|----------------|----|----------|
| auth.spec | 正确凭据登录 + 管理导航可见 | AUTH-01 | L1 | 落 /chat；"用户管理"可见 |
| auth.spec | 错误凭据被拒 | AUTH-01b（配套） | L1 | 停留 /login |
| auth.spec | 首登强制改密 | AUTH-04 | L1 | 守卫拦业务页；改密后进 /chat |
| documents.spec | 上传 docx 同步解析就绪 | DOC-01/02 | L1 | 弹窗关闭；行"● 就绪" |
| documents.spec | 空状态引导 | DOC-04 配套 | L1 | 仅空库执行，否则 skip |
| chat.spec | 新会话 + 种子问答对直答 + 引用 | CHAT-01/02 | L1 | cite-card-[1]；qa 命中相似度 1 |
| chat-real.spec | 无关问题逐字拒答 | CHAT-09 | **L2** | 逐字"知识库中未找到相关内容" |
| chat-real.spec | 真实检索命中引用 | DOC-RAG | **L2** | cite-card + rerank 分 |
| security.spec | member 管理面守卫 | SEC-01 | L1 | /admin 重定向；管理菜单隐藏 |
| security.spec | 串库提问拒答（单向冒烟） | SEC-03 | **L2** | 逐字拒答 |

## L1 层的确定性边界（为什么部分断言只在上层做）

- TEST_MODE 下 LLM 输出固定文案（StubLLMProvider.DEFAULT_CHAT_CHUNKS）——**答案语义
  正确性不可断言**，断言"通道命中 + 引用结构"（qa 直答相似度恒 1、cite-card 程序编号）。
- Stub rerank 分恒高于拒答阈值——**拒答语义在 L1 不可测**，红线由 API 测试
  （test_chat_stream 拒答用例）与评测集把守，UI 层归 L2。
- 默认选中最近会话（deviations #18）会让历史触发 stub 改写污染问题向量——qa 直答用例
  必须先开新会话（ChatPage.newConversation）。

## P0 其余用例的归属现状（W6 前补齐 UI 层缺口）

| 域 | P0 条目 | 现状 | 计划 |
|----|---------|------|------|
| AUTH | 03/05/06 | API 层已覆盖 | 锁定提示 UI 断言 → L1（W6 前） |
| CHAT | 04/10/13 | API 层已覆盖 | 停止按钮/反馈弹窗 → L1（W6 前） |
| DOC | 03/05/06/08/10-13/15/17/20 | API 层已覆盖；L1 有 01/02 | 版本上传/zip/到期 → L1 批次（W6 前） |
| QA | 01/02/04/05 | API 层已覆盖 | 导入/启停 UI 流 → L1 批次 |
| RET/EVAL | RET-01/02/06、EVAL-02/04/07/08 | API 层已覆盖 | 检索测试页/评测跑分 UI → L1 批次 |
| ADM/NFR/SEC | ADM-03/04/08、NFR-01/02/03/05、SEC-02/05 | API/部署档 | SEC 串库全量 → 评测集；NFR → 部署机档（§15.9） |

## 运行方式

```bash
# L1 栈（TEST_MODE 由 deploy/docker-compose.e2e.yml 硬编码，杜绝环境传递丢失）：
docker compose -f docker-compose.yml -f deploy/docker-compose.e2e.yml up -d --build pg app
docker compose exec -T -e E2E_PASSWORD=e2e-ci-password app python -m app.db.e2e_seed
cd frontend && E2E_CHANNEL=msedge npm run e2e     # 本机无 Playwright 浏览器下载通道时用系统 Edge
# L2（部署机）：E2E_REAL=1 E2E_CHANNEL=chrome npm run e2e
# CI：e2e job 自动（TEST_MODE，官方 CDN 装 chromium）
```
