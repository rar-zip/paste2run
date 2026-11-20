#!/usr/bin/env python3
"""Paste2Run 本地落地器

接收网页 AI 回复的纯文本，按 ===FILE: 路径=== 标记拆成文件，写进指定项目目录。

用法：
  python3 p2r.py                             # 启动并打开引导页（等价 up）
  python3 p2r.py up        [--port 18765]    # 启动服务并打开浏览器
  python3 p2r.py serve     [--port 18765]    # 只启动服务
  python3 p2r.py apply reply.md [--dir 目录]  # 手动把一份回复落地，- 表示读 stdin
  python3 p2r.py doctor                      # 环境自检
  python3 p2r.py selftest                    # 解析自检

协议（让 AI 按这个格式输出）：
  ===FILE: src/app.js===
  <完整文件内容>
  ===FILE: package.json===
  <完整文件内容>
  ===RUN===
  npm install && npm run dev
"""
import argparse
import hmac
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.3.1"
HOME = os.path.expanduser("~/.p2r")
CONFIG = os.path.join(HOME, "config.json")
LOG = os.path.join(HOME, "p2r.log")
DEFAULT_PORT = 18765
DEFAULT_TARGET = os.path.join(os.path.expanduser("~"), "p2r-projects")
MAX_BODY = 4 * 1024 * 1024
PORT_TRIES = 10

FILE_RE = re.compile(r"^[ \t]*(?:\*\*)?===\s*FILE:\s*(.+?)\s*===\s*(?:\*\*)?[ \t]*$", re.M)
RUN_RE = re.compile(r"^[ \t]*(?:\*\*)?===\s*RUN\s*===\s*(?:\*\*)?[ \t]*$", re.M)

_CONFIG_LOCK = threading.Lock()


def log(msg):
    os.makedirs(HOME, exist_ok=True)
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if sys.stderr.isatty():
        sys.stderr.write(line + "\n")


def default_config():
    return {"target": DEFAULT_TARGET, "allow_run": False, "port": DEFAULT_PORT,
            "token": secrets.token_hex(16)}


