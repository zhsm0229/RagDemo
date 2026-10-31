# 01: git 基线冻结——F-1019 全部交付分批提交 + tag

**What to build:** 当前工作树的 86 条脏项（4 个迁移 0013-0016、~10 个新 service/api、~10 个测试文件、uv.lock、前端评测域五 tab、文档五处同步、golden fixtures）按功能分组提交为干净的 Conventional Commits 序列，打上版本 tag。完成后 `git status` 干净、`git log` 可读、交付基线可复现——这是商用交付合同意义上"存在交付物"的前提。

**Blocked by:** None (can start immediately)。

**注意（评审 P0-3 证据）**：工作树混杂两个会话的改动（scheduler.py / test_scheduler.py / admin_overview.py 为 CRLF 行尾）；`.git/worktrees/` 残留 3 个指向 `D:/Project/...` 的 prunable worktree；`.v2c/`、`.video_agent/`、`.DS_Store`、`.vscode/` 为无关工具产物。提交前先与并行会话核对在途文件清单。**禁止直推 main 的约束不适用于本机 dev 仓库（本仓库即开发主干），但提交信息须符合 Conventional Commits。**

**Status:** ready-for-agent

- [ ] 无关产物加入 .gitignore 并从工作树剔除（.v2c/.video_agent/.DS_Store/.vscode/out 等）
- [ ] `git worktree prune` 清理失效 worktree
- [ ] F-1019 改动按工单分组提交（feat/test/docs 分离，一组提交一件事），CRLF 行尾规整
- [ ] 打 tag（如 v0.9.0-f1019），`git status` 干净
- [ ] 提交后跑一次 `make ci` 与前端四门确认提交内容自洽（fpdf 失败除外，见工单 02）
