# ADR-002：fork-and-prune 与上游同步

- 状态：Accepted
- 日期：2026-07-29

## 决策

采用 fork-and-prune：先保留完整固定 `codex-rs` 使其可编译、可测试，再按依赖图和产品范围裁剪。禁止先复制零散模块再重新拼装核心。

每次上游同步按以下提交边界执行：纯上游机械变更、Coomi 适配修复、产品功能变化分别提交。`upstream-codex` 只允许 fetch，push URL 设为 `DISABLED`。

## 后果

- 上游同步可审计，冲突不会被品牌改动淹没；
- 初期编译规模较大，裁剪速度让位于行为正确性；
- 删除 crate 时必须同时证明对应 prompt、配置、事件和依赖已无消费者；
- 同步步骤与来源字段见根目录 `UPSTREAM_CODEX.md`。