def load_config():
    """读配置；仅在配置缺失或字段异常时补齐落盘，避免每次 HTTP 请求都写盘竞态。"""
    cfg = default_config()
    dirty = not os.path.exists(CONFIG)
    if not dirty:
        try:
            with open(CONFIG, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key in ("target", "token", "port"):
                    if not loaded.get(key):
                        dirty = True
                cfg.update(loaded)
            else:
                dirty = True
        except Exception:
            dirty = True
    if not cfg.get("target"):
        cfg["target"] = DEFAULT_TARGET
        dirty = True
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
        dirty = True
    try:
        cfg["port"] = int(cfg.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        cfg["port"] = DEFAULT_PORT
        dirty = True
    if dirty:
        save_config(cfg)
    return cfg


def save_config(cfg):
    with _CONFIG_LOCK:
        os.makedirs(HOME, exist_ok=True)
        tmp = "%s.%d.%d.tmp" % (CONFIG, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG)
        try:
            os.chmod(CONFIG, 0o600)
        except OSError:
            pass


def strip_fences(body):
    """去掉 AI 习惯性包在代码块外面的 ```lang 和结尾 ```，并截断结尾围栏之后的闲聊结语。"""
    lines = (body or "").strip("\n").splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        close_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            if lines[idx].strip() == "```":
                close_idx = idx
                break
        if close_idx is not None:
            lines = lines[:close_idx]
    elif lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines) + "\n"


def parse(text):
    """返回 (files: [(path, content)], run_commands: str)"""
    text = (text or "").replace("\ufeff", "").replace("\r\n", "\n")
    markers = []
    for mt in FILE_RE.finditer(text):
        markers.append(("file", mt.start(), mt.end(), mt.group(1).strip().strip("`\"'")))
    for mt in RUN_RE.finditer(text):
        markers.append(("run", mt.start(), mt.end(), ""))
    if not markers:
        return [], ""

    markers.sort(key=lambda item: item[1])
    files = []
    run_parts = []
    for i, (kind, _start, end_pos, label) in enumerate(markers):
        next_start = markers[i + 1][1] if i + 1 < len(markers) else len(text)
        chunk = text[end_pos:next_start]
        if kind == "file":
            files.append((label, strip_fences(chunk)))
        else:
            cmd = strip_fences(chunk).strip()
            if cmd:
                run_parts.append(cmd)
    return files, "\n".join(run_parts).strip()


PROTECTED_TOP_DIRS = {".git", ".p2r"}


def safe_target(root, rel):
    """把模型给的相对路径限制在 root 内，拒绝绝对路径、.. 以及 .git/.p2r 受保护目录。"""
    raw = (rel or "").replace("\\", "/")
    if raw.startswith("/") or raw.startswith("~") or re.match(r"^[a-zA-Z]:", raw):
        raise ValueError("拒绝绝对路径: %s" % rel)
    cleaned = raw
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned or cleaned in (".", "..") or "\0" in cleaned:
        raise ValueError("非法路径: %r" % rel)
    parts = [p for p in cleaned.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise ValueError("路径越界: %s" % rel)
    if parts[0] in PROTECTED_TOP_DIRS:
        raise ValueError("拒绝写入受保护目录: %s" % rel)
    full = os.path.realpath(os.path.join(root, cleaned))
    root_real = os.path.realpath(root)
    if not (full == root_real or full.startswith(root_real + os.sep)):
        raise ValueError("路径越界: %s" % rel)
    return full


def apply_text(text, target, dry_run=False, backup=True, source="manual"):
    files, run_cmds = parse(text)
    if not files:
        return {"ok": False, "error": "没找到 ===FILE: ...=== 标记", "files": []}

    root = os.path.abspath(os.path.expanduser(target))
    os.makedirs(root, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    written, skipped = [], []

    for rel, content in files:
        try:
            full = safe_target(root, rel)
        except ValueError as exc:
            skipped.append({"path": rel, "reason": str(exc)})
            continue
        if dry_run:
            written.append({"path": rel, "bytes": len(content.encode()), "dryRun": True})
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if backup and os.path.exists(full):
            bdir = os.path.join(root, ".p2r", "backup", stamp)
            bpath = os.path.join(bdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            shutil.copy2(full, bpath)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        written.append({"path": rel, "bytes": len(content.encode())})

    manifest = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "target": root,
        "files": written,
        "skipped": skipped,
        "run": run_cmds,
    }
    if not dry_run:
        os.makedirs(os.path.join(root, ".p2r"), exist_ok=True)
        with open(os.path.join(root, ".p2r", "last-apply.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
    log("apply source=%s target=%s files=%d skipped=%d dry=%s"
        % (source, root, len(written), len(skipped), dry_run))
    return {"ok": True, **manifest}


SKIP_DIRS = {".git", "node_modules", ".p2r", "__pycache__", ".venv", "venv", "dist", "build"}
MAX_TAIL = 4000


def project_tree(root, max_entries=80, max_depth=2):
    """给修复 prompt 和前端展示用的轻量 ASCII 目录树。"""
    lines = []

    def walk(base, prefix, depth):
        if len(lines) >= max_entries or depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return
        entries = [e for e in entries if e not in SKIP_DIRS and not e.startswith(".DS_Store")]
        entries.sort(key=lambda e: (not os.path.isdir(os.path.join(base, e)), e.lower()))
        for idx, name in enumerate(entries):
            if len(lines) >= max_entries:
                return
            full = os.path.join(base, name)
            is_last = idx == len(entries) - 1
            branch = "└── " if is_last else "├── "
            is_dir = os.path.isdir(full)
            lines.append(prefix + branch + name + ("/" if is_dir else ""))
            if is_dir:
                walk(full, prefix + ("    " if is_last else "│   "), depth + 1)

    walk(root, "", 0)
    if len(lines) >= max_entries:
        lines.append("… (已截断)")
    return "\n".join(lines)


BLOCKED_PATTERNS = (
    re.compile(r"\bsudo\b"),
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+/(\s|$)"),
    re.compile(r"\brm\s+-[a-z]*f[a-z]*r[a-z]*\s+/(\s|$)"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdiskutil\s+erase\b"),
    re.compile(r"\bformat\s+c:"),
    re.compile(r":\(\)\s*\{"),
    re.compile(r"\b(shutdown|reboot)\b"),
    re.compile(r"\bdel\s+/[a-z\s]*c:\\"),
)


def run_in(root, commands, timeout=300):
    """在项目目录里执行命令，返回退出码和输出尾部。"""
    if not commands or not commands.strip():
        return {"ok": False, "error": "没有可执行的命令", "code": None, "output": ""}
    lowered = commands.lower()
    for pat in BLOCKED_PATTERNS:
        m = pat.search(lowered)
        if m:
            return {"ok": False, "error": "命令里包含危险操作，已拒绝: %s" % m.group(0), "code": None, "output": ""}
    started = time.time()
    try:
        proc = subprocess.run(
            commands, shell=True, cwd=root, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "CI": "1", "NO_COLOR": "1"},
        )
        out = proc.stdout.decode("utf-8", "replace")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        out = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)) + "\n[超时 %ss]" % timeout
        code = 124
    except OSError as exc:
        return {"ok": False, "error": "无法执行: %s" % exc, "code": None, "output": ""}
    tail = out[-MAX_TAIL:]
    log("run cmd=%r code=%s %.1fs" % (commands[:60], code, time.time() - started))
    return {"ok": code == 0, "code": code, "output": tail, "duration": round(time.time() - started, 1)}


def repair_prompt(root, output, original_mood=""):
    tree = project_tree(root)
    return (
        "上面的项目跑不起来，报错如下：\n\n"
        + (output or "(没有输出，但进程非零退出)").strip()
        + "\n\n当前项目结构：\n" + tree
        + "\n\n请只输出需要修改或新增的文件，仍然使用 ===FILE: 相对路径=== 格式，"
          "每个文件给完整内容，不要 diff，不要省略号，不要解释。"
          "如果错误来自缺少依赖，请同时更新 package.json 或给出安装命令放在 ===RUN=== 段。"
    )


def parse_host_header(header_val):
    """正确提取 Host 头中的主机名（兼容 IPv4、hostname 与带端口的 IPv6 [::1]:18765）。"""
    h = (header_val or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[:end + 1] if end != -1 else h
    return h.split(":", 1)[0]


# ---- 黑白极简 ASCII 控制台 ----
SETUP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PASTE2RUN // ASCII CONTROL CONSOLE</title>
<style>
:root {
  --bg: #070707;
  --surface: #0d0d0d;
  --surface-2: #141414;
  --fg: #f2f2f2;
  --fg-dim: #8a8a8a;
  --fg-faint: #4a4a4a;
  --border: #2a2a2a;
  --border-hi: #f2f2f2;
  --inv-bg: #f2f2f2;
  --inv-fg: #070707;
  --dot: rgba(255,255,255,0.055);
  --mono: "JetBrains Mono","Fira Code","SF Mono","Cascadia Code",ui-monospace,Menlo,Consolas,"PingFang SC","Noto Sans Mono CJK SC",monospace;
}
html[data-theme="light"] {
  --bg: #f5f5f3;
  --surface: #ffffff;
  --surface-2: #ecece8;
  --fg: #0a0a0a;
  --fg-dim: #5c5c5c;
  --fg-faint: #9e9e9e;
  --border: #c8c8c4;
  --border-hi: #0a0a0a;
  --inv-bg: #0a0a0a;
  --inv-fg: #f5f5f3;
  --dot: rgba(0,0,0,0.06);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background-color: var(--bg);
  background-image: radial-gradient(var(--dot) 1px, transparent 1px);
  background-size: 16px 16px;
  color: var(--fg);
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.65;
  padding: 20px 16px 64px;
  -webkit-font-smoothing: antialiased;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.shell { max-width: 1120px; margin: 0 auto; }
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap; gap: 10px; padding-bottom: 12px; margin-bottom: 16px;
  border-bottom: 1px solid var(--border); font-size: 12px; letter-spacing: 0.04em;
}
.topbar-left, .topbar-right { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.badge {
  display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px;
  border: 1px solid var(--border); background: var(--surface);
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
}
.badge.solid { background: var(--inv-bg); color: var(--inv-fg); border-color: var(--inv-bg); font-weight: 700; }
.pulse { display: inline-block; width: 7px; height: 7px; background: var(--fg); animation: blink 1.4s steps(2, start) infinite; }
.pulse.off { background: transparent; border: 1px solid var(--fg-dim); animation: none; }
@keyframes blink { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0.2; } }

.hero { border: 1px solid var(--border); background: var(--surface); padding: 18px 20px 16px; margin-bottom: 16px; }
.hero-grid { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px; flex-wrap: wrap; }
.ascii-logo {
  margin: 0; font-size: clamp(7.5px, 1.35vw, 11.5px); line-height: 1.18;
  letter-spacing: 0.02em; color: var(--fg); overflow-x: auto; white-space: pre; user-select: none;
}
.hero-meta { flex: 1; min-width: 240px; display: flex; flex-direction: column; gap: 8px; align-items: flex-end; text-align: right; }
.hero-tagline { font-size: 12px; color: var(--fg-dim); letter-spacing: 0.03em; }
.pipeline {
  margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--border);
  font-size: 11.5px; color: var(--fg-dim); display: flex; align-items: center;
  justify-content: space-between; flex-wrap: wrap; gap: 8px;
}
.pipe-flow { white-space: pre; overflow-x: auto; color: var(--fg); }
.pipe-flow span { color: var(--fg-dim); }
.cursor { display: inline-block; width: 7px; height: 13px; background: var(--fg); vertical-align: -2px; margin-left: 4px; animation: blink 1s steps(2, start) infinite; }

.grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr); gap: 16px; align-items: start; }
@media (max-width: 860px) {
  .grid { grid-template-columns: 1fr; }
  .hero-meta { align-items: flex-start; text-align: left; }
}
.col { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.panel { border: 1px solid var(--border); background: var(--surface); min-width: 0; }
.panel-head {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 9px 14px; border-bottom: 1px solid var(--border); background: var(--surface-2);
  font-size: 11.5px; letter-spacing: 0.08em; text-transform: uppercase;
}
.panel-title { font-weight: 700; color: var(--fg); }
.panel-title .idx { color: var(--fg-dim); font-weight: 400; margin-right: 6px; }
.panel-body { padding: 14px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }

input[type="text"], textarea {
  width: 100%; background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  padding: 8px 10px; font-family: var(--mono); font-size: 12.5px; line-height: 1.5;
  outline: none; border-radius: 0; transition: border-color 0.12s ease;
}
input[type="text"] { flex: 1; min-width: 180px; }
input[type="text"]:focus, textarea:focus { border-color: var(--border-hi); }
textarea { min-height: 148px; resize: vertical; tab-size: 2; }

button, .btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 7px 12px; border: 1px solid var(--border); background: var(--surface);
  color: var(--fg); font-family: var(--mono); font-size: 12px; letter-spacing: 0.04em;
  text-decoration: none; cursor: pointer; border-radius: 0; white-space: nowrap;
  transition: background-color 0.1s ease, color 0.1s ease, border-color 0.1s ease;
}
button:hover, .btn:hover { border-color: var(--border-hi); background: var(--inv-bg); color: var(--inv-fg); }
button:disabled { opacity: 0.4; cursor: not-allowed; }
button.primary, .btn.primary { background: var(--inv-bg); color: var(--inv-fg); border-color: var(--inv-bg); font-weight: 700; }
button.primary:hover, .btn.primary:hover { background: var(--bg); color: var(--fg); border-color: var(--border-hi); }
button.sm { padding: 3px 8px; font-size: 11px; }
button.tab { border-color: transparent; background: transparent; color: var(--fg-dim); padding: 3px 8px; font-size: 11px; }
button.tab.active { border-color: var(--border-hi); background: var(--inv-bg); color: var(--inv-fg); font-weight: 700; }

.toggle-row {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-top: 12px; padding: 8px 10px; border: 1px dashed var(--border);
  background: var(--bg); cursor: pointer; user-select: none;
}
.toggle-row:hover { border-color: var(--fg-dim); }
.toggle-label { display: flex; align-items: center; gap: 8px; font-size: 12.5px; }
.ascii-ck { font-weight: 700; }
.hint { color: var(--fg-dim); font-size: 11.5px; margin-top: 9px; line-height: 1.6; }
.sites-bar {
  margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border);
  font-size: 11px; color: var(--fg-dim); display: flex; flex-wrap: wrap; gap: 6px;
}
.site-tag { border: 1px solid var(--border); padding: 1px 6px; color: var(--fg); }

