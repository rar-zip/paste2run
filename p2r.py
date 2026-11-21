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
from collections import deque
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.4.2"
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
            "token": secrets.token_hex(16),
            "backup_mode": "last",   # last = 只保留最近一份备份，不随轮次增长
            "auto_open": "static"}   # static = 纯静态项目落地后自动打开；always / off


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
    if cfg.get("backup_mode") not in ("last", "stamp", "off"):
        cfg["backup_mode"] = "last"
    if cfg.get("auto_open") not in ("static", "always", "off"):
        cfg["auto_open"] = "static"
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


def apply_text(text, target, dry_run=False, backup=True, source="manual", backup_mode=None):
    files, run_cmds = parse(text)
    if not files:
        return {"ok": False, "error": "没找到 ===FILE: ...=== 标记", "files": []}

    if backup_mode not in ("last", "stamp", "off"):
        backup_mode = (load_config().get("backup_mode") or "last")

    root = os.path.abspath(os.path.expanduser(target))
    os.makedirs(root, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    written, skipped = [], []

    # 惰性备份目录：last 模式只保留最近一份，多轮修复不会把磁盘堆满
    bdir = [None]

    def backup_one(rel, full):
        if not backup or backup_mode == "off":
            return
        if bdir[0] is None:
            if backup_mode == "last":
                base = os.path.join(root, ".p2r", "backup", "last")
                if os.path.isdir(base):
                    shutil.rmtree(base, ignore_errors=True)
                bdir[0] = base
            else:
                bdir[0] = os.path.join(root, ".p2r", "backup", stamp)
        bpath = os.path.join(bdir[0], rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(bpath), exist_ok=True)
        shutil.copy2(full, bpath)

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
        if os.path.exists(full):
            backup_one(rel, full)
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
        "backupMode": backup_mode,
    }
    if not dry_run:
        os.makedirs(os.path.join(root, ".p2r"), exist_ok=True)
        with open(os.path.join(root, ".p2r", "last-apply.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
    log("apply source=%s target=%s files=%d skipped=%d dry=%s backup=%s"
        % (source, root, len(written), len(skipped), dry_run, backup_mode))
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


# ----------------------------------------------------------------
# 报错归因：区分「代码写错了」和「环境/脚本跑不起来」
# ----------------------------------------------------------------

_ENV_RULES = (
    (re.compile(r"(?:command not found|is not recognized as an internal or external command|不是内部或外部命令)", re.I),
     "命令不存在，系统里没有装这个工具"),
    (re.compile(r"\b(?:spawn\s+\S+\s+ENOENT|ENOENT[^\n]*spawn)", re.I), "要执行的程序不存在"),
    (re.compile(r"No such file or directory: '?(npm|node|python3?|pip3?|yarn|pnpm|cargo|go)'?", re.I),
     "运行环境缺失"),
    (re.compile(r"EADDRINUSE|address already in use|端口.{0,8}被占用", re.I), "端口被占用"),
    (re.compile(r"EACCES|Permission denied", re.I), "权限不足"),
    (re.compile(r"(?:Could not resolve host|getaddrinfo ENOTFOUND|ETIMEDOUT|EAI_AGAIN|ECONNREFUSED|ENETUNREACH|ECONNRESET|ERR_NETWORK|socket hang up|network\s+(?:error|timeout|unreachable|failed))", re.I),
     "网络不可达，依赖下载失败"),
    (re.compile(r"npm ERR! code (?:E404|EAI_AGAIN|ETIMEDOUT|ECONNREFUSED|ENOTFOUND|EACCES)", re.I),
     "npm 拉包失败"),
)

_DEP_ERR_RULES = (
    (re.compile(r"(?:Cannot find module|Cannot find package|ERR_MODULE_NOT_FOUND)[^\n]*?['\"]((?!\.)[^'\"/][^'\"]*)['\"]", re.I),
     "缺少第三方依赖"),
    (re.compile(r"ModuleNotFoundError: No module named '([^']+)'", re.I), "缺少 Python 依赖"),
)

_FILE_RULES = (
    (re.compile(r"(?:SyntaxError|IndentationError|TabError|Unexpected token|Unexpected end of input)", re.I),
     "语法错误"),
    (re.compile(r"error TS\d+", re.I), "TypeScript 类型错误"),
    (re.compile(r"Module not found: Error: Can't resolve '(?:\./|\.\./)", re.I), "引用了不存在的本地文件"),
    (re.compile(r"(?:Cannot find module|ERR_MODULE_NOT_FOUND)[^\n]*['\"](?:\./|\.\./)", re.I),
     "引用了不存在的本地文件"),
    (re.compile(r"(?:ReferenceError|TypeError|NameError|ImportError|AttributeError|KeyError|IndexError)", re.I),
     "运行时代码错误"),
    (re.compile(r"(?:Failed to compile|BUILD FAILED|Compilation failed|Parse error)", re.I), "构建/解析失败"),
    (re.compile(r"ENOENT[^\n]*?open ['\"](?!/)([^'\"]+)['\"]", re.I), "代码引用了不存在的文件"),
)


def classify_error(output, code=None):
    """把运行失败归成三类：
    env   = 脚本/环境跑不起来，改代码没用，应该给用户一个最简方案
    file  = AI 写的文件有问题，应该生成修复提示词
    unknown = 无法判断，交给 AI 自查（提示词里会要求它先判断）
    """
    out = (output or "")
    reasons = []

    if code in (126, 127):
        return {"class": "env",
                "reasons": ["退出码 %s：命令找不到或无法执行" % code],
                "hint": "这不是代码问题，是本机缺运行环境"}

    for pat, why in _ENV_RULES:
        m = pat.search(out)
        if m:
            reasons.append("%s：%s" % (why, m.group(0)[:80]))
            return {"class": "env", "reasons": reasons, "hint": "这不是代码问题，是本机环境或命令的问题"}

    for pat, why in _DEP_ERR_RULES:
        m = pat.search(out)
        if m:
            reasons.append("%s：%s" % (why, m.group(1)[:60]))
            return {"class": "env", "reasons": reasons,
                    "hint": "依赖没装，装一下就行，代码本身没坏", "missing": m.group(1)}

    for pat, why in _FILE_RULES:
        m = pat.search(out)
        if m:
            reasons.append("%s：%s" % (why, m.group(0)[:80]))
            return {"class": "file", "reasons": reasons, "hint": "这是文件内容的问题，让 AI 改文件"}

    return {"class": "unknown", "reasons": reasons,
            "hint": "我判断不出来是环境还是代码问题，交给 AI 自查"}


def detect_entry(root):
    """找最适合直接打开的页面入口。"""
    for rel in ("index.html", "public/index.html", "src/index.html",
                "dist/index.html", "build/index.html", "www/index.html"):
        p = os.path.join(root, rel.replace("/", os.sep))
        if os.path.isfile(p):
            return p
    return None


def detect_project(root):
    """静态推断项目类型，用于给出最简运行方案。"""
    def has(name):
        return os.path.isfile(os.path.join(root, name))

    info = {"kind": "unknown", "entry": detect_entry(root), "commands": [], "notes": []}

    if has("package.json"):
        info["kind"] = "node"
        scripts = {}
        try:
            with open(os.path.join(root, "package.json"), encoding="utf-8") as fh:
                scripts = (json.load(fh) or {}).get("scripts") or {}
        except Exception:
            info["notes"].append("package.json 解析失败")
        need_install = not os.path.isdir(os.path.join(root, "node_modules"))
        for name in ("dev", "start", "serve", "preview"):
            if name in scripts:
                if need_install:
                    info["commands"].append("npm install")
                info["commands"].append("npm run %s" % name)
                info["long_running"] = True
                break
        else:
            if "build" in scripts:
                if need_install:
                    info["commands"].append("npm install")
                info["commands"].append("npm run build")
            elif need_install:
                info["commands"].append("npm install")
        if info["entry"] is None and os.path.isdir(os.path.join(root, "dist")):
            info["entry"] = detect_entry(root)
        return info

    if has("Cargo.toml"):
        info.update({"kind": "rust", "commands": ["cargo run"]})
        return info
    if has("go.mod"):
        info.update({"kind": "go", "commands": ["go run ."]})
        return info
    if has("requirements.txt") or any(f.endswith(".py") for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))):
        info["kind"] = "python"
        if has("requirements.txt"):
            info["commands"].append("pip install -r requirements.txt")
        for cand in ("main.py", "app.py", "run.py", "server.py"):
            if has(cand):
                info["commands"].append("python3 %s" % cand)
                info["long_running"] = True
                break
        if not info.get("long_running"):
            # 有 .py 但没有标准入口：如果同时有页面入口，就当静态站处理
            if info["entry"]:
                info.update({"kind": "static", "commands": []})
            else:
                info["notes"].append("没找到 main.py / app.py / run.py 入口")
        return info
    if has("Makefile"):
        info.update({"kind": "make", "commands": ["make"]})
        return info
    if info["entry"]:
        info.update({"kind": "static", "commands": []})
        return info
    return info


