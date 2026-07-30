# Coomi Agent

一个比较纯净的 AI Agent项目。基于CLI的自主 Agent，能读取文件、编辑代码、执行命令、管理任务。

![Coomi Agent 终端界面](https://raw.githubusercontent.com/Septemc/Coomi/main/image/README/1780235212332.png)

## 特性

- **纯净 Agent 设计** — 以开发学习为目标，流程透明，模块边界清晰，便于观察、复盘和二次改造。
- **核心能力完整** — 覆盖文件操作、搜索、Shell、网页、任务、子 Agent 、 Plan 模式和Loop模式等基础工具链。
- **可切换的 LLM Provider** — 统一支持 OpenAI Compatible、OpenAI Responses、Anthropic Messages 三种兼容模式。
- **上下文与记忆机制** — 提供上下文压缩、记忆管理、语义召回和工具结果缓存等基础工程能力。
- **流式终端界面** — 基于 Textual 的 TUI，支持流式输出、工具调用提示和状态信息展示。
- **精选扩展中心** — 在终端内浏览、安装、更新和卸载精选 Skill，并配置和测试热门 MCP Server。

## Skill 与 MCP 管理中心

在主输入框输入 `/`，可以直接选择已启用的 Skill 和 MCP。选择 Skill 后补充任务，例如
`/skill frontend-design 设计登录页`；它会在当前会话持续激活，每轮显示“Skill frontend-design 已触发”。
选择 MCP 会进入操作提示，例如 `/mcp memory 保存信息 我的项目名称是 Coomi`。界面中的“已选择”只表示工具可用，
只有出现“正在调用”和“调用成功”才表示 MCP 工具真实执行。可使用 `/skill deactivate all` 和
`/mcp deactivate all` 清除当前会话状态。

也可以让 Coomi 联网检索合适的 Skill 或 MCP。推荐结果会附带适配 Coomi 的安装内容：Skill 使用可验证的
GitHub 地址或本地目录；MCP 使用完整 JSON、服务 URL 或 `/mcp add` 命令，并说明依赖、环境变量、权限和验证方式。

在主界面按 `F3` 打开 Settings，然后选择“管理 Skill”或“管理 MCP”。管理页面完全支持键盘操作：

| 按键 | 行为 |
| --- | --- |
| `↑` / `↓` | 移动选中项，支持首尾循环 |
| `Enter` | 安装；已安装时检查更新；发现更新后应用更新；MCP 中用于配置或测试并刷新工具 |
| `Delete` | 进入卸载/移除确认，再次按 `Delete` 或 `Enter` 确认 |
| `←` / `→` | 在管理界面切换启用、配置、测试、检查更新和卸载操作 |
| `Esc` | 取消确认或返回 |

主输入框使用 `Enter` 发送，`Shift+Enter`、`Ctrl+Enter` 或 `Ctrl+J` 插入换行。
长代码和多行说明会作为普通对话安全提交；Shell 工具与 TUI 输入相互隔离，输入框会过滤意外泄漏的终端鼠标报告。

精选目录只随安装包保存名称、说明和经过验证的上游来源，不会把第三方 Skill/MCP 内容复制进 Coomi。安装前请检查页面显示的来源、许可证和运行要求。第三方扩展可能执行代码或访问外部服务，应仅安装信任的来源并使用最小权限。

部分 MCP 需要额外运行环境或凭据，例如 Node.js、`npx`、Python、`uvx`、Docker、API Token、允许访问的目录或数据库连接地址。Coomi 会在安装前要求填写必需配置；秘密值不会在列表中明文显示，但会保存在本机 `~/.coomi/config/mcp_servers.json`，请保护该文件。

权限模式严格遵循以下语义：`Ask for approval` 在工具执行前询问；`Approve for me` 自动批准普通操作，仅对风险操作询问；
`Full access` 将所有原生、文本回退、MCP、Agent、网络、写入和命令操作视为已批准，不显示权限确认。

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

支持的兼容模式：`OpenAI Compatible`（最常用）、`OpenAI Responses`（GPT 专用）和 `Anthropic Messages`。旧配置中的 `generic`、`openai`、`anthropic`、`deepseek` 会继续自动兼容。

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
coomi-py/                       # Python 版本
└── coomi/                      # 可导入主包（import coomi）
    ├── __init__.py             # 版本信息
    ├── __main__.py             # python -m coomi 入口
    ├── cli.py                  # CLI 入口（coomi 命令）
    ├── first_run.py            # 首次配置引导
    ├── types.py                # Message, Session, ToolCall, LLMResponse
    ├── engine/                 # Agent 循环、会话与工具执行
    ├── services/               # Provider、上下文、Memory、MCP 与 Skill
    ├── tools/                  # Python 内置工具
    └── ui/                     # Textual TUI 界面

coomi-rs/                       # Rust 版本
├── engine/
├── services/
├── tools/
├── security/
├── ui/
└── catalogs/
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
