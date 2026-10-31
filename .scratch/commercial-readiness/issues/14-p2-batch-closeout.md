# 14: 剩余 P2 批量收口（架构与体验小项打包）

**What to build:** 评审报告 P2 清单中剩余可编码项一次性清零——交付形态收尾，无新能力。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] 镜像 digest 钉版：prod compose 的 gotenberg/pgvector/TEI 三方镜像补 digest（对齐自述"一律 digest"），登记 deploy/releases.md
- [ ] DB 连接池 env 可配：pool_size/max_overflow 进 Settings（缺省维持 5+10），注释标定压测口径
- [ ] STATIC_DIR 并入 Settings（pydantic 字段），消除 os.getenv 形态差异
- [ ] SSE 中断重试不重复计额度：mark_interrupted 轮次重试走同额度（DailyCounter 口径裁决+实现+测试）
- [ ] eval_synth.py 拆分（317 行红线）：seed/doc 两通道模块 + 共用解析模块，单函数 ≤50 行
- [ ] 0014 downgrade 防断：downgrade 先清理 NULL triggered_by 行
- [ ] OverviewView.vue:64-65 注释错挂修正
- [ ] e2e-case-matrix.md 对账重写（15 spec/51 用例现状入账，真缺口清单单列）
- [ ] 每项带测试/验证；make ci + 前端四门全绿
