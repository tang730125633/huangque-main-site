/* 黄雀工作台 · 全局任务追踪器 (tasks.js)
 * 把"正在处理的任务"持久化到 localStorage，并在每页共享侧栏注入"任务进行中"徽标。
 * 切页 / 刷新都不丢任务；点徽标回到任务所属页面续看结果。
 * 纯 vanilla、无依赖、全程 try/catch 降级——坏了顶多退回原行为，绝不让页面崩。
 * 只存 job_id 等元数据，不存名单本体(PII 不落浏览器)。
 */
(function () {
  "use strict";

  if (window.HQTasks) {
    try { window.HQTasks.renderBadge(); } catch (e) {}
    try { window.dispatchEvent(new CustomEvent("hq:tasks-ready")); } catch (e2) {}
    return;
  }

  var KEY = "hq_jobs";
  var ACTIVE = { queued: 1, running: 1, pending: 1, processing: 1 };
  var MAX_HISTORY = 30; // 完成态最多留 30 条，进行中全留
  var mem = null;       // localStorage 不可用时的内存兜底
  var listeners = [];

  function now() { try { return Date.now(); } catch (e) { return 0; } }

  function read() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return mem || [];
      var arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return mem || []; }
  }

  function write(arr) {
    try {
      var active = arr.filter(function (j) { return ACTIVE[j.status]; });
      var done = arr.filter(function (j) { return !ACTIVE[j.status]; }).slice(-MAX_HISTORY);
      var out = active.concat(done);
      mem = out;
      try { localStorage.setItem(KEY, JSON.stringify(out)); } catch (e) {}
      return out;
    } catch (e) { mem = arr; return arr; }
  }

  function list() { return read(); }

  function storedKind(job) {
    return String(job && job.kind || "leads");
  }

  function sameTask(job, id, kind) {
    return String(job.id) === String(id) && (!kind || storedKind(job) === String(kind));
  }

  function get(id, kind) {
    var a = read();
    for (var i = 0; i < a.length; i++) {
      if (sameTask(a[i], id, kind)) return a[i];
    }
    return null;
  }

  function upsert(job) {
    if (!job || job.id == null) return;
    var a = read(), found = false, kind = storedKind(job);
    for (var i = 0; i < a.length; i++) {
      if (sameTask(a[i], job.id, kind)) {
        var merged = {};
        for (var k in a[i]) merged[k] = a[i][k];
        for (var k2 in job) merged[k2] = job[k2];
        merged.kind = kind;
        merged.updatedAt = now();
        a[i] = merged;
        found = true;
        break;
      }
    }
    if (!found) {
      job.kind = kind;
      job.createdAt = job.createdAt || now();
      job.updatedAt = now();
      a.push(job);
    }
    write(a);
    emit();
  }

  function remove(id, kind) {
    var a = read().filter(function (j) { return !sameTask(j, id, kind); });
    write(a);
    emit();
  }

  function matchesKind(job, kind) {
    return !kind || storedKind(job) === String(kind);
  }

  function activeCount(kind) {
    return read().filter(function (j) { return ACTIVE[j.status] && matchesKind(j, kind); }).length;
  }

  function latestActive(kind) {
    var a = read().filter(function (j) { return ACTIVE[j.status] && matchesKind(j, kind); });
    a.sort(function (x, y) {
      return (y.updatedAt || y.createdAt || 0) - (x.updatedAt || x.createdAt || 0);
    });
    return a[0] || null;
  }

  function taskKind(job) {
    return job && job.kind === "video" ? "video" : "leads";
  }

  function taskHref(job) {
    var id = encodeURIComponent(String(job && job.id != null ? job.id : ""));
    return taskKind(job) === "video" ? "video.html?task=" + id : "leads.html#task=" + id;
  }

  function onChange(cb) { if (typeof cb === "function") listeners.push(cb); }

  function emit() {
    var n = activeCount();
    renderBadge();
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](n); } catch (e) {}
    }
  }

  // ---- 徽标：优先注入新版共享侧栏；旧 rail 保留兼容 ----
  function renderBadge() {
    try {
      var nav = document.querySelector(".hq-aside nav");
      var rail = document.querySelector(".rail");
      var host = nav || rail;
      if (!host) return;
      var n = activeCount();
      var el = document.getElementById("hq-tasks-badge");
      if (!el) {
        el = document.createElement("a");
        el.id = "hq-tasks-badge";
        if (nav) {
          el.className = "hq-navitem";
          el.style.cssText = "position:relative;display:flex;align-items:center;gap:12px;padding:10px 13px;border-radius:11px;color:#e7b24c;background:rgba(231,178,76,.08);font-size:14px;font-weight:600;transition:.16s";
          var clock = window.HQ && window.HQ.icon
            ? window.HQ.icon("clock", "18px")
            : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
          el.innerHTML = '<span style="display:flex;width:18px">' + clock + '</span><span>任务</span>' +
            '<span id="hq-tasks-count" style="margin-left:auto;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:#e7b24c;color:#1c1402;font:800 10px/18px var(--hq-mono,monospace);text-align:center"></span>';
          nav.appendChild(el);
        } else {
          el.className = "navbtn";
          el.style.position = "relative";
          el.innerHTML =
            '<svg viewBox="0 0 24 24" style="width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>' +
            '<span class="lb">任务</span>' +
            '<span id="hq-tasks-count" style="position:absolute;top:5px;right:12px;min-width:16px;height:16px;padding:0 4px;border-radius:9px;background:#e7b24c;color:#0a0e16;font-size:10px;font-weight:800;line-height:16px;text-align:center;box-shadow:0 0 0 2px rgba(8,13,22,.92)"></span>';
          var spacer = rail.querySelector(".spacer");
          if (spacer) spacer.insertAdjacentElement("afterend", el);
          else rail.appendChild(el);
        }
        // 在任务所属页面点徽标不整页跳，直接通知页面恢复轮询。
        el.addEventListener("click", function (ev) {
          try {
            var active = latestActive();
            if (!active) return;
            var kind = taskKind(active);
            if (kind === "video" && /\/video(?:\.html)?$/.test(location.pathname)) {
              ev.preventDefault();
              var current = new URL(location.href);
              current.searchParams.set("task", String(active.id));
              history.replaceState(null, "", current.pathname + current.search + current.hash);
              window.dispatchEvent(new CustomEvent("hq:resume-task", { detail: { id: active.id, kind: kind } }));
            } else if (kind === "leads" && /\/leads(?:\.html)?$/.test(location.pathname)) {
              ev.preventDefault();
              location.hash = "task=" + active.id;
              window.dispatchEvent(new HashChangeEvent("hashchange"));
            }
          } catch (e) {}
        });
      }
      var la2 = latestActive();
      el.setAttribute("href", taskHref(la2));
      el.setAttribute("aria-label", "查看 " + n + " 个进行中任务");
      el.setAttribute("title", "查看进行中任务");
      var cnt = document.getElementById("hq-tasks-count");
      if (n > 0) {
        el.style.display = nav ? "flex" : "";
        el.style.color = "#e7b24c";
        if (cnt) cnt.textContent = n > 99 ? "99+" : String(n);
      } else {
        el.style.display = "none";
      }
    } catch (e) { /* 徽标坏了不影响主功能 */ }
  }

  // 跨标签页同步：其它标签页写 localStorage 时刷新本页徽标
  try {
    window.addEventListener("storage", function (e) { if (e.key === KEY) emit(); });
  } catch (e) {}

  window.HQTasks = {
    list: list, get: get, upsert: upsert, remove: remove,
    activeCount: activeCount, latestActive: latestActive,
    onChange: onChange, renderBadge: renderBadge
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderBadge);
  } else {
    renderBadge();
  }
  try { window.dispatchEvent(new CustomEvent("hq:tasks-ready")); } catch (e) {}
})();
