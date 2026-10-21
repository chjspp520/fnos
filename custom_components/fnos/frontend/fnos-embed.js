/*!
 * 飞牛 fnOS —— Home Assistant 前端悬浮窗模块
 *
 * 通过 homeassistant.components.frontend.add_extra_js_url 注入 HA 前端。
 * 拦截设备页「访问」链接（/fnos/web?entry=..&target=..，带 authSig）的点击：
 * 链接在 ha-button 的 Shadow DOM 里，用 composedPath() 穿透；阻止整页跳转，
 * 改为在 HA 页面内弹出可拖动、可缩放的悬浮窗，iframe 加载 HA 同源反代页面。
 * 每种 target（桌面/文件/相册/影视/设置/Docker/虚拟机）一个独立悬浮窗，
 * 可同时打开、互不替换。
 *
 * 同时负责「地址栏来源心跳」：把浏览器当前 origin 上报给 /fnos/api/origin，
 * 服务端据此重签设备链接与「远程地址」传感器（端口转发/反代场景自动跟随）。
 * 心跳必须带 HA 登录态（fetchWithAuth / Bearer），拿不到就不发 —— 否则未鉴权
 * 请求会被 HA 记为失败登录并封掉用户 IP。
 */
(() => {
  "use strict";

  const PAGE_PATH = "/fnos/web";
  const HOST_ID = "fnos-float-host";

  const TARGET_CONF = {
    host: { title: "飞牛 · 系统总览", width: "min(1180px, 94vw)", height: "min(840px, 90vh)" },
    desktop: { title: "飞牛桌面", width: "min(1280px, 96vw)", height: "min(880px, 92vh)" },
    file: { title: "飞牛 · 文件管理", width: "min(1280px, 96vw)", height: "min(880px, 92vh)" },
    photo: { title: "飞牛 · 相册", width: "min(1280px, 96vw)", height: "min(880px, 92vh)" },
    video: { title: "飞牛 · 影视", width: "min(1280px, 96vw)", height: "min(880px, 92vh)" },
    setting: { title: "飞牛 · 系统设置", width: "min(1180px, 94vw)", height: "min(840px, 90vh)" },
    docker: { title: "飞牛 · Docker 管理", width: "min(1280px, 96vw)", height: "min(880px, 92vh)" },
    vm: { title: "飞牛 · 虚拟机", width: "min(1180px, 94vw)", height: "min(840px, 90vh)" },
  };

  const CSS = `
    .fn-window {
      position: fixed;
      z-index: 1;
      display: none;
      flex-direction: column;
      min-width: 320px;
      min-height: 240px;
      background: #11151c;
      border: 1px solid #2f3c4e;
      border-radius: 12px;
      box-shadow: 0 14px 48px rgba(0, 0, 0, .55);
      overflow: hidden;
      resize: both;
      pointer-events: auto;
    }
    .fn-window.show { display: flex; }
    .fn-window.fn-flash { animation: fn-pop .35s ease; }
    @keyframes fn-pop {
      0% { transform: scale(.97); }
      60% { transform: scale(1.012); }
      100% { transform: scale(1); }
    }
    .fn-head {
      display: flex;
      align-items: center;
      gap: 8px;
      height: 42px;
      padding: 0 10px;
      background: #1b222d;
      border-bottom: 1px solid #2f3c4e;
      cursor: move;
      touch-action: none;
      color: #e7edf5;
      flex: 0 0 auto;
    }
    .fn-grip { color: #6b7787; font-size: 14px; letter-spacing: -2px; }
    .fn-title { font-size: 13.5px; font-weight: 600; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis; }
    .fn-spacer { flex: 1; }
    .fn-btn {
      appearance: none;
      border: 1px solid #2f3c4e;
      background: #202a38;
      color: #e7edf5;
      border-radius: 7px;
      width: 30px;
      height: 28px;
      font-size: 15px;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
      padding: 0;
    }
    .fn-btn:hover { background: #2a3646; border-color: #2f9bff; }
    .fn-x:hover { background: #5a2327; border-color: #f0564a; }
    .fn-body { flex: 1 1 auto; position: relative; background: #fff; }
    .fn-body iframe { position: absolute; inset: 0; width: 100%; height: 100%;
      border: 0; background: #fff; }
  `;

  // composedPath 穿透 ha-button 的 Shadow DOM 拿到真实 <a>/href
  function linkFromEvent(e) {
    let path = null;
    try { path = typeof e.composedPath === "function" ? e.composedPath() : null; }
    catch (_) { path = null; }
    if (!path || !path.length) {
      path = [];
      let n = e.target;
      while (n) {
        path.push(n);
        const root = n.getRootNode ? n.getRootNode() : null;
        n = n.parentNode || (root && root.host) || null;
      }
    }
    for (const node of path) {
      if (!node || node.nodeType !== 1) { continue; }
      let href = null;
      if (node.tagName === "A") { href = node.href; }
      if (!href && typeof node.getAttribute === "function") {
        href = node.getAttribute("href");
      }
      if (!href) { continue; }
      let u;
      try { u = new URL(href, location.href); } catch (_) { continue; }
      if (u.pathname === PAGE_PATH && u.searchParams.has("authSig")) {
        // 主机一律以浏览器当前地址栏为准（authSig 只签 path+query 不签 host）
        try { u = new URL(u.pathname + u.search, window.location.origin); }
        catch (_) { /* 解析失败就用原链接 */ }
        return { url: u, node };
      }
    }
    return null;
  }

  function rewriteLinkToLocation(match) {
    if (!match) { return; }
    try {
      if (match.node.getAttribute && match.node.getAttribute("href")) {
        match.node.setAttribute("href", match.url.href);
      }
    } catch (_) { /* 只读 DOM 改不动时静默 */ }
  }

  function ensureHost() {
    let host = document.getElementById(HOST_ID);
    if (host) { return host; }
    host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText =
      "position:fixed;inset:0;z-index:999999;pointer-events:none";
    const shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);
    document.body.appendChild(host);
    return host;
  }

  function centerWindow(win) {
    const w = win.offsetWidth || Math.min(1280, window.innerWidth * 0.96);
    const h = win.offsetHeight || Math.min(880, window.innerHeight * 0.92);
    win.style.left = Math.max(8, (window.innerWidth - w) / 2) + "px";
    win.style.top = Math.max(8, (window.innerHeight - h) / 2 - 10) + "px";
  }

  function enableDrag(win, head) {
    head.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".fn-btn")) { return; }
      e.preventDefault();
      const offX = e.clientX - win.offsetLeft;
      const offY = e.clientY - win.offsetTop;
      const move = (ev) => {
        const x = Math.max(-win.offsetWidth + 120,
          Math.min(window.innerWidth - 60, ev.clientX - offX));
        const y = Math.max(0,
          Math.min(window.innerHeight - 40, ev.clientY - offY));
        win.style.left = x + "px";
        win.style.top = y + "px";
      };
      const up = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    });
  }

  function buildWindow(root, kind) {
    // vm:<name> / docker:<name> → 每台一个独立窗口，标题带名称
    const base = kind.split(":")[0];
    const conf = TARGET_CONF[base] || TARGET_CONF[kind] || TARGET_CONF.desktop;
    let title = conf.title;
    if (base === "vm" && kind.indexOf("vm:") === 0) {
      title = `虚拟机控制台 · ${decodeURIComponent(kind.slice(3))}`;
    }
    if (base === "docker" && kind.indexOf("docker:") === 0) {
      title = `Docker · ${decodeURIComponent(kind.slice(7))}`;
    }
    const win = document.createElement("div");
    win.className = "fn-window";
    win.dataset.kind = kind;
    win.style.width = conf.width;
    win.style.height = conf.height;
    win.innerHTML = `
      <div class="fn-head">
        <span class="fn-grip">⠿</span>
        <span class="fn-title"></span>
        <span class="fn-spacer"></span>
        <button type="button" class="fn-btn" data-act="reload" title="重新加载">↻</button>
        <a class="fn-btn fn-link" target="_blank" rel="noopener" title="在新标签页打开">↗</a>
        <button type="button" class="fn-btn fn-x" data-act="close" title="关闭">✕</button>
      </div>
      <div class="fn-body"><iframe
          allow="fullscreen; clipboard-read; clipboard-write"
          allowfullscreen></iframe></div>`;
    win.querySelector(".fn-title").textContent = title;
    root.appendChild(win);

    const frame = win.querySelector("iframe");
    centerWindow(win);
    enableDrag(win, win.querySelector(".fn-head"));

    win.querySelector('[data-act="reload"]').addEventListener("click", () => {
      frame.src = frame.dataset.url;
    });
    win.querySelector('[data-act="close"]').addEventListener("click", () => {
      frame.src = "about:blank";
      frame.dataset.url = "";
      win.classList.remove("show");
    });
    return win;
  }

  function openFloat(kind, url) {
    const host = ensureHost();
    const root = host.shadowRoot;
    let win = root.querySelector(`.fn-window[data-kind="${kind}"]`);
    if (!win) {
      win = buildWindow(root, kind);
    }
    const frame = win.querySelector("iframe");
    if (frame.dataset.url !== url.href) {
      frame.dataset.url = url.href;
      frame.src = url.href;
      win.querySelector(".fn-link").href = url.href;
    }
    win.classList.add("show");
    win.classList.remove("fn-flash");
    void win.offsetWidth;
    win.classList.add("fn-flash");
    const kinds = Object.keys(TARGET_CONF);
    // vm:<name> / docker:<name> 不在 TARGET_CONF 里：置最上层
    const zi = (kind.indexOf("vm:") === 0 || kind.indexOf("docker:") === 0)
      ? 100 : Math.max(0, kinds.indexOf(kind));
    win.style.zIndex = String(10 + zi);
  }

  function targetFromUrl(u) {
    const target = u.searchParams.get("target") || "desktop";
    if (target === "vm" || target === "docker") {
      // 每台虚拟机 / 每个容器独立窗口（kind 含 name，互不顶替）
      const name = u.searchParams.get("name") || "";
      return name ? `${target}:${name}` : target;
    }
    return target;
  }

  function onClick(e) {
    const match = linkFromEvent(e);
    if (!match) { return; }
    rewriteLinkToLocation(match);
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) {
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    openFloat(targetFromUrl(match.url), match.url);
  }
  document.addEventListener("click", onClick, true);

  // ---- 地址栏来源心跳（必须带登录态，拿不到就不发）----
  const ORIGIN_PATH = "/fnos/api/origin";
  const ORIGIN_HEADERS = { "Cache-Control": "no-store" };
  let lastOriginReport = 0;
  let originRetries = 0;

  function authFetcher() {
    try {
      const root = document.querySelector("home-assistant");
      const hass = root && root.hass;
      if (!hass) { return null; }
      if (typeof hass.fetchWithAuth === "function") {
        return (url, init) => hass.fetchWithAuth(url, init);
      }
      const token = hass.auth && hass.auth.data && hass.auth.data.access_token;
      if (token) {
        return (url, init) => fetch(url, {
          ...init,
          headers: { ...(init && init.headers), Authorization: "Bearer " + token }
        });
      }
    } catch (_) { /* 取不到就当作未登录 */ }
    return null;
  }

  function reportOrigin() {
    const now = Date.now();
    if (now - lastOriginReport < 30000) { return; }
    const fetcher = authFetcher();
    if (!fetcher) {
      if (originRetries < 10) {
        originRetries += 1;
        setTimeout(reportOrigin, 1500);
      }
      return;
    }
    originRetries = 0;
    lastOriginReport = now;
    try {
      fetcher(ORIGIN_PATH, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: ORIGIN_HEADERS
      }).catch(() => {});
    } catch (_) { /* 页面正在卸载等场景，静默 */ }
  }
  reportOrigin();
  window.addEventListener("focus", reportOrigin);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { reportOrigin(); }
  });
  window.addEventListener("pageshow", reportOrigin);

  window.__fnosFloatEmbed = "1.0.0";
})();