pre.box {
  background: var(--bg); border: 1px solid var(--border); padding: 11px 12px;
  margin: 10px 0 0; font-family: var(--mono); font-size: 12px; line-height: 1.55;
  color: var(--fg); white-space: pre-wrap; word-break: break-word;
  overflow-x: auto; max-height: 260px; overflow-y: auto;
}
pre.tree-box { white-space: pre; word-break: normal; max-height: 220px; }

.sec-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; margin-top: 10px; font-size: 11px; }
.sec-item { border: 1px solid var(--border); padding: 5px 8px; display: flex; justify-content: space-between; gap: 6px; background: var(--bg); }
.sec-item .k { color: var(--fg-dim); }
.sec-item .v { color: var(--fg); font-weight: 700; }

.manifest-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 12px; }
.manifest-table th, .manifest-table td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); }
.manifest-table th { color: var(--fg-dim); font-weight: 400; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid var(--fg-dim); }
.manifest-table td.num, .manifest-table th.num { text-align: right; white-space: nowrap; }
.run-box { margin-top: 12px; padding: 10px; border: 1px solid var(--border); background: var(--bg); }
.run-cmd { font-weight: 700; word-break: break-all; }

.toast {
  position: fixed; right: 18px; bottom: 48px; z-index: 999;
  border: 1px solid var(--border-hi); background: var(--inv-bg); color: var(--inv-fg);
  padding: 8px 14px; font-size: 12px; font-weight: 700; letter-spacing: 0.03em;
  opacity: 0; transform: translateY(6px); pointer-events: none;
  transition: opacity 0.15s ease, transform 0.15s ease;
  max-width: min(420px, calc(100vw - 36px));
}
.toast.show { opacity: 1; transform: translateY(0); }

