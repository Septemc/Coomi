# ADR-005A：Codex canonical tool protocol

- 状态：Accepted
- 日期：2026-07-29

## 决策

工具协议的唯一类型真源为 Codex `ToolSpec`、`ResponseItem` 和 `ResponseEvent`。Coomi adapter 可以转换 wire encoding，但不得创建第二套核心工具类型或改变 call id、参数增量、完成事件和输出关联语义。

禁止 XML、DSML、正则、Markdown fence 或自然语言解析作为工具调用 fallback。malformed native tool call 作为协议错误处理，不追加“请按某格式重试”的隐藏模型回合。

## Conformance 门槛

每个 provider/model 至少执行 C01-C10：工具声明、单次调用、流式参数、call id、工具输出回传、多工具、并行声明、schema 边界、取消/断流、malformed/未知工具。必需项未通过时不得稳定启用。

## 后果

`coomi-provider-adapters` 直接重导出 Codex canonical types。协议升级必须先更新固定基线/ADR，再更新 adapter 和 snapshot；UI 不解析 provider wire event。
