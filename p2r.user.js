// ==UserScript==
// @name         Paste2Run
// @namespace    https://local.p2r
// @version      0.3.0
// @updateURL    __P2R_ENDPOINT__/p2r.user.js
// @downloadURL  __P2R_ENDPOINT__/p2r.user.js
// @description  网页 AI 回复完成后，自动把 ===FILE:=== 段落落地成本地项目文件
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @match        https://claude.ai/*
// @match        https://gemini.google.com/*
// @match        https://chat.deepseek.com/*
// @match        https://yuanbao.tencent.com/*
// @match        https://www.doubao.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @grant        GM_setClipboard
// @grant        GM_openInTab
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  "use strict";

  const ENDPOINT = "__P2R_ENDPOINT__";
  const TOKEN = "__P2R_TOKEN__";
  const LS = {
    auto: "p2r_auto",
    target: "p2r_target",
    dry: "p2r_dry",
    run: "p2r_run",
    lastHash: "p2r_last",
  };

  const get = (k, d) => localStorage.getItem(k) ?? d;
  const set = (k, v) => localStorage.setItem(k, v);
  const autoOn = () => get(LS.auto, "1") === "1";
  const dryOn = () => get(LS.dry, "0") === "1";
  const runOn = () => get(LS.run, "0") === "1";

  // 各站点助手消息选择器，按优先级尝试
  const SELECTORS = [
    '[data-message-author-role="assistant"]',
    '[data-testid="assistant-message"]',
    ".font-claude-message",
    '[data-is-streaming="false"]',
    ".ds-markdown",
    "message-content",
    ".model-response-text",
    '[class*="markdown"]',
  ];

  function lastAssistantNode() {
    for (const sel of SELECTORS) {
      const nodes = document.querySelectorAll(sel);
      if (nodes.length) return nodes[nodes.length - 1];
    }
    return null;
  }

  function toast(text, ms = 2600) {
    let el = document.getElementById("p2r-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "p2r-toast";
      el.style.cssText = [
        "position:fixed", "right:18px", "bottom:18px", "z-index:2147483647",
        "max-width:360px", "padding:11px 15px", "border-radius:10px",
        "background:#111", "color:#f5efe4", "font:13px/1.5 -apple-system,sans-serif",
        "box-shadow:0 8px 30px rgba(0,0,0,.35)", "white-space:pre-wrap",
        "border:1px solid rgba(255,255,255,.15)", "opacity:0",
        "transition:opacity .2s",
      ].join(";");
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.style.opacity = "1";
    clearTimeout(el._t);
    el._t = setTimeout(() => (el.style.opacity = "0"), ms);
  }

  function post(path, payload, timeout = 30000) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: ENDPOINT + path,
        headers: { "Content-Type": "application/json", "X-P2R-Token": TOKEN },
        data: JSON.stringify(payload),
        timeout,
        onload: (r) => {
          try { resolve(JSON.parse(r.responseText)); }
          catch { reject(new Error("响应不是 JSON")); }
        },
        onerror: () => reject(new Error("连不上本地服务")),
        ontimeout: () => reject(new Error("本地服务超时")),
      });
    });
  }

  function ping() {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "GET",
        url: ENDPOINT + "/ping",
        timeout: 4000,
        onload: (r) => {
          try { resolve(JSON.parse(r.responseText)); } catch { resolve(null); }
        },
        onerror: () => resolve(null),
      });
    });
  }

  function hash(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return String(h) + ":" + s.length;
  }

  async function applyNode(node, opts = {}) {
    const text = (node.innerText || node.textContent || "").trim();
    if (!text.includes("===FILE:")) {
      if (!opts.silent) toast("这段回复里没有 ===FILE: 标记");
      return;
    }
    const h = hash(text);
    if (!opts.force && get(LS.lastHash) === h) return;

    const target = get(LS.target, "");
    if (!target) {
      toast("先打开设置页配好目标目录（油猴菜单 → 打开设置页）");
      return;
    }

    set(LS.lastHash, h);
    toast("正在落地…");
    try {
      const res = await post("/apply", { text, target, dryRun: dryOn() });
      if (res.ok) {
        const list = (res.files || []).map((f) => "· " + f.path).slice(0, 6).join("\n");
        const more = (res.files || []).length > 6 ? `\n…共 ${res.files.length} 个文件` : "";
        toast(`已写入 ${res.files.length} 个文件${dryOn() ? "（试运行）" : ""}\n${list}${more}`, 4200);
        if (GM_notification) {
          GM_notification({ text: `Paste2Run 写入 ${res.files.length} 个文件`, timeout: 3000 });
        }
        if (!dryOn() && runOn() && res.run) {
          toast("已写入，正在启动，可能要一会儿…", 4000);
          post("/run", { target }, 300000)
            .then(async (r) => {
              if (r.ok) {
                toast(`启动成功（${r.duration}s）`);
              } else if (r.error) {
                toast("启动失败：" + r.error, 4200);
              } else {
                const rep = await post("/repair", { target, output: r.output || "" });
                GM_setClipboard(rep.prompt || "", "text");
                const first = (r.output || "").split("\n").filter(Boolean).slice(-1)[0] || "";
                toast("启动失败，修复提示词已复制\n粘回对话框即可\n" + first.slice(0, 120), 6000);
              }
            })
            .catch((e) => toast("启动请求失败：" + e.message, 4200));
        }
      } else if (res.error && res.error.indexOf("X-P2R-Token") >= 0) {
        toast("脚本和本地服务对不上了\n打开 " + ENDPOINT + " 重新点一次「安装脚本」", 6000);
      } else {
        toast("落地失败：" + (res.error || "未知错误"));
      }
    } catch (e) {
      toast(e.message + "，确认 Paste2Run 本地服务在跑（双击 start 脚本）");
    }
  }

  // 观测：内容停止变化 1.4 秒后视为生成完成
  let timer = null;
  let lastText = "";

  function watch() {
    const mo = new MutationObserver(() => {
      if (!autoOn()) return;
      const node = lastAssistantNode();
      if (!node) return;
      const t = (node.innerText || "").trim();
      if (!t || t === lastText) return;
      lastText = t;
      clearTimeout(timer);
      timer = setTimeout(() => {
        const now = (node.innerText || "").trim();
        if (now === lastText && now.includes("===FILE:")) applyNode(node, { silent: true });
      }, 1400);
    });
    mo.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  // 菜单
  GM_registerMenuCommand("打开设置页", () => {
    if (GM_openInTab) GM_openInTab(ENDPOINT + "/", { active: true });
    else window.open(ENDPOINT + "/", "_blank");
  });

  GM_registerMenuCommand("设置目标目录", async () => {
    const cur = (await ping())?.target || get(LS.target, "");
    const dir = prompt("输入本机项目目录（绝对路径）", cur);
    if (!dir) return;
    try {
      const r = await post("/config", { target: dir });
      if (r.ok) { set(LS.target, r.target); toast("目标目录已设为\n" + r.target); }
      else toast("设置失败：" + (r.error || ""));
    } catch (e) { toast(e.message); }
  });

  GM_registerMenuCommand("切换自动落地（当前：开/关）", () => {
    const v = autoOn() ? "0" : "1";
    set(LS.auto, v);
    toast(v === "1" ? "自动落地：开" : "自动落地：关");
  });

  GM_registerMenuCommand("切换试运行（只解析不写盘）", () => {
    const v = dryOn() ? "0" : "1";
    set(LS.dry, v);
    toast(v === "1" ? "试运行：开" : "试运行：关");
  });

  GM_registerMenuCommand("切换自动启动/修复（当前：关）", () => {
    const v = runOn() ? "0" : "1";
    set(LS.run, v);
    post("/config", { allow_run: v === "1" }).catch(() => {});
    toast(v === "1" ? "自动启动：开（会执行 ===RUN=== 里的命令）" : "自动启动：关");
  });

  GM_registerMenuCommand("手动落地最后一条回复", () => {
    const node = lastAssistantNode();
    if (!node) return toast("没找到助手消息");
    applyNode(node, { force: true });
  });

  // 左下角悬浮按钮，站点快捷键失效时兜底
  function mountButton() {
    const b = document.createElement("button");
    b.textContent = "落地";
    b.title = "Paste2Run：把最后一条回复写成文件";
    b.style.cssText = [
      "position:fixed", "left:16px", "bottom:16px", "z-index:2147483647",
      "padding:8px 14px", "border-radius:999px", "border:1px solid rgba(255,255,255,.18)",
      "background:#1b1b1b", "color:#f0c98a", "font:12px/1 -apple-system,sans-serif",
      "cursor:pointer", "opacity:.55",
    ].join(";");
    b.onmouseenter = () => (b.style.opacity = "1");
    b.onmouseleave = () => (b.style.opacity = ".55");
    b.onclick = () => {
      const node = lastAssistantNode();
      if (!node) return toast("没找到助手消息");
      applyNode(node, { force: true });
    };
    document.body.appendChild(b);
  }

  setTimeout(async () => {
    const info = await ping();
    if (info && info.target) set(LS.target, info.target);
    watch();
    mountButton();
    if (!info) toast("Paste2Run：本地服务没连上\n先双击 start 脚本启动本地服务", 5000);
  }, 1200);
})();
