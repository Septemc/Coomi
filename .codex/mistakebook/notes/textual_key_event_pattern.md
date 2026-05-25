# Textual Key 事件修饰键检测模式

## 事项

在 Textual 中检测修饰键的正确方式。

## 核心内容

### Key 事件属性

```python
event.key      # str: "enter", "ctrl+enter", "shift+tab" 等
event.character # str: 实际字符，如 "\r"
event.name     # str: 同 key
event.is_printable  # bool
event.aliases  # list: 别名列表
```

### 修饰键判断

```python
# 正确：用 key 字符串
if event.key == "ctrl+enter": ...
if event.key == "shift+tab": ...
if event.key == "ctrl+shift+a": ...

# 错误：Key 没有这些属性
# event.shift  → AttributeError
# event.ctrl   → AttributeError
# event.alt    → AttributeError
```

### BINDINGS vs _on_key 优先级

1. BINDINGS 先匹配（Textual 的 binding 系统）
2. _on_key 后执行（widget 的事件处理）
3. 如果 BINDINGS 匹配并 stop 了事件，_on_key 不会收到

### 推荐模式

```python
class MyWidget(Widget):
    BINDINGS = [
        Binding("ctrl+enter", "my_action", show=False),
    ]

    def action_my_action(self) -> None:
        """Ctrl+Enter 触发"""
        ...

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            # 处理 Enter
        else:
            await super()._on_key(event)
```

## 为什么值得记录

这是 Textual 框架的基础知识，错误使用会导致运行时崩溃。

## 归档信息

- **scope**: global
- **时间**: 2025-05-25
