# ADR-011：旧数据迁移与 Python 退出

- 状态：Accepted
- 日期：2026-07-29

## 决策

Python Coomi 在 P0-P6 保留为只读行为、数据格式和成本基线。Rust 实现采用显式导入器迁移配置、Memory、Skill、MCP 和可恢复会话；不让 Rust 运行时长期读取两套可变存储。

满足以下条件后才能删除 Python 产品路径：B01-B10 基线达标、关键迁移可回滚、Rust release/CI 不调用 Python、用户入口只指向 `coomi` Rust binary、发布说明包含兼容和备份策略。

## 后果

旧实现不会逐文件翻译，也不会在迁移完成前被删除。测试行为逐步转为 Rust integration/snapshot tests；无法可靠迁移的数据必须报告而不是静默丢弃。
