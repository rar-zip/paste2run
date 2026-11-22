// ==UserScript==
// @name         Paste2Run
// @namespace    https://local.p2r
// @version      0.4.7
// @updateURL    __P2R_ENDPOINT__/p2r.user.js
// @downloadURL  __P2R_ENDPOINT__/p2r.user.js
// @description  网页 AI 回复完成后，自动把 ===FILE:=== 段落落地成本地项目文件
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
  };

  const get = (k, d) => localStorage.getItem(k) ?? d;
  const set = (k, v) => localStorage.setItem(k, v);
  const autoOn = () => get(LS.auto, "1") === "1";
  // 门禁：AI 必须在最后输出 P2R-READY 才自动落地，防止网页里复述/引用被误判
  const GATE = /P2R[-_\s]?READY/i;
  const gateOn = () => get(LS.gate, "1") === "1";
  const dryOn = () => get(LS.dry, "0") === "1";

  // 站点选择器只是「候选来源」，真正的判定靠内容（见 findTarget）
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

  // 必须匹配独立行头的 ===FILE: ...===，避免把用户自己粘贴的提示词误判成 AI 代码回复
  const MARK = /^[ \t]*(?:\*\*)?===\s*FILE:\s*[^\r\n]+?===/m;
  const IGNORE_CLOSEST = '[data-message-author-role="user"], [data-testid="user-message"], textarea, input, [contenteditable="true"], #p2r-toast';

  const textOf = (el) => (el ? (el.innerText || el.textContent || "").trim() : "");

  /**
   * 内容优先定位：先按站点选择器收候选，再用 DOM 文本兜底。
   * 这样即使某个站点整体改版、选择器全失效，只要页面上出现 ===FILE: 就还能认出来。
   */
  function findTarget() {
    const cands = [];
    const seen = new Set();
    const push = (el) => {
      if (!el || seen.has(el)) return;
      if (el.closest && el.closest(IGNORE_CLOSEST)) return;
      const t = textOf(el);
      if (!t || !MARK.test(t)) return;
      seen.add(el);
      cands.push(el);
    };

    for (const sel of SELECTORS) {
      let nodes;
      try {
        nodes = document.querySelectorAll(sel);
      } catch {
        continue;
      }
      const start = Math.max(0, nodes.length - 3);
      for (let i = start; i < nodes.length; i++) push(nodes[i]);
    }

    // 兜底：直接找文本里带 ===FILE: 的节点，向上收拢到「装得下整段」的容器
    try {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
        acceptNode(n) {
          return MARK.test(n.nodeValue || "")
            ? NodeFilter.FILTER_ACCEPT
            : NodeFilter.FILTER_SKIP;
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
          if (!MARK.test(pt)) break;
          if (pt.length > baseLen * 8 + 2000) break;
          el = parent;
        }
        push(el);
      }
    } catch {
      /* 某些页面会禁用 TreeWalker，忽略 */
    }

    if (!cands.length) return null;

    // 打分：标记数量是主信号，文本长度辅助，越靠后的消息优先
    let best = null;
    let bestScore = -1;
    cands.forEach((el, idx) => {
      const t = textOf(el);
      const marks = (t.match(/===\s*FILE:/g) || []).length;
      const hasRun = /===\s*RUN\s*===/.test(t);
      let score = marks * 1000 + Math.min(t.length, 40000) / 50 + idx;
      if (hasRun) score += 200;
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
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
      // 环境问题：不让 AI 瞎改代码，直接把最简运行方案交给用户
      clip((rep.plan && rep.plan.text) || "");
      const why = (rep.reasons || []).slice(0, 2).join("；");
      toast(
        "[P2R] 不是代码问题，是本机环境跑不起来\n" +
          (why || rep.hint || "") + "\n" +
          "最简运行方案已复制到剪贴板",
        9000
      );
      return;
    }

    clip(rep && rep.prompt ? rep.prompt : "");
    const tail = (r.output || "").split("\n").filter(Boolean).slice(-1)[0] || "";
    toast(
      (cls === "unknown" ? "[P2R] 判断不了是哪类问题，让 AI 先自查\n" : "[P2R] 代码有问题 -> 修复提示词已复制\n") +
        tail.slice(0, 130),
      7000
    );
  }

  let lastFailAt = 0;

  async function applyNode(node, opts = {}) {
    const text = textOf(node);
    if (!MARK.test(text)) {
      if (!opts.silent) toast("这段回复里没有 ===FILE: 标记");
      return;
    }
    const h = hash(text);
    if (!opts.force && get(LS.lastHash) === h) return;
    if (!opts.force && gateOn() && !GATE.test(text)) {
      // 没看到门禁标记：不自动落地，也不写 lastHash，等 AI 补上标记后还能重试
      if (!opts.silent) {
        toast("[P2R] 没看到 P2R-READY 标记，不自动落地\n手动点 [ P2R: APPLY ] 可以强制", 5000);
      }
      return;
    }
    // 失败后退避 60 秒，避免 MutationObserver 刷出一连串无效重试；手动点击不受限
    if (!opts.force && Date.now() - lastFailAt < 60000) return;

    const info = await ping();
    if (!info) {
      toast("连不上本地服务，请确认 Paste2Run 正在运行");
      return;
    }
    if (info.target) set(LS.target, info.target);
    const serverAllowRun = !!info.allow_run;
    set(LS.run, serverAllowRun ? "1" : "0");

    toast("[P2R] APPLYING…");
    try {
      const res = await post("/apply", {
        text,
        dryRun: dryOn(),
        source: location.hostname,
      });
      if (res.ok) {
        set(LS.lastHash, h);
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
                toast("[P2R] 没识别出运行命令，已跳过", 4000);
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
        // 落地失败不锁死内容，用户修好环境/换个目录后还能重落
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
      // 节流：流式输出时回调非常密集，300ms 合并一次
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
          if (now === lastText && MARK.test(now)) applyNode(node, { silent: true });
        }, 1400);
      }, 300);
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

  GM_registerMenuCommand("切换门禁标记 P2R-READY（开/关）", () => {
    const v = gateOn() ? "0" : "1";
    set(LS.gate, v);
    toast(v === "1" ? "GATE: ON（需要 P2R-READY 才落地）" : "GATE: OFF（看到 ===FILE: 就落地）");
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
    const node = findTarget();
    if (!node) return toast("没找到带 ===FILE: 的回复");
    applyNode(node, { force: true });
  });

  // 新：不用去目录里找 index.html，直接让服务端按项目类型打开
  GM_registerMenuCommand("打开项目入口（浏览器）", async () => {
    try {
      const r = await post("/open", {}, 60000);
      if (r.ok) toast("已打开 " + (r.url || ""), 4000);
      else {
        const plan = r.plan && r.plan.text ? "\n" + r.plan.text : "";
        toast("打开失败：" + (r.error || "") + plan, 7000);
      }
    } catch (e) {
      toast(e.message);
    }
  });

  // 平台直接给 zip 下载链接时，拦下来交给本地服务下载并解压
  document.addEventListener("click", (e) => {
    if (!gateOn()) return;
    const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    const href = a.href || "";
    if (!/^https?:/i.test(href)) return;   // blob: / data: 拿不到，交给控制台手动拖拽
    const dl = a.getAttribute("download") || "";
    if (!/\.zip(\?|#|$)/i.test(href) && !/zip/i.test(dl)) return;
    e.preventDefault();
    e.stopPropagation();
    toast("[P2R] 抓到 zip 链接，下载并解压中…", 5000);
    post("/fetch-zip", { url: href }, 180000)
      .then((r) => {
        if (r.ok) {
          toast(`[P2R] 解压 ${r.files.length} 个文件 -> ${r.target}`, 7000);
          if (GM_notification) GM_notification({ text: `Paste2Run 从 zip 导入 ${r.files.length} 个文件`, timeout: 3000 });
        } else {
          toast("[P2R] 导入失败: " + (r.error || ""), 7000);
        }
      })
      .catch((err) => toast("[P2R] 导入失败: " + err.message, 7000));
  }, true);

  // 左下角黑白极简 ASCII 悬浮按钮
  function mountButton() {
    const b = document.createElement("button");
    b.textContent = "[ P2R: APPLY ]";
    b.title = "Paste2Run：把最后一条回复写成文件（Shift+点击 = 打开项目）";
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
    b.onclick = async (ev) => {
      if (ev.shiftKey) {
        try {
          const r = await post("/open", {}, 60000);
          if (r.ok) toast("已打开 " + (r.url || ""), 4000);
          else toast("打开失败：" + (r.error || ""), 5000);
        } catch (e) { toast(e.message); }
        return;
      }
      const node = findTarget();
      if (!node) return toast("没找到带 ===FILE: 的回复");
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
