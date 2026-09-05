(function () {
  "use strict";

  var sessionId = null;
  var streaming = false;
  var sendController = null; // 在途请求的 AbortController（停止/超时用）
  var pollTimer = null;
  var statusBubble = null;      // 处理中状态气泡（DOM）
  var statusBubbleBody = null;
  var statusTimer = null;       // 2 秒一刷的实时进度轮询
  var statusStartedAt = 0;
  // 后台任务卡死兜底：同一个「处理中」气泡超过阈值还不结束就明确收工，绝不无限转圈。
  // 阈值可用 window.HQ_JOB_TIMEOUT_MS 覆盖（自动化测试用），默认 15 分钟。
  var JOB_TIMEOUT_MS = Number(window.HQ_JOB_TIMEOUT_MS || 15 * 60 * 1000);
  var jobTimedOut = false;      // 已对当前忙碌窗口报过超时：不再重挂气泡，直到服务端转闲
  var activeSeqs = {};          // 在途轮次 seq -> true（全部完成才撤状态气泡）
  var renderedSeqs = {};        // 已渲染的结果 seq 去重（防重复说话）
  var activeJobs = false;       // 是否有后台报告任务在跑（有则气泡常驻显示进度）
  var pickedChoices = {};       // kind -> {id,label,image_url,preview_url,widgetTitle} 已选素材
  var voiceTab = "mine";        // 音色合并面板当前标签：mine | system
  // 素材卡三纪律（前端侧）：
  // 1) 卡片只在服务器注册素材时才渲染，平时聊天零卡片（注册由主 Agent 纪律约束）；
  // 2) 每张卡都能 × 关掉——关掉=本次不出片、对话继续，dismissed 后绝不自动重弹
  //    （按 widget id + gen 记忆：子 Agent 重新查询注册会 gen+1，用户再次要求才重新出现）；
  // 3) 卡片是消息流里的一次性附件：下一条普通回复发出时旧卡自动收起（collapseOldCards），
  //    绝不钉在输入框上方当常驻栏。
  var dismissedWidgetIds = {};  // "widgetId@gen" -> true（sessionStorage 按 sid 持久化）
  var summaryCard = null;       // 出片配置卡（左脸/中声/右文案 + 绿色确认按钮）DOM
  var summaryConsumed = null;   // 已关闭/已提交的配置卡签名：同签名不再自动弹出
  var modCache = { m5: { path: null }, m6: { path: null } }; // 报告模块内容加载缓存
  // 出片引导四句（前端侧）：引导在上、选择在下、未选文案不出片。
  // - scriptWidgetsOffered：本会话出现过文案三版卡 → 出片必须选一版，确认生成不可点
  // - autoPickedDefaults：默认形象/音色（本人形象、本人声音）只自动勾选一次，用户撤掉后不再自动选回
  var scriptWidgetsOffered = false;
  var autoPickedDefaults = {};  // kind -> true

  var $ = function (id) { return document.getElementById(id); };
  var messages = $("messages");
  var input = $("input");
  var composer = $("composer");
  var sendBtn = $("send-btn");
  var stopBtn = $("stop-btn");
  var toolList = $("tool-list");
  var modeBadge = $("mode-badge");
  var intro = $("intro");
  var reportBox = $("report-box");
  var reportPane = $("report-pane"); // 左侧报告栏：无报告时整列隐藏，聊天占满全宽
  var reportStatus = $("report-status");
  var reportPhase = $("report-phase");
  var reportLinks = $("report-links");
  var m5Box = $("report-m5");
  var m6Box = $("report-m6");
  var m5Status = $("m5-status");
  var m6Status = $("m6-status");
  var m5Content = $("m5-content");
  var m6Content = $("m6-content");

  var STATE_LABEL = {
    completed: "已完成", running: "运行中", needs_user_input: "要补充信息",
    needs_approval: "等确认报价", failed: "失败", cancelled: "已取消",
  };
  var AGENT_LABEL = {
    image: "出图", video: "视频", audio: "音频", copy: "文案编导",
    "digital-human": "数字人", "short-drama": "短剧", compose: "成片",
    canvas: "画布", leads: "获客", collect: "采集", "ip-positioning": "IP 定位",
    system: "系统",
  };

  // ---- 滚动策略：只有用户停在底部（粘底）时才自动跟随滚动；
  // 用户一往上翻（离开底部超过阈值）就绝不打扰，Agent 思考时也不拖人。----
  var scrollSticky = true;

  function updateSticky() {
    var gap = document.body.scrollHeight - (window.scrollY + window.innerHeight);
    scrollSticky = gap < 120;
  }
  window.addEventListener("scroll", updateSticky, { passive: true });
  window.addEventListener("resize", updateSticky);

  function autoScroll() {
    if (!scrollSticky) return;
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  // 用户主动动作（发消息/点卡片）后强制回到粘底模式并滚到底
  function forceScrollBottom() {
    scrollSticky = true;
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  // 组装一条消息的 DOM（不插入、不滚动）：addMsg 与恢复历史的批量渲染共用。
  function makeMsg(role, text, images) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "assistant") {
      bubble.innerHTML = mdText(text); // 轻量 Markdown：加粗/换行/链接
    } else {
      bubble.textContent = text;
    }
    wrap.appendChild(bubble);
    attachImages(wrap, images);
    return wrap;
  }

  function addMsg(role, text, images, noScroll) {
    var wrap = makeMsg(role, text, images);
    var bubble = wrap.querySelector(".bubble");
    messages.appendChild(wrap);
    if (!noScroll) autoScroll();
    return bubble;
  }

  // ---- 图片贴条：采集等任务交付的本地图片，直接渲染成可见缩略图（点开看原图）----
  function attachImages(wrap, images) {
    if (!images || !images.length) return;
    var row = document.createElement("div");
    row.className = "msg-images";
    images.forEach(function (u) {
      var a = document.createElement("a");
      a.href = u;
      a.target = "_blank";
      a.rel = "noopener";
      var img = document.createElement("img");
      img.src = u;
      img.alt = "图片";
      img.loading = "lazy";
      img.onerror = function () { brokenImage(img, a); }; // 坏图不裂：换占位提示
      a.appendChild(img);
      row.appendChild(a);
    });
    wrap.appendChild(row);
  }

  // 坏图占位：图片加载失败（源文件被清理/网络问题）时显示占位块，不露裂图图标
  function brokenImage(img, host) {
    var ph = document.createElement("div");
    ph.className = "img-broken";
    ph.textContent = "🖼 图片暂时看不了";
    if (host && host.parentNode) host.replaceChild(ph, img);
  }

  // ---- 轻量 Markdown 渲染（加粗/行内代码/链接/换行），其余原样转义 ----
  function mdText(s) {
    var esc = String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
    esc = esc.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    esc = esc.replace(/`([^`]+)`/g, "<code>$1</code>");
    esc = esc.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // 裸图片链接直接渲染成图（不碰 href/src 里已有的 URL）
    esc = esc.replace(/(?<!["'=])https?:\/\/[^\s"'<>()]+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s"'<>()]*)?/gi,
      '<a href="$&" target="_blank" rel="noopener"><img src="$&" alt="图片" loading="lazy"></a>');
    // 裸音频链接 → 内联试听播放器（可直接点播放）
    esc = esc.replace(/(?<!["'=])https?:\/\/[^\s"'<>()]+\.(?:mp3|wav|m4a|aac|ogg)(?:\?[^\s"'<>()]*)?/gi,
      '<audio controls preload="none" src="$&"></audio>');
    // 裸视频链接 → 内联视频播放器（可直接点播放）
    esc = esc.replace(/(?<!["'=])https?:\/\/[^\s"'<>()]+\.(?:mp4|mov|webm)(?:\?[^\s"'<>()]*)?/gi,
      '<video controls preload="metadata" src="$&"></video>');
    // 其余裸链接 → 可点击
    esc = esc.replace(/(?<!["'=])https?:\/\/[^\s"'<>()]+/gi,
      '<a href="$&" target="_blank" rel="noopener">$&</a>');
    esc = esc.replace(/\n/g, "<br>");
    return esc;
  }

  function showTyping() {
    var wrap = document.createElement("div");
    wrap.className = "msg assistant";
    wrap.id = "typing";
    var b = document.createElement("div");
    b.className = "bubble";
    b.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    wrap.appendChild(b);
    messages.appendChild(wrap);
    forceScrollBottom();
  }
  function hideTyping() { var t = $("typing"); if (t) t.remove(); }

  function setBadge(mode) {
    modeBadge.classList.remove("live");
    if (mode === "openai") {
      modeBadge.classList.add("live");
      modeBadge.innerHTML = '<span class="dot"></span>主 Agent 在线';
    } else if (mode === "mock") {
      modeBadge.innerHTML = '<span class="dot"></span>演示模式（无 API Key）';
    } else {
      modeBadge.textContent = mode || "检测中…";
    }
  }

  // ---- 黄雀 CLI 授权状态徽标：过期/未登录一眼可见，不用等到任务失败才发现 ----
  var hqBadge = $("hq-badge");
  var authTimer = null;

  function fmtAuthExpiry(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return "（" + (d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + " 到期）";
  }

  function setHqBadge(h) {
    if (!hqBadge || !h || !h.hq_status) return;
    var st = h.hq_status;
    var el = hqBadge;
    el.classList.remove("auth-ok", "auth-bad");
    if (st.ok) {
      var detail = st.detail || {};
      var r = detail.result || {};
      el.classList.add("auth-ok");
      el.textContent = "🪶 黄雀已授权" + fmtAuthExpiry(r.expires_at);
    } else {
      el.classList.add("auth-bad");
      el.textContent = "🪶 黄雀授权已过期";
      el.title = "黄雀 CLI 授权已过期：需要重新登录后才能出图/视频/数字人等。";
    }
  }

  // ---- 路由轨迹 ----
  function renderRouting(routing) {
    if (!routing || !routing.length) return;
    var empty = toolList.querySelector(".tool-empty");
    if (empty) empty.remove();
    routing.forEach(function (r) {
      var item = document.createElement("div");
      item.className = "trace-item";
      var head = document.createElement("div");
      head.innerHTML = '<span class="agent-chip">' + (AGENT_LABEL[r.domain] || r.domain) +
        ' · hq-' + r.domain + '</span>' +
        '<span class="state-badge state-' + r.state + '">' + (STATE_LABEL[r.state] || r.state) + "</span>";
      var task = document.createElement("div");
      task.className = "trace-task";
      task.textContent = (r.summary || r.task || "");
      item.appendChild(head);
      item.appendChild(task);
      toolList.prepend(item);
    });
  }

  // ---- 报告卡片（IP 定位管线是主 Agent 内建能力，产物在 output/）----
  var STATUS_TEXT = {
    "draft_generating": "初稿生成中…",
    "draft_validated": "初稿校验通过，渲染 PDF…",
    "draft_ready": "初稿就绪",
    "final_generating": "定稿生成中…",
    "final_validated": "定稿校验通过，渲染 PDF…",
    "final": "定稿完成 ✓",
    "incomplete": "生成未通过校验",
    "failed": "生成失败",
    "no_info": "信息不足",
  };
  var MOD_STATUS_TEXT = {
    "m5_generating": "选题生成中…",
    "m5_validated": "选题校验通过，渲染 PDF…",
    "ready": "已就绪 ✓",
    "m6_generating": "文案生成中…",
    "m6_validated": "文案校验通过，渲染 PDF…",
    "incomplete": "未通过校验",
    "failed": "生成失败",
  };

  function renderReport(r) {
    if (!r || !r.status) {
      reportPane.hidden = true;
      return;
    }
    reportPane.hidden = false;
    reportStatus.textContent = STATUS_TEXT[r.status] || r.status || "处理中…";
    reportPhase.textContent = r.phase || "";
    if (r.gaps && r.gaps.length) {
      reportPhase.textContent += "　（缺口：" + r.gaps.length + " 处，让生成模型修订重跑）";
    }
    if (r.chosen_title) {
      reportPhase.textContent += "　已选方案 " + r.chosen + "《" + r.chosen_title + "》";
    }
    var files = r.files || {};
    // 定位报告（模块1-4）只保留一个下载入口：PDF。模块5/6 点开直接看内容，不给下载链接。
    var html = "";
    if (files.pdf) html += '<a class="report-link" href="' + escapeText(files.pdf) + '" target="_blank">⬇ 下载 PDF</a>';
    reportLinks.innerHTML = html;

    renderModContent("m5", m5Box, m5Status, m5Content, r.m5);
    renderModContent("m6", m6Box, m6Status, m6Content, r.m6);
  }

  // 模块5/6：标题就是「选题已出 / 口播已出」，状态只报进度；有内容就出「看内容」按钮，
  // 点开渲染 md 正文（不显示选题文本、不给下载链接——客户要点开就是内容）。
  function renderModContent(key, box, statusEl, contentEl, mod) {
    if (!mod || !mod.status) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    var st = MOD_STATUS_TEXT[mod.status] || mod.status || "";
    if (mod.status !== "ready" && mod.phase) st += "　" + mod.phase;
    statusEl.textContent = st;
    var md = (mod.files && mod.files.md) || null;
    if (md === modCache[key].path) return; // 内容链接没变：不重建按钮/正文
    modCache[key] = { path: md };
    contentEl.innerHTML = "";
    if (!md) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "report-view-btn";
    btn.textContent = "📖 看内容";
    btn.addEventListener("click", function () {
      var c = contentEl.querySelector(".report-md");
      if (c) {
        var willHide = !c.hidden;
        c.hidden = willHide;
        btn.textContent = willHide ? "📖 看内容" : "收起";
        return;
      }
      btn.disabled = true;
      btn.textContent = "加载中…";
      fetch(md)
        .then(function (resp) {
          if (!resp.ok) throw new Error(String(resp.status));
          return resp.text();
        })
        .then(function (t) {
          var div = document.createElement("div");
          div.className = "report-md";
          div.innerHTML = mdText(t);
          contentEl.appendChild(div);
          btn.textContent = "收起";
          btn.disabled = false;
        })
        .catch(function () {
          btn.textContent = "📖 看内容（加载失败，点重试）";
          btn.disabled = false;
        });
    });
    contentEl.appendChild(btn);
  }

  function pollReport() {
    if (!sessionId) return;
    fetch("api/report/" + encodeURIComponent(sessionId))
      .then(function (r) { return r.json(); })
      .then(renderReport)
      .catch(function () {});
  }

  // ---- 发送 ----
  var pendingAttachments = []; // {file_id, url}

  function setStreaming(v) {
    streaming = v;
    sendBtn.disabled = v;
    if (stopBtn) stopBtn.style.display = v ? "" : "none";
  }

  // 「停止等待」：中止在途请求，恢复输入（服务端这轮会自己超时收尾，不影响会话存档）
  if (stopBtn) {
    stopBtn.addEventListener("click", function () {
      if (sendController) sendController.abort();
    });
  }

  var turnPoll = null; // 后台结果轮询 {timer, tries}

  function cancelTurnPoll() {
    if (turnPoll && turnPoll.timer) clearInterval(turnPoll.timer);
    turnPoll = null;
  }

  // ---- 处理中状态气泡：一条气泡把「正在干什么」实时画出来，完成后消失，不重复说话 ----
  function ensureStatusBubble() {
    if (statusBubble || jobTimedOut) return;
    statusStartedAt = Date.now();
    var wrap = document.createElement("div");
    wrap.className = "msg assistant";
    wrap.id = "status-bubble";
    var b = document.createElement("div");
    b.className = "bubble status-bubble";
    b.innerHTML = '<div class="sb-head"><span class="sb-pulse"></span>' +
      '<span class="sb-title">主 Agent 处理中</span>' +
      '<span class="sb-elapsed"> · 0s</span></div>' +
      '<div class="sb-body"><div class="sb-line">正在理解你的需求…</div></div>';
    wrap.appendChild(b);
    messages.appendChild(wrap);
    statusBubble = wrap;
    statusBubbleBody = b.querySelector(".sb-body");
    autoScroll();
  }

  function removeStatusBubble() {
    if (statusBubble) { statusBubble.remove(); statusBubble = null; statusBubbleBody = null; }
    if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  }

  // 后台任务卡死兜底：同一个忙碌窗口（气泡开始计时起）超过阈值仍不结束 →
  // 撤气泡、停轮询、明确告诉用户可以刷新/重发。服务端转闲后窗口重计。
  function handleJobTimeout() {
    if (jobTimedOut || !statusStartedAt) return;
    if (Date.now() - statusStartedAt <= JOB_TIMEOUT_MS) return;
    jobTimedOut = true;
    cancelTurnPoll();
    removeStatusBubble();
    addMsg("assistant", "这个后台任务超过 15 分钟还没结束，我先不继续盯进度了。你可以刷新页面看最新状态，或直接再发一条消息。");
  }

  function fmtElapsed(s) {
    s = Math.max(0, Math.round(s));
    return s < 60 ? s + "s" : Math.floor(s / 60) + "m" + (s % 60) + "s";
  }

  // 把服务端实时进度画进状态气泡：耗时、正在调用的工具、各域六态、报告阶段
  function renderStatus(st) {
    if (!statusBubble) return;
    var elapsed = (Date.now() - statusStartedAt) / 1000;
    var el = statusBubble.querySelector(".sb-elapsed");
    if (el) el.textContent = " · " + fmtElapsed(elapsed);
    var html = "";
    var working = (st.turns || []).filter(function (t) { return t.state === "working"; });
    if (working.length > 1) {
      html += '<div class="sb-line">共 ' + working.length + ' 个请求同时处理中</div>';
    }
    if (st.tool && st.tool.name) {
      html += '<div class="sb-line sb-tool">⚙️ ' +
        escapeText(AGENT_LABEL[st.tool.domain] || st.tool.domain) +
        ' 正在调用 <code>' + escapeText(st.tool.name) + '</code></div>';
    }
    var dels = st.delegations || {};
    var chips = "";
    Object.keys(dels).forEach(function (domain) {
      var d = dels[domain];
      if (!d || !d.state) return;
      chips += '<span class="sb-chip state-' + escapeText(d.state) + '">' +
        escapeText(AGENT_LABEL[domain] || domain) + ' · ' +
        escapeText(STATE_LABEL[d.state] || d.state) + '</span>';
    });
    if (chips) html += '<div class="sb-chips">' + chips + '</div>';
    var jobs = st.jobs || [];
    if (jobs.length) {
      var jobLabel = { generate_report: "报告初稿", finalize_report: "报告定稿",
        report_revise: "报告修订", m5_topics: "模块5选题", m6_scripts: "模块6文案",
        script_revise: "文案修订" };
      html += '<div class="sb-line sb-tool">📄 后台生成中：' +
        escapeText(jobLabel[jobs[0]] || jobs[0]) + '（不耽误聊天，随时可以说话）</div>';
    }
    var rp = st.report || {};
    if (rp.status) {
      html += '<div class="sb-line">📄 报告：' +
        escapeText(STATUS_TEXT[rp.status] || rp.status || "处理中") +
        (rp.phase ? " · " + escapeText(rp.phase) : "") + '</div>';
    }
    statusBubbleBody.innerHTML = html || '<div class="sb-line">正在理解你的需求…</div>';
    // 状态帧兜底：即使 turn 事件丢失，待办卡片也一定会出现（出片提问卡按后端 film 标志门控）
    renderActionCards(st.delegations, st.film === true);
    // 有待办卡片且它不在视口内 → 锚定到卡片（不让用户错过）；否则跟随状态气泡滚到底部
    var newestAction = null;
    Object.keys(actionCards).forEach(function (k) {
      var r = actionCards[k];
      if (r && r.el && r.el.parentNode) newestAction = r.el;
    });
    if (newestAction) {
      // 用户粘底才锚定待办卡片；往上翻看历史时不打扰
      var ar = newestAction.getBoundingClientRect();
      if (scrollSticky && (ar.top < 0 || ar.bottom > window.innerHeight)) {
        newestAction.scrollIntoView({ block: "start" });
      }
    } else {
      autoScroll();
    }
  }

  function startStatusPoll() {
    if (statusTimer) return;
    statusTimer = setInterval(function () {
      if (!sessionId) return;
      fetch("api/v4/status/" + encodeURIComponent(sessionId))
        .then(function (r) { return r.json(); })
        .then(function (st) {
          activeJobs = (st.jobs || []).length > 0;
          var working = (st.turns || []).filter(function (t) { return t.state === "working"; });
          var serverBusy = activeJobs || working.length > 0;
          if (serverBusy) {
            // 后台报告任务/在途轮次还在跑：气泡常驻显示进度，结果轮询保持在线
            handleJobTimeout();
            if (!jobTimedOut) {
              ensureStatusBubble();
              if (!turnPoll) startTurnPoll(null);
            }
          } else if (Object.keys(activeSeqs).length === 0 &&
                     !(turnPoll && turnPoll.drainOnce)) {
            // 服务器上没有在途轮次、没有后台任务，我们也没在等任何注册过的轮次：收工
            // 例外：恢复排空轮询（drainOnce）还没跑完第一拍——它要兜「刷新窗口内刚完成的轮次」，
            // 不能在这里提前取消；它自己会在拿到 idle 后收工。
            jobTimedOut = false; // 转闲：卡死窗口重计
            cancelTurnPoll();
            removeStatusBubble();
          }
          renderStatus(st);
          renderReport(st.report);
          renderDeliveries(st.deliveries);
        })
        .catch(function () { /* 网络抖动：下轮重试 */ });
    }, 2000);
  }

  // 贴图断线补偿：SSE 掉线窗口内完成的采集交付，随 status 帧补渲染（按回复文本去重）。
  // 只在「缺了」时补：已渲染过（含恢复历史）的交付绝不重复贴。
  function renderDeliveries(list) {
    if (!list || !list.length) return;
    list.slice().reverse().forEach(function (d) {
      if (!d.reply || alreadyVisibleReply(d.reply)) return;
      var el = addMsg("assistant", d.reply, d.images);
      if (el) el.dataset.reply = d.reply;
    });
  }

  // ---- 结果交付：回复渲染 + 执行明细/路由/产物具象挂进气泡，同一句话绝不重复 ----
  // 双通道乱序防护：SSE 按完成序推送、poll 按 seq 升序交付，连发两条时后发先回会颠倒。
  // 有更小的在途 seq 时先把结果暂存，等小的到了再按序放出；8 秒超时兜底全放（不无限憋着）。
  var heldResults = {};   // seq -> 结果（乱序暂存）
  var heldTimer = null;

  function flushHeld(force) {
    if (heldTimer) { clearTimeout(heldTimer); heldTimer = null; }
    var seqs = Object.keys(heldResults).map(Number).sort(function (a, b) { return a - b; });
    for (var i = 0; i < seqs.length; i++) {
      var s = seqs[i];
      var waiting = Object.keys(activeSeqs).map(Number).some(function (k) { return k < s; });
      if (waiting && !force) continue;
      deliverResult(heldResults[s]);
      delete heldResults[s];
    }
    if (Object.keys(heldResults).length && !heldTimer) {
      heldTimer = setTimeout(function () { flushHeld(true); }, 8000);
    }
  }

  function deliverInOrder(d) {
    if (d.seq == null) { deliverResult(d); return; }
    if (renderedSeqs[d.seq]) return; // 已渲染过（双通道重复）直接丢弃
    var waiting = Object.keys(activeSeqs).map(Number).some(function (k) { return k < d.seq; });
    if (waiting) {
      heldResults[d.seq] = d;
      if (!heldTimer) heldTimer = setTimeout(function () { flushHeld(true); }, 8000);
      return;
    }
    deliverResult(d);
    flushHeld();
  }

  // 记账并修剪：renderedSeqs 只保留最近 200 个 seq，长会话不膨胀（去重只需要近期窗口）
  function markRendered(seq) {
    renderedSeqs[seq] = true;
    var keys = Object.keys(renderedSeqs);
    if (keys.length <= 200) return;
    keys.sort(function (a, b) { return Number(a) - Number(b); });
    var keep = {};
    keys.slice(keys.length - 200).forEach(function (k) { keep[k] = true; });
    renderedSeqs = keep;
  }

  function deliverResult(d) {
    if (d.seq != null && renderedSeqs[d.seq]) return; // 同一条结果只渲染一次
    if (d.seq != null) markRendered(d.seq);
    if (d.seq != null) delete activeSeqs[d.seq];
    var reply = (d.reply || "").trim();
    var el = null;
    if (reply) {
      // 每一轮都要可见：内容与上一条一字不差（用户重复提问，答案相同）
      // 也照常渲染新气泡——绝不能把回复静默吞掉，让用户以为 Agent 没回答。
      el = addMsg("assistant", reply);
      el.dataset.reply = reply;
    } else if (d.tool_log && d.tool_log.length) {
      el = addMsg("assistant", "已完成，明细如下：");
    } else {
      el = addMsg("assistant", d.state === "error" ? "处理出错了，请再试一次。" : "这轮处理完成。");
    }
    if (el) attachExtras(el, d);
    renderRouting(d.routing);
    // ---- 意图门控：出片区只跟「本轮是否出片」走，不跟会话历史走（权威信号=后端 film 标志）----
    // 出片轮（后端本轮派发了 digital-human）→ 卸掉上一轮的旧卡，渲染本轮卡（暂存的选择先恢复）；
    // 非出片轮 → 卸载全部出片组件（不是收起），出片流程的待办卡也不挂。
    // 注意：绝不看 d.widgets 判断——widgets 是会话级全量聚合，出过片后每轮都非空，
    // 用它会误判每轮都是出片轮，闲聊也会把货架挂回来。唯一信号是 film 标志。
    // 出错轮例外：报告栏与出片货架都是「和这轮错误无关的正常界面」，绝不动——
    // （老毛病：错误结果缺 report/film 字段，页面顺手把报告栏藏了、把用户选好的出片配置清了）。
    if (d.state === "error") {
      // 出错轮：只收尾状态气泡，报告栏与出片货架保持原样
    } else {
      renderReport(d.report);
      if (d.film === true) {
        restoreFilmStash();
        unloadFilmWidgetsDOM();
        collapseOldCards();            // 非出片的待办/报价卡照旧收起（纪律3）
        renderActionCards(d.delegations, true); // 待回复/待确认：摆在眼前（先看问题）
        // 出片引导四句：卡片是附件不是主角。锚定目标=本轮引导气泡（回复），
        // 保证「引导在上、选择在下」整体进入视口，而不是卡片把话顶出屏幕。
        renderWidgets(d.widgets, { anchor: el });
      } else {
        filmStash = null;
        unloadFilmKit();
        collapseOldCards();
        renderActionCards(d.delegations, false);
      }
    }
    setBadge(d.mode);
    // 在途轮次全部完成且没有后台任务时，才撤状态气泡并停结果轮询
    if (Object.keys(activeSeqs).length === 0 && !activeJobs) {
      cancelTurnPoll();
      removeStatusBubble();
    }
  }

  // 把 agent 干的实事具象画进气泡：路由轨迹、工具调用与参数、报告产物链接
  function attachExtras(el, d) {
    if (!el) return;
    var html = "";
    if (d.routing && d.routing.length) {
      html += '<div class="x-chips">';
      d.routing.forEach(function (r) {
        html += '<span class="x-chip state-' + escapeText(r.state || "") + '">' +
          escapeText(AGENT_LABEL[r.domain] || r.domain) + ' · ' +
          escapeText(STATE_LABEL[r.state] || r.state || "") + '</span>';
      });
      html += "</div>";
    }
    if (d.tool_log && d.tool_log.length) {
      html += '<details class="tool-detail"><summary>🔧 执行明细 · ' + d.tool_log.length + ' 步</summary><div class="tool-steps">';
      d.tool_log.forEach(function (t) {
        var args = "";
        try { args = JSON.stringify(t.args || {}); } catch (e) { args = String(t.args || ""); }
        if (args.length > 160) args = args.slice(0, 160) + "…";
        html += '<div class="tool-step"><span class="ts-ok' + (t.ok === false ? " ts-no" : "") + '">' +
          (t.ok === false ? "✕" : "✓") + '</span><span class="ts-name">' + escapeText(t.name || "tool") +
          '</span><code class="ts-args">' + escapeText(args) + '</code></div>';
      });
      html += "</div></details>";
    }
    var files = d.report && d.report.files;
    if (files && files.pdf) {
      html += '<div class="x-dlv">📄 报告产物：';
      html += '<a class="x-link" href="' + escapeText(files.pdf) + '" target="_blank" rel="noopener">⬇ PDF</a>';
      html += "</div>";
    }
    if (!html) return;
    var extras = document.createElement("div");
    extras.className = "msg-extras";
    extras.innerHTML = html;
    el.appendChild(extras);
    autoScroll();
  }

  // ---- SSE 实时推送：首次断线后固定降级 HTTPS 轮询，不反复重建长连接 ----
  var eventSource = null;
  var streamDisabled = false;

  function startStream() {
    if (streamDisabled) { startStatusPoll(); return; }
    if (eventSource || !sessionId) return;
    if (!window.EventSource) { startStatusPoll(); return; } // 老环境降级轮询
    try {
      eventSource = new EventSource("api/v4/stream/" + encodeURIComponent(sessionId));
    } catch (e) { eventSource = null; startStatusPoll(); return; }
    eventSource.onopen = function () {
      // SSE 已通。轮询不取消：作为兜底双通道，SSE 假死时轮询仍能把结果送到；
      // renderedSeqs 按 seq 去重，双通道重复交付只渲染一次。
    };
    eventSource.addEventListener("turn", function (e) {
      var d;
      try { d = JSON.parse(e.data); } catch (err) { return; }
      if (d.seq != null) { delete activeSeqs[d.seq]; }
      deliverInOrder(d);
    });
    // 采集等后台任务完成交付：带图消息直接贴进对话（不动出片货架）
    eventSource.addEventListener("delivery", function (e) {
      var d;
      try { d = JSON.parse(e.data); } catch (err) { return; }
      addMsg("assistant", d.reply || "后台任务有新交付。", d.images);
    });
    eventSource.addEventListener("status", function (e) {
      var st;
      try { st = JSON.parse(e.data); } catch (err) { return; }
      activeJobs = (st.jobs || []).length > 0;
      var working = (st.turns || []).filter(function (t) { return t.state === "working"; });
      var serverBusy = activeJobs || working.length > 0;
      if (serverBusy) {
        handleJobTimeout();
        if (!jobTimedOut) ensureStatusBubble();
      } else if (Object.keys(activeSeqs).length === 0) {
        jobTimedOut = false; // 转闲：卡死窗口重计
        cancelTurnPoll();
        removeStatusBubble();
      }
      renderStatus(st);
      renderReport(st.report);
      renderDeliveries(st.deliveries);
    });
    eventSource.onerror = function () {
      // 断线（移动网络切换/代理关闭长连接）：本会话固定降级到 HTTPS 轮询，
      // 不再反复建立 SSE；结果仍由 status + poll 双接口兜底交付。
      try { eventSource.close(); } catch (e2) {}
      eventSource = null;
      streamDisabled = true;
      startStatusPoll();
      if (Object.keys(activeSeqs).length > 0) startTurnPoll(null);
    };
  }

  // 轮询循环只有一条：服务端按序交付每轮结果；只要还有在途轮次/后台任务就继续等。
  // SSE 在线时轮询也保持（兜底双通道）：SSE 假死/断连都不会让结果无人送达。
  // drainOnce（恢复排空模式）：恢复历史后一次性把「刷新窗口内刚完成、历史里没有」的
  // 轮次兜回来；已在历史里渲染过的回放轮次按回复内容去重跳过，绝不重复贴气泡。
  function startTurnPoll(seq, drainOnce) {
    if (seq != null) activeSeqs[seq] = true;
    if (turnPoll) {
      turnPoll.tries = 0;
      if (drainOnce) turnPoll.drainOnce = true;
      return;
    }
    turnPoll = { timer: null, tries: 0, drainOnce: !!drainOnce };
    turnPoll.timer = setInterval(function () {
      if (!turnPoll) return;
      turnPoll.tries += 1;
      if (turnPoll.tries > 360) { // 15 分钟兜底
        cancelTurnPoll();
        if (Object.keys(activeSeqs).length > 0) {
          removeStatusBubble();
          addMsg("assistant", "这轮处理超过了 15 分钟，我先不继续等了。刷新页面可以看到结果，或直接再发一条。");
        }
        return;
      }
      fetch("api/v4/poll/" + encodeURIComponent(sessionId))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!turnPoll) return;
          if (d.state === "done" || d.state === "error") {
            turnPoll.tries = 0;
            // 回放轮次（不是我们注册等待的 seq）：若回复文本已随恢复历史渲染过，只记账不重画
            var backlog = d.seq != null && !activeSeqs[d.seq];
            if (backlog && d.reply && alreadyVisibleReply(d.reply)) {
              if (d.seq != null) markRendered(d.seq);
              return;
            }
            deliverInOrder(d);
          } else if (d.state === "idle") {
            // 排空模式且没有在途轮次：积压已清空，SSE 接管后续，收工
            if (turnPoll.drainOnce && Object.keys(activeSeqs).length === 0) {
              cancelTurnPoll();
              return;
            }
            if (Object.keys(activeSeqs).length > 0) {
              // 我们明确注册过的轮次在服务端查不到（服务重启丢结果）：明确提示而不是干等
              activeSeqs = {};
              cancelTurnPoll();
              removeStatusBubble();
              addMsg("assistant", "后台处理结果丢了（服务重启过）。请重新发送刚才那条消息。");
            }
            // 无注册轮次的 idle：后台任务完成前事件轮次尚未注册，继续等
          }
        })
        .catch(function () { /* 网络抖动：下轮重试 */ });
    }, 2500);
  }

  // 回复文本是否已在对话里出现过（含恢复的历史）：按原始文本精确比对。
  // deliverResult 与 tryRestore 渲染的气泡都带 data-reply（原始 markdown 文本），
  // 状态气泡（status-bubble）不参与比较。
  function alreadyVisibleReply(text) {
    if (!text) return false;
    var nodes = messages.querySelectorAll(".bubble[data-reply]");
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].getAttribute("data-reply") === text) return true;
    }
    return false;
  }

  function send(text, approval, onSendError) {
    if (streaming) return;
    text = (text || input.value).trim();
    // 只贴了附件没打字也可以发送（附件-only 消息）
    if (!text && !pendingAttachments.length) return;
    var attachments = pendingAttachments.map(function (a) { return a.file_id; });
    var attNames = pendingAttachments.map(function (a) {
      return (a.kind === "audio" ? "录音：" : a.kind === "image" ? "图片：" : "文件：") + (a.name || "");
    });
    var messageText = text;
    if (!messageText && attNames.length) messageText = "（附件：" + attNames.join("、") + "）";
    else if (attNames.length) messageText += "\n（附件：" + attNames.join("、") + "）";
    forceScrollBottom(); // 用户主动发送：回到底部看自己的消息和回复
    addMsg("user", messageText);
    input.value = "";
    intro.style.display = "none";
    // 意图门控：出片区是「上一条出片消息」的附件——聊下一句普通话，附件立刻下线（不等回复）。
    // 发送瞬间先卸货架并暂存已选；【已选汇总】/【点选】是出片流程自身按钮发出的续消息，不卸。
    if (!/^【(已选汇总|点选)】/.test(messageText)) { stashFilmKit(); unloadFilmKit(); }

    pendingAttachments = [];
    renderUploads();

    setStreaming(true);
    showTyping();
    sendController = new AbortController();
    // 发送本身应该秒回（服务端立即返回确认）；万一网络卡住，60 秒兜底中止
    var timer = setTimeout(function () {
      if (sendController) sendController.abort();
    }, 60000);

    fetch("api/v4/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: messageText, attachments: attachments, approval: approval }),
      signal: sendController.signal,
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        hideTyping();
        if (data.error) {
          removeStatusBubble(); addMsg("assistant", "出错了：" + data.error);
          if (onSendError) onSendError();
          return;
        }
        if (data.async && data.seq != null) {
          // 即时反馈：状态气泡立刻出现并实时更新，输入框马上可继续发
          jobTimedOut = false; // 新一轮开始：卡死窗口重计
          ensureStatusBubble();
          startTurnPoll(data.seq);
          startStream();
          return;
        }
        // 兼容旧同步响应
        deliverResult(data);
      })
      .catch(function (err) {
        hideTyping();
        if (onSendError) onSendError();
        if (err && err.name === "AbortError") {
          addMsg("assistant", "发送超时了，请检查网络后重发。");
        } else {
          addMsg("assistant", "网络请求失败，请刷新重试。");
        }
      })
      .finally(function () {
        clearTimeout(timer);
        sendController = null;
        setStreaming(false);
      });
    return true;
  }

  // ---- 附件上传：＋号选文件 / 输入框粘贴 → 预览暂存（不直接发送）→ 随文字一起发 ----
  var uploadRow = $("upload-row");
  var fileInput = $("file-input");

  $("upload-btn").addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    Array.prototype.forEach.call(fileInput.files, function (f) { uploadFile(f); });
    fileInput.value = "";
  });

  function fileKind(name) {
    var m = String(name || "").toLowerCase().match(/\.(png|jpe?g|webp|gif)$/);
    if (m) return "image";
    m = String(name || "").toLowerCase().match(/\.(mp3|wav|m4a|aac|ogg)$/);
    if (m) return "audio";
    return "file";
  }

  function uploadFile(f) {
    if (!f) return;
    // 客户端预检：类型/大小不对当场提示，不用白传一遍等后端拒绝
    var ALLOWED_EXT = /\.(png|jpg|jpeg|webp|gif|mp3|wav|m4a|aac|ogg)$/i;
    var MAX_UPLOAD_MB = 10;
    if (!ALLOWED_EXT.test(f.name)) {
      addMsg("assistant", "附件「" + f.name + "」格式不支持（支持图片和音频）。");
      return;
    }
    if (f.size > MAX_UPLOAD_MB * 1024 * 1024) {
      addMsg("assistant", "附件「" + f.name + "」超过 " + MAX_UPLOAD_MB + " MB，请压缩后再上传。");
      return;
    }
    var fd = new FormData();
    fd.append("session_id", sessionId);
    fd.append("file", f);
    fetch("api/v4/upload", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) { addMsg("assistant", "附件上传失败：" + (data.error || "未知错误")); return; }
        pendingAttachments.push({ file_id: data.file_id, url: data.url, name: data.name || f.name, kind: data.kind || fileKind(f.name) });
        renderUploads();
      })
      .catch(function () { addMsg("assistant", "附件上传失败，请重试。"); });
  }

  function renderUploads() {
    uploadRow.innerHTML = "";
    uploadRow.hidden = pendingAttachments.length === 0;
    pendingAttachments.forEach(function (a, i) {
      var chip = document.createElement("div");
      chip.className = "upload-chip";
      var rm = document.createElement("button");
      rm.className = "rm";
      rm.textContent = "✕";
      rm.title = "移除这个附件";
      rm.addEventListener("click", function () {
        pendingAttachments.splice(i, 1);
        renderUploads();
      });
      chip.appendChild(rm);
      if (a.kind === "image") {
        var img = document.createElement("img");
        img.src = a.url;
        img.alt = a.name || "已上传图片";
        chip.appendChild(img);
      } else if (a.kind === "audio") {
        // 音频附件：图标 + 文件名 + 内置试听，点击图片位置直接播放
        var icon = document.createElement("span");
        icon.className = "upload-chip-icon";
        icon.textContent = "🎵";
        chip.appendChild(icon);
        var nm = document.createElement("span");
        nm.className = "upload-chip-name";
        nm.textContent = (a.name || "录音").slice(0, 14) + ((a.name || "").length > 14 ? "…" : "");
        nm.title = a.name || "";
        chip.appendChild(nm);
        var play = document.createElement("button");
        play.className = "chip-play";
        play.textContent = "▶";
        play.title = "试听这段录音";
        var audition = null;
        play.addEventListener("click", function (e) {
          e.stopPropagation();
          if (audition) { audition.pause(); audition = null; play.textContent = "▶"; return; }
          audition = new Audio(a.url);
          play.textContent = "⏸";
          audition.play().catch(function () { play.textContent = "▶"; });
          audition.addEventListener("ended", function () { play.textContent = "▶"; audition = null; });
          audition.addEventListener("error", function () { play.textContent = "▶"; audition = null; });
        });
        chip.appendChild(play);
      } else {
        var fic = document.createElement("span");
        fic.className = "upload-chip-icon";
        fic.textContent = "📎";
        chip.appendChild(fic);
        var fnm = document.createElement("span");
        fnm.className = "upload-chip-name";
        fnm.textContent = (a.name || "文件").slice(0, 14) + ((a.name || "").length > 14 ? "…" : "");
        fnm.title = a.name || "";
        chip.appendChild(fnm);
      }
      uploadRow.appendChild(chip);
    });
  }

  // 输入框粘贴：剪贴板里的图片/文件直接作为附件暂存，文字照常留在输入框
  input.addEventListener("paste", function (e) {
    var files = e.clipboardData && e.clipboardData.files;
    if (!files || !files.length) return; // 纯文字粘贴：不干预
    e.preventDefault();
    Array.prototype.forEach.call(files, function (f) { uploadFile(f); });
  });

  composer.addEventListener("submit", function (e) { e.preventDefault(); send(); });

  // ---- 交互卡片：形象缩略图 / 音色试听 / 文案多版，点选即发送，无需打字 ----
  // 卡片是消息流里的一次性附件（素材卡三纪律见文件头部注释）。
  var renderedWidgetIds = {};
  var renderedWidgets = {}; // widget id -> {fp, el}：内容变化时覆盖重渲染

  function widgetsFingerprint(w) {
    return [w.type, w.id, w.title || "", w.gen || 1].concat(
      (w.items || []).map(function (i) { return (i.id || "") + "=" + (i.name || i.title || ""); })
    ).join("::");
  }

  // ---- 关卡片（纪律2）：关掉=本次不出片、对话继续；按 id@gen 记住，绝不自动重弹。
  // gen 由后端每次重新注册 +1：用户再次明确要求（子 Agent 重新查询）才会重新出现。----
  function widgetDismissKey(w) {
    return (w.id || "w") + "@" + (w.gen || 1);
  }
  function isWidgetDismissed(w) {
    return !!dismissedWidgetIds[widgetDismissKey(w)];
  }
  function saveDismissed() {
    try {
      if (!sessionId) return;
      sessionStorage.setItem("hq-v4-dismissed:" + sessionId, JSON.stringify(dismissedWidgetIds));
    } catch (e) { /* 隐私模式等不可用就只留在内存 */ }
  }
  function loadDismissed() {
    try {
      if (!sessionId) return;
      var v = sessionStorage.getItem("hq-v4-dismissed:" + sessionId);
      dismissedWidgetIds = v ? JSON.parse(v) : {};
    } catch (e) { dismissedWidgetIds = {}; }
  }
  function cleanupOldDismissed(keepSid) {
    // 只保留当前会话的已关闭记录：换会话/重置后旧 sid 的键不再用，清了防 sessionStorage 堆积
    try {
      for (var i = sessionStorage.length - 1; i >= 0; i--) {
        var k = sessionStorage.key(i);
        if (k && k.indexOf("hq-v4-dismissed:") === 0 && k !== "hq-v4-dismissed:" + keepSid) {
          sessionStorage.removeItem(k);
        }
      }
    } catch (e) { /* 隐私模式等不可用就算了 */ }
  }
  function dismissCard(key, kind, box, w) {
    dismissedWidgetIds[key] = true;
    saveDismissed();
    if (box && box.parentNode) box.remove();
    if (w && w.id) delete renderedWidgets[w.id];
    if (kind) {
      delete pickedChoices[kind];   // 关掉=本次不出片：已选的同来源项一并撤销
      clearPickedMarks(kind);
    }
    updateSummaryCard();
  }

  // ---- 意图门控：出片区（音色/形象/文案卡 + 出片配置卡）只是「出片那条消息」的附件，
  // 不是会话常驻货架。权威信号 = 后端每轮的 film 标志（本轮是否派发 digital-human）：
  // film=false 的轮次 → 卸载全部出片组件（不是收起，是卸掉）；
  // 只有出片轮（film=true）才重新挂上。前端不再拿关键词猜用户意图。----
  function isFilmWidget(w) {
    return !!w && (w.type === "avatar_pick" || w.type === "voice_pick" ||
      w.type === "script_pick" || w.type === "option_pick");
  }
  // 卸掉出片卡 DOM 与渲染记录（出片轮换新卡用）：保留用户已点选的选择。
  // 判定靠渲染时打的 film 标记（id 不靠谱：script_pick 的 id 是自定义的，如 script_10s）。
  function unloadFilmWidgetsDOM() {
    Object.keys(renderedWidgets).forEach(function (k) {
      var rec = renderedWidgets[k];
      var film = (rec && rec.film) || /^(avatar_pick:|voice_pick:|script_pick:|option_pick:)/.test(k) || k === "voice_panel";
      if (!film) return;
      if (rec && rec.el && rec.el.parentNode) rec.el.remove();
      delete renderedWidgets[k];
    });
  }
  // 卸载全部出片组件（当前轮不是出片轮）：不是收起，是彻底移除；选择状态一并清空，
  // 下一次出片从干净状态重新开始（默认勾选重新生效）。数字人待办卡也属出片流程，一并下线。
  function unloadFilmKit() {
    unloadFilmWidgetsDOM();
    if (summaryCard && summaryCard.parentNode) summaryCard.remove();
    summaryCard = null;
    pickedChoices = {};
    scriptWidgetsOffered = false;
    autoPickedDefaults = {};
    summaryConsumed = null;
    Array.prototype.forEach.call(
      document.querySelectorAll(".widget-card.picked, .widget-row.picked"),
      function (n) { n.classList.remove("picked"); }
    );
    var dh = actionCards["digital-human"];
    if (dh && dh.el && dh.el.parentNode) dh.el.remove();
    delete actionCards["digital-human"];
  }
  // ---- 乐观卸载 + 暂存：用户发消息瞬间就先把出片区卸掉（附件跟着话下线），
  // 但把已点选的选择暂存；若后端这轮回是出片轮，选择原样恢复（出片续聊不丢已选）。----
  var filmStash = null;
  function stashFilmKit() {
    if (!pickedChoices.avatar && !pickedChoices.voice && !pickedChoices.script) {
      filmStash = null;
      return;
    }
    filmStash = {
      pickedChoices: JSON.parse(JSON.stringify(pickedChoices)),
      scriptWidgetsOffered: scriptWidgetsOffered,
      autoPickedDefaults: JSON.parse(JSON.stringify(autoPickedDefaults)),
      summaryConsumed: summaryConsumed,
    };
  }
  function restoreFilmStash() {
    if (!filmStash) return;
    pickedChoices = filmStash.pickedChoices || {};
    scriptWidgetsOffered = filmStash.scriptWidgetsOffered;
    autoPickedDefaults = filmStash.autoPickedDefaults || {};
    summaryConsumed = filmStash.summaryConsumed;
    filmStash = null;
  }

  // ---- 收起旧卡（纪律3）：新回复发出时，把此前渲染的素材卡/出片配置卡收成一行 ----
  function collapseOldCards() {
    Array.prototype.forEach.call(
      document.querySelectorAll("#messages > .widget-box:not(.action-box), #messages > .summary-card"),
      function (n) { n.classList.add("collapsed"); }
    );
  }

  function renderWidgets(widgets, opts) {
    if (!widgets || !widgets.length) return;
    opts = opts || {};
    var anyNew = false;
    var lastNewBox = null;

    // 音色合并：所有 voice_pick（我的克隆槽位 + 系统公共音色）并成「一块」，
    // 用「我的 / 系统」标签切开，绝不上下一拆二。
    var voiceWidgets = widgets.filter(function (w) { return w.type === "voice_pick"; });
    var others = widgets.filter(function (w) { return w.type !== "voice_pick" && !isWidgetDismissed(w); });
    var voiceAllDismissed = voiceWidgets.every(isWidgetDismissed);
    if (voiceWidgets.length && !voiceAllDismissed) {
      var vp = voiceWidgetsFingerprint(voiceWidgets);
      var vrec = renderedWidgets["voice_panel"];
      if (!(vrec && vrec.fp === vp && vrec.el && vrec.el.parentNode)) {
        if (vrec && vrec.el) vrec.el.remove();
        var vbox = renderVoicePanel(voiceWidgets, opts);
        if (vbox) {
          messages.appendChild(vbox);
          renderedWidgets["voice_panel"] = { fp: vp, el: vbox, film: true };
          anyNew = true;
          lastNewBox = vbox;
        } else {
          delete renderedWidgets["voice_panel"];
        }
      }
    }

    others.forEach(function (w) {
      if (!w || !w.id) return;
      if (w.type === "script_pick") scriptWidgetsOffered = true; // 本会话有过文案三版卡
      var fp = widgetsFingerprint(w);
      var rec = renderedWidgets[w.id];
      if (rec && rec.fp === fp && rec.el && rec.el.parentNode) return; // 已渲染且内容未变
      if (rec && rec.el) rec.el.remove();
      var box = renderWidget(w, opts);
      if (!box) { delete renderedWidgets[w.id]; return; }
      messages.appendChild(box);
      renderedWidgets[w.id] = { fp: fp, el: box, film: isFilmWidget(w) };
      anyNew = true;
      lastNewBox = box;
    });
    autoPickDefaults(widgets);
    if (scriptWidgetsOffered) updateSummaryCard({ scroll: false }); // 文案卡已出现：配置卡立刻按「必选文案」口径刷新
    if (!anyNew) return;
    // 锚定优先级：本轮引导气泡（出片引导四句：引导在上、选择在下）→ 待办卡片 → 新选项卡片。
    // 绝不让卡片把话顶出屏幕：引导文字是主角，卡片是附件。
    var newestAction = null;
    Object.keys(actionCards).forEach(function (k) {
      var r = actionCards[k];
      if (r && r.el && r.el.parentNode) newestAction = r.el;
    });
    var target = opts.anchor || newestAction || lastNewBox;
    // 用户粘底才锚定（往上翻历史时不打扰）
    if (scrollSticky && target && target.scrollIntoView) target.scrollIntoView({ block: "start" });
  }

  // ---- 给默认（出片引导第三句）：默认形象=本人形象、默认音色=本人声音/克隆音色，
  // 渲染时自动勾选一次；用户 × 撤掉后绝不自动选回。----
  function autoPickDefaults(widgets) {
    if (!autoPickedDefaults.avatar && !pickedChoices.avatar) {
      var aw = (widgets || []).filter(function (w) { return w.type === "avatar_pick"; })[0];
      var item = defaultOf(aw, function (it) { return it.name === "本人形象"; });
      if (item) autoPickChoice(aw, item, "avatar");
    }
    if (!autoPickedDefaults.voice && !pickedChoices.voice) {
      var vws = (widgets || []).filter(function (w) { return w.type === "voice_pick"; });
      var def = null;
      // 优先「本人」名字的音色，其次克隆音色，最后第一个
      vws.forEach(function (w) {
        if (def) return;
        (w.items || []).forEach(function (it) {
          if (def) return;
          if ((it.name || "").indexOf("本人") >= 0) def = { w: w, it: it };
        });
      });
      if (!def) {
        vws.forEach(function (w) {
          if (def) return;
          (w.items || []).forEach(function (it) {
            if (def) return;
            if (it.kind === "clone") def = { w: w, it: it };
          });
        });
      }
      if (!def && vws.length) {
        var w0 = vws[0];
        if ((w0.items || []).length) def = { w: w0, it: w0.items[0] };
      }
      if (def) autoPickChoice(def.w, def.it, "voice");
    }
  }

  function defaultOf(w, pred) {
    if (!w || !w.items || !w.items.length) return null;
    var hit = null;
    w.items.forEach(function (it) { if (pred(it) && !hit) hit = it; });
    return hit || w.items[0];
  }

  function autoPickChoice(w, it, kind) {
    autoPickedDefaults[kind] = true;
    pickWidget(w, it, null, true); // silent：不滚动，引导锚定负责视口
  }

  function voiceWidgetsFingerprint(ws) {
    return "voice_panel::" + ws.map(function (w) {
      return (w.id || "") + "@" + (w.gen || 1) + "|" + (w.items || []).map(function (i) {
        return [i.id, i.name, i.kind, i.scope, i.raw_label || "", i.preview_url || "", i.created_at || ""].join("=");
      }).join(";");
    }).join(";;");
  }

  // 卡片标题行：标题 + × 关闭（纪律2：每张卡都能关掉）
  function makeWidgetHead(box, titleText, onClose) {
    var head = document.createElement("div");
    head.className = "widget-head";
    var title = document.createElement("div");
    title.className = "widget-title";
    title.textContent = titleText;
    head.appendChild(title);
    var close = document.createElement("button");
    close.type = "button";
    close.className = "widget-close";
    close.textContent = "×";
    close.title = "关掉这张卡（这次不出片，对话继续）";
    close.addEventListener("click", function (e) {
      e.stopPropagation();
      if (onClose) onClose();
    });
    head.appendChild(close);
    // 收起状态下点标题行可重新展开
    head.addEventListener("click", function (e) {
      if (e.target.closest(".widget-close")) return;
      box.classList.toggle("collapsed");
    });
    box.appendChild(head);
    return head;
  }

  function renderVoicePanel(ws, opts) {
    var all = [];
    ws.forEach(function (w) {
      (w.items || []).forEach(function (it) { all.push({ item: it, widget: w }); });
    });
    if (!all.length) return null;
    var mine = all.filter(function (x) { return x.item.kind === "clone"; });
    var system = all.filter(function (x) { return x.item.kind !== "clone"; });
    if (!mine.length && !system.length) return null;
    if (voiceTab === "mine" && !mine.length) voiceTab = "system";
    if (voiceTab === "system" && !system.length) voiceTab = "mine";

    var box = document.createElement("div");
    box.className = "widget-box voice-panel";
    makeWidgetHead(box, "🎙 音色（▶ 可试听，点击选中）", function () {
      // 关掉整个音色面板：底层每个 voice_pick 都记关闭，绝不自动重弹
      ws.forEach(function (w) { dismissedWidgetIds[widgetDismissKey(w)] = true; });
      saveDismissed();
      delete pickedChoices.voice;
      clearPickedMarks("voice");
      if (box.parentNode) box.remove();
      delete renderedWidgets["voice_panel"];
      updateSummaryCard();
    });
    if (opts && opts.collapsed) box.classList.add("collapsed");

    var tabs = document.createElement("div");
    tabs.className = "voice-tabs";
    var mkTab = function (key, label, count) {
      var t = document.createElement("button");
      t.type = "button";
      t.className = "voice-tab" + (voiceTab === key ? " active" : "");
      t.textContent = label + " · " + count;
      t.addEventListener("click", function () {
        voiceTab = key;
        refreshVoicePanel(box, mine, system);
      });
      tabs.appendChild(t);
    };
    if (mine.length) mkTab("mine", "我的", mine.length);
    if (system.length) mkTab("system", "系统", system.length);
    box.appendChild(tabs);

    var listHost = document.createElement("div");
    listHost.className = "voice-list-host";
    box.appendChild(listHost);
    refreshVoicePanel(box, mine, system);
    return box;
  }

  function refreshVoicePanel(box, mine, system) {
    var listHost = box.querySelector(".voice-list-host");
    if (!listHost) return;
    listHost.innerHTML = "";
    Array.prototype.forEach.call(box.querySelectorAll(".voice-tab"), function (t) {
      var mineTab = t.textContent.indexOf("我的") === 0;
      t.classList.toggle("active", mineTab === (voiceTab === "mine"));
    });
    var rows = voiceTab === "mine" ? mine : system;
    if (!rows.length) {
      var empty = document.createElement("div");
      empty.className = "voice-empty";
      empty.textContent = voiceTab === "mine"
        ? "还没有克隆音色。跟我说「克隆我的声音」，做好后会出现在这里。"
        : "暂无系统音色。";
      listHost.appendChild(empty);
      return;
    }
    var body = document.createElement("div");
    body.className = "widget-list";
    rows.forEach(function (x) { body.appendChild(renderVoiceRow(x.widget, x.item)); });
    listHost.appendChild(body);
  }

  function renderVoiceRow(w, it) {
    var row = document.createElement("div");
    row.className = "widget-row voice-row";
    row.dataset.itemId = String(it.id);
    var main = document.createElement("div");
    main.className = "wr-main";
    var nm = document.createElement("div");
    nm.className = "wr-name";
    var hn = humanName(it, "voice");
    nm.textContent = hn.main;
    nm.title = hn.main;
    main.appendChild(nm);
    if (hn.sub) {
      var sub = document.createElement("div");
      sub.className = "wr-sub";
      sub.textContent = hn.sub;
      main.appendChild(sub);
    }
    var acts = document.createElement("div");
    acts.className = "wr-actions";
    if (it.preview_url) {
      var play = document.createElement("button");
      play.textContent = "▶ 试听";
      play.addEventListener("click", function (e) {
        e.stopPropagation();
        toggleAudition(it.preview_url, play, main);
      });
      acts.appendChild(play);
    }
    var pick = document.createElement("button");
    pick.className = "pick";
    pick.textContent = "选这个";
    pick.addEventListener("click", function () { pickWidget(w, it, row); });
    acts.appendChild(pick);
    row.appendChild(main); row.appendChild(acts);
    if (pickedChoices.voice && String(pickedChoices.voice.id) === String(it.id)) {
      markPickedEl(row, pick);
    }
    return row;
  }

  function renderWidget(w, opts) {
    var box = document.createElement("div");
    box.className = "widget-box";
    var kind = w.type === "avatar_pick" ? "avatar" : w.type === "voice_pick" ? "voice" : "script";
    makeWidgetHead(box, w.title || "请选择", function () {
      dismissCard(widgetDismissKey(w), kind, box, w);
    });
    if (opts && opts.collapsed) box.classList.add("collapsed");
    if (w.hint) {
      var hint = document.createElement("div");
      hint.className = "widget-hint";
      hint.textContent = w.hint;
      box.appendChild(hint);
    }

    var body = document.createElement("div");
    if (w.type === "avatar_pick") {
      body.className = "widget-grid";
      // 插画/原画不是真人数字人素材：渲染层再滤一道（旧会话遗留的卡片也一起清掉），
      // 别混进「我的形象」。
      var ILLUST_RE = /大师|patreon|原画|插画/i;
      (w.items || []).forEach(function (it) {
        if (ILLUST_RE.test((it.name || "") + (it.raw_label || ""))) return;
        var card = document.createElement("div");
        card.className = "widget-card";
        card.dataset.itemId = String(it.id);
        var img = document.createElement("img");
        img.src = it.image_url;
        img.alt = it.name;
        img.loading = "lazy";
        img.onerror = function () { brokenImage(img, card); };
        var hn = humanName(it, "avatar");
        card.appendChild(img);
        if (hn.main) { // 编号型形象（raw_label=形象 N）没有可读名字：只留脸，不写字
          var name = document.createElement("div");
          name.className = "wc-name";
          name.textContent = hn.main;
          name.title = (it.name || "") + (it.status ? "（" + it.status + "）" : "");
          card.appendChild(name);
        }
        if (hn.sub) {
          var sub = document.createElement("div");
          sub.className = "wc-sub";
          sub.textContent = hn.sub;
          card.appendChild(sub);
        }
        var act = document.createElement("div");
        act.className = "wc-act";
        var pick = document.createElement("button");
        pick.className = "pick";
        pick.textContent = "选这个";
        pick.addEventListener("click", function () { pickWidget(w, it, card); });
        act.appendChild(pick);
        card.appendChild(act);
        if (pickedChoices.avatar && String(pickedChoices.avatar.id) === String(it.id)) {
          markPickedEl(card, pick);
        }
        body.appendChild(card);
      });
    } else if (w.type === "script_pick" || w.type === "option_pick") {
      body.className = "widget-list";
      (w.items || []).forEach(function (it) {
        var row = document.createElement("div");
        row.className = "widget-row";
        row.dataset.itemId = String(it.id || "");
        var main = document.createElement("div");
        main.className = "wr-main";
        // 选项带图（image_url 或 summary 里的图片链接）→ 缩略图
        var imgUrl = it.image_url;
        var audioUrl = it.preview_url;
        var summaryText = it.summary || "";
        if (!imgUrl) {
          var m = summaryText.match(/https?:\/\/[^\s"'<>()]+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s"'<>()]*)?/i);
          if (m) {
            imgUrl = m[0];
            summaryText = summaryText.replace(m[0], "").replace(/预览[:：]\s*/g, "").replace(/[（(]\s*[)）]\s*$/g, "").trim();
          }
        }
        if (!audioUrl) {
          var am = summaryText.match(/https?:\/\/[^\s"'<>()]+\.(?:mp3|wav|m4a|aac|ogg)(?:\?[^\s"'<>()]*)?/i);
          if (am) {
            audioUrl = am[0];
            summaryText = summaryText.replace(am[0], "").replace(/试听[:：]\s*/g, "").trim();
          }
        }
        if (imgUrl) {
          var thumb = document.createElement("img");
          thumb.className = "wr-thumb";
          thumb.src = imgUrl;
          thumb.alt = it.title || "";
          thumb.loading = "lazy";
          thumb.onerror = function () { brokenImage(thumb, thumbLink); };
          var thumbLink = document.createElement("a");
          thumbLink.href = imgUrl; thumbLink.target = "_blank"; thumbLink.rel = "noopener";
          thumbLink.appendChild(thumb);
          main.appendChild(thumbLink);
        }
        var nm = document.createElement("div");
        nm.className = "wr-name";
        nm.textContent = it.title;
        main.appendChild(nm);
        if (summaryText) {
          var sub = document.createElement("div");
          sub.className = "wr-sub";
          sub.textContent = summaryText;
          main.appendChild(sub);
        }
        var acts = document.createElement("div");
        acts.className = "wr-actions";
        if (audioUrl) {
          var play = document.createElement("button");
          play.textContent = "▶ 试听";
          play.addEventListener("click", function (e) {
            e.stopPropagation();
            toggleAudition(audioUrl, play, main);
          });
          acts.appendChild(play);
        }
        if (it.body) {
          var view = document.createElement("button");
          view.textContent = "看全文";
          view.addEventListener("click", function (e) {
            e.stopPropagation();
            var b = row.querySelector(".wr-body");
            if (!b) {
              b = document.createElement("div");
              b.className = "wr-body open";
              b.textContent = it.body;
              main.appendChild(b);
              view.textContent = "收起";
            } else {
              b.classList.toggle("open");
              view.textContent = b.classList.contains("open") ? "收起" : "看全文";
            }
          });
          acts.appendChild(view);
        }
        var pick = document.createElement("button");
        pick.className = "pick";
        pick.textContent = "选这版";
        pick.addEventListener("click", function () { pickWidget(w, it, row); });
        acts.appendChild(pick);
        row.appendChild(main); row.appendChild(acts);
        if (pickedChoices.script && String(pickedChoices.script.id) === String(it.id)) {
          markPickedEl(row, pick);
        }
        body.appendChild(row);
      });
    } else {
      return null;
    }
    box.appendChild(body);
    return box;
  }

  // ---- 素材选择：本地暂存（不立刻打扰后端），收口在消息流末尾的「出片配置卡」点确认生成 ----
  function pickWidget(w, it, el, silent) {
    var kind = w.type === "avatar_pick" ? "avatar" : w.type === "voice_pick" ? "voice" : "script";
    var hn = humanName(it, kind);
    // 编号型形象没有可读名字：配置卡上只露脸，不写字
    var label = hn.main || (kind === "avatar" ? "" : it.name || it.title || String(it.id || ""));
    if (hn.main && hn.sub) label = label + " · " + hn.sub.split(" · ")[0]; // 同名项用创建日期区分
    pickedChoices[kind] = {
      id: String(it.id || ""),
      label: label,
      image_url: it.image_url || "",
      preview_url: it.preview_url || "",
      widgetTitle: w.title || "",
    };
    // 同一类内单选：清掉该类其他卡片的高亮（形象/音色/文案各只留一个选中）
    clearPickedMarks(kind);
    if (el) { // 手动点选：直接标记点击的卡片
      el.dataset.kind = kind;
      var pb = el.querySelector("button.pick");
      el.dataset.origText = pb ? pb.textContent : "选这个";
      markPickedEl(el, pb);
    } else { // 自动勾选默认项（无 DOM 锚点）：按 item id 找到已渲染的卡片补高亮
      markPickedById(kind, String(it.id || ""));
    }
    summaryConsumed = null; // 用户又在主动选了：允许出新配置卡
    updateSummaryCard(silent ? { scroll: false } : {});
  }

  function markPickedById(kind, itemId) {
    Array.prototype.forEach.call(document.querySelectorAll(".widget-card, .widget-row"), function (n) {
      if (n.dataset.itemId !== itemId) return;
      var pb = n.querySelector("button.pick");
      if (!pb) return;
      n.dataset.kind = kind;
      n.dataset.origText = pb.textContent || "选这个";
      markPickedEl(n, pb);
    });
  }

  function markPickedEl(el, pb) {
    el.classList.add("picked");
    if (pb) pb.textContent = "已选 ✓";
  }

  function clearPickedMarks(kind) {
    Array.prototype.forEach.call(document.querySelectorAll(".widget-card.picked, .widget-row.picked"), function (n) {
      if (n.dataset.kind !== kind) return;
      n.classList.remove("picked");
      var b = n.querySelector("button.pick");
      if (b) b.textContent = n.dataset.origText || "选这个";
    });
  }

  // ---- 可读名：编号（「音色 17」「形象 19」）是库存编号，不是给人看的名字。
  // 编号型形象：只留脸，不写任何字；编号型音色：叫「我的克隆音色/我的音色」，用创建日期区分。----
  function fmtDate(ts) {
    if (!ts) return "";
    var d = new Date(typeof ts === "number" && ts < 1e12 ? ts * 1000 : ts);
    if (isNaN(d.getTime())) return "";
    var mm = String(d.getMonth() + 1); if (mm.length < 2) mm = "0" + mm;
    var dd = String(d.getDate()); if (dd.length < 2) dd = "0" + dd;
    return d.getFullYear() + "-" + mm + "-" + dd;
  }

  var NUMBERED_RE = /^(音色|形象|槽位|voice|avatar)\s*\d+/i;
  function humanName(it, kind) {
    var raw = (it.name || it.title || "").trim();
    var numbered = NUMBERED_RE.test(raw) || NUMBERED_RE.test((it.raw_label || "").trim());
    if (numbered && kind === "avatar") {
      return { main: "", sub: "" }; // 只留脸
    }
    if (numbered && kind === "voice") {
      var bits = [];
      if (it.created_at) bits.push("创建于 " + fmtDate(it.created_at));
      return {
        main: it.kind === "clone" ? "我的克隆音色" : "我的音色",
        sub: bits.join(" · "),
      };
    }
    var sub = "";
    if (kind === "voice" && it.kind === "clone" && it.created_at) sub = "创建于 " + fmtDate(it.created_at);
    if (!sub && it.raw_label) sub = it.raw_label;
    return { main: raw || ("#" + it.id), sub: sub };
  }

  // ---- 出片配置卡（一次性附件，不在输入框上方钉常驻栏）：
  // 左脸（形象缩略图）· 中声（音色名）· 右文案（标题）+ 大绿色确认按钮。
  // 每个格子都能 × 撤销；整卡 × 关掉=本次不出片、对话继续，且不再自动弹出。----
  function summarySignature() {
    return ["avatar", "voice", "script"].map(function (k) {
      var c = pickedChoices[k];
      return k + "=" + (c ? c.id : "");
    }).join("|");
  }

  function updateSummaryCard(opts) {
    opts = opts || {};
    var sig = summarySignature();
    var needScript = scriptWidgetsOffered && !pickedChoices.script; // 出过文案卡就必须选一版
    var hasAny = !!(pickedChoices.avatar || pickedChoices.voice || pickedChoices.script);
    if (!hasAny) {
      if (summaryCard) { summaryCard.remove(); summaryCard = null; }
      return;
    }
    if (sig === summaryConsumed && !summaryCard) return;      // 已关掉的配置：不再自动弹出
    // 内容没变且「必选文案」口径没变：不重建
    if (summaryCard && summaryCard.dataset.sig === sig && summaryCard.dataset.need === (needScript ? "1" : "0")) return;
    if (summaryCard) summaryCard.remove();
    summaryCard = buildSummaryCard(sig);
    messages.appendChild(summaryCard);
    // 业务结果必须摆在眼前：卡片出现即滚进视口（用户粘底时才打扰）；
    // silent（自动勾选默认）不滚——引导气泡锚定负责视口，卡片不许把话顶出屏幕
    if (opts.scroll !== false && scrollSticky && summaryCard.scrollIntoView) summaryCard.scrollIntoView({ block: "nearest" });
  }

  function buildSummaryCard(sig) {
    var needScript = scriptWidgetsOffered && !pickedChoices.script; // 文案未选：确认生成不可点
    var card = document.createElement("div");
    card.className = "summary-card";
    card.dataset.sig = sig;
    card.dataset.need = needScript ? "1" : "0";

    var head = document.createElement("div");
    head.className = "sum-head";
    var t = document.createElement("span");
    t.className = "sum-title";
    t.textContent = "🎬 本次出片配置";
    head.appendChild(t);
    var close = document.createElement("button");
    close.type = "button";
    close.className = "widget-close";
    close.textContent = "×";
    close.title = "取消本次出片（对话继续，不再自动弹出）";
    close.addEventListener("click", function () {
      Object.keys(pickedChoices).forEach(function (k) { delete pickedChoices[k]; });
      ["avatar", "voice", "script"].forEach(clearPickedMarks);
      summaryConsumed = sig;
      if (summaryCard) { summaryCard.remove(); summaryCard = null; }
    });
    head.appendChild(close);
    head.addEventListener("click", function (e) {
      if (e.target.closest(".widget-close")) return;
      card.classList.toggle("collapsed");
    });
    card.appendChild(head);

    var slots = document.createElement("div");
    slots.className = "sum-slots";
    [["avatar", "👤 未选形象"], ["voice", "🎙 未选音色"], ["script", "📝 未选文案"]].forEach(function (pair) {
      var kind = pair[0];
      var emptyText = pair[1];
      var slot = document.createElement("div");
      slot.className = "sum-slot";
      var c = pickedChoices[kind];
      if (c) {
        var body = document.createElement("div");
        body.className = "sum-slot-body";
        if (kind === "avatar" && c.image_url) {
          var img = document.createElement("img");
          img.className = "sum-avatar";
          img.src = c.image_url;
          img.alt = c.label || "形象";
          body.appendChild(img);
        } else {
          var ic = document.createElement("span");
          ic.className = "sum-ic";
          ic.textContent = kind === "voice" ? "🎙" : "📝";
          body.appendChild(ic);
        }
        if (c.label) {
          var nm = document.createElement("span");
          nm.className = "sum-name";
          nm.textContent = c.label;
          body.appendChild(nm);
        }
        var x = document.createElement("button");
        x.type = "button";
        x.className = "sum-x";
        x.textContent = "×";
        x.title = "移除这一项";
        x.addEventListener("click", function () {
          delete pickedChoices[kind];
          clearPickedMarks(kind);
          updateSummaryCard();
        });
        slot.appendChild(body);
        slot.appendChild(x);
      } else {
        slot.className = "sum-slot sum-slot-empty";
        if (kind === "script" && needScript) {
          // 文案卡出过但还没选：确认生成不可点，文案格亮起必选提示
          slot.className += " sum-slot-required";
          slot.textContent = "📝 必选：点上方文案卡选一版";
        } else {
          slot.textContent = emptyText;
        }
      }
      slots.appendChild(slot);
    });
    card.appendChild(slots);

    var foot = document.createElement("div");
    foot.className = "sum-foot";
    var go = document.createElement("button");
    go.type = "button";
    go.className = "sum-go";
    if (needScript) {
      // 未选文案不允许出片：按钮不可点，直到文案卡里点了一版
      go.disabled = true;
      go.textContent = "📝 先选一版文案";
      go.title = "文案还没选：先在上方文案卡里点选一版，才能确认生成";
    } else {
      go.textContent = "✅ 确认生成";
    }
    go.addEventListener("click", function () {
      if (go.disabled) return;
      var lines = [];
      if (pickedChoices.avatar) {
        lines.push(pickedChoices.avatar.label
          ? "形象：" + pickedChoices.avatar.label + "（id=" + pickedChoices.avatar.id + "）"
          : "形象：id=" + pickedChoices.avatar.id);
      }
      if (pickedChoices.voice) lines.push("音色：" + pickedChoices.voice.label + "（id=" + pickedChoices.voice.id + "）");
      if (pickedChoices.script) lines.push("文案：" + pickedChoices.script.label + "（id=" + pickedChoices.script.id + "）");
      if (!lines.length) return;
      var msg = "【已选汇总】\n" + lines.join("\n") + "\n请按以上选择继续。";
      go.disabled = true;
      go.textContent = "已提交，生成中…";
      card.classList.add("submitted");
      summaryConsumed = sig;
      send(msg);
    });
    foot.appendChild(go);
    card.appendChild(foot);
    return card;
  }

  function toggleAudition(url, btn, host) {
    var existing = host && host.querySelector("audio.wr-aud");
    if (existing) {
      existing.remove();
      btn.textContent = "▶ 试听";
      return;
    }
    var a = document.createElement("audio");
    a.className = "wr-aud";
    a.controls = true;
    a.preload = "none";
    a.src = url;
    btn.textContent = "⏸ 收起试听";
    a.addEventListener("ended", function () { btn.textContent = "▶ 试听"; });
    a.addEventListener("error", function () { a.remove(); btn.textContent = "▶ 试听"; });
    host.appendChild(a);
    a.play().catch(function () {});
  }

  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      if (e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      send();
    }
  });

  // ---- 示例卡 ----
  Array.prototype.forEach.call(document.querySelectorAll(".example-card"), function (btn) {
    btn.addEventListener("click", function () { send(btn.getAttribute("data-q")); });
  });

  // ---- 重置 ----
  $("reset-btn").addEventListener("click", function () {
    if (sessionId) {
      fetch("api/v4/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId }) });
    }
    localStorage.removeItem("hq-v4-session-id");
    messages.innerHTML = "";
    toolList.innerHTML = '<p class="tool-empty">还没有派发子 Agent。你说「出张海报」「查点数」这类需求，主 Agent 会路由到对应子 Agent。</p>';
    reportPane.hidden = true;
    m5Box.hidden = true;
    m6Box.hidden = true;
    renderedWidgetIds = {};
    renderedWidgets = {};
    renderedSeqs = {};
    activeSeqs = {};
    pickedChoices = {};
    scriptWidgetsOffered = false;
    autoPickedDefaults = {};
    filmStash = null;
    voiceTab = "mine";
    summaryCard = null;
    summaryConsumed = null;
    dismissedWidgetIds = {};
    modCache = { m5: { path: null }, m6: { path: null } };
    m5Content.innerHTML = "";
    m6Content.innerHTML = "";
    cancelTurnPoll();
    removeStatusBubble();
    jobTimedOut = false; // 新会话：卡死窗口重计
    // 关掉旧会话的 SSE 长连接：否则旧 sid 的事件会继续打进新会话 DOM。
    if (eventSource) { try { eventSource.close(); } catch (e2) {} eventSource = null; }
    streamDisabled = false;
    pendingAttachments = [];
    renderUploads();
    intro.style.display = "";
    start();
  });

  $("clear-tools").addEventListener("click", function () {
    toolList.innerHTML = '<p class="tool-empty">还没有派发子 Agent。你说「出张海报」「查点数」这类需求，主 Agent 会路由到对应子 Agent。</p>';
  });

  // ---- 启动 ----
  function start() {
    fetch("api/v4/start", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        sessionId = data.session_id;
        localStorage.setItem("hq-v4-session-id", sessionId);
        cleanupOldDismissed(sessionId);
        loadDismissed();
        setBadge(data.mode);
        if (!pollTimer) pollTimer = setInterval(pollReport, 3000);
        startStream(); // 常开长连接：后台任务完成交付（如采集贴图）实时送达
        if (data.async && data.seq != null) {
          // 开场白在后台生成：状态气泡 + 轮询等待（兼容旧服务同步返回走下面的旧路径）
          jobTimedOut = false;
          ensureStatusBubble();
          startTurnPoll(data.seq);
          return;
        }
        addMsg("assistant", data.reply || "你好，说说你想要什么成果。");
      })
      .catch(function () { addMsg("assistant", "启动失败，请确认服务已运行。"); });
  }

  // ---- 恢复：刷新页面后找回上次对话（sid 存 localStorage，服务端已落盘）----
  function renderDelegationStates(delegations) {
    if (!delegations) return;
    Object.keys(delegations).forEach(function (domain) {
      var d = delegations[domain];
      if (!d || !d.state) return;
      renderRouting([{ domain: domain, state: d.state, summary: d.summary || "" }]);
      if (d.state === "needs_approval") renderApprovalCard(domain, d);
    });
  }

  // ---- 报价确认卡片：needs_approval 时出现，点按钮即可确认/取消 ----
  function renderApprovalCard(domain, d) {
    var key = "approval:" + domain + ":" + (d.quote_id || "legacy");
    var previous = messages.querySelector('.approval-box[data-domain="' + domain + '"]');
    if (previous && previous.dataset.quoteId === (d.quote_id || "")) {
      previous.classList.remove("collapsed");
      return;
    }
    if (previous) previous.remove();
    renderedWidgetIds[key] = true;
    var box = document.createElement("div");
    box.className = "widget-box approval-box";
    box.dataset.domain = domain;
    box.dataset.quoteId = d.quote_id || "";
    var title = document.createElement("div");
    title.className = "widget-title";
    title.textContent = (AGENT_LABEL[domain] || domain) + " 报价确认";
    box.appendChild(title);
    var body = document.createElement("div");
    body.className = "widget-list";
    var row = document.createElement("div");
    row.className = "widget-row";
    var main = document.createElement("div");
    main.className = "wr-main";
    var nm = document.createElement("div");
    nm.className = "wr-name";
    var q = d.quote || {};
    nm.textContent = d.summary || ("本次将扣 " + (q.cost != null ? q.cost + " 点" : "相应点数"));
    main.appendChild(nm);
    if (q.cost != null) {
      var sub = document.createElement("div");
      sub.className = "wr-sub";
      sub.textContent = "余额 " + (q.points != null ? q.points + " 点" : "—") +
        (q.expires_in ? "，报价 " + Math.max(1, Math.round(q.expires_in / 60)) + " 分钟内有效" : "");
      main.appendChild(sub);
    }
    var acts = document.createElement("div");
    acts.className = "wr-actions";
    var ok = document.createElement("button");
    ok.className = "pick";
    ok.textContent = domain === "collect" ? "确认采集" : "确认执行";
    function choose(decision) {
      if (streaming || ok.disabled) return;
      if (!d.quote_id) {
        addMsg("assistant", "这是一张旧报价卡，请刷新页面获取当前任务状态后再操作。");
        return;
      }
      if (send(decision === "confirm" ? "确认" : "先不生成，我再想想",
               { domain: domain, quote_id: d.quote_id, decision: decision }, function () {
                 ok.disabled = false; no.disabled = false;
                 ok.textContent = "重试当前报价确认";
                 row.classList.remove("picked");
               })) {
        ok.disabled = true; no.disabled = true;
        ok.textContent = decision === "confirm" ? "已收到确认，正在处理…" : "正在取消…";
        row.classList.add("picked");
      }
    }
    ok.addEventListener("click", function () {
      choose("confirm");
    });
    var no = document.createElement("button");
    no.textContent = "先不生成";
    no.addEventListener("click", function () {
      choose("cancel");
    });
    acts.appendChild(ok); acts.appendChild(no);
    row.appendChild(main); row.appendChild(acts);
    body.appendChild(row);
    box.appendChild(body);
    messages.appendChild(box);
    autoScroll();
  }

  // ---- 「等你回复」待办卡片：子 Agent 六态为 needs_user_input 时，
  // 把它的提问渲染成消息流末尾的醒目卡片并自动滚到可见——用户必须看得到、可交互。
  // （路由轨迹面板里的小字不算数，业务结果要摆在眼前。）----
  var actionCards = {}; // domain -> {q, el}

  function buildActionCard(domain, question) {
    var box = document.createElement("div");
    box.className = "widget-box action-box";
    var title = document.createElement("div");
    title.className = "widget-title";
    title.textContent = "⏳ " + (AGENT_LABEL[domain] || domain) + " 等你回复";
    box.appendChild(title);
    var hint = document.createElement("div");
    hint.className = "widget-hint";
    hint.textContent = question;
    box.appendChild(hint);
    var foot = document.createElement("div");
    foot.className = "action-foot";
    foot.textContent = "请按上面的问题回复（或点下方卡片选择），我马上继续。";
    box.appendChild(foot);
    return box;
  }

  // filmMode：当前语境是否出片。非出片语境时，「数字人 等你回复」这类出片流程的
  // 待办卡不渲染（意图门控：出片区不常驻，货架跟着当前意图走）；报价卡与其它域待办照常。
  function renderActionCards(delegations, filmMode) {
    if (!delegations) return;
    Array.prototype.forEach.call(messages.querySelectorAll(".approval-box"), function (box) {
      var current = delegations[box.dataset.domain];
      if (!current || current.state !== "needs_approval" || !current.quote_id) box.remove();
    });
    Object.keys(delegations).forEach(function (domain) {
      var d = delegations[domain];
      if (!d || !d.state) return;
      if (d.state === "needs_approval") {
        renderApprovalCard(domain, d); // 报价确认卡片（带去重，点按钮即确认）
        return;
      }
      if (d.state !== "needs_user_input") return;
      if (domain === "digital-human" && !filmMode) return; // 出片流程的提问只在出片语境里挂
      var q = (d.question || d.summary || "").trim();
      if (!q) return;
      var rec = actionCards[domain];
      if (rec && rec.q === q && rec.el && rec.el.parentNode) return; // 内容没变：不重复渲染
      if (rec && rec.el) rec.el.remove();
      var el = buildActionCard(domain, q);
      if (!el) { delete actionCards[domain]; return; }
      messages.appendChild(el);
      actionCards[domain] = { q: q, el: el };
      // 用户粘底时才锚定待办卡片（往上翻历史时不打扰）
      if (scrollSticky && el.scrollIntoView) el.scrollIntoView({ block: "start" });
    });
  }

  function tryRestore() {
    var savedSid = localStorage.getItem("hq-v4-session-id");
    try {
      var qsSid = new URLSearchParams(location.search).get("sid");
      if (qsSid) { savedSid = qsSid; localStorage.setItem("hq-v4-session-id", qsSid); }
    } catch (e) {}
    if (!savedSid) { start(); return; }
    fetch("api/v4/restore/" + encodeURIComponent(savedSid))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok || !data.history || !data.history.length) {
          localStorage.removeItem("hq-v4-session-id");
          start();
          return;
        }
        sessionId = savedSid;
        cleanupOldDismissed(sessionId);
        loadDismissed();
        // 恢复的助手气泡也带 data-reply（原始文本）：轮询排空时按内容去重，回放轮次绝不重复贴。
        // 批量建 DOM（DocumentFragment）一次插入、只在末尾滚一次：几百上千条消息恢复不再逐条平滑滚动卡顿。
        var frag = document.createDocumentFragment();
        data.history.forEach(function (m) {
          var wrap = makeMsg(m.role, m.content, m.images);
          var bubble = wrap.querySelector(".bubble");
          if (m.role === "assistant" && m.content && bubble) bubble.dataset.reply = m.content;
          frag.appendChild(wrap);
        });
        messages.appendChild(frag);
        forceScrollBottom();
        // 意图门控：恢复时按后端权威信号 data.film（最后轮的 routing 是否派发 digital-human）决定挂不挂卡。
        // 关键词不可靠（如最后消息含「口播」二字但其实是闲聊）——一律只信 film 标志。
        renderDelegationStates(data.delegations);
        renderActionCards(data.delegations, data.film === true); // 恢复后待办卡片立即出现（先看问题；出片提问卡按意图门控）
        // 旧素材卡只在「最后意图仍是出片」时按收起态恢复（该条消息的附件）；
        // 最后在聊别的（哪怕会话里出过片）→ 不渲染任何出片卡，连收起态都不留。
        if (data.film === true) {
          renderWidgets(data.widgets, { collapsed: true });
        }
        renderReport(data.report);
        setBadge(data.mode);
        intro.style.display = "none";
        if (!pollTimer) pollTimer = setInterval(pollReport, 3000);
        startStream(); // 常开长连接：后台任务完成交付（如采集贴图）实时送达
        // 刷新页面时若有轮次还在后台跑：恢复状态气泡继续等，不让用户迷茫
        fetch("api/v4/status/" + encodeURIComponent(savedSid))
          .then(function (r) { return r.json(); })
          .then(function (st) {
            var working = (st.turns || []).filter(function (t) { return t.state === "working"; });
            var jobs = (st.jobs || []).length;
            if (working.length || jobs) {
              ensureStatusBubble();
              working.forEach(function (t) { startTurnPoll(t.seq); });
              startStream();
            }
            // 无条件排空一次：轮次恰在「restore 快照之后、本次 status 之前」完成时，
            // SSE 事件已错过、status 又查不到 working——poll 的 FIFO 会把这类
            // 「历史里没有的已完成轮次」兜回来（已在历史里的回放按内容去重跳过）。
            startTurnPoll(null, true);
          })
          .catch(function () {
            // status 查询失败也排空：只依赖 poll 兜底
            startTurnPoll(null, true);
          });
      })
      .catch(function () { showRestoreRetry(); });
  }

  // 恢复失败绝不静默开新会话（那会把 localStorage 里的历史 sid 覆盖掉，看起来像记录丢了）。
  // 给用户两个明确选择：重试恢复 / 主动开新会话（原记录在服务器上还在）。
  function showRestoreRetry() {
    var old = document.querySelector(".restore-retry-msg");
    if (old) old.remove();
    var wrap = document.createElement("div");
    wrap.className = "msg assistant restore-retry-msg";
    var b = document.createElement("div");
    b.className = "bubble";
    b.textContent = "恢复历史失败了（可能是网络抖动，记录还在服务器上）。";
    var row = document.createElement("div");
    row.className = "restore-retry-row";
    var retry = document.createElement("button");
    retry.className = "restore-retry-btn";
    retry.textContent = "🔄 重试恢复";
    retry.addEventListener("click", function () { wrap.remove(); tryRestore(); });
    var fresh = document.createElement("button");
    fresh.className = "restore-retry-btn";
    fresh.textContent = "开新会话";
    fresh.addEventListener("click", function () {
      wrap.remove();
      localStorage.removeItem("hq-v4-session-id");
      start();
    });
    row.appendChild(retry);
    row.appendChild(fresh);
    b.appendChild(row);
    wrap.appendChild(b);
    messages.appendChild(wrap);
    intro.style.display = "";
    autoScroll();
  }

  fetch("api/health")
    .then(function (r) { return r.json(); })
    .then(function (h) {
      setBadge(h.llm_mode);
      setHqBadge(h);
      // 授权徽标每分钟自刷：过期了立刻变红，不等任务失败才发现
      if (!authTimer) {
        authTimer = setInterval(function () {
          fetch("api/health")
            .then(function (r) { return r.json(); })
            .then(setHqBadge)
            .catch(function () {});
        }, 60000);
      }
    })
    .catch(function () {});

  // ---- 历史会话：服务端按落盘文件列出，点一下接回任何一次对话 ----
  var histBtn = $("hist-btn");
  var histPanel = $("hist-panel");

  function fmtTime(ts) {
    var d = new Date(ts * 1000);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function renderSessions(list) {
    histPanel.innerHTML = "";
    if (!list || !list.length) {
      var empty = document.createElement("div");
      empty.className = "hist-empty";
      empty.textContent = "还没有历史会话。";
      histPanel.appendChild(empty);
      return;
    }
    list.forEach(function (s) {
      var item = document.createElement("button");
      item.className = "hist-item" + (s.sid === sessionId ? " hist-current" : "");
      item.innerHTML = '<span class="hist-meta">' + fmtTime(s.updated_at) + " · " + s.turns + " 轮</span>" +
        '<span>' + escapeText(s.preview) + "</span>";
      item.title = "接回这次对话";
      item.addEventListener("click", function () {
        localStorage.setItem("hq-v4-session-id", s.sid);
        location.reload();
      });
      histPanel.appendChild(item);
    });
  }
  function escapeText(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  histBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    var willShow = histPanel.hidden;
    histPanel.hidden = !histPanel.hidden;
    if (willShow) {
      fetch("api/v4/sessions")
        .then(function (r) { return r.json(); })
        .then(function (d) { renderSessions(d.sessions || []); })
        .catch(function () { renderSessions([]); });
    }
  });
  document.addEventListener("click", function (e) {
    if (!histPanel.hidden && !histPanel.contains(e.target) && e.target !== histBtn) {
      histPanel.hidden = true;
    }
  });

  tryRestore();
})();
