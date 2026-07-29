# ADR-010：Coomi TUI 信息架构

- 状态：Accepted
- 日期：2026-07-29

## 决策

以 Codex ratatui runtime、composer、timeline、picker、审批和 streaming 渲染为交互基础；在 P6 重构为 Coomi 的视觉与信息架构，不另建 Web UI 或继续使用 Textual。

主视图保持工作导向：对话时间线、composer、紧凑状态栏和按需活动区。Sessions、Models、Skills、MCP、Memory、Permissions、Diagnostics 作为可导航管理视图，不能以大量常驻卡片挤压编码上下文。

## 验收约束

支持 80x24 至 160x45、CJK、resize、复制、长 streaming 和键盘工作流；显示公开 reasoning summary、工具阶段、权限状态和 usage，不显示或持久化隐藏思维链。