_COMMON_PORTS = (5173, 3000, 4173, 8080, 8000, 5000, 4200)
_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?\S*", re.I)
_LONG_RUNNING_RE = re.compile(
    r"(?:\bdev\b|\bserve\b|http\.server|live-server|json-server|\bpreview\b|\bstart\b|runserver|\bflask\s+run\b|\bvite\b|\bng\s+serve\b|\bnext\s+dev\b|\buvicorn\b|\bgunicorn\b)",
    re.I)

def looks_long_running(cmd):
    """dev server 类命令不适合同步等待，要改成后台起；链式命令以最后一段为准。"""
    if not cmd:
        return False
    parts = [p.strip() for p in re.split(r"&&|;|\n", cmd) if p.strip()]
    tail = (parts[-1] if parts else cmd).lower()
    if any(k in tail for k in ("build", "install", "test", "lint")) and not re.search(r"\b(dev|serve|start|preview)\b", tail):
        return False
    return bool(_LONG_RUNNING_RE.search(tail))


def _is_entry_cmd(cmd, plan):
    """命令本身就是长驻入口（python3 app.py / cargo run）时，同步跑会一直挂着。"""
    if not cmd or not plan.get("longRunning"):
        return False
    c = cmd.strip().lower()
    for entry_cmd in (plan.get("commands") or []):
        e = entry_cmd.strip().lower()
        if e and (c == e or e in c):
            return True
    return False


