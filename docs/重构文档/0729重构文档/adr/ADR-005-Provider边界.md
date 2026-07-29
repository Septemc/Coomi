# ADR-005：Responses-only Core 与 provider 边界

- 状态：Accepted
- 日期：2026-07-29

## 决策

Core 只处理 Codex canonical Responses 语义。OpenAI 原生 Responses 直接进入 Core；DeepSeek、MiniMax、MiMo、GLM 等差异只能存在于 `coomi-provider-adapters`，由 adapter 将受支持的 wire format 映射为 canonical 请求和事件。

首版 adapter 只允许 `Responses` 和具备原生 function/tool calls 的 `OpenAI Chat Tools` 子集。无原生结构化工具调用的模型标记为 unsupported；能力不完整的模型 fail closed 或显式降级，禁止静默兼容。

## 后果

- Chat history replay、cache 和 reasoning 差异由 adapter 明确声明；
- provider 名称不能等同于模型能力，兼容等级以 model probe 为准；
- Core、ToolRouter、TUI 不包含厂商条件分支；
- 当前预计等级只是待验证假设：OpenAI A、MiniMax A-、DeepSeek/GLM B、MiMo experimental。
