# Textual Key 事件没有 shift/ctrl 布尔属性

## 问题

在 Textual 的 TextArea 子类中重写 `_on_key` 时，错误地使用了 `event.shift` 和 `event.ctrl` 布尔属性来检测修饰键，导致 `AttributeError: 'Key' object has no attribute 'shift'`。

## 根因

Textual 的 `Key` 事件**没有** `shift`、`ctrl`、`alt` 等布尔属性。修饰键信息编码在 `key` 字符串中：

- 普通 Enter: `event.key == "enter"`
- Ctrl+Enter: `event.key == "ctrl+enter"`
- Shift+Enter: `event.key == "shift+enter"`（终端可能不支持）

## 正确做法

```python
# ❌ 错误
if event.key == "enter":
    if event.shift or event.ctrl:  # AttributeError!
        ...

# ✅ 正确 — 方案1：用 key 字符串判断
if event.key == "ctrl+enter":
    # Ctrl+Enter 处理
elif event.key == "enter":
    # 普通 Enter 处理

# ✅ 正确 — 方案2：用 BINDINGS 声明修饰键组合
BINDINGS = [
    Binding("ctrl+enter", "insert_newline", ...),
    # Enter 由 _on_key 处理
]
```

## 关键规则

1. Textual 的 `Key` 事件用 `key` 字符串表示修饰键，不是布尔属性
2. `event.key` 格式: `"ctrl+enter"`, `"shift+tab"`, `"ctrl+shift+a"` 等
3. BINDINGS 比 `_on_key` 优先级高（BINDINGS 先匹配）
4. 终端对 Shift+Enter 支持不一致，推荐用 Ctrl+Enter 作为换行替代

## 归档信息

- **scope**: project
- **时间**: 2025-05-25
- **相关文件**: `apps/backend/core/ui/widgets/prompt_text_area.py`
