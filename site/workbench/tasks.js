/* 黄雀工作台 · 全局任务追踪器 (tasks.js)
 * 把"正在处理的任务"持久化到 localStorage，供铃铛通知与任务恢复复用。
 * 只存 job_id 等轻量元数据，不存名单本体(PII 不落浏览器)。
 */
(function () {
  "use strict";

  var KEY = "hq_jobs";
  var ACTIVE = { queued: 1, running: 1, pending: 1 };
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

  function normalize(job) {
    if (!job || job.id == null) return null;
    var out = {};
    for (var k in job) out[k] = job[k];
    out.id = String(job.id);
    out.kind = job.kind || "leads";
    out.status = job.status || "pending";
    out.title = job.title || job.keyword || "获客任务";
    out.keyword = job.keyword || job.title || "";
    out.t0 = Number(job.t0 || 0) || now();
    out.href = job.href || hrefFor(out);
    if (job.unread == null) out.unread = ACTIVE[out.status] ? false : true;
    return out;
  }

  function sortJobs(arr) {
    return arr.slice().sort(function (x, y) {
      return (y.updatedAt || y.createdAt || 0) - (x.updatedAt || x.createdAt || 0);
    });
  }

  function list(filter) {
    var arr = sortJobs(read());
    if (!filter) return arr;
    return arr.filter(function (job) {
      if (filter.kind && job.kind !== filter.kind) return false;
      if (filter.status && job.status !== filter.status) return false;
      if (filter.activeOnly && !ACTIVE[job.status]) return false;
      if (filter.unreadOnly && !job.unread) return false;
      return true;
    });
  }

  function listRecent(filter) {
    var limit = filter && filter.limit ? Number(filter.limit) : 5;
    return list(filter).slice(0, limit > 0 ? limit : 5);
  }

  function get(id) {
    var a = read();
    for (var i = 0; i < a.length; i++) {
      if (String(a[i].id) === String(id)) return a[i];
    }
    return null;
  }

  function hrefFor(job) {
    if (!job || job.id == null) return "leads.html";
    return "leads.html#task=" + encodeURIComponent(String(job.id));
  }

  function upsert(job) {
    var normalized = normalize(job);
    if (!normalized) return null;
    var a = read(), found = false;
    for (var i = 0; i < a.length; i++) {
      if (String(a[i].id) === normalized.id) {
        var merged = {};
        for (var k in a[i]) merged[k] = a[i][k];
        for (var k2 in normalized) merged[k2] = normalized[k2];
        merged.updatedAt = now();
        merged.href = hrefFor(merged);
        a[i] = merged;
        normalized = merged;
        found = true;
        break;
      }
    }
    if (!found) {
      normalized.createdAt = normalized.createdAt || now();
      normalized.updatedAt = now();
      normalized.href = hrefFor(normalized);
      a.push(normalized);
    }
    write(a);
    emit();
    return normalized;
  }

  function remove(id) {
    var a = read().filter(function (j) { return String(j.id) !== String(id); });
    write(a);
    emit();
  }

  function activeCount(filter) {
    return list(filter).filter(function (j) { return ACTIVE[j.status]; }).length;
  }

  function latestActive(filter) {
    var a = list(filter).filter(function (j) { return ACTIVE[j.status]; });
    return a[0] || null;
  }

  function unreadCount(filter) {
    return list(filter).filter(function (j) { return !!j.unread; }).length;
  }

  function markRead(id) {
    if (id == null) return;
    upsert({ id: id, unread: false });
  }

  function markAllRead(filter) {
    var a = read();
    var changed = false;
    for (var i = 0; i < a.length; i++) {
      if (filter && filter.kind && a[i].kind !== filter.kind) continue;
      if (a[i].unread) {
        a[i].unread = false;
        a[i].updatedAt = now();
        changed = true;
      }
    }
    if (changed) {
      write(a);
      emit();
    }
  }

  function onChange(cb) { if (typeof cb === "function") listeners.push(cb); }

  function emit() {
    var payload = {
      total: list().length,
      active: activeCount(),
      unread: unreadCount()
    };
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](payload); } catch (e) {}
    }
  }

  try {
    window.addEventListener("storage", function (e) { if (e.key === KEY) emit(); });
  } catch (e) {}

  window.HQTasks = {
    KEY: KEY,
    list: list,
    listRecent: listRecent,
    get: get,
    upsert: upsert,
    remove: remove,
    hrefFor: hrefFor,
    activeCount: activeCount,
    latestActive: latestActive,
    unreadCount: unreadCount,
    markRead: markRead,
    markAllRead: markAllRead,
    onChange: onChange
  };
})();