.statusbar {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 50;
  background: var(--inv-bg); color: var(--inv-fg); font-size: 11.5px;
  padding: 6px 16px; display: flex; justify-content: space-between;
  align-items: center; gap: 12px; overflow: hidden;
}
.status-left, .status-right { display: flex; align-items: center; gap: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.status-sep { opacity: 0.45; }
</style>
</head>
<body>
<div class="shell">
  <div class="topbar">
    <div class="topbar-left">
      <span class="badge solid">P2R // v__VERSION__</span>
      <span class="badge"><span class="pulse off" id="dot"></span><span id="state-text">CONNECTING…</span></span>
    </div>
    <div class="topbar-right">
      <button class="sm" id="btn-refresh">[ ↻ SYNC ]</button>
      <button class="sm" id="btn-theme">[ ◐ INVERT B/W ]</button>
    </div>
  </div>

  <section class="hero">
    <div class="hero-grid">
      <pre class="ascii-logo" aria-label="PASTE2RUN">██████╗  █████╗ ███████╗████████╗███████╗██████╗ ██████╗ ██╗   ██╗███╗   ██╗
██╔══██╗██╔══██╗██╔════╝╚══██╔══╝██╔════╝╚════██╗██╔══██╗██║   ██║████╗  ██║
██████╔╝███████║███████╗   ██║   █████╗   █████╔╝██████╔╝██║   ██║██╔██╗ ██║
██╔═══╝ ██╔══██║╚════██║   ██║   ██╔══╝  ██╔═══╝ ██╔══██╗██║   ██║██║╚██╗██║
██║     ██║  ██║███████║   ██║   ███████╗███████╗██║  ██║╚██████╔╝██║ ╚████║
╚═╝     ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝</pre>
      <div class="hero-meta">
        <div class="hero-tagline">ZERO-DEPENDENCY LOCAL AI-TO-DISK BRIDGE</div>
        <div class="hero-tagline">网页 AI 代码流 ──▶ 本地工程目录自动落地</div>
      </div>
    </div>
    <div class="pipeline">
      <div class="pipe-flow">[ AI STREAM ] <span>───▶</span> [ USERSCRIPT ] <span>───▶</span> [ PARSER / GUARD ] <span>───▶</span> [ DISK + BACKUP ] <span>───▶</span> [ EXEC ]<span class="cursor"></span></div>
      <div id="clock">--:--:--</div>
    </div>
  </section>

  <main class="grid">
    <div class="col">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="idx">01 //</span>USERSCRIPT BRIDGE</div>
          <span style="color:var(--fg-dim)">TAMPERMONKEY</span>
        </div>
        <div class="panel-body">
          <div class="row">
            <a class="btn primary" href="/p2r.user.js">[ ↓ INSTALL / UPDATE p2r.user.js ]</a>
            <button id="btn-copy-endpoint">[ COPY ENDPOINT ]</button>
          </div>
          <div class="hint">需要先安装 Tampermonkey。从本页安装会自动注入当前端口与 <code>X-P2R-Token</code>。</div>
          <div class="sites-bar">
            <span class="site-tag">ChatGPT</span>
            <span class="site-tag">Claude</span>
            <span class="site-tag">Gemini</span>
            <span class="site-tag">DeepSeek</span>
            <span class="site-tag">腾讯元宝</span>
            <span class="site-tag">豆包</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="idx">02 //</span>TARGET FILESYSTEM</div>
          <span style="color:var(--fg-dim)">CONFIG</span>
        </div>
        <div class="panel-body">
          <div class="row">
            <input type="text" id="target-input" placeholder="__TARGET__" aria-label="落地目标目录">
            <button class="primary" id="btn-save">[ SAVE ]</button>
          </div>
          <div class="toggle-row" id="toggle-run" role="checkbox" aria-checked="false" tabindex="0">
            <span class="toggle-label">
              <span class="ascii-ck" id="run-ck">[ ]</span>
              <span>落地后自动执行 <code>===RUN===</code> 命令</span>
            </span>
            <span id="run-state-tag" style="font-size:11px;color:var(--fg-dim)">SAFE: OFF</span>
          </div>
          <div class="sec-grid">
            <div class="sec-item"><span class="k">BIND</span><span class="v">127.0.0.1</span></div>
            <div class="sec-item"><span class="k">AUTH</span><span class="v">X-P2R-TOKEN</span></div>
            <div class="sec-item"><span class="k">GUARD</span><span class="v">NO ../ .git .p2r</span></div>
            <div class="sec-item"><span class="k">BACKUP</span><span class="v">.p2r/backup/</span></div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="idx">03 //</span>PROMPT PROTOCOL</div>
          <div class="row" style="gap:4px">
            <button class="tab active" data-preset="std">STD</button>
            <button class="tab" data-preset="strict">STRICT</button>
            <button class="tab" data-preset="patch">PATCH</button>
          </div>
        </div>
        <div class="panel-body">
          <div class="row" style="justify-content:space-between">
            <span style="font-size:11.5px;color:var(--fg-dim)">粘贴至 AI 对话末尾，触发自动解析</span>
            <button class="primary sm" id="btn-copy-prompt">[ COPY PROMPT ]</button>
          </div>
          <pre class="box" id="prompt-box"></pre>
        </div>
      </section>
    </div>

    <div class="col">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="idx">04 //</span>MANUAL APPLY // DRY-RUN</div>
          <button class="sm" id="btn-load-sample">[ LOAD SAMPLE ]</button>
        </div>
        <div class="panel-body">
          <textarea id="paste-input" placeholder="直接粘贴包含 ===FILE: 路径=== 的 AI 回复文本，支持试运行预览或直接落盘…"></textarea>
          <div class="row" style="margin-top:10px;justify-content:space-between">
            <div class="row">
              <button id="btn-dry-run">[ DRY-RUN PREVIEW ]</button>
              <button class="primary" id="btn-apply-now">[ APPLY TO DISK ]</button>
            </div>
            <button class="sm" id="btn-clear-paste">[ CLEAR ]</button>
          </div>
          <pre class="box" id="apply-output" style="display:none"></pre>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="idx">05 //</span>PROJECT TREE</div>
          <div class="row" style="gap:6px">
            <span id="tree-count" style="font-size:11px;color:var(--fg-dim)">—</span>
            <button class="sm" id="btn-refresh-tree">[ ↻ ]</button>
          </div>
        </div>
        <div class="panel-body">
          <pre class="box tree-box" id="tree-box" style="margin-top:0">加载目录树…</pre>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="idx">06 //</span>LAST APPLY &amp; RUNNER</div>
          <span id="last-time" style="font-size:11px;color:var(--fg-dim)">NO RECORD</span>
        </div>
        <div class="panel-body">
          <div id="recent-wrap"><div style="color:var(--fg-dim)">还没有落地记录</div></div>
          <div class="run-box" id="run-panel" style="display:none">
            <div class="row" style="justify-content:space-between;margin-bottom:8px">
              <span style="font-size:11px;color:var(--fg-dim)">DETECTED ===RUN===</span>
              <div class="row" style="gap:6px">
                <button class="sm" id="btn-exec-run">[ ▶ EXEC RUN ]</button>
                <button class="sm" id="btn-copy-repair" style="display:none">[ COPY REPAIR PROMPT ]</button>
              </div>
            </div>
            <div class="run-cmd" id="run-cmd-text"></div>
            <pre class="box" id="run-output" style="display:none"></pre>
          </div>
        </div>
      </section>
    </div>
  </main>
</div>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<footer class="statusbar">
  <div class="status-left">
    <strong>NORMAL</strong>
    <span class="status-sep">│</span>
    <span id="bar-target">DIR: —</span>
  </div>
  <div class="status-right">
    <span id="bar-run">RUN: OFF</span>
    <span class="status-sep">│</span>
    <span id="bar-theme">THEME: DARK</span>
  </div>
</footer>

<script>
(function () {
  "use strict";
  const TOKEN = "__TOKEN__";
  const PROMPTS = {
    std:
`输出格式严格要求：
每个文件用 ===FILE: 相对路径=== 开头，下面给该文件的完整内容，不要省略号，不要"其余同上"。
所有文件给完后，用 ===RUN=== 开头给出启动命令。
不要在文件内容里再嵌套 ===FILE: 这样的行。`,
    strict:
`STRICT OUTPUT PROTOCOL (Paste2Run):
1. Output ONLY ===FILE: relative/path=== sections and one optional ===RUN=== section.
2. Provide complete, runnable file contents with ZERO omissions or placeholders.
3. Do NOT add explanations after closing code fences.
4. Keep paths relative (no leading /, no .., no .git/ or .p2r/).`,
    patch:
`请仅输出本次需要修改或新增的文件：
每个文件以 ===FILE: 相对路径=== 开头并给出完整文件内容（不要 diff 片段，不要省略号）。
如需新增依赖或重新构建，请在最后附加 ===RUN=== 段给出命令。`
  };
  const SAMPLE_REPLY = `===FILE: index.html===
\`\`\`html
<!doctype html>
<html><body><h1>Hello Paste2Run</h1></body></html>
\`\`\`
===RUN===
echo "Paste2Run OK"`;

  const $ = (s) => document.querySelector(s);
  const H = () => ({ "Content-Type": "application/json", "X-P2R-Token": TOKEN });
  let curState = { allow_run: false, target: "", port: 18765, last: null };
  let lastRunOutput = "";
  let toastTimer = null;

  function toast(msg) {
    const el = $("#toast");
    el.textContent = "> " + msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
  }
  function copyText(text, label) {
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(fallback);
    } else fallback();
    toast(label || "COPIED");
  }

  function renderRunToggle() {
    $("#run-ck").textContent = curState.allow_run ? "[■]" : "[ ]";
    $("#toggle-run").setAttribute("aria-checked", String(curState.allow_run));
    $("#run-state-tag").textContent = curState.allow_run ? "ACTIVE: ON" : "SAFE: OFF";
    $("#bar-run").textContent = "RUN: " + (curState.allow_run ? "ON" : "OFF");
  }

  function renderRecent(last) {
    const wrap = $("#recent-wrap");
    if (!last || !last.time) {
      $("#last-time").textContent = "NO RECORD";
      wrap.innerHTML = '<div style="color:var(--fg-dim)">还没有落地记录</div>';
      $("#run-panel").style.display = "none";
      return;
    }
    $("#last-time").textContent = last.time + " // " + (last.source || "web").toUpperCase();
    const rows = (last.files || []).map((f) =>
      `<tr><td>${escapeHtml(f.path)}</td><td class="num">${f.bytes} B</td><td class="num">${f.dryRun ? "DRY" : "OK"}</td></tr>`
    ).join("");
    const skipRows = (last.skipped || []).map((s) =>
      `<tr><td>${escapeHtml(s.path)}</td><td class="num">—</td><td class="num">SKIPPED</td></tr>`
    ).join("");
    wrap.innerHTML = `<table class="manifest-table"><thead><tr><th>FILE PATH</th><th class="num">SIZE</th><th class="num">STATUS</th></tr></thead><tbody>${rows}${skipRows}</tbody></table>`;
    if (last.run) {
      $("#run-panel").style.display = "block";
      $("#run-cmd-text").textContent = "$ " + last.run;
    } else {
      $("#run-panel").style.display = "none";
    }
  }

  async function loadTree() {
    try {
      const r = await fetch("/tree", { cache: "no-store" });
      const j = await r.json();
      if (j.ok) {
        const lines = j.tree ? j.tree.split("\n") : [];
        $("#tree-box").textContent = (j.target || ".") + "/\n" + (j.tree || "(空目录)");
        $("#tree-count").textContent = lines.length + " ENTRIES";
      } else {
        $("#tree-box").textContent = "[ERR] " + (j.error || "无法读取目录");
      }
    } catch (e) {
      $("#tree-box").textContent = "[OFFLINE] 无法连接本地服务";
    }
  }

  async function loadState() {
    try {
      const r = await fetch("/state", { cache: "no-store" });
      const c = await r.json();
      curState = c;
      $("#target-input").value = c.target || "";
      $("#dot").classList.remove("off");
      $("#state-text").textContent = "ONLINE :" + c.port;
      $("#bar-target").textContent = c.target ? ("DIR: " + c.target) : "DIR: UNSET";
      renderRunToggle();
      renderRecent(c.last);
      loadTree();
    } catch (e) {
      $("#dot").classList.add("off");
      $("#state-text").textContent = "OFFLINE";
    }
  }

  async function saveConfig(nextAllowRun) {
    const body = {
      target: $("#target-input").value.trim(),
      allow_run: typeof nextAllowRun === "boolean" ? nextAllowRun : curState.allow_run
    };
    const r = await fetch("/config", { method: "POST", headers: H(), body: JSON.stringify(body) });
    const j = await r.json();
    if (j.ok) {
      curState.target = j.target;
      curState.allow_run = !!j.allow_run;
      $("#target-input").value = j.target;
      $("#bar-target").textContent = "DIR: " + j.target;
      renderRunToggle();
      loadTree();
      toast("SAVED: " + j.target);
    } else {
      toast("SAVE FAILED: " + (j.error || r.status));
    }
  }

  async function submitApply(dryRun) {
    const text = $("#paste-input").value;
    const outBox = $("#apply-output");
    if (!text.trim()) return toast("PASTE INPUT IS EMPTY");
    try {
      const r = await fetch("/apply", {
        method: "POST", headers: H(),
        body: JSON.stringify({ text, dryRun, source: "web-console" })
      });
      const j = await r.json();
      outBox.style.display = "block";
      if (!j.ok) {
        outBox.textContent = "[ERR] " + (j.error || r.status);
        toast("APPLY FAILED");
        return;
      }
      const lines = [
        dryRun ? "=== DRY-RUN REPORT ===" : "=== APPLY COMPLETE ===",
        `TARGET : ${j.target}`,
        `WRITTEN: ${(j.files || []).length} file(s)`,
        ...(j.files || []).map((f) => `  + ${f.path} (${f.bytes} B)`),
      ];
      if ((j.skipped || []).length) {
        lines.push(`SKIPPED: ${j.skipped.length} file(s)`);
        j.skipped.forEach((s) => lines.push(`  ! ${s.path} -> ${s.reason}`));
      }
      if (j.run) lines.push(`RUN CMD: ${j.run}`);
      outBox.textContent = lines.join("\n");
      toast(dryRun ? "DRY-RUN DONE" : `WRITTEN ${(j.files || []).length} FILE(S)`);
      if (!dryRun) loadState();
    } catch (e) {
      toast("REQUEST ERROR");
    }
  }

  $("#btn-save").addEventListener("click", () => saveConfig());
  const onToggleRun = () => saveConfig(!curState.allow_run);
  $("#toggle-run").addEventListener("click", onToggleRun);
  $("#toggle-run").addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); onToggleRun(); } });
  $("#btn-refresh").addEventListener("click", () => { loadState(); toast("SYNCED"); });
  $("#btn-refresh-tree").addEventListener("click", () => { loadTree(); toast("TREE REFRESHED"); });
  $("#btn-copy-endpoint").addEventListener("click", () => copyText(location.origin, "ENDPOINT COPIED"));
  $("#btn-load-sample").addEventListener("click", () => { $("#paste-input").value = SAMPLE_REPLY; toast("SAMPLE LOADED"); });
  $("#btn-dry-run").addEventListener("click", () => submitApply(true));
  $("#btn-apply-now").addEventListener("click", () => submitApply(false));
  $("#btn-clear-paste").addEventListener("click", () => { $("#paste-input").value = ""; $("#apply-output").style.display = "none"; });

  document.querySelectorAll("button.tab[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("button.tab[data-preset]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      $("#prompt-box").textContent = PROMPTS[btn.dataset.preset];
    });
  });
  $("#btn-copy-prompt").addEventListener("click", () => copyText($("#prompt-box").textContent, "PROMPT COPIED"));

  $("#btn-exec-run").addEventListener("click", async () => {
    const out = $("#run-output");
    out.style.display = "block";
    out.textContent = "EXECUTING…";
    try {
      const r = await fetch("/run", { method: "POST", headers: H(), body: JSON.stringify({}) });
      const j = await r.json();
      if (j.error) {
        out.textContent = `[ERR] ${j.error}`;
        $("#btn-copy-repair").style.display = "none";
      } else {
        lastRunOutput = j.output || "";
        out.textContent = `[EXIT ${j.code}] (${j.duration}s)\n` + (lastRunOutput || "(no output)");
        $("#btn-copy-repair").style.display = j.ok ? "none" : "inline-flex";
        toast(j.ok ? `RUN OK (${j.duration}s)` : `RUN FAILED (EXIT ${j.code})`);
      }
    } catch (e) {
      out.textContent = "[ERR] 请求失败";
    }
  });

  $("#btn-copy-repair").addEventListener("click", async () => {
    try {
      const r = await fetch("/repair", { method: "POST", headers: H(), body: JSON.stringify({ output: lastRunOutput }) });
      const j = await r.json();
      if (j.ok && j.prompt) copyText(j.prompt, "REPAIR PROMPT COPIED");
    } catch (e) { toast("REPAIR FAILED"); }
  });

  $("#btn-theme").addEventListener("click", () => {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    document.documentElement.setAttribute("data-theme", isLight ? "dark" : "light");
    $("#bar-theme").textContent = "THEME: " + (isLight ? "DARK" : "LIGHT");
    try { localStorage.setItem("p2r_theme", isLight ? "dark" : "light"); } catch (e) {}
  });

  function tick() { $("#clock").textContent = "SYS " + new Date().toTimeString().slice(0, 8); }
  tick(); setInterval(tick, 1000);
  try {
    if (localStorage.getItem("p2r_theme") === "light") {
      document.documentElement.setAttribute("data-theme", "light");
      $("#bar-theme").textContent = "THEME: LIGHT";
    }
  } catch (e) {}
  $("#prompt-box").textContent = PROMPTS.std;
  loadState();
})();
</script>
</body>
</html>
"""


def setup_html(cfg):
    return (SETUP_HTML.replace("__VERSION__", html.escape(VERSION, quote=True))
            .replace("__TOKEN__", html.escape(cfg.get("token", ""), quote=True))
            .replace("__TARGET__", html.escape(cfg.get("target") or DEFAULT_TARGET, quote=True)))


def userscript_text(cfg):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p2r.user.js")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    endpoint = "http://127.0.0.1:%d" % int(getattr_current_port(cfg))
    return (src.replace("__P2R_ENDPOINT__", endpoint)
               .replace("__P2R_TOKEN__", cfg.get("token", "")))


_CURRENT_PORT = [DEFAULT_PORT]


def getattr_current_port(cfg=None):
    return _CURRENT_PORT[0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Paste2Run/" + VERSION

    def log_message(self, fmt, *args):
        pass

    # ---- 基础校验 ----
    def _host_ok(self):
        host = parse_host_header(self.headers.get("Host"))
        return host in ("127.0.0.1", "localhost", "::1", "[::1]")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = _CURRENT_PORT[0]
        return origin in (
            "http://127.0.0.1:%d" % port,
            "http://localhost:%d" % port,
            "http://[::1]:%d" % port,
        )

    def _token_ok(self):
        cfg = load_config()
        tok = self.headers.get("X-P2R-Token") or ""
        if not tok and "?" in self.path:
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            tok = (qs.get("token") or [""])[0]
        return bool(tok) and hmac.compare_digest(tok, cfg.get("token", ""))

    def _guard(self, need_token=False):
        if not self._host_ok():
            self._json(403, {"ok": False, "error": "Host 不被允许"})
            return False
        if not self._origin_ok():
            self._json(403, {"ok": False, "error": "来源不被允许"})
            return False
        if need_token and not self._token_ok():
            self._json(401, {"ok": False, "error": "缺少或错误的 X-P2R-Token，请重新安装脚本"})
            return False
        return True

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _raw(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("内容太大")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        cfg = load_config()

        if path in ("/", "/index.html"):
            return self._raw(200, setup_html(cfg).encode("utf-8"), "text/html; charset=utf-8")

        if path == "/p2r.user.js":
            src = userscript_text(cfg)
            if src is None:
                return self._json(404, {"ok": False, "error": "找不到 p2r.user.js"})
            return self._raw(200, src.encode("utf-8"), "application/javascript; charset=utf-8")

        if path == "/ping":
            return self._json(200, {
                "ok": True, "version": VERSION, "port": _CURRENT_PORT[0],
                "target": cfg.get("target"), "allow_run": bool(cfg.get("allow_run")),
            })

        if path == "/state":
            target = cfg.get("target")
            last = None
            if target:
                lp = os.path.join(os.path.abspath(os.path.expanduser(target)), ".p2r", "last-apply.json")
                if os.path.exists(lp):
                    try:
                        with open(lp, encoding="utf-8") as fh:
                            last = json.load(fh)
                    except Exception:
                        last = None
            return self._json(200, {"ok": True, "version": VERSION, "port": _CURRENT_PORT[0],
                                    "target": target, "allow_run": bool(cfg.get("allow_run")),
                                    "last": last})

        if path == "/config":
            pub = {k: cfg.get(k) for k in ("target", "allow_run")}
            pub.update({"ok": True, "version": VERSION, "port": _CURRENT_PORT[0]})
            return self._json(200, pub)

        if path == "/tree":
            target = cfg.get("target")
            if not target:
                return self._json(400, {"ok": False, "error": "没有目标目录"})
            root = os.path.abspath(os.path.expanduser(target))
            return self._json(200, {"ok": True, "target": root, "tree": project_tree(root)})

        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._guard(need_token=True):
            return
        try:
            body = self._body()
        except ValueError as exc:
            return self._json(413, {"ok": False, "error": str(exc)})
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})

        cfg = load_config()

        if path == "/config":
            if body.get("target"):
                cfg["target"] = os.path.abspath(os.path.expanduser(body["target"]))
            if "allow_run" in body:
                cfg["allow_run"] = bool(body["allow_run"])
            os.makedirs(os.path.expanduser(cfg["target"]), exist_ok=True)
            save_config(cfg)
            return self._json(200, {"ok": True, "target": cfg["target"],
                                    "allow_run": cfg["allow_run"]})

        target = body.get("target") or cfg.get("target")
        if not target:
            return self._json(400, {"ok": False, "error": "没有目标目录，先在设置页填一个"})
        root = os.path.abspath(os.path.expanduser(target))

        if path == "/run" or path == "/repair":
            if path == "/repair":
                return self._json(200, {"ok": True, "prompt": repair_prompt(root, body.get("output") or "")})
            if not cfg.get("allow_run"):
                return self._json(403, {"ok": False, "error": "执行命令未启用，先在设置页勾选「自动执行」"})
            cmds = body.get("commands")
            if not cmds:
                last = os.path.join(root, ".p2r", "last-apply.json")
                if os.path.exists(last):
                    try:
                        with open(last, encoding="utf-8") as fh:
                            cmds = json.load(fh).get("run") or ""
                    except Exception:
                        cmds = ""
            res = run_in(root, cmds)
            res["target"] = root
            return self._json(200, res)

        if path != "/apply":
            return self._json(404, {"ok": False, "error": "not found"})

        result = apply_text(body.get("text") or "", target,
                            dry_run=bool(body.get("dryRun")),
                            source=body.get("source") or "web")
        result["target"] = root
        return self._json(200 if result.get("ok") else 422, result)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def bind(host, port):
    last = None
    for p in range(port, port + PORT_TRIES):
        try:
            srv = Server((host, p), Handler)
            _CURRENT_PORT[0] = srv.server_address[1]
            return srv
        except OSError as exc:
            last = exc
            continue
    raise SystemExit("端口 %d-%d 都被占用了：%s\n用 --port 换一个" % (port, port + PORT_TRIES - 1, last))


def cmd_serve(host, port, open_browser):
    os.makedirs(HOME, exist_ok=True)
    cfg = load_config()
    target = os.path.expanduser(cfg["target"])
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        print("警告：目标目录创建失败 %s (%s)，启动后可在设置页修改" % (target, exc))

    srv = bind(host, port)
    real_port = _CURRENT_PORT[0]
    if cfg.get("port") != real_port:
        cfg["port"] = real_port
        save_config(cfg)

    url = "http://%s:%d/" % (host, real_port)
    print("Paste2Run %s 已启动" % VERSION)
    print("引导页: %s" % url)
    print("目标目录: %s" % cfg["target"])
    if real_port != port:
        print("（端口 %d 被占用，自动换成 %d）" % (port, real_port))
    print("Ctrl+C 退出")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def cmd_apply(src, target, dry_run):
    text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    cfg = load_config()
    target = target or cfg.get("target")
    print(json.dumps(apply_text(text, target, dry_run=dry_run, source="cli"),
                     ensure_ascii=False, indent=2))


def cmd_doctor():
    ok = True
    print("Paste2Run %s 环境自检" % VERSION)
    print("Python: %s" % sys.version.split()[0])
    if sys.version_info < (3, 8):
        print("  ✗ 需要 Python 3.8+")
        ok = False
    cfg = load_config()
    print("配置: %s" % CONFIG)
    print("  目标目录: %s" % cfg["target"])
    tdir = os.path.expanduser(cfg["target"])
    try:
        os.makedirs(tdir, exist_ok=True)
        probe = os.path.join(tdir, ".p2r-write-test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        print("  ✓ 目录可写")
    except OSError as exc:
        print("  ✗ 目录不可写: %s" % exc)
        ok = False
    print("  自动执行: %s" % ("开" if cfg.get("allow_run") else "关"))
    print("  端口: %s" % cfg.get("port"))
    us = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p2r.user.js")
    print("油猴脚本: %s" % ("✓ 找到" if os.path.exists(us) else "✗ 缺失"))
    files, cmds = parse("===FILE: a.txt===\nx\n===RUN===\necho hi\n")
    print("解析器: %s" % ("✓ 正常" if files and cmds else "✗ 异常"))
    ok = ok and bool(files) and bool(cmds)
    print("结论: %s" % ("可以用了，双击 start 脚本或在浏览器打开引导页" if ok else "还有问题，按上面的 ✗ 处理"))
    return 0 if ok else 1


def cmd_selftest():
    sample = """好的，给你一个最小项目。

