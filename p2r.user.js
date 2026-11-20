// ==UserScript==
// @name         Paste2Run
// @namespace    https://local.p2r
// @version      0.3.1
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

  function toast(text, ms = 2800) {
    let el = document.getElementById("p2r-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "p2r-toast";
      el.style.cssText = [
        "position:fixed", "right:18px", "bottom:18px", "z-index:2147483647",
        "max-width:380px", "padding:10px 14px", "border-radius:0",
        "background:#070707", "color:#f2f2f2",
        "font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
        "box-shadow:4px 4px 0 rgba(255,255,255,.15)", "white-space:pre-wrap",
        "border:1px solid #f2f2f2", "opacity:0",
        "transition:opacity .15s ease",
      ].join(";");
      document.body.appendChild(el);
    }
    el.textContent = "> " + text;
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

    // 实时同步服务端最新 target 与 allow_run，避免多标签页配置漂移
    const info = await ping();
    if (!info) {
      toast("连不上本地服务，请确认 Paste2Run 正在运行");
      return;
    }
    if (info.target) set(LS.target, info.target);
    const serverAllowRun = !!info.allow_run;
    set(LS.run, serverAllowRun ? "1" : "0");

    set(LS.lastHash, h);
    toast("[P2R] APPLYING…");
    try {
      const res = await post("/apply", {
        text,
        dryRun: dryOn(),
        source: location.hostname,
      });
      if (res.ok) {
        const list = (res.files || []).map((f) => "  + " + f.path).slice(0, 6).join("\n");
        const more = (res.files || []).length > 6 ? `\n  …共 ${res.files.length} 个文件` : "";
        const skipNote = (res.skipped || []).length ? `\n  ! 跳过 ${res.skipped.length} 个越界/受保护路径` : "";
        toast(`WRITTEN ${res.files.length} FILE(S)${dryOn() ? " [DRY-RUN]" : ""}\n${list}${more}${skipNote}`, 4200);
        if (GM_notification) {
          GM_notification({ text: `Paste2Run 写入 ${res.files.length} 个文件`, timeout: 3000 });
        }
        if (!dryOn() && serverAllowRun && res.run) {
          toast("[P2R] EXEC ===RUN===…", 4000);
          post("/run", {}, 300000)
            .then(async (r) => {
              if (r.ok) {
                toast(`[P2R] RUN OK (${r.duration}s)`);
              } else if (r.error) {
                toast("[P2R] RUN BLOCKED: " + r.error, 4200);
              } else {
                const rep = await post("/repair", { output: r.output || "" });
                GM_setClipboard(rep.prompt || "", "text");
                const first = (r.output || "").split("\n").filter(Boolean).slice(-1)[0] || "";
                toast("[P2R] RUN FAILED -> 修复提示词已复制\n" + first.slice(0, 120), 6000);
              }
            })
            .catch((e) => toast("[P2R] RUN ERROR: " + e.message, 4200));
        }
      } else if (res.error && res.error.indexOf("X-P2R-Token") >= 0) {
        toast("TOKEN 不匹配，请打开 " + ENDPOINT + " 重新安装脚本", 6000);
      } else {
        toast("APPLY FAILED: " + (res.error || "未知错误"));
      }
    } catch (e) {
      toast(e.message + "，确认 Paste2Run 本地服务在跑");
    }
  }

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
      if (r.ok) { set(LS.target, r.target); toast("TARGET SET:\n" + r.target); }
      else toast("设置失败：" + (r.error || ""));
    } catch (e) { toast(e.message); }
  });

  GM_registerMenuCommand("切换自动落地（开/关）", () => {
    const v = autoOn() ? "0" : "1";
    set(LS.auto, v);
    toast(v === "1" ? "AUTO_APPLY: ON" : "AUTO_APPLY: OFF");
  });

  GM_registerMenuCommand("切换试运行（只解析不写盘）", () => {
    const v = dryOn() ? "0" : "1";
    set(LS.dry, v);
    toast(v === "1" ? "DRY_RUN: ON" : "DRY_RUN: OFF");
  });

  GM_registerMenuCommand("切换自动执行 ===RUN===", async () => {
    const info = await ping();
    const next = !(info ? !!info.allow_run : get(LS.run, "0") === "1");
    set(LS.run, next ? "1" : "0");
    post("/config", { allow_run: next }).catch(() => {});
    toast(next ? "AUTO_RUN: ON" : "AUTO_RUN: OFF");
  });

  GM_registerMenuCommand("手动落地最后一条回复", () => {
    const node = lastAssistantNode();
    if (!node) return toast("没找到助手消息");
    applyNode(node, { force: true });
  });

  // 左下角黑白极简 ASCII 悬浮按钮
  function mountButton() {
    const b = document.createElement("button");
    b.textContent = "[ P2R: APPLY ]";
    b.title = "Paste2Run：把最后一条回复写成文件";
    b.style.cssText = [
      "position:fixed", "left:16px", "bottom:16px", "z-index:2147483647",
      "padding:7px 12px", "border-radius:0", "border:1px solid #f2f2f2",
      "background:#070707", "color:#f2f2f2",
      "font:700 11px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
      "letter-spacing:.06em", "cursor:pointer", "opacity:.65",
      "transition:opacity .15s,background .15s,color .15s",
    ].join(";");
    b.onmouseenter = () => { b.style.opacity = "1"; b.style.background = "#f2f2f2"; b.style.color = "#070707"; };
    b.onmouseleave = () => { b.style.opacity = ".65"; b.style.background = "#070707"; b.style.color = "#f2f2f2"; };
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
    if (info && typeof info.allow_run === "boolean") set(LS.run, info.allow_run ? "1" : "0");
    watch();
    mountButton();
    if (!info) toast("LOCAL SERVER OFFLINE\n先双击 start 脚本启动 Paste2Run", 5000);
  }, 1200);
})();
