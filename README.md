# Coomi Agent

一个比较纯净的 AI Agent项目。基于CLI的自主 Agent，能读取文件、编辑代码、执行命令、管理任务。

![1780235212332](image/README/1780235212332.png)

## 特性

- **纯净 Agent 设计** — 以开发学习为目标，流程透明，模块边界清晰，便于观察、复盘和二次改造。
- **核心能力完整** — 覆盖文件操作、搜索、Shell、网页、任务、子 Agent 、 Plan 模式和Loop模式等基础工具链。
- **可切换的 LLM Provider** — 支持 DeepSeek、OpenAI、Anthropic 和兼容 OpenAI API 的通用服务。
- **上下文与记忆机制** — 提供上下文压缩、记忆管理、语义召回和工具结果缓存等基础工程能力。
- **流式终端界面** — 基于 Textual 的 TUI，支持流式输出、工具调用提示和状态信息展示。

## 快速开始

### 环境要求

- Python >= 3.9

### 安装

#### 从 PyPI 安装（推荐）

```bash
pip install coomi-agent
```

#### 从源码安装

```bash
git clone https://github.com/Septemc/Coomi.git
cd Coomi
pip install -e .
```

### Python 环境说明

> **注意**：如果你的系统有多个 Python 环境，`pip install -e .` 会安装到当前 shell 激活的 Python 环境中。

**推荐安装位置**：默认 Python 环境对应的库目录，也就是当前 Python 解释器的 `site-packages`。

| 环境类型    | 常见安装位置                          |
| ----------- | ------------------------------------- |
| 系统 Python | `Python 安装目录\Lib\site-packages` |
| Conda 环境  | `环境目录\Lib\site-packages`        |
| 虚拟环境    | `项目目录\.venv\Lib\site-packages`  |

**验证当前环境**：

```bash
# 查看 Python 路径
which python
# 或 Windows:
where python

# 查看 pip 目标路径
python -m pip show coomi-agent | grep Location
```

**推荐安装方式**：

先激活你希望使用的默认 Python 环境，再执行：

```bash
python -m pip install -e .
```

### 更新

#### 从 PyPI 更新（推荐）

```bash
pip install --upgrade coomi-agent
```

#### 从源码更新

```bash
cd Coomi
git pull origin main
pip install -e .
```

**注意**：从源码安装（`pip install -e .`）时，代码会指向你的本地目录，每次 `git pull` 后无需重新安装。只有当 `pyproject.toml` 中的依赖发生变化时才需要重新执行 `pip install -e .`。

### 首次运行

```bash
coomi
```

首次运行时，Coomi 会自动引导你配置 LLM Provider。配置优先级：

1. **环境变量** — 自动检测 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`
2. **.env 文件** — 项目根目录的 `.env` 文件，用于开发学习使用
3. **交互式配置** — 终端交互引导

### 配置文件

所有模型配置存储在 `~/.coomi/config/providers.json`：

```json
{
  "version": 1,
  "active": "default",
  "providers": {
    "default": {
      "type": "deepseek",
      "display": "DeepSeek V4",
      "api_key": "sk-xxx",
      "base_url": "https://api.deepseek.com",
      "model": "deepseek-v4-pro",
      "fast_model": "deepseek-v4-flash"
    }
  }
}
```

支持的 Provider 类型：`deepseek`、`openai`、`anthropic`、`generic`（任意兼容 OpenAI API 的服务）。

### 运行方式

```bash
# CLI 命令（推荐）
coomi