def _port_open(port, host="127.0.0.1", timeout=0.6):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pids_on_port(port):
    """找出监听指定端口的进程（无第三方依赖，平台各用各的命令）。"""
    pids = set()
    if not port:
        return pids
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=6).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[-2].upper() == "LISTENING" and parts[1].endswith(":%d" % port):
                    if parts[-1].isdigit():
                        pids.add(int(parts[-1]))
        elif sys.platform == "darwin":
            out = subprocess.run(["lsof", "-ti", "tcp:%d" % port],
                                 capture_output=True, text=True, timeout=6).stdout
            pids.update(int(x) for x in out.split() if x.isdigit())
        else:
            out = subprocess.run(["ss", "-lntp"], capture_output=True, text=True, timeout=6).stdout
            for line in out.splitlines():
                if (":%d" % port) in line:
                    pids.update(int(m) for m in re.findall(r"pid=(\d+)", line))
    except Exception:
        pass
    return pids


def stop_dev_server(root):
    """停止该项目记录在 .p2r/dev.pid 的后台服务，以端口是否关闭作为最终依据。"""
    pidf = os.path.join(root, ".p2r", "dev.pid")
    urlf = os.path.join(root, ".p2r", "dev.url")
    url = None
    pid = None
    try:
        if os.path.isfile(urlf):
            url = open(urlf, encoding="utf-8").read().strip() or None
        if os.path.isfile(pidf):
            pid = int(open(pidf, encoding="utf-8").read().strip())
    except Exception:
        pass

    if not pid and not url:
        return {"ok": False, "error": "没有正在运行的后台服务记录"}

    port = None
    if url:
        pm = re.search(r":(\d+)", url)
        if pm:
            port = int(pm.group(1))

    if pid:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    break
                try:
                    os.killpg(os.getpgid(pid), sig)
                except ProcessLookupError:
                    break
                except Exception:
                    os.kill(pid, sig)
            except Exception:
                pass
            time.sleep(0.6)

    # 记录的 pid 可能已经退出（shell 被接管/重启过），端口还占着就按端口兑底
    if port and _port_open(port, timeout=0.2):
        for other in _pids_on_port(port):
            if other == os.getpid():
                continue
            try:
                os.kill(other, signal.SIGTERM)
            except Exception:
                pass

    # 以「端口是不是真的关了」为准，不依赖进程信号是否报错
    stopped = False
    for _ in range(12):
        if port is None or not _port_open(port, timeout=0.2):
            stopped = True
            break
        time.sleep(0.25)

    for fp in (pidf, urlf):
        try:
            os.remove(fp)
        except OSError:
            pass
    log("dev-server stop pid=%s port=%s stopped=%s" % (pid, port, stopped))
    return {"ok": True, "pid": pid, "port": port, "stopped": stopped}


def start_dev_server(root, cmd, wait=25):
    """后台起 dev server，尽量抓到它监听的地址，然后返回给前端打开。"""
    for pat in BLOCKED_PATTERNS:
        if pat.search((cmd or "").lower()):
            return {"ok": False, "error": "命令里包含危险操作，已拒绝"}
    # 启动前先快照已占用端口，避免把本机无关服务误认成刚起的项目
    pre_open = {p for p in _COMMON_PORTS if _port_open(p, timeout=0.15)}
    popen_kw = {
        "shell": True, "cwd": root,
        "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
        "env": {**os.environ, "CI": "1", "NO_COLOR": "1", "BROWSER": "none"},
    }
    if os.name == "nt":
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **popen_kw)
    except OSError as exc:
        return {"ok": False, "error": "无法执行: %s" % exc}

    buf = deque(maxlen=400)

    def reader():
        try:
            for raw in iter(proc.stdout.readline, b""):
                buf.append(raw.decode("utf-8", "replace"))
        except Exception:
            pass

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    url = None
    deadline = time.time() + wait
    while time.time() < deadline:
        joined = "".join(buf)
        m = _URL_RE.search(joined)
        if m:
            url = m.group(0).rstrip(".,;)")
            break
        if proc.poll() is not None and buf and not _URL_RE.search("".join(buf)):
            break
        time.sleep(0.4)

    if not url:
        # 只认本次新监听的端口，避免劫持本机已运行的无关服务
        exited = proc.poll() is not None
        if not exited:
            for port in _COMMON_PORTS:
                if port not in pre_open and _port_open(port):
                    url = "http://127.0.0.1:%d/" % port
                    break

    os.makedirs(os.path.join(root, ".p2r"), exist_ok=True)
    try:
        with open(os.path.join(root, ".p2r", "dev.pid"), "w") as fh:
            fh.write(str(proc.pid))
        if url:
            with open(os.path.join(root, ".p2r", "dev.url"), "w") as fh:
                fh.write(url)
    except OSError:
        pass

    out = "".join(buf)[-MAX_TAIL:]
    alive = proc.poll() is None
    if not alive and url:
        # shell 可能已退出而服务进程被接管，以端口能否连上为准
        pm = re.search(r":(\d+)", url)
        alive = bool(pm and _port_open(int(pm.group(1))))
    log("dev-server pid=%s url=%s alive=%s" % (proc.pid, url, alive))
    if not alive and not url:
        return {"ok": False, "error": "启动命令直接退出，没跑起来",
                "output": out, "alive": False, "pid": proc.pid}
    return {"ok": True, "pid": proc.pid, "url": url, "output": out, "alive": alive}


def split_chain(cmds):
    """把 `a && b; c` 拆成独立命令。"""
    return [p.strip() for p in re.split(r"&&|;|\n", cmds or "") if p.strip()]


