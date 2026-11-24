# Paste2Run

**面向网页 AI 的本地零依赖代码落地与轻量 Agent 桥接器（Local Web-AI Bridge & Lightweight Agent）**

![release](https://img.shields.io/badge/release-v0.5.1-orange)
![license](https://img.shields.io/badge/license-MIT-brightgreen)
![python](https://img.shields.io/badge/python-3.8+-blue)
![deps](https://img.shields.io/badge/deps-zero-lightgrey)

`Paste2Run`（`p2r`）是一个运行在本机环境下的零依赖工具，由一个 `p2r.py` 本地服务和一个 `p2r.user.js` 油猴脚本组成。它实时感知 ChatGPT、Claude、Gemini、DeepSeek、LMArena、豆包等网页 AI 输出的 `===FILE:===`、`===TOOL:===`、`===RUN===` 段落或项目 ZIP 包，自动完成落盘、备份、启动预览与报错归因，无需手动建文件或反复复制粘贴。

**核心设计原则：零 Token 消耗、本地优先、默认安全收敛。** `Paste2Run` 不调用任何付费 API，仅监听 `127.0.0.1` 并强制校验 `X-P2R-Token` / `Host` / `Origin`。默认模式下只写项目目录、绝不越界（拦截 `..`、绝对路径、`.git` 与 `.p2r`）、覆写前自动快照可回滚、默认不自动执行命令且绝不篡改非空聊天输入框。

## 核心能力

| 模块 | 说明 |
| :--- | :--- |
| **自动落盘与容错解析** | 识别 `===FILE: 相对路径===`，自动剥离 Markdown 代码块围栏、截断闭合围栏后的闲聊文本，并自动剔除 `P2R-READY` 门禁标记。 |
| **项目隔离与快照回滚** | 支持 `===PROJECT: 名称===` 与按文件重合度自动分目录；提供 `+NEW` 一键切空白新项目、`BACKUP: LAST` 防膨胀备份及 `POST /rollback` 一键回滚。 |
| **零 Token 轻量只读 Agent** | 支持 `===TOOL: tree \| list_files \| read_file \| search \| status===`，网页 AI 可按需查本地源码，结果默认写入剪贴板（可选空框自动填入）。 |
| **落地即预览与两阶段运行** | 纯静态项目通过无缓存内置 HTTP 服务自动打开（解决 `file://` 下 ES Module / `fetch` 跨域失效）；`npm install && npm run dev` 自动拆分，前置同步、末段长驻后台拉起。 |
| **报错归因与修复闭环** | 运行失败先区分「本机环境问题（`env`）」与「代码问题（`file`）」：前者直接给最简运行方案，后者截取 4000 字符报错与目录树生成修复提示词。 |
| **ZIP 三路接管** | 控制台支持拖拽导入；油猴脚本深度 Hook 离屏 `a.click()`、`blob:` 与直链 ZIP，按 `PK` 文件头校验，非 ZIP 原样放行。 |
| **黑白极简 ASCII 控制台** | 启动即开本地控制台，支持深黑 / 纸白一键反色、四套 Prompt 协议切换、Dry-Run 试运行、实时目录树与 `workflow.md` 工作流导出。 |

## 60 秒上手

### 1. 启动本地服务

需要 Python 3.8+（Windows 安装时勾选 **Add Python to PATH**）：

- **Windows（无黑窗静默）**：双击 `start_silent.vbs`
- **Windows（带窗口）**：双击 `start.bat`
- **macOS**：双击 `start.command`（首次右键 → 打开）
- **Linux / 命令行**：`./start.sh` 或 `python3 p2r.py`
- **开机静默自启**：`python3 p2r.py autostart`（取消加 `--off`）

启动后浏览器打开 `http://127.0.0.1:18765/`（端口被占用时自动顺延）。

### 2. 安装油猴脚本

1. 在浏览器安装 [Tampermonkey](https://www.tampermonkey.net/)。
2. 在本地控制台 `01 // USERSCRIPT BRIDGE` 点击 **`[ ↓ INSTALL / UPDATE p2r.user.js ]`** 安装（服务端会自动注入当前端口与 `X-P2R-Token`；如需覆盖未收录站点，可安装同栏的 `ALL-SITES` 宽松版）。

### 3. 开始使用

1. 在控制台 `02 // TARGET FILESYSTEM` 设置项目根目录并保存。
2. 打开任意支持的网页 AI，点击左下角悬浮微条 **`+P2R`** 将协议追加到输入框末尾（新项目前可先点 **`+NEW`** 切到干净空目录）。
3. AI 输出完毕并出现 `P2R-READY` 后，内容停止变化 **1.4 秒** 即自动落盘并按项目类型打开预览。

## 协议格式

```text
===PROJECT: my-app===
===FILE: index.html===
<!doctype html>
<html><body><h1>Hello Paste2Run</h1></body></html>

===FILE: src/main.js===
console.log("ready");

===RUN===
npm install && npm run build
P2R-READY
```

只读工具调用（可选，用于让网页 AI 自行查阅本地代码）：

```text
===TOOL: tree===
===TOOL: read_file===
src/main.js:1-80
===TOOL: search===
initApp
P2R-READY
```

## 悬浮微条与回填模式

AI 网页左下角提供极简悬浮条 `[ +NEW | +P2R | APPLY | OPEN | FILL:CLIP | ⚙ ]`：

- **`+NEW`**：切到全新空白项目子目录，彻底隔离旧项目树。
- **`+P2R`**：向当前输入框末尾追加协议提示词（不覆盖已有输入）。
- **`APPLY`**：忽略 `P2R-READY` 门禁，手动强制落地或执行最后一条回复。
- **`OPEN`**：按项目类型在浏览器打开当前项目入口。
- **`FILL:CLIP / SEMI / AUTO`**：
  - `CLIP`（默认）：工具结果与修复提示词仅写入剪贴板，绝不碰输入框。
  - `SEMI`：仅当输入框为空时自动填入，已有内容时自动降级为剪贴板。
  - `AUTO`：空框自动填入并发送（上限 5 步，`Shift + ⚙` 随时急停）。

## 安全边界

| 维度 | 机制 |
| :--- | :--- |
| **网络隔离** | 仅绑定 `127.0.0.1`，拒绝非本地 `Host`（兼容 IPv6 `[::1]`）与非法 `Origin` |
| **接口鉴权** | 全部写入、执行与工具接口强制校验 `X-P2R-Token`（恒定时间比对） |
| **路径沙箱** | `realpath` 归一化校验，拒绝 `..`、符号链接逃逸、绝对路径、`.git/` 与 `.p2r/` |
| **备份与回滚** | 覆写前默认备份至 `.p2r/backup/last/`，回滚前自动留存 `pre-rollback` |
| **命令限制** | `allow_run` 默认关闭；开启后仍拦截 `sudo`、`rm -rf /`、`mkfs`、`shutdown` 等高危命令，单次超时 300 秒 |

## 命令行与本地接口

### CLI 子命令

| 命令 | 说明 |
| :--- | :--- |
| `python3 p2r.py` / `up` | 启动服务并打开控制台 |
| `python3 p2r.py serve [--port 18765]` | 仅启动服务，不弹浏览器 |
| `python3 p2r.py autostart [--off]` | 配置或取消跨平台开机无黑窗自启 |
| `python3 p2r.py apply <文件\|-> [--dir 目录] [--dry-run]` | 从文件或标准输入手动落地 |
| `python3 p2r.py open [--dir 目录]` | 按项目类型启动并打开预览 |
| `python3 p2r.py doctor` | 环境、权限与解析器自检 |
| `python3 p2r.py selftest` | 运行完整内置单元测试 |
| `python3 p2r.py update [--check] [--tag vX.Y.Z]` | 从 GitHub 拉取更新，只覆盖代码文件并自动备份旧版 |

### HTTP API

| 方法 | 路径 | 鉴权 | 说明 |
| :--- | :--- | :--- | :--- |
| `GET` | `/` `/p2r.user.js` `/ping` `/state` `/config` `/tree` | 无 | 控制台、带令牌脚本、状态与目录树查询 |
| `POST` | `/config` | Token | 更新目录、`allow_run`、`backup_mode`、`auto_open`、`static_open`、`project_mode` |
| `POST` | `/apply` | Token | 解析并写入文件（支持 `dryRun`） |
| `POST` | `/tool` | Token | 执行白名单只读工具并返回 `===TOOL_RESULT===` |
| `POST` | `/new-project` | Token | 切换到空白新项目目录，清除旧项目指针 |
| `POST` | `/run` `/stop` | Token | 两阶段执行命令 / 停止后台 dev server |
| `POST` | `/repair` | Token | 报错归因并生成最简方案或修复提示词 |
| `POST` | `/open` `/rollback` `/export` | Token | 打开项目预览 / 回滚快照 / 导出 `workflow.md` |
| `POST` | `/import-zip` `/fetch-zip` | Token | 导入上传或公网链接 ZIP（上限 40MB） |

## 适配站点

默认内置 31 条 `@match`，覆盖 ChatGPT、Claude、Gemini、AI Studio、DeepSeek、LMArena（含子域）、Kimi、通义 / Qwen、智谱 / Z.ai、文心一言、腾讯元宝、豆包、Copilot、Poe、Grok、Perplexity、Mistral、OpenRouter Chat、HuggingFace Chat、MiniMax、海螺，并采用内容优先匹配与 `ALL-SITES` 宽松版兜底。

## 配置文件与数据路径

- `~/.p2r/config.json`：全局配置（权限 `600`）
- `~/.p2r/p2r.log`：服务与落地日志
- `<项目>/.p2r/last-apply.json`：最近一次落地清单
- `<项目>/.p2r/backup/last/`：覆写前自动快照

## License

[MIT](./LICENSE)