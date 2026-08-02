# Coomi Agent

一个比较纯净的 AI Agent 项目。基于 CLI 的自主 Agent，能读取文件、编辑代码、执行命令、管理任务。

![Coomi Agent 终端界面](https://raw.githubusercontent.com/Septemc/Coomi/main/image/README/1780235212332.png)

## 特性

- **纯净 Agent 设计** — 以开发学习为目标，流程透明，模块边界清晰，便于观察、复盘和二次改造。
- **核心能力完整** — 覆盖文件操作、搜索、Shell、网页、任务、子 Agent、Plan 模式和 Loop 模式等基础工具链。
- **可切换的 LLM Provider** — 统一支持 OpenAI Compatible、OpenAI Responses、Anthropic Messages、Gemini 四种兼容模式。
- **上下文与记忆机制** — 提供上下文压缩、记忆管理、语义召回和工具结果缓存等基础工程能力。
- **流式终端界面** — 基于 Ratatui 的 TUI（Rust 版本），支持流式输出、Markdown 渲染、鼠标滚轮、Plan Mode 和状态信息展示。
- **精选扩展中心** — 在终端内浏览、安装、更新和卸载精选 Skill，并配置和测试热门 MCP Server。

## 安装

### 一键安装（推荐）

**macOS / Linux：**

```bash
curl -fsSL https://raw.githubusercontent.com/Septemc/Coomi/main/install.sh | bash
```

**Windows (PowerShell)：**

```powershell
irm https://raw.githubusercontent.com/Septemc/Coomi/main/install.ps1 | iex
```

安装脚本会自动下载适合你平台的最新版本到 `~/.local/bin`（Windows 为 `%USERPROFILE%\.local\bin`），确保该目录在 PATH 中即可。

### 从 GitHub Releases 下载

前往 [Releases 页面](https://github.com/Septemc/Coomi/releases) 下载对应平台的二进制文件：

| 平台 | 文件 |
|------|------|
| macOS (Apple Silicon) | `coomi-aarch64-apple-darwin.tar.gz` |
| macOS (Intel) | `coomi-x86_64-apple-darwin.tar.gz` |
| Linux (x86_64) | `coomi-x86_64-unknown-linux-gnu.tar.gz` |
| Linux (ARM64) | `coomi-aarch64-unknown-linux-gnu.tar.gz` |
| Windows (x86_64) | `coomi-x86_64-pc-windows-msvc.zip` |

下载解压后将 `coomi`（或 `coomi.exe`）放入 PATH 目录即可。

### 从源码构建

需要 Rust 工具链（`rustup`）。

```bash
git clone https://github.com/Septemc/Coomi.git
cd Coomi/coomi-rs
cargo build --release
```

编译产物在 `coomi-rs/target/release/coomi`（Windows 为 `coomi.exe`）。

### 使用 cargo install

```bash
cargo install --path coomi-rs/ui
```

## 快速开始

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
      "type": "openai_compatible",
      "display": "DeepSeek V4",
      "api_key": "sk-xxx",
      "base_url": "https://api.deepseek.com",
      "model": "deepseek-v4-pro",
      "fast_model": "deepseek-v4-flash"
    }
  }
}
```

支持的兼容模式：`openai_compatible`（最常用）、`openai_responses`（GPT 专用）、`anthropic_messages` 和 `gemini_native`。

## 终端操作

### 快捷键

| 按键 | 功能 |
|------|------|
| `Ctrl+K` | 打开命令面板 |
| `Ctrl+R` | 打开会话历史 |
| `Ctrl+Y` | 复制最近一条助手回复 |
| `Ctrl+L` | 清空时间线 |
| `Alt+M` | 切换模型 |
| `Alt+S` | 打开设置 |
| `Alt+H` | 打开快捷键帮助 |
| `Alt+L` | Loop 控制 |
| `Alt+I` | 启动只读 Side Session |
| `Shift+Tab` | 切换访问策略 |
| `Up/Down` | 编辑器空时：输入历史；有内容时：编辑器内导航 |
| `PageUp/PageDown` | 滚动对话 |
| `Home/End` | 编辑器空时：跳至顶部/底部 |
| `鼠标滚轮` | 滚动对话 |
| `Esc` | 关闭 / 取消 / 双击退出 |
| `Ctrl+C` | 取消当前任务 / 清空输入 / 退出 |

### Slash 命令

在输入框输入 `/` 打开命令选择器：

| 命令 | 说明 |
|------|------|
| `/status` | 显示会话状态 |
| `/compact` | 手动压缩上下文 |
| `/model [id]` | 切换模型 |
| `/history` | 会话历史 |
| `/plan` | 进入 Plan 模式（只读规划） |
| `/exit_plan` | 退出 Plan 模式 |
| `/loop [目标]` | 创建/控制 Loop |
| `/memory` | 记忆管理 |
| `/mcp` | MCP 管理 |
| `/skills` | Skill 管理 |
| `/settings` | 配置设置 |
| `/catalog` | 扩展目录 |
| `/new` | 新会话 |
| `/clear` | 清空时间线 |
| `/quit` | 退出 |

### Plan 模式

输入 `/plan` 进入 Plan 模式 — 策略自动切换为只读，用于规划阶段。在 Plan 模式下提交消息会弹出确认对话框，你可以选择：

1. 退出 Plan 模式并发送（恢复之前的策略）
2. 以只读 Side Session 方式发送
3. 取消

Agent 也可以在工作过程中自动进入/退出 Plan 模式。

### 访问策略

| 策略 | 说明 |
|------|------|
| `read-only` | 禁止写入，只读操作 |
| `workspace-write` | 允许工作区内文件编辑（默认） |
| `full-access` | 完全访问，仍会询问危险命令 |

## Skill 与 MCP 管理中心

在主输入框输入 `/`，可以直接选择已启用的 Skill 和 MCP。选择 Skill 后补充任务，例如
`/skill frontend-design 设计登录页`；它会在当前会话持续激活。
选择 MCP 会进入操作提示，例如 `/mcp memory 保存信息 我的项目名称是 Coomi`。

精选目录只随安装包保存名称、说明和经过验证的上游来源，不会把第三方 Skill/MCP 内容复制进 Coomi。

## 架构

```
coomi-rs/                       # Rust 版本（主版本）
├── engine/                     # Agent 循环、会话管理、工具生命周期
├── services/                   # Provider 注册与适配器
├── tools/                      # 文件、搜索、Shell 工具
├── security/                   # 访问控制与工作区边界
├── ui/                         # Ratatui 终端界面
└── catalogs/                   # 可安装的 MCP/Skill 目录

coomi-py/                       # Python 版本（兼容保留）
└── coomi/                      # 可导入主包
```

## 上下文压缩

三层压缩，当前估算 Token 数超过上下文窗口 90% 时触发：

1. **Microcompact（微清理）** — 将超出保留数量的旧工具结果替换为 `[cleared]` 标记。零 API 开销。
2. **消息裁剪** — 保留第一条消息 + 最近若干条消息。零 API 开销。
3. **LLM 摘要** — 生成结构化摘要。使用当前模型。

## 记忆系统

三层存储，优先级逐级递减：

1. `.coomi/memory/` — 项目本地（最高优先级）
2. `~/.coomi/projects/{hash}/memory/` — 项目全局
3. `~/.coomi/memory/` — 全局（所有项目共享）

## 项目参考来源

本项目在工程复现和实现思路上参考了以下项目：

- Claude Code
- Codex
- OpenCode

参考重点主要是 Agent 主循环、工具调用编排、上下文管理和终端交互方式。

## License

MIT，详见 [LICENSE](LICENSE)
