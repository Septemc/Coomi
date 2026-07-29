# ADR-008：Memory、Skill 与 MCP 渐进加载

- 状态：Accepted
- 日期：2026-07-29

## 决策

Memory、Skill 和 MCP 默认不向模型全量注入内容或 schema。Skill 先发现 metadata，命中后加载正文和必要引用；Memory 按用户/项目作用域、相关性和预算检索；MCP 先加载 server/tool 索引，通过 tool search 或明确选择后才暴露完整 schema。

## 不变量

- 功能禁用时模型可见成本为 0；
- 低信号输入不触发 Memory 辅助模型调用；
- 100 个 MCP tools 不得每轮全量进入 prompt；
- MCP 工具走统一权限、沙箱、输出裁剪和审计；
- Skill/MCP 热更新不得修改正在执行 Turn 的协议快照。