def run_pipeline(root, cmds, timeout=300, wait=25):
    """两阶段执行：前置 install/build 同步跑完，最后一条长驻命令才后台起。

    修的是这个坑：`npm install && npm run dev` 整条交给 start_dev_server 时，
    install 一旦超过 wait 秒就会误报「没抓到地址」。
    """
    parts = split_chain(cmds)
    if not parts:
        return {"ok": False, "error": "没有可执行的命令", "code": None,
                "output": "", "stages": []}

    info = detect_project(root)
    plan_like = {"longRunning": bool(info.get("long_running")),
                 "commands": info.get("commands") or []}
    stages, outs = [], []

    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        if is_last and (looks_long_running(part) or _is_entry_cmd(part, plan_like)):
            res = start_dev_server(root, part, wait=wait)
            res["longRunning"] = True
            res["stages"] = stages
            res.setdefault("duration", 0)
            res["output"] = ("".join(outs)[-MAX_TAIL:] + (res.get("output") or ""))[-MAX_TAIL:]
            return res

        r = run_in(root, part, timeout=timeout)
        stages.append({"cmd": part, "ok": bool(r.get("ok")),
                       "code": r.get("code"), "duration": r.get("duration")})
        outs.append(r.get("output") or "")
        if not r.get("ok"):
            r["stages"] = stages
            r["failedStage"] = part
            r["output"] = r.get("output") or "".join(outs)[-MAX_TAIL:]
            return r

    return {"ok": True, "code": 0, "output": "".join(outs)[-MAX_TAIL:],
            "stages": stages, "duration": round(sum(s.get("duration") or 0 for s in stages), 1)}


def dev_server_status(root):
    """当前项目有没有活着的后台服务。"""
    pidf = os.path.join(root, ".p2r", "dev.pid")
    urlf = os.path.join(root, ".p2r", "dev.url")
    url = None
    pid = None
    try:
        if os.path.isfile(urlf):
            url = open(urlf, encoding="utf-8").read().strip() or None
        if os.path.isfile(pidf):
            pid = int(open(pidf, encoding="utf-8").read().strip())
    except Exception:
        pass
    alive = False
    if url:
        pm = re.search(r":(\d+)", url)
        alive = bool(pm and _port_open(int(pm.group(1))))
    return {"alive": alive, "url": url, "pid": pid}


def rollback_last(root):
    """把 .p2r/backup/last 里的快照盖回项目（回滚前先存一份 pre-rollback）。"""
    bdir = os.path.join(root, ".p2r", "backup", "last")
    if not os.path.isdir(bdir):
        return {"ok": False, "error": "没有可回滚的备份（.p2r/backup/last 不存在）"}

    files = []
    for dirpath, _dirs, names in os.walk(bdir):
        for name in names:
            full = os.path.join(dirpath, name)
            files.append(os.path.relpath(full, bdir))
    if not files:
        return {"ok": False, "error": "备份目录是空的"}

    stamp = time.strftime("%Y%m%d-%H%M%S")
    pre = os.path.join(root, ".p2r", "backup", "pre-rollback")
    if os.path.isdir(pre):
        shutil.rmtree(pre, ignore_errors=True)

    restored, failed = [], []
    for rel in files:
        try:
            target = safe_target(root, rel.replace(os.sep, "/"))
            if os.path.exists(target):
                keep = os.path.join(pre, rel)
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                shutil.copy2(target, keep)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(bdir, rel), target)
            restored.append(rel.replace(os.sep, "/"))
        except Exception as exc:
            failed.append({"path": rel, "reason": str(exc)})

    log("rollback target=%s restored=%d failed=%d" % (root, len(restored), len(failed)))
    return {"ok": bool(restored), "restored": restored, "failed": failed,
            "preRollback": os.path.relpath(pre, root), "stamp": stamp}


