# ADR-001：Codex 固定提交为架构真源

- 状态：Accepted
- 日期：2026-07-29

## 决策

以 `openai/codex@9a6668f674d74b35418fa534b3b6285a315d0765` 的 `codex-rs/` 作为本次重构的架构和行为真源。Coomi 继承其 Agent Loop、Thread/Turn/Item、上下文、compact、rollout、工具路由、TUI、权限和沙箱语义。

## 原因

固定 commit 使设计依据、测试结果和许可证来源可复现，也避免开发过程中追逐上游主分支造成协议漂移。Coomi 当前 Python 行为只作为迁移与基准参考；冲突时默认采用固定 Codex 语义。

## 后果

- 上游来源由根目录 `UPSTREAM_CODEX.toml` 记录；
- 任何偏离必须有 benchmark、ADR 和回归测试；
- 不以“功能相似”为理由重写核心循环；
- 更新基线必须走 ADR-002 的独立同步流程。
