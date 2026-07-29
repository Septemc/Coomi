# ADR-004：embedded App Server 边界

- 状态：Accepted
- 日期：2026-07-29

## 决策

首版 CLI/TUI 在同一进程内运行 Codex embedded App Server 与 Core。TUI 只消费协议事件并发送命令，不直接拥有模型、工具或会话状态。

远程 daemon、桌面端和 IDE transport 不进入首版产品范围，但保留协议边界，避免未来扩展迫使 Core 重写。

## 后果

- 首版没有 daemon 部署、鉴权和版本协商成本；
- Thread 状态保持单一真源；
- TUI 崩溃恢复、取消和 streaming 仍由上游事件语义约束；
- `coomi-cli` 直接链接 Rust crate，不 shell out 到 `codex` binary。
