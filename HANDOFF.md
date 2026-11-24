# Paste2Run AI 接力文档 (HANDOFF.md)

> 换 AI 或额度耗尽时，直接把本文件贴给下一个 AI 即可零损耗接棒。

## 1. 定位与不可妥协红线

- **定位**：零第三方依赖、零 Token 消耗的本地网页 AI 轻量 Agent 桥接器（1 个 `p2r.py` + 1 个 `p2r.user.js`）。
- **核心约束**：
  1. 仅使用 Python 3.8+ 标准库，严禁引入 `pip` 第三方依赖。
  2. 仅监听 `127.0.0.1`，所有写操作与工具调用必须校验 `X-P2R-Token`、`Host`（兼容 IPv6 `[::1]`）与 `Origin`。
  3. 路径写读全部走 `safe_target()`（拦截 `..`、绝对路径、符号链接逃逸及 `.git`/`.p2r`）。
  4. 控制台保持**黑白极简 ASCII 终端风**（等宽字体、无圆角、深黑/纸白反色）。
  5. `===TOOL:===` 仅开放只读白名单工具，不可绕过 `allow_run` 执行任意命令。

## 2. 当前版本与文件清单（v0.5.1）

| 文件 | 职责 |
| :--- | :--- |
| `p2r.py` | 本地 HTTP 服务、协议解析器、项目隔离、ZIP 落地、两阶段运行器、报错归因、无缓存内置静态服、轻量 Agent 只读工具、开机无窗自启、黑白 ASCII 控制台 HTML |
| `p2r.user.js` | 28 站点油猴脚本：DOM 内容优先检测、`===TOOL:===` 观察回填闭环（SEMI/AUTO/OFF）、左下角极简微条（`+P2R`/`APPLY`/`OPEN`/`AGENT`/`⚙`）、三路 ZIP 拦截 |
| `start_silent.vbs` | Windows 双击无黑窗后台启动脚本 |
| `start.bat` / `start.command` / `start.sh` | 跨平台标准启动脚本 |

## 3. 文本协议规范

- `===PROJECT: 英文小写名===`：指定项目子目录名。
- `===TOOL: tree | list_files | read_file | search | status===`：网页 AI 请求本地只读上下文，油猴调 `POST /tool` 并将 `===TOOL_RESULT===` 自动回填输入框。
- `===FILE: 相对路径===`：写入完整文件内容（自动剥离代码块围栏与末尾闲聊）。
- `===RUN===`：可选启动/构建命令（两阶段执行，前置同步、末段长驻后台拉起）。
- `P2R-READY`：独立行门禁标记，解析器自动剥离，避免污染代码或被当成命令执行。

## 4. 必跑验收命令

每次修改后必须全部通过：

```bash
python3 p2r.py doctor
python3 p2r.py selftest
```

## 5. 下一阶段（v0.6.0）优先事项

1. **轻量前端 Style Kits**：新增 `templates/<风格>/`（`tokens.css` + 20 行硬约束），控制台一键选用并自动注入项目与提示词。
2. **多媒体资产落地**：新增 `POST /apply-media` 接收图片/视频/音频落盘至 `assets/` 并写 `.meta.json` 记录 Prompt。
3. **模块拆分**：`p2r.py` 超 3000 行后可按 `parser` / `runner` / `tools` / `ui` 做清晰分区或拆包。
