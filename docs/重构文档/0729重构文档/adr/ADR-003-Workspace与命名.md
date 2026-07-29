# ADR-003：workspace 目录与内部命名

- 状态：Accepted
- 日期：2026-07-29

## 决策

保持单一 `codex-rs/` Cargo workspace。固定上游 crate 保留 `codex-*` 名称；Coomi 自有代码使用 `coomi-*` crate，并作为同一 workspace member。首批为 `coomi-cli` 和 `coomi-provider-adapters`。

用户可见 binary、命令、配置根和 TUI 品牌使用 Coomi。P0-P6 不进行全量内部 crate rename。

## 原因与后果

单一 workspace 能复用锁文件、workspace lint、patch、toolchain 和内部 path dependency，避免双 workspace 的版本和 feature 漂移。内部名称会继续暴露上游来源，但大幅降低无业务价值的 merge 冲突。全量 rename 必须在稳定发布后单独评估。
