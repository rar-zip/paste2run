// ==UserScript==
// @name         Paste2Run
// @namespace    https://local.p2r
// @version      0.5.0
// @updateURL    __P2R_ENDPOINT__/p2r.user.js
// @downloadURL  __P2R_ENDPOINT__/p2r.user.js
// @description  零 Token 网页 AI 轻量 Agent：自动落地 ===FILE:===、执行本地只读 ===TOOL:===、自动回填修复与结果
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @match        https://claude.ai/*
// @match        https://gemini.google.com/*
// @match        https://aistudio.google.com/*
// @match        https://chat.deepseek.com/*
// @match        https://yuanbao.tencent.com/*
// @match        https://www.doubao.com/*
// @match        https://lmarena.ai/*
// @match        https://chat.lmsys.org/*
// @match        https://arena.lmsys.org/*
// @match        https://copilot.microsoft.com/*
// @match        https://kimi.moonshot.cn/*
// @match        https://www.kimi.com/*
// @match        https://chatglm.cn/*
// @match        https://chat.z.ai/*
// @match        https://tongyi.aliyun.com/*
// @match        https://www.tongyi.com/*
// @match        https://yiyan.baidu.com/*
// @match        https://chat.qwen.ai/*
// @match        https://poe.com/*
// @match        https://grok.com/*
// @match        https://www.perplexity.ai/*
// @match        https://chat.mistral.ai/*
// @match        https://openrouter.ai/chat*
// @match        https://huggingface.co/chat/*
// @match        https://chat.minimax.io/*
// @match        https://hailuoai.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @grant        GM_setClipboard
// @grant        GM_openInTab
// @grant        unsafeWindow
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
    gate: "p2r_gate",
    agentMode: "p2r_agent_mode", // semi | auto | off
  };

  const get = (k, d) => localStorage.getItem(k) ?? d;
  const set = (k, v) => localStorage.setItem(k, v);
  const autoOn = () => get(LS.auto, "1") === "1";
  const gateOn = () => get(LS.gate, "1") === "1";
  const dryOn = () => get(LS.dry, "0") === "1";
  const agentMode = () => get(LS.agentMode, "semi");

  const GATE = /P2R[-_\s]?READY/i;
  const FILE_MARK = /^[ \t]*(?:\*\*)?===\s*FILE:\s*[^\r\n]+?===/m;
  const TOOL_MARK = /^[ \t]*(?:\*\*)?===\s*TOOL:\s*[A-Za-z0-9_.-]+\s*===/m;
  const ANY_MARK = /^[ \t]*(?:\*\*)?===\s*(?:FILE:\s*[^\r\n]+?|TOOL:\s*[A-Za-z0-9_.-]+)\s*===/m;
  const IGNORE_CLOSEST = '[data-message-author-role="user"], [data-testid="user-message"], textarea, input, [contenteditable="true"], #p2r-toast, #p2r-bar';

  const AGENT_PROMPT = `Paste2Run 轻量 Agent 协议（零 Token 本地工具）：
1. 需要查看本地项目上下文时，可输出工具调用块（等待下一轮工具结果）：
   ===TOOL: tree===
   ===TOOL: list_files===
   ===TOOL: read_file===
   src/app.js
   ===TOOL: search===
   关键字
   ===TOOL: status===
2. 准备好修改或生成代码时，用 ===FILE: 相对路径=== 输出完整文件内容（不要省略号、不要 diff）。
3. 新项目首行加 ===PROJECT: 英文小写项目名===；确定可运行的启动命令才附 ===RUN===。
4. 本轮工具调用或代码全部输出完后，最后单独一行输出 P2R-READY。`;

  const SELECTORS = [
    '[data-message-author-role="assistant"]',
    '[data-testid="assistant-message"]',
    '[data-testid="conversation-turn"]',
    ".font-claude-message",
    ".ds-markdown",
    "message-content",
    ".model-response-text",
    "model-response",
    ".chat-message",
    ".message-content",
    '[class*="assistant"]',
    '[class*="markdown"]',
  ];

  const INPUT_SELECTORS = [
    "#prompt-textarea",
    'div[contenteditable="true"][role="textbox"]',
    'div.ProseMirror[contenteditable="true"]',
    'rich-textarea div[contenteditable="true"]',
    'textarea[placeholder]',
    "textarea",
    'div[contenteditable="true"]',
  ];

  const SEND_SELECTORS = [
    'button[data-testid="send-button"]',
    'button[aria-label*="Send"]',
    'button[aria-label*="发送"]',
    'button[title*="发送"]',
    'button[type="submit"]',
  ];

  const MAX_AUTO_STEPS = 5;
  let autoSteps = 0;
  let agentStopped = false;

  const textOf = (el) => (el ? (el.innerText || el.textContent || "").trim() : "");

  function findChatInput() {
    for (const sel of INPUT_SELECTORS) {
      const nodes = document.querySelectorAll(sel);
      for (let i = nodes.length - 1; i >= 0; i--) {
        const el = nodes[i];
        if (el.closest && el.closest("#p2r-bar")) continue;
        const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
        if (rect && rect.width > 40 && rect.height > 16) return el;
      }
    }
    return null;
  }

  function fillChatInput(text, append = false) {
    const el = findChatInput();
    if (!el) return false;
    el.focus();
    const isEditable = el.isContentEditable || el.getAttribute("contenteditable") === "true";
    if (isEditable) {
      const cur = append ? (el.innerText || "").trim() : "";
      const next = cur ? (cur + "\n\n" + text) : text;
      el.textContent = next;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      const cur = append ? (el.value || "").trim() : "";
      const next = cur ? (cur + "\n\n" + text) : text;
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(el, next);
      else el.value = next;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return true;
  }

  function triggerSend() {
    for (const sel of SEND_SELECTORS) {
      const btn = document.querySelector(sel);
      if (btn && !btn.disabled) {
        const rect = btn.getBoundingClientRect ? btn.getBoundingClientRect() : null;
        if (rect && rect.width > 0 && rect.height > 0) {
          btn.click();
          return true;
        }
      }
    }
    const input = findChatInput();
    if (input) {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
      return true;
    }
    return false;
  }

  function deliverToChat(text, label) {
    clip(text);
    const mode = agentMode();
    if (mode === "off") {
      toast(label + "（已复制剪贴板）", 5000);
      return;
    }
    const filled = fillChatInput(text, false);
    if (!filled) {
      toast(label + "（未找到输入框，已复制剪贴板）", 5500);
      return;
    }
    if (mode === "auto" && !agentStopped && autoSteps < MAX_AUTO_STEPS) {
      autoSteps++;
      toast(`${label} · 自动发送 (${autoSteps}/${MAX_AUTO_STEPS})`, 3500);
      setTimeout(() => {
        if (!agentStopped) triggerSend();
      }, 600);
    } else {
      toast(label + "（已填入输入框，按回车发送）", 5500);
    }
  }

  function findTarget() {
    const cands = [];
    const seen = new Set();
    const push = (el) => {
      if (!el || seen.has(el)) return;
      if (el.closest && el.closest(IGNORE_CLOSEST)) return;
      const t = textOf(el);
      if (!t || !ANY_MARK.test(t)) return;
      seen.add(el);
      cands.push(el);
    };

    for (const sel of SELECTORS) {
      let nodes;
      try { nodes = document.querySelectorAll(sel); } catch { continue; }
      const start = Math.max(0, nodes.length - 3);
      for (let i = start; i < nodes.length; i++) push(nodes[i]);
    }

    try {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
        acceptNode(n) {
          return ANY_MARK.test(n.nodeValue || "") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
        },
      });
      let hits = 0;
      while (walker.nextNode() && hits < 24) {
        hits++;
        const leaf = walker.currentNode.parentElement;
        if (!leaf || (leaf.closest && leaf.closest(IGNORE_CLOSEST))) continue;
        const baseLen = textOf(leaf).length || 1;
        let el = leaf;
        for (let i = 0; i < 8 && el.parentElement; i++) {
          const parent = el.parentElement;
          if (parent === document.body) break;
          const pt = textOf(parent);
          if (!ANY_MARK.test(pt)) break;
          if (pt.length > baseLen * 8 + 2000) break;
          el = parent;
        }
        push(el);
      }
    } catch {}

    if (!cands.length) return null;

    let best = null;
    let bestScore = -1;
    cands.forEach((el, idx) => {
      const t = textOf(el);
      const fMarks = (t.match(/===\s*FILE:/g) || []).length;
      const tMarks = (t.match(/===\s*TOOL:/g) || []).length;
      const hasRun = /===\s*RUN\s*===/.test(t);
      let score = (fMarks + tMarks) * 1000 + Math.min(t.length, 40000) / 50 + idx;
      if (hasRun) score += 200;
      if (score > bestScore) { bestScore = score; best = el; }
    });
    return best;
  }

  function toast(text, ms = 2800) {
    let el = document.getElementById("p2r-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "p2r-toast";
      el.style.cssText = [
        "position:fixed", "right:18px", "bottom:18px", "z-index:2147483647",
        "max-width:390px", "padding:10px 14px", "border-radius:0",
        "background:#070707", "color:#f2f2f2",
        "font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
        "box-shadow:4px 4px 0 rgba(255,255,255,.15)", "white-space:pre-wrap",
        "border:1px solid #f2f2f2", "opacity:0", "transition:opacity .15s ease",
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

  function clip(text) {
    try { GM_setClipboard(text || "", "text"); return true; } catch { return false; }
  }

  async function runFailure(r) {
    const cls = r.class || "unknown";
    let rep = null;
    try {
      rep = await post("/repair", {
        output: r.output || "",
        code: r.code,
        force: cls === "file",
      }, 20000);
    } catch (e) {
      return toast("[P2R] RUN FAILED: " + e.message, 5000);
    }

    if (rep && rep.mode === "env") {
      clip((rep.plan && rep.plan.text) || "");
      const why = (rep.reasons || []).slice(0, 2).join("；");
      toast(
        "[P2R] 本机环境问题（未打扰 AI）\n" +
          (why || rep.hint || "") + "\n" +
          "最简运行方案已复制到剪贴板",
        8500
      );
      return;
    }

    if (rep && rep.prompt) {
      deliverToChat(rep.prompt, "[P2R] 代码报错 -> 修复提示词就绪");
    }
  }

  let lastFailAt = 0;

  async function handleNode(node, opts = {}) {
    const text = textOf(node);
    const hasFile = FILE_MARK.test(text);
    const hasTool = TOOL_MARK.test(text);
    if (!hasFile && !hasTool) {
      if (!opts.silent) toast("这段回复里没有 ===FILE: 或 ===TOOL: 标记");
      return;
    }
    const h = hash(text);
    if (!opts.force && get(LS.lastHash) === h) return;
    if (!opts.force && gateOn() && !GATE.test(text)) {
      if (!opts.silent) {
        toast("[P2R] 未检测到 P2R-READY，点 [ APPLY ] 可强制执行", 4500);
      }
      return;
    }
    if (!opts.force && Date.now() - lastFailAt < 60000) return;

    const info = await ping();
    if (!info) {
      toast("连不上本地服务，请确认 Paste2Run 正在运行");
      return;
    }
    if (info.target) set(LS.target, info.target);
    const serverAllowRun = !!info.allow_run;
    set(LS.run, serverAllowRun ? "1" : "0");

    // 1. 若存在本地只读工具调用（===TOOL:===），先执行并把观察结果回填给网页 AI
    if (hasTool && !hasFile) {
      toast("[P2R AGENT] EXEC TOOLS…");
      try {
        const res = await post("/tool", { text }, 30000);
        if (res.ok && res.reply) {
          set(LS.lastHash, h);
          const names = (res.results || []).map((x) => x.tool).join(", ");
          deliverToChat(res.reply, `[P2R AGENT] 工具完成 (${names})`);
        } else {
          lastFailAt = Date.now();
          toast("[P2R AGENT] TOOL FAIL: " + (res.error || ""));
        }
      } catch (e) {
        lastFailAt = Date.now();
        toast("[P2R AGENT] " + e.message);
      }
      return;
    }

    // 2. 落地文件（===FILE:===）
    toast("[P2R] APPLYING…");
    try {
      const res = await post("/apply", {
        text,
        dryRun: dryOn(),
        source: location.hostname,
      });
      if (res.ok) {
        set(LS.lastHash, h);
        autoSteps = 0; // 成功落地一轮后重置自动步数
        const list = (res.files || []).map((f) => "  + " + f.path).slice(0, 6).join("\n");
        const more = (res.files || []).length > 6 ? `\n  …共 ${res.files.length} 个文件` : "";
        const skipNote = (res.skipped || []).length ? `\n  ! 跳过 ${res.skipped.length} 个越界/受保护路径` : "";
        const openNote = res.open && res.open.ok && res.open.url ? `\n  > 已打开 ${res.open.url}` : "";
        toast(`WRITTEN ${res.files.length} FILE(S)${dryOn() ? " [DRY-RUN]" : ""}\n${list}${more}${skipNote}${openNote}`, 4200);
        if (GM_notification) {
          GM_notification({ text: `Paste2Run 写入 ${res.files.length} 个文件`, timeout: 3000 });
        }
        if (!dryOn() && serverAllowRun) {
          toast(res.run ? "[P2R] EXEC ===RUN===…" : "[P2R] AUTO RUN…", 4000);
          post("/run", {}, 300000)
            .then(async (r) => {
              if (r.ok && r.skipped) {
                toast("[P2R] 没识别出运行命令，已跳过", 3500);
                return;
              }
              if (r.ok) {
                const where = r.url ? ` -> ${r.url}` : "";
                toast(`[P2R] RUN OK (${r.duration ?? "?"}s)${where}`, 5000);
              } else if (r.error) {
                toast("[P2R] RUN BLOCKED: " + r.error, 4200);
              } else {
                await runFailure(r);
              }
            })
            .catch((e) => {
              lastFailAt = Date.now();
              toast("[P2R] RUN ERROR: " + e.message, 4200);
            });
        }
      } else {
        try { localStorage.removeItem(LS.lastHash); } catch {}
        lastFailAt = Date.now();
        if (res.error && res.error.indexOf("X-P2R-Token") >= 0) {
          toast("TOKEN 不匹配，请打开 " + ENDPOINT + " 重新安装脚本", 6000);
        } else {
          toast("APPLY FAILED: " + (res.error || "未知错误"));
        }
      }
    } catch (e) {
      lastFailAt = Date.now();
      toast(e.message + "，确认 Paste2Run 本地服务在跑");
    }
  }

  let timer = null;
  let lastText = "";
  let scanPending = false;

  function watch() {
    const mo = new MutationObserver(() => {
      if (!autoOn() || scanPending) return;
      scanPending = true;
      setTimeout(() => {
        scanPending = false;
        const node = findTarget();
        if (!node) return;
        const t = textOf(node);
        if (!t || t === lastText) return;
        lastText = t;
        clearTimeout(timer);
        timer = setTimeout(() => {
          const now = textOf(node);
          if (now === lastText && ANY_MARK.test(now)) handleNode(node, { silent: true });
        }, 1400);
      }, 300);
    });
    mo.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  // ── 菜单命令 ──────────────────────────────────────────────────
  GM_registerMenuCommand("打开设置页", () => {
    if (GM_openInTab) GM_openInTab(ENDPOINT + "/", { active: true });
    else window.open(ENDPOINT + "/", "_blank");
  });
  GM_registerMenuCommand("向输入框注入 Agent 协议提示词", () => {
    if (fillChatInput(AGENT_PROMPT, true)) toast("[P2R] 协议已注入输入框");
    else { clip(AGENT_PROMPT); toast("[P2R] 未找到输入框，协议已复制"); }
  });
  GM_registerMenuCommand("切换 Agent 回填模式（SEMI / AUTO / OFF）", () => {
    const seq = ["semi", "auto", "off"];
    const next = seq[(seq.indexOf(agentMode()) + 1) % seq.length];
    set(LS.agentMode, next);
    agentStopped = false;
    autoSteps = 0;
    refreshBar();
    toast("AGENT MODE: " + next.toUpperCase());
  });
  GM_registerMenuCommand("切换自动落地（开/关）", () => {
    const v = autoOn() ? "0" : "1";
    set(LS.auto, v);
    toast(v === "1" ? "AUTO_APPLY: ON" : "AUTO_APPLY: OFF");
  });
  GM_registerMenuCommand("切换门禁标记 P2R-READY（开/关）", () => {
    const v = gateOn() ? "0" : "1";
    set(LS.gate, v);
    toast(v === "1" ? "GATE: ON" : "GATE: OFF");
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
  GM_registerMenuCommand("手动落地 / 执行最后一条回复", () => {
    const node = findTarget();
    if (!node) return toast("没找到带 ===FILE: 或 ===TOOL: 的回复");
    handleNode(node, { force: true });
  });
  GM_registerMenuCommand("打开项目入口（浏览器）", async () => {
    try {
      const r = await post("/open", {}, 60000);
      if (r.ok) toast("已打开 " + (r.url || ""), 4000);
      else toast("打开失败：" + (r.error || ""), 6000);
    } catch (e) { toast(e.message); }
  });

  // ── zip 自动接管（已解耦，不再错误绑定在 P2R-READY 文本门禁上） ──
  const pageWin = (typeof unsafeWindow !== "undefined" && unsafeWindow) ? unsafeWindow : window;
  const zipSeen = new Set();
  const recentZipBlobs = [];

  function looksZipType(t) { return /zip/i.test(t || ""); }

  async function isPkBlob(blob) {
    try {
      const head = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
      return head.length >= 4 && head[0] === 0x50 && head[1] === 0x4b &&
        (head[2] === 0x03 || head[2] === 0x05 || head[2] === 0x07);
    } catch { return false; }
  }

  function blobToB64(blob) {
    return new Promise((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => {
        const r = String(fr.result || "");
        const i = r.indexOf(",");
        res(i >= 0 ? r.slice(i + 1) : "");
      };
      fr.onerror = () => rej(new Error("读取 blob 失败"));
      fr.readAsDataURL(blob);
    });
  }

  function replayBlob(a, href) {
    try {
      const t = document.createElement("a");
      t.href = href;
      t.setAttribute("data-p2r-replay", "1");
      const dl = a && a.getAttribute ? a.getAttribute("download") : "";
      if (dl) t.download = dl;
      t.style.display = "none";
      document.body.appendChild(t);
      t.click();
      t.remove();
    } catch {}
  }

  async function importZipBlob(blob, why) {
    if (!blob || !blob.size) return;
    if (blob.size > 40 * 1024 * 1024) return toast("[P2R] zip 超过 40MB，跳过", 5000);
    if (!(await isPkBlob(blob))) return;
    try {
      toast("[P2R] 抓到 zip，正在解压…", 5000);
      const b64 = await blobToB64(blob);
      const r = await post("/import-zip", { zip: b64, source: why || "zip-blob" }, 180000);
      if (r.ok) {
        const openNote = r.open && r.open.url ? `\n> 已打开 ${r.open.url}` : "";
        toast(`[P2R] 解压 ${r.files.length} 个文件 -> ${r.target}${openNote}`, 7000);
        if (GM_notification) {
          GM_notification({ text: `Paste2Run 从 zip 导入 ${r.files.length} 个文件`, timeout: 3000 });
        }
      } else {
        toast("[P2R] 导入失败: " + (r.error || ""), 7000);
      }
    } catch (err) {
      toast("[P2R] 导入失败: " + err.message, 7000);
    }
  }

  try {
    const U = pageWin.URL;
    const origCreate = U.createObjectURL;
    if (origCreate && !origCreate.__p2rHook) {
      const hooked = function (obj) {
        const url = origCreate.call(U, obj);
        try {
          const isBlob = obj && typeof obj.size === "number";
          if (isBlob && (looksZipType(obj.type) || obj.size > 1024)) {
            recentZipBlobs.push({ url, blob: obj, at: Date.now(), type: obj.type || "" });
            while (recentZipBlobs.length > 6) recentZipBlobs.shift();
          }
        } catch {}
        return url;
      };
      hooked.__p2rHook = true;
      U.createObjectURL = hooked;
    }
  } catch {}

  document.addEventListener("click", async (e) => {
    const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    const href = a ? (a.href || "") : "";
    const dl = a ? (a.getAttribute("download") || "") : "";

    if (/^blob:/i.test(href)) {
      if (a.hasAttribute("data-p2r-replay")) return;
      e.preventDefault();
      e.stopPropagation();
      if (zipSeen.has(href)) return;
      let blob = null;
      try {
        const resp = await pageWin.fetch(href);
        blob = await resp.blob();
      } catch {
        replayBlob(a, href);
        return;
      }
      if (await isPkBlob(blob)) {
        zipSeen.add(href);
        await importZipBlob(blob, "zip-blob-url");
      } else {
        replayBlob(a, href);
      }
      return;
    }

    if (/^https?:/i.test(href) && (/\.zip(\?|#|$)/i.test(href) || /zip/i.test(dl))) {
      e.preventDefault();
      e.stopPropagation();
      toast("[P2R] 抓到 zip 链接，下载并解压中…", 5000);
      post("/fetch-zip", { url: href }, 180000)
        .then((r) => {
          if (r.ok) {
            const openNote = r.open && r.open.url ? `\n> 已打开 ${r.open.url}` : "";
            toast(`[P2R] 解压 ${r.files.length} 个文件 -> ${r.target}${openNote}`, 7000);
          } else {
            toast("[P2R] 导入失败: " + (r.error || ""), 7000);
          }
        })
        .catch((err) => toast("[P2R] 导入失败: " + err.message, 7000));
      return;
    }

    if (!a) {
      const t = e.target || {};
      const label = String((t.textContent || "") + " " +
        (t.getAttribute ? (t.getAttribute("aria-label") || "") + " " + (t.title || "") : "")).slice(0, 60);
      if (!/zip|下载|download|导出|打包/i.test(label)) return;
      setTimeout(() => {
        const now = Date.now();
        const fresh = recentZipBlobs.filter((b) => now - b.at < 1500 && !zipSeen.has(b.url));
        if (!fresh.length) return;
        const last = fresh[fresh.length - 1];
        zipSeen.add(last.url);
        importZipBlob(last.blob, "zip-button");
      }, 350);
    }
  }, true);

  // ── 左下角黑白极简 ASCII 悬浮微条（零摩擦操作，无需切回控制台） ──
  let btnAgent = null;
  function refreshBar() {
    if (btnAgent) btnAgent.textContent = "AGENT:" + agentMode().toUpperCase();
  }

  function makeBtn(label, title, onClick) {
    const b = document.createElement("button");
    b.textContent = label;
    b.title = title;
    b.style.cssText = [
      "padding:6px 8px", "border:none", "border-right:1px solid #333",
      "background:#070707", "color:#f2f2f2", "cursor:pointer",
      "font:700 11px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
      "letter-spacing:.04em", "transition:background .12s,color .12s",
    ].join(";");
    b.onmouseenter = () => { b.style.background = "#f2f2f2"; b.style.color = "#070707"; };
    b.onmouseleave = () => { b.style.background = "#070707"; b.style.color = "#f2f2f2"; };
    b.onclick = onClick;
    return b;
  }

  function mountBar() {
    const bar = document.createElement("div");
    bar.id = "p2r-bar";
    bar.style.cssText = [
      "position:fixed", "left:16px", "bottom:16px", "z-index:2147483647",
      "display:inline-flex", "align-items:center",
      "border:1px solid #f2f2f2", "background:#070707", "opacity:.72",
      "transition:opacity .15s",
    ].join(";");
    bar.onmouseenter = () => (bar.style.opacity = "1");
    bar.onmouseleave = () => (bar.style.opacity = ".72");

    bar.appendChild(makeBtn("+P2R", "一键往当前输入框注入 Agent 协议提示词", () => {
      agentStopped = false;
      autoSteps = 0;
      if (fillChatInput(AGENT_PROMPT, true)) toast("[P2R] Agent 协议已注入输入框");
      else { clip(AGENT_PROMPT); toast("[P2R] 未找到输入框，协议已复制"); }
    }));

    bar.appendChild(makeBtn("APPLY", "手动落地或执行最后一条 AI 回复", () => {
      const node = findTarget();
      if (!node) return toast("没找到带 ===FILE: 或 ===TOOL: 的回复");
      handleNode(node, { force: true });
    }));

    bar.appendChild(makeBtn("OPEN", "一键打开当前项目预览", async () => {
      try {
        const r = await post("/open", {}, 60000);
        if (r.ok) toast("已打开 " + (r.url || ""), 4000);
        else toast("打开失败：" + (r.error || ""), 5000);
      } catch (e) { toast(e.message); }
    }));

    btnAgent = makeBtn("AGENT:" + agentMode().toUpperCase(), "切换轻量 Agent 回填模式（SEMI=填输入框 / AUTO=自动接力 / OFF=仅复制）", () => {
      const seq = ["semi", "auto", "off"];
      const next = seq[(seq.indexOf(agentMode()) + 1) % seq.length];
      set(LS.agentMode, next);
      agentStopped = false;
      autoSteps = 0;
      refreshBar();
      toast("AGENT MODE: " + next.toUpperCase());
    });
    bar.appendChild(btnAgent);

    const btnCfg = makeBtn("⚙", "打开 Paste2Run 控制台（Shift+点 = 急停自动接力）", (ev) => {
      if (ev.shiftKey) {
        agentStopped = true;
        toast("[P2R AGENT] 已急停自动接力");
        return;
      }
      if (GM_openInTab) GM_openInTab(ENDPOINT + "/", { active: true });
      else window.open(ENDPOINT + "/", "_blank");
    });
    btnCfg.style.borderRight = "none";
    bar.appendChild(btnCfg);

    document.body.appendChild(bar);
  }

  setTimeout(async () => {
    const info = await ping();
    if (info && info.target) set(LS.target, info.target);
    if (info && typeof info.allow_run === "boolean") set(LS.run, info.allow_run ? "1" : "0");
    watch();
    mountBar();
    if (!info) toast("LOCAL SERVER OFFLINE\n先运行 start_silent.vbs 或 start 脚本启动 Paste2Run", 5000);
  }, 1200);
})();