===FILE: index.html===
<!doctype html>
<html><body><h1>hi</h1></body></html>
===FILE: src/main.js===
console.log("ok");
===RUN===
npm install && npm run dev
"""
    files, run_cmds = parse(sample)
    assert len(files) == 2, files
    assert files[0][0] == "index.html"
    assert "hi" in files[0][1]
    assert run_cmds.startswith("npm install")

    fenced_prose = "===FILE: a.py===\n```python\nprint(1)\n```\n\n希望这能帮到你！\n"
    f2, _ = parse(fenced_prose)
    assert f2[0][1].strip() == "print(1)", f2

    run_before_file = "===FILE: a.txt===\n1\n===RUN===\necho ok\n===FILE: b.txt===\n2\n"
    f_ord, r_ord = parse(run_before_file)
    assert len(f_ord) == 2 and r_ord == "echo ok", (f_ord, r_ord)

    bold = "**===FILE: ./src/b.js===**\nconsole.log(1)\n"
    f3, _ = parse(bold)
    assert f3 and f3[0][0] == "./src/b.js", f3

    assert parse_host_header("[::1]:18765") == "[::1]"
    assert parse_host_header("127.0.0.1:18765") == "127.0.0.1"

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        res = apply_text(sample, tmp)
        assert res["ok"] and os.path.exists(os.path.join(tmp, "src", "main.js"))
        bad = apply_text("===FILE: ../evil.txt===\nx\n===FILE: .git/hooks/pre-commit===\ny\n", tmp)
        assert bad["ok"] and bad["files"] == [] and len(bad["skipped"]) == 2, bad
        once = apply_text("===FILE: index.html===\nv2\n", tmp)
        assert once["ok"] and os.path.exists(os.path.join(tmp, ".p2r", "last-apply.json"))
        bdir = os.path.join(tmp, ".p2r", "backup")
        assert os.path.isdir(bdir) and os.listdir(bdir), "覆写没有备份"

    print("selftest ok: 解析 / 围栏与尾随结语 / 乱序RUN / 加粗标记 / 越界与.git拦截 / IPv6 Host / 覆写备份 全部通过")


def main():
    ap = argparse.ArgumentParser(description="Paste2Run 本地落地器", add_help=True)
    ap.add_argument("--version", action="version", version="Paste2Run " + VERSION)
    sub = ap.add_subparsers(dest="cmd")

    for name, hlp in (("serve", "只启动本地服务"), ("up", "启动并打开引导页")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--port", type=int, default=None)
        s.add_argument("--host", default="127.0.0.1")
        s.add_argument("--no-browser", action="store_true")

    a = sub.add_parser("apply", help="把一份回复落地（- 表示读 stdin）")
    a.add_argument("file")
    a.add_argument("--dir", default=None)
    a.add_argument("--dry-run", action="store_true")

    sub.add_parser("doctor", help="环境自检")
    sub.add_parser("selftest", help="解析自检")

    args = ap.parse_args()
    cmd = args.cmd or "up"

    if cmd in ("serve", "up"):
        cfg = load_config()
        port = args.port or cfg.get("port") or DEFAULT_PORT
        cmd_serve(args.host, int(port), open_browser=(cmd == "up" and not args.no_browser))
    elif cmd == "apply":
        cmd_apply(args.file, args.dir, args.dry_run)
    elif cmd == "doctor":
        sys.exit(cmd_doctor())
    else:
        cmd_selftest()


if __name__ == "__main__":
    main()