def open_target(path_or_url):
    """跨平台打开文件或网址。"""
    raw = path_or_url
    if not raw:
        return {"ok": False, "error": "没有可打开的目标"}
    is_url = raw.startswith(("http://", "https://", "file://"))
    target = raw if is_url else Path(os.path.abspath(raw)).as_uri()
    try:
        if os.name == "nt":
            os.startfile(raw if not is_url else target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            opener = shutil.which("xdg-open") or shutil.which("gio")
            if not opener:
                return {"ok": False, "error": "本机没有 xdg-open，无法自动打开浏览器",
                        "url": target, "manual": True}
            subprocess.Popen([opener, target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": target}
    return {"ok": True, "url": target}


def open_project(root, allow_run=False):
    """按项目类型决定：静态页直接开，需要服务的须在 allow_run 开启时才后台拉起。"""
    info = detect_project(root)
    if info.get("long_running") and info.get("commands"):
        # 已经在跑就直接打开，不要重复拉起端口冲突的第二个实例
        running = _running_dev_url(root)
        if running:
            opened = open_target(running)
            return {"ok": True, "mode": "server", "url": running,
                    "opened": opened, "manual": bool(opened.get("manual")), "reused": True}
        if not allow_run:
            return {"ok": False, "mode": "server", "blocked": True,
                    "error": "该项目需启动本地服务，请先开启「自动执行 ===RUN===」或按下方方案手动运行",
                    "plan": minimal_run_plan(root)}
        cmd = " && ".join(info["commands"])
        res = run_pipeline(root, cmd)
        if res.get("url"):
            opened = open_target(res["url"])
            return {"ok": True, "mode": "server", "url": res["url"],
                    "opened": opened, "manual": bool(opened.get("manual")),
                    "output": res.get("output", "")[:1500]}
        return {"ok": False, "mode": "server", "error": "服务没起来，没抓到地址",
                "output": res.get("output", "")[:1500], "plan": minimal_run_plan(root)}
    entry = info.get("entry")
    if entry:
        opened = open_target(entry)
        return {"ok": bool(opened.get("ok")), "mode": "static",
                "url": opened.get("url"), "manual": bool(opened.get("manual")),
                "error": opened.get("error"), "plan": minimal_run_plan(root)}
    return {"ok": False, "error": "这个项目没有可直接打开的入口，需要在终端里手动跑", "plan": minimal_run_plan(root)}


def minimal_run_plan(root):
    """给用户看的「最简运行方案」，不依赖 AI。"""
    info = detect_project(root)
    cmds = info.get("commands") or []
    lines = []
    if info.get("kind") == "static" and info.get("entry"):
        lines.append("这是个纯静态页，双击 %s 就能看" % os.path.relpath(info["entry"], root))
    elif cmds:
        lines.append("在终端进入 %s 然后依次执行：" % root)
        lines.extend("  " + c for c in cmds)
        if info.get("long_running"):
            lines.append("（长驻服务，也可以直接点 [ ◎ OPEN PROJECT ]，脚本会后台起好再打开浏览器）")
    elif info.get("kind") == "python":
        lines.append("是个 Python 项目，但没找到 main.py / app.py / run.py，先看 README 确认入口文件")
    else:
        lines.append("没识别出项目类型，可能需要先看 README，或把报错发给 AI 让它给运行命令")
    if info.get("kind") == "node" and not shutil.which("npm"):
        lines.append("提示：本机没检测到 npm，先装 Node.js（https://nodejs.org）")
    if info.get("kind") == "python" and not shutil.which("python3"):
        lines.append("提示：本机没检测到 python3")
    return {"kind": info.get("kind"), "commands": cmds,
            "entry": info.get("entry"), "longRunning": bool(info.get("long_running")),
            "notes": info.get("notes"), "text": "\n".join(lines)}


_LAST_OPEN = {"key": None, "at": 0.0}


def _running_dev_url(root):
    """上次起的 dev server 还在监听就复用，不看 pid（shell 可能已经把进程交给 init）。"""
    urlf = os.path.join(root, ".p2r", "dev.url")
    if not os.path.isfile(urlf):
        return None
    try:
        url = open(urlf).read().strip()
    except Exception:
        return None
    pm = re.search(r":(\d+)", url or "")
    if pm and _port_open(int(pm.group(1))):
        return url
    return None


def _should_open(path, cooldown=10.0):
    """同一个入口短时间内不重复弹浏览器（多轮修复时很必要）。"""
    key = os.path.abspath(path)
    now = time.time()
    if _LAST_OPEN["key"] == key and now - _LAST_OPEN["at"] < cooldown:
        return False
    _LAST_OPEN["key"] = key
    _LAST_OPEN["at"] = now
    return True


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
            <a class="btn" href="/p2r.user.js?all=1">[ ↓ ALL-SITES 宽松版 ]</a>
            <button id="btn-copy-endpoint">[ COPY ENDPOINT ]</button>
          </div>
          <div class="hint">需要先安装 Tampermonkey。从本页安装会自动注入当前端口与 <code>X-P2R-Token</code>。宽松版会在所有网站注入，靠内容里的 <code>===FILE:</code> 判定要不要动作，适配没收录的新站点用。</div>
          <div class="sites-bar">
            <span class="site-tag">ChatGPT</span>
            <span class="site-tag">Claude</span>
            <span class="site-tag">Gemini</span>
            <span class="site-tag">AI Studio</span>
            <span class="site-tag">DeepSeek</span>
            <span class="site-tag">LMArena</span>
            <span class="site-tag">Kimi</span>
            <span class="site-tag">通义 / Qwen</span>
            <span class="site-tag">腾讯元宝</span>
            <span class="site-tag">豆包</span>
            <span class="site-tag">Copilot</span>
            <span class="site-tag">Poe</span>
            <span class="site-tag">Grok</span>
            <span class="site-tag">Perplexity</span>
            <span class="site-tag">OpenRouter</span>
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
          <div class="row" style="margin-top:8px;gap:8px">
            <button class="sm" id="btn-backup-mode">[ BACKUP: LAST ]</button>
            <button class="sm" id="btn-auto-open">[ AUTO-OPEN: STATIC ]</button>
          </div>
          <div class="hint" style="margin-top:6px">BACKUP 切 LAST 只留最近一份备份，多轮修复不会把磁盘堆满；OFF 完全不备份。AUTO-OPEN 控制落地后要不要帮你打开页面。</div>
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
          <div class="row" style="margin:10px 0 4px">
            <button class="sm" id="btn-open-project">[ ◎ OPEN PROJECT ]</button>
            <button class="sm" id="btn-stop-server">[ ■ STOP SERVER ]</button>
            <button class="sm" id="btn-rollback">[ ↩ ROLLBACK ]</button>
            <span id="dev-state" style="font-size:11px;color:var(--fg-dim);align-self:center">SERVER: OFF</span>
          </div>
          <div class="hint" style="margin-bottom:10px">静态页直接开；带 dev server 的项目会后台起服务再开，不用自己去目录里点 index.html。</div>
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
  let lastRunCode = null;
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
    const bm = $("#btn-backup-mode");
    if (bm) bm.textContent = "[ BACKUP: " + String(curState.backup_mode || "last").toUpperCase() + " ]";
    const ao = $("#btn-auto-open");
    if (ao) ao.textContent = "[ AUTO-OPEN: " + String(curState.auto_open || "static").toUpperCase() + " ]";
  }

  async function cycleSetting(key, seq) {
    const cur = curState[key] || seq[0];
    const next = seq[(seq.indexOf(cur) + 1) % seq.length];
    try {
      const r = await fetch("/config", { method: "POST", headers: H(), body: JSON.stringify({ [key]: next }) });
      const j = await r.json();
      if (j.ok) {
        curState[key] = j[key];
        renderRunToggle();
        toast(key.toUpperCase().replace("_", "-") + ": " + String(j[key]).toUpperCase());
      } else toast("SET FAILED");
    } catch (e) { toast("SET FAILED"); }
  }

  function renderDevState(dev) {
    const el = $("#dev-state");
    if (!el) return;
    if (dev && dev.alive) {
      el.textContent = "SERVER: ON " + (dev.url || "");
      el.style.color = "var(--fg)";
    } else {
      el.textContent = "SERVER: OFF";
      el.style.color = "var(--fg-dim)";
    }
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
      renderDevState(c.devServer);
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
  $("#btn-backup-mode").addEventListener("click", () => cycleSetting("backup_mode", ["last", "off", "stamp"]));
  $("#btn-auto-open").addEventListener("click", () => cycleSetting("auto_open", ["static", "always", "off"]));
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

  $("#btn-open-project").addEventListener("click", async () => {
    const out = $("#run-output");
    out.style.display = "block";
    out.textContent = "OPENING…";
    try {
      const r = await fetch("/open", { method: "POST", headers: H(), body: JSON.stringify({}) });
      const j = await r.json();
      if (j.ok) {
        out.textContent = (j.manual ? "[MANUAL OPEN] " : "[OPENED] ") + `${j.mode} -> ${j.url || ""}`;
        toast(j.manual ? "请手动打开 " + (j.url || "") : "已打开 " + (j.url || ""));
      } else {
        out.textContent = `[ERR] ${j.error || "打开失败"}\n` + (j.url ? `URL: ${j.url}\n` : "") + ((j.plan && j.plan.text) || (j.output || ""));
        toast("打开失败");
      }
    } catch (e) { out.textContent = "[ERR] 请求失败"; }
  });

  $("#btn-stop-server").addEventListener("click", async () => {
    const out = $("#run-output");
    out.style.display = "block";
    try {
      const r = await fetch("/stop", { method: "POST", headers: H(), body: JSON.stringify({}) });
      const j = await r.json();
      out.textContent = j.ok ? `[STOPPED] pid=${j.pid || "-"}` : `[ERR] ${j.error || "停止失败"}`;
      toast(j.ok ? "后台服务已停止" : (j.error || "没有运行中的服务"));
      loadState();
    } catch (e) { out.textContent = "[ERR] 请求失败"; }
  });

  $("#btn-rollback").addEventListener("click", async () => {
    const out = $("#run-output");
    out.style.display = "block";
    if (!confirm("用 .p2r/backup/last 里的快照覆盖当前文件？当前版本会存到 backup/pre-rollback")) return;
    try {
      const r = await fetch("/rollback", { method: "POST", headers: H(), body: JSON.stringify({}) });
      const j = await r.json();
      if (j.ok) {
        out.textContent = `[ROLLBACK OK] ${j.restored.length} file(s)\n` + j.restored.map((p) => "  < " + p).join("\n");
        toast(`已回滚 ${j.restored.length} 个文件`);
        loadState();
      } else {
        out.textContent = `[ERR] ${j.error || "回滚失败"}`;
        toast(j.error || "回滚失败");
      }
    } catch (e) { out.textContent = "[ERR] 请求失败"; }
  });

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
      } else if (j.longRunning) {
        lastRunOutput = j.output || "";
        out.textContent = `[DEV SERVER] ${j.alive ? "RUNNING" : "EXITED"}${j.url ? " -> " + j.url : ""}\n` +
          (lastRunOutput || "(no output)");
        toast(j.url ? `DEV SERVER: ${j.url}` : "dev server 没抓到地址");
        $("#btn-copy-repair").style.display = "none";
      } else {
        lastRunOutput = j.output || "";
        lastRunCode = j.code;
        const cls = j.class || "";
        const why = (j.reasons || []).join("；");
        out.textContent = `[EXIT ${j.code}]${cls ? " " + cls.toUpperCase() : ""} (${j.duration}s)\n` +
          (why ? why + "\n\n" : "") + (lastRunOutput || "(no output)");
        if (j.ok) {
          $("#btn-copy-repair").style.display = "none";
          toast(`RUN OK (${j.duration}s)` + (j.open && j.open.url ? " -> " + j.open.url : ""));
        } else {
          $("#btn-copy-repair").style.display = "inline-flex";
          $("#btn-copy-repair").textContent = cls === "env" ? "[ COPY 最简运行方案 ]" : "[ COPY REPAIR PROMPT ]";
          $("#btn-copy-repair").dataset.mode = cls === "env" ? "env" : "repair";
          toast(cls === "env" ? "这不是代码问题，是本机环境" : `RUN FAILED (EXIT ${j.code})`);
        }
      }
    } catch (e) {
      out.textContent = "[ERR] 请求失败";
    }
  });

  $("#btn-copy-repair").addEventListener("click", async () => {
    try {
      const mode = $("#btn-copy-repair").dataset.mode || "repair";
      const r = await fetch("/repair", { method: "POST", headers: H(),
        body: JSON.stringify({ output: lastRunOutput, code: lastRunCode, force: mode === "repair" }) });
      const j = await r.json();
      if (j.mode === "env") {
        copyText((j.plan && j.plan.text) || "", "最简运行方案已复制");
      } else if (j.prompt) {
        copyText(j.prompt, "REPAIR PROMPT COPIED");
      } else {
        toast("没有可复制的内容");
      }
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


def userscript_text(cfg, all_sites=False):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p2r.user.js")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    endpoint = "http://127.0.0.1:%d" % int(getattr_current_port(cfg))
    out = (src.replace("__P2R_ENDPOINT__", endpoint)
              .replace("__P2R_TOKEN__", cfg.get("token", "")))
    if all_sites:
        # 宽松版：所有站点注入，靠内容识别判断要不要动作
        out = out.replace("// @grant", "// @match        *://*/*\n// @grant", 1)
    return out


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
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            all_sites = (qs.get("all") or [""])[0] in ("1", "true", "yes")
            src = userscript_text(cfg, all_sites=all_sites)
            if src is None:
                return self._json(404, {"ok": False, "error": "找不到 p2r.user.js"})
            return self._raw(200, src.encode("utf-8"), "application/javascript; charset=utf-8")

        if path == "/ping":
            return self._json(200, {
                "ok": True, "version": VERSION, "port": _CURRENT_PORT[0],
                "target": cfg.get("target"), "allow_run": bool(cfg.get("allow_run")),
                "backup_mode": cfg.get("backup_mode"), "auto_open": cfg.get("auto_open"),
            })

        if path == "/state":
            target = cfg.get("target")
            last = None
            dev = None
            if target:
                troot = os.path.abspath(os.path.expanduser(target))
                lp = os.path.join(troot, ".p2r", "last-apply.json")
                if os.path.exists(lp):
                    try:
                        with open(lp, encoding="utf-8") as fh:
                            last = json.load(fh)
                    except Exception:
                        last = None
                dev = dev_server_status(troot)
            return self._json(200, {"ok": True, "version": VERSION, "port": _CURRENT_PORT[0],
                                    "target": target, "allow_run": bool(cfg.get("allow_run")),
                                    "backup_mode": cfg.get("backup_mode"),
                                    "auto_open": cfg.get("auto_open"),
                                    "devServer": dev, "last": last})

        if path == "/config":
            pub = {k: cfg.get(k) for k in ("target", "allow_run", "backup_mode", "auto_open")}
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
            if body.get("backup_mode") in ("last", "stamp", "off"):
                cfg["backup_mode"] = body["backup_mode"]
            if body.get("auto_open") in ("static", "always", "off"):
                cfg["auto_open"] = body["auto_open"]
            os.makedirs(os.path.expanduser(cfg["target"]), exist_ok=True)
            save_config(cfg)
            return self._json(200, {"ok": True, "target": cfg["target"],
                                    "allow_run": cfg["allow_run"],
                                    "backup_mode": cfg["backup_mode"],
                                    "auto_open": cfg["auto_open"]})

        target = body.get("target") or cfg.get("target")
        if not target:
            return self._json(400, {"ok": False, "error": "没有目标目录，先在设置页填一个"})
        root = os.path.abspath(os.path.expanduser(target))

        if path == "/open":
            res = open_project(root, allow_run=bool(cfg.get("allow_run")))
            res["target"] = root
            code = 200 if res.get("ok") else (403 if res.get("blocked") else 422)
            return self._json(code, res)

        if path == "/stop":
            res = stop_dev_server(root)
            res["target"] = root
            return self._json(200 if res.get("ok") else 404, res)

        if path == "/rollback":
            res = rollback_last(root)
            res["target"] = root
            return self._json(200 if res.get("ok") else 422, res)

        if path == "/run" or path == "/repair":
            if path == "/repair":
                output = body.get("output") or ""
                verdict = classify_error(output, body.get("code"))
                plan = minimal_run_plan(root)
                force = bool(body.get("force"))
                if verdict["class"] == "env" and not force:
                    # 环境问题不让 AI 改代码，直接给用户最简方案
                    return self._json(200, {
                        "ok": True, "mode": "env", "prompt": "",
                        "class": "env", "reasons": verdict.get("reasons"),
                        "hint": verdict.get("hint"), "plan": plan,
                    })
                return self._json(200, {
                    "ok": True, "mode": "repair",
                    "class": verdict["class"], "reasons": verdict.get("reasons"),
                    "hint": verdict.get("hint"), "plan": plan,
                    "prompt": repair_prompt(root, output),
                })
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
            # 先用静态推断补全安装步骤，但 ===RUN=== 里用户自己的命令优先
            plan = minimal_run_plan(root)
            res = run_pipeline(root, cmds)
            res["target"] = root
            res["plan"] = plan
            if res.get("longRunning"):
                res["class"] = "env" if (not res.get("ok") or not res.get("alive")) else "ok"
                if res.get("url"):
                    res["open"] = open_target(res["url"])
                log("run(long-running) url=%s alive=%s stages=%d"
                    % (res.get("url"), res.get("alive"), len(res.get("stages") or [])))
                return self._json(200, res)

            if not res.get("ok"):
                res.update(classify_error(res.get("output"), res.get("code")))
                stages = res.get("stages") or []
                if res.get("failedStage") and len(stages) > 1:
                    res["hint"] = ("第 %d/%d 步失败（%s）。%s"
                                    % (len(stages), len(split_chain(cmds)),
                                       res["failedStage"][:60], res.get("hint") or ""))
            # 跑完如果项目有页面入口，顺手打开，少让用户自己找 index
            if res.get("ok") and cfg.get("auto_open") in ("static", "always"):
                entry = plan.get("entry")
                if entry:
                    res["open"] = open_target(entry)
            return self._json(200, res)

        if path != "/apply":
            return self._json(404, {"ok": False, "error": "not found"})

        result = apply_text(body.get("text") or "", target,
                            dry_run=bool(body.get("dryRun")),
                            source=body.get("source") or "web")
        result["target"] = root
        if result.get("ok"):
            plan = minimal_run_plan(root)
            result["plan"] = plan
            # 纯静态项目：落地即自动打开，省掉用户自己找 index.html 那一步
            if not body.get("dryRun") and cfg.get("auto_open") in ("static", "always"):
                want = cfg.get("auto_open") == "always" or (
                    plan.get("kind") == "static" and not result.get("run"))
                entry = plan.get("entry")
                if want and entry and _should_open(entry):
                    result["open"] = open_target(entry)
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

        # last 模式：多轮覆写后备份体积不增长，只保留一份
        for i in range(4):
            apply_text("===FILE: index.html===\nv%d\n" % i, tmp, backup_mode="last")
        last_dir = os.path.join(bdir, "last")
        assert os.path.isdir(last_dir), "last 模式备份目录不存在"
        stamps = [d for d in os.listdir(bdir) if d != "last"]
        assert len(stamps) <= 1, "last 模式不应该产生多个时间戳目录: %s" % stamps
        assert os.path.isfile(os.path.join(last_dir, "index.html")), "last 模式没有备份到文件"

        # 一键回滚：last 里存的是上一版，回滚后应盖回去，且当前版留在 pre-rollback
        rb = rollback_last(tmp)
        assert rb["ok"] and rb["restored"], rb
        assert open(os.path.join(tmp, "index.html"), encoding="utf-8").read().strip() == "v2", rb
        assert os.path.isdir(os.path.join(tmp, ".p2r", "backup", "pre-rollback"))
        assert os.path.isfile(os.path.join(tmp, ".p2r", "backup", "pre-rollback", "index.html"))

        # 链式命令拆分
        assert split_chain("npm install && npm run dev") == ["npm install", "npm run dev"]
        assert split_chain("a; b\nc") == ["a", "b", "c"]
        assert split_chain("  ") == []

        # 错误归因
        env_cases = [
            ("sh: 1: vite: command not found", 127),
            ("'npm' is not recognized as an internal or external command", 1),
            ("Error: listen EADDRINUSE: address already in use :::3000", 1),
            ("npm ERR! code E404 npm ERR! 404 Not Found", 1),
            ("Error: Cannot find module 'react'", 1),
        ]
        for out, code in env_cases:
            v = classify_error(out, code)
            assert v["class"] == "env", (out, v)

        file_cases = [
            ("SyntaxError: Unexpected token '<'", 1),
            ("src/App.tsx(12,5): error TS2304: Cannot find name 'foo'.", 2),
            ("Module not found: Error: Can't resolve './components/Header' in '/app/src'", 1),
            ("ReferenceError: useState is not defined", 1),
        ]
        for out, code in file_cases:
            v = classify_error(out, code)
            assert v["class"] == "file", (out, v)

        # 项目类型与最简方案
        with tempfile.TemporaryDirectory() as tmp2:
            with open(os.path.join(tmp2, "index.html"), "w", encoding="utf-8") as fh:
                fh.write("<h1>x</h1>")
            info = detect_project(tmp2)
            assert info["kind"] == "static" and info["entry"].endswith("index.html"), info
            plan = minimal_run_plan(tmp2)
            assert "双击" in plan["text"], plan
            with open(os.path.join(tmp2, "package.json"), "w", encoding="utf-8") as fh:
                json.dump({"scripts": {"dev": "vite", "build": "vite build"}}, fh)
            info2 = detect_project(tmp2)
            assert info2["kind"] == "node" and any("npm run dev" in c for c in info2["commands"]), info2
            assert looks_long_running("npm run dev") is True
            assert looks_long_running("npm install && npm run start") is True
            assert looks_long_running("npm install && npm run serve") is True
            assert looks_long_running("npm install && npm run build") is False
            assert looks_long_running("cp server.py dist/") is False
            assert classify_error("ReferenceError: networkConfig is not defined", 1)["class"] == "file"

        # 只有 .py 没有标准入口的分支不能崩
        with tempfile.TemporaryDirectory() as tmp3:
            with open(os.path.join(tmp3, "foo.py"), "w", encoding="utf-8") as fh:
                fh.write("print(1)\n")
            i3 = detect_project(tmp3)
            assert i3["kind"] == "python" and not i3.get("long_running"), i3
            assert minimal_run_plan(tmp3)["text"], "空方案"
            with open(os.path.join(tmp3, "index.html"), "w", encoding="utf-8") as fh:
                fh.write("<h1>x</h1>")
            i4 = detect_project(tmp3)
            assert i4["kind"] == "static", i4

    print("selftest ok: 解析 / 围栏与尾随结语 / 乱序RUN / 加粗标记 / 越界与.git拦截 / IPv6 Host /"
          " 覆写备份(last不膨胀) / 报错归因 / 项目探测 全部通过")


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
