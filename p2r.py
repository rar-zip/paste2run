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
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.3.0"
HOME = os.path.expanduser("~/.p2r")
CONFIG = os.path.join(HOME, "config.json")
LOG = os.path.join(HOME, "p2r.log")
DEFAULT_PORT = 18765
DEFAULT_TARGET = os.path.join(os.path.expanduser("~"), "p2r-projects")
MAX_BODY = 4 * 1024 * 1024
PORT_TRIES = 10

FILE_RE = re.compile(r"^[ \t]*(?:\*\*)?===\s*FILE:\s*(.+?)\s*===\s*(?:\*\*)?[ \t]*$", re.M)
RUN_RE = re.compile(r"^[ \t]*(?:\*\*)?===\s*RUN\s*===\s*(?:\*\*)?[ \t]*$", re.M)


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
    """读配置；缺字段就补上并落盘，保证 token / 端口总是存在。"""
    cfg = default_config()
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as fh:
                cfg.update(json.load(fh) or {})
        except Exception:
            pass
    if not cfg.get("target"):
        cfg["target"] = DEFAULT_TARGET
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
    try:
        cfg["port"] = int(cfg.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        cfg["port"] = DEFAULT_PORT
    save_config(cfg)
    return cfg


def save_config(cfg):
    os.makedirs(HOME, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def strip_fences(body):
    """去掉 AI 习惯性包在代码块外面的 ```lang 和结尾 ```。"""
    lines = body.strip("\n").splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines) + "\n"


def parse(text):
    """返回 (files: [(path, content)], run_commands: str)"""
    text = (text or "").replace("\ufeff", "").replace("\r\n", "\n")
    matches = list(FILE_RE.finditer(text))
    if not matches:
        return [], ""

    run_at = None
    m = RUN_RE.search(text)
    if m:
        run_at = m.start()

    files = []
    for i, mt in enumerate(matches):
        path = mt.group(1).strip().strip("`\"'")
        start = mt.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        if run_at is not None and run_at > start and (i + 1 >= len(matches) or matches[i + 1].start() > run_at):
            end = run_at
        files.append((path, strip_fences(text[start:end])))

    run_cmds = ""
    if run_at is not None:
        tail = text[run_at:]
        run_cmds = RUN_RE.sub("", tail).strip()
        run_cmds = strip_fences(run_cmds).strip()
    return files, run_cmds


def safe_target(root, rel):
    """把模型给的相对路径限制在 root 内，拒绝绝对路径和 ..。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel in (".", "..") or "\0" in rel:
        raise ValueError("非法路径: %r" % rel)
    if rel.startswith("~") or re.match(r"^[a-zA-Z]:", rel):
        raise ValueError("拒绝绝对路径: %s" % rel)
    full = os.path.realpath(os.path.join(root, rel))
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
    """给修复 prompt 用的轻量目录树。"""
    lines = []

    def walk(base, prefix, depth):
        if len(lines) >= max_entries or depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return
        entries = [e for e in entries if e not in SKIP_DIRS and not e.startswith(".DS_Store")]
        for name in entries:
            if len(lines) >= max_entries:
                return
            full = os.path.join(base, name)
            lines.append(prefix + name + ("/" if os.path.isdir(full) else ""))
            if os.path.isdir(full):
                walk(full, prefix + "  ", depth + 1)

    walk(root, "", 0)
    if len(lines) >= max_entries:
        lines.append("… (已截断)")
    return "\n".join(lines)


BLOCKED_CMDS = ("sudo ", "rm -rf /", "rm -fr /", "mkfs", "diskutil erase", "format c:",
                ":(){", "shutdown ", "reboot ", "del /f /s /q c:\\")


def run_in(root, commands, timeout=300):
    """在项目目录里执行命令，返回退出码和输出尾部。"""
    if not commands or not commands.strip():
        return {"ok": False, "error": "没有可执行的命令", "code": None, "output": ""}
    lowered = commands.lower()
    for bad in BLOCKED_CMDS:
        if bad in lowered:
            return {"ok": False, "error": "命令里包含危险操作，已拒绝: %s" % bad, "code": None, "output": ""}
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


# ---- 引导页 ----
SETUP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paste2Run</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:24px 16px 44px;background:#0e1116;color:#e6edf3;font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:640px;margin:0 auto}
h1{font-size:22px;margin:0 0 2px}
h1 small{font-size:12px;color:#8b949e;font-weight:400;margin-left:6px}
.sub{color:#8b949e;font-size:13px;margin-bottom:20px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#8b949e;margin-right:6px;vertical-align:2px}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:15px 15px 17px;margin-bottom:13px}
.card h2{font-size:15px;margin:0 0 11px}
.card h2 span{color:#8b949e;font-weight:400;margin-right:7px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=text]{flex:1;min-width:190px;padding:9px 11px;border-radius:7px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;font-size:14px;font-family:inherit}
button,.btn{display:inline-block;padding:9px 14px;border-radius:7px;border:1px solid #30363d;background:#21262d;color:#e6edf3;font-size:14px;font-family:inherit;cursor:pointer;text-decoration:none}
button.primary,.btn.primary{background:#238636;border-color:#2ea043;color:#fff;font-weight:500}
button:hover{background:#30363d}
button.primary:hover{background:#2ea043}
label.ck{display:flex;gap:7px;align-items:center;color:#8b949e;font-size:13.5px;cursor:pointer;margin-top:11px}
.tip{color:#8b949e;font-size:12.5px;margin-top:9px}
pre{background:#0d1117;border:1px solid #21262d;border-radius:7px;padding:11px;overflow:auto;font-size:12.5px;line-height:1.55;margin:11px 0 0;color:#9fb3c8;white-space:pre-wrap;word-break:break-all}
#msg{font-size:13px;color:#3fb950;min-height:20px;margin-top:6px}
.muted{color:#8b949e}
.sticky{position:fixed;left:0;right:0;bottom:0;padding:10px 16px;background:#0e1116f2;border-top:1px solid #21262d;font-size:13px;color:#8b949e;text-align:center}
</style>
</head>
<body>
<div class="wrap">
<h1>Paste2Run <small>v__VERSION__</small></h1>
<div class="sub"><span class="dot" id="dot"></span><span id="state">正在连接本地服务…</span></div>

<div class="card">
<h2><span>1</span>装油猴脚本</h2>
<div class="row"><a class="btn primary" href="/p2r.user.js">一键安装 / 更新脚本</a></div>
<div class="tip">需要先装好 Tampermonkey（Chrome / Edge / Firefox 都有）。点上面按钮会直接弹安装页</div>
</div>

<div class="card">
<h2><span>2</span>落地目录</h2>
<div class="row">
<input type="text" id="target" placeholder="__TARGET__">
<button class="primary" id="save">保存</button>
</div>
<label class="ck"><input type="checkbox" id="run"> 落地后自动执行 ===RUN=== 命令</label>
<div class="tip">路径不存在会自动创建；建议每个项目一个目录</div>
</div>

<div class="card">
<h2><span>3</span>提示词</h2>
<div class="row"><button id="copy">复制提示词</button></div>
<pre id="prompt">输出格式严格要求：
每个文件用 ===FILE: 相对路径=== 开头，下面给该文件的完整内容，不要省略号，不要"其余同上"。
所有文件给完后，用 ===RUN=== 开头给出启动命令。
不要在文件内容里再嵌套 ===FILE: 这样的行。</pre>
<div class="tip">把这段粘到对话末尾，AI 就会按格式输出，脚本自动落地</div>
</div>

<div class="card">
<h2><span>4</span>最近一次落地</h2>
<div id="recent" class="muted">还没有记录</div>
</div>

<div id="msg"></div>
</div>
<div class="sticky" id="bar">—</div>

<script>
var TOKEN="__TOKEN__";
var $=function(s){return document.querySelector(s)};
function H(){return {"Content-Type":"application/json","X-P2R-Token":TOKEN}}
function show(t){$("#msg").textContent=t}

async function load(){
  try{
    var r=await fetch("/state",{cache:"no-store"});
    var c=await r.json();
    $("#target").value=c.target||"";
    $("#run").checked=!!c.allow_run;
    $("#dot").style.background="#3fb950";
    $("#state").textContent="服务在线 · 端口 "+c.port;
    $("#bar").textContent=c.target?("当前目录 "+c.target):"还没设落地目录";
    renderRecent(c.last);
  }catch(e){
    $("#dot").style.background="#f85149";
    $("#state").textContent="服务不在线";
  }
}

function renderRecent(last){
  var box=$("#recent");
  if(!last||!last.time){box.textContent="还没有记录";return;}
  var names=(last.files||[]).map(function(f){return "· "+f.path});
  var more=names.length>6?"\n…共 "+names.length+" 个文件":"";
  box.textContent=last.time+" · "+names.length+" 个文件\n"+names.slice(0,6).join("\n")+more+
    (last.run?("\n\n启动命令："+last.run):"");
}

$("#save").onclick=async function(){
  var body={target:$("#target").value.trim(),allow_run:$("#run").checked};
  var r=await fetch("/config",{method:"POST",headers:H(),body:JSON.stringify(body)});
  var j=await r.json();
  show(j.ok?("已保存 "+j.target):("保存失败 "+(j.error||r.status)));
  if(j.ok){$("#target").value=j.target;$("#bar").textContent="当前目录 "+j.target}
};
$("#run").onchange=function(){$("#save").click()};
$("#copy").onclick=async function(){
  var t=$("#prompt").textContent;
  try{await navigator.clipboard.writeText(t);}
  catch(e){
    var r=document.createRange();r.selectNodeContents($("#prompt"));
    var s=getSelection();s.removeAllRanges();s.addRange(r);
    document.execCommand("copy");
  }
  show("提示词已复制");
};
load();
</script>
</body>
</html>
"""


def setup_html(cfg):
    return (SETUP_HTML.replace("__VERSION__", VERSION)
            .replace("__TOKEN__", cfg.get("token", ""))
            .replace("__TARGET__", cfg.get("target") or DEFAULT_TARGET))


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
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "::1", "[::1]")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = _CURRENT_PORT[0]
        return origin in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)

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
            return self._json(200, {"ok": True, "version": VERSION, "port": _CURRENT_PORT[0],
                                    "target": cfg.get("target")})

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
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            target = cfg.get("target")
            for part in qs.split("&"):
                if part.startswith("target="):
                    target = urllib.parse.unquote(part.split("=", 1)[1])
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

    fenced = "===FILE: a.txt===\n```js\nx\n```\n"
    f2, _ = parse(fenced)
    assert f2[0][1].strip() == "x", f2

    bold = "**===FILE: ./src/b.js===**\nconsole.log(1)\n"
    f3, _ = parse(bold)
    assert f3 and f3[0][0] == "./src/b.js", f3

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        res = apply_text(sample, tmp)
        assert res["ok"] and os.path.exists(os.path.join(tmp, "src", "main.js"))
        bad = apply_text("===FILE: ../evil.txt===\nx\n", tmp)
        assert bad["ok"] and bad["files"] == [] and bad["skipped"], bad
        once = apply_text("===FILE: index.html===\nv2\n", tmp)
        assert once["ok"] and os.path.exists(os.path.join(tmp, ".p2r", "last-apply.json"))
        bdir = os.path.join(tmp, ".p2r", "backup")
        assert os.path.isdir(bdir) and os.listdir(bdir), "覆写没有备份"

    print("selftest ok: 解析 / 围栏 / 加粗标记 / 越界拒绝 / 覆写备份 全部通过")


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