# 模块运行
python -m coomi
```

## 内置命令

| 命令                        | 说明                                            |
| --------------------------- | ----------------------------------------------- |
| `/model`                  | 列出所有可用模型                                |
| `/model <id>`             | 切换到指定 Provider                             |
| `/context`                | 显示当前上下文窗口大小                          |
| `/context 256k`           | 设置上下文窗口（如 `128k`、`512k`、`1m`） |
| `/memory list`            | 列出所有记忆                                    |
| `/memory add <内容>`      | 添加新记忆                                      |
| `/memory search <关键词>` | 搜索记忆                                        |
| `/memory delete <名称>`   | 删除记忆                                        |
| `/clear`                  | 清除当前会话                                    |
| `exit` / `quit`         | 退出                                            |

## 架构

```
coomi/                          # 主包
├── __init__.py                 # 版本信息
├── __main__.py                 # python -m coomi 入口
├── cli.py                      # CLI 入口（coomi 命令）
├── setup.py                    # 首次配置引导
├── types.py                    # Message, Session, ToolCall, LLMResponse
├── engine/
│   ├── loop.py                 # AgentLoop — 感知-决策-执行循环
│   └── session.py              # SessionManager，System Prompt 构建器
├── services/
│   ├── llm/                    # Provider 层（抽象基类 + 4 种实现）
│   │   ├── provider.py         # LLMProvider 抽象基类
│   │   ├── generic.py          # GenericOpenAIProvider（配置驱动）
│   │   ├── deepseek.py         # DeepSeekProvider（thinking mode）
│   │   ├── openai.py           # OpenAIProvider
│   │   ├── anthropic.py        # AnthropicProvider（可选依赖）
│   │   ├── factory.py          # Provider 工厂 + Flash 模型降级
│   │   └── config.py           # ConfigManager（~/.coomi/config/providers.json）
│   ├── context/
│   │   ├── compressor.py       # 三层压缩
│   │   └── cache.py            # 工具结果磁盘缓存
│   └── memory/
│       ├── manager.py          # 三层记忆存储
│       ├── extractor.py        # 对话自动提取
│       └── recall.py           # 语义记忆召回
├── tools/                      # 15+ 内置工具
│   ├── file_ops/               # Read, Write, Edit
│   ├── search/                 # Glob, Grep
│   ├── shell/                  # Bash, PowerShell
│   ├── web/                    # WebFetch, WebSearch
│   ├── task/                   # TodoWrite
│   ├── agent/                  # 子 Agent 委托
│   ├── user/                   # AskUserQuestion
│   └── workspace/              # Plan 模式（进入/退出）
└── ui/                         # Textual TUI 界面
    ├── textual_app.py          # 主应用
    ├── status_line.py          # 状态栏（模型 + Token 用量）
    ├── tool_formatter.py       # 工具调用详情格式化
    ├── screens/                # 屏幕
    └── widgets/                # 组件
```

## 上下文压缩

三层压缩，当前估算 Token 数超过上下文窗口 90% 时触发：

1. **Microcompact（微清理）** — 将超出保留数量（6 条）的旧工具结果内容替换为 `[cleared]` 标记。零 API 开销。
2. **消息裁剪** — 保留第一条消息 + 最近 8 条消息。零 API 开销。
3. **LLM 摘要** — 生成 9 段结构化摘要（核心需求、关键概念、文件与代码、错误与修复、问题解决、用户消息、待办任务、当前工作、建议下一步）。使用当前模型。

## 记忆系统

三层存储，优先级逐级递减：

1. `.coomi/memory/` — 项目本地（最高优先级）
2. `~/.coomi/projects/{hash}/memory/` — 项目全局
3. `~/.coomi/memory/` — 全局（所有项目共享）

记忆类型：`user`（用户偏好）、`feedback`（反馈纠正）、`project`（项目上下文）、`reference`（外部引用）。

MemoryExtractor 自动分析对话并保存相关记忆。MemoryRecall 执行语义选择，将相关记忆注入 System Prompt。

## 项目参考来源

本项目在工程复现和实现思路上参考了以下项目（或源码实现，或界面交互体验）：

- Claude Code
- Codex
- OpenCode

参考重点主要是 Agent 主循环、工具调用编排、上下文管理和终端交互方式，不代表与原项目具有相同实现或功能范围。

## License

MIT，详见 [LICENSE](LICENSE)
