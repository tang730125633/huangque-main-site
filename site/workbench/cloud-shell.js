/* ============================================================
   黄雀 AI · Cloud Design 落地 — 工作台共享外壳 cloud-shell.js
   注入 228px 侧导 + 顶栏，环绕 .hq-content。各页只写内容 + data-active。
   用法：<div class="hq-app"><div class="hq-content" data-active="dashboard">…内容…</div></div>
   图标集 HQ.icon('name')。登录守卫：未登录可选跳转（默认关，后端接好再开）。
   ============================================================ */
(function(){
  "use strict";
  /* --- duotone-mini 图标集（PR: duotone 图标体系）--- */
  /* 黄雀 duotone-mini 图标集 — 功能位用（16-24px）：白线 1.7 + 单一强调色（__ACC__ 占位，渲染时注入），无网点。
     骨架与 assets/icons-duotone/hq_*.svg 同源，是其小尺寸简化变体。 */
  (function () {
    var L = 'stroke="currentColor" stroke-width="1.7"';
    var A = 'stroke="__ACC__" stroke-width="1.7"';
    var D = {
      home: '<path d="M3 11l9-8 9 8M5 10v10h14V10" ' + L + '/><path d="M10 20v-6h4v6" ' + A + '/>',
      sparkles: '<path d="M11.5 3.4l1.9 5.1 5.1 1.9-5.1 1.9-1.9 5.1-1.9-5.1-5.1-1.9 5.1-1.9z" ' + L + '/><path d="M18.8 15.4l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6z" fill="__ACC__"/>',
      search: '<circle cx="11" cy="11" r="7" ' + L + '/><path d="M21 21l-4-4" ' + A + '/>',
      link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" ' + A + '/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" ' + L + '/>',
      image: '<rect x="3" y="3" width="18" height="18" rx="2" ' + L + '/><circle cx="8.5" cy="8.5" r="1.6" fill="__ACC__"/><path d="M21 15l-5-5L5 21" ' + L + '/>',
      video: '<rect x="2" y="6" width="14" height="12" rx="2" ' + L + '/><path d="M22 8l-6 4 6 4z" fill="__ACC__"/>',
      mic: '<rect x="9" y="2" width="6" height="12" rx="3" ' + L + '/><path d="M5 10a7 7 0 0 0 14 0" ' + A + '/><path d="M12 17v4" ' + L + '/>',
      edit: '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" ' + L + '/><path d="M12 20h9" ' + A + '/>',
      clapper: '<path d="M3 9h18v11H3zM3 9l2-5h16l-2 5" ' + L + '/><path d="M7 4l3 5M14 4l3 5M9 13l5 3-5 3z" ' + A + '/>',
      layers: '<path d="M12 2l9 5-9 5-9-5z" ' + A + '/><path d="M3 12l9 5 9-5M3 17l9 5 9-5" ' + L + '/>',
      folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" ' + L + '/><path d="M7 13.5h4" ' + A + '/>',
      coins: '<circle cx="8" cy="8" r="6" ' + L + '/><path d="M18.1 6.6A6 6 0 1 1 9.9 18.5" ' + A + '/><path d="M7 6h1v4" ' + L + '/><path d="M16.7 12.6h1v4" ' + A + '/>',
      gear: '<circle cx="12" cy="12" r="6.6" ' + L + '/><circle cx="12" cy="12" r="2.6" ' + A + '/><path d="M12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.8 1.8M16.7 16.7l1.8 1.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8" ' + L + '/>',
      bell: '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" ' + L + '/><path d="M13.7 21a2 2 0 0 1-3.4 0" ' + L + '/><circle cx="18" cy="5" r="1.7" fill="__ACC__"/>',
      message: '<path d="M21 11.5a8.4 8.4 0 0 1-9 8 9 9 0 0 1-4-1l-5 1 1-4a8.4 8.4 0 0 1-1-4 8.4 8.4 0 0 1 9-8 8.4 8.4 0 0 1 9 8z" ' + L + '/><circle cx="8.6" cy="11.5" r="1" fill="__ACC__"/><circle cx="12" cy="11.5" r="1" fill="__ACC__"/><circle cx="15.4" cy="11.5" r="1" fill="__ACC__"/>',
      checkCircle: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" ' + L + '/><path d="M22 4L12 14.01l-3-3" ' + A + '/>',
      alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" ' + L + '/><path d="M12 9v4M12 17h.01" ' + A + '/>',
      clock: '<circle cx="12" cy="12" r="9" ' + L + '/><path d="M12 7v5l3 2" ' + A + '/>',
      send: '<path d="M21 3.4L10.4 14M21 3.4l-6.7 17.2-3.9-6.6-6.6-3.9z" ' + L + '/><path d="M4.4 7h2.8M3 10.2h2" ' + A + '/>',
      trend: '<path d="M3 17l6-6 4 4 7-7" ' + L + '/><path d="M15 8h6v6" ' + A + '/>',
      users: '<circle cx="9" cy="7" r="4" ' + L + '/><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" ' + L + '/><path d="M16 3.13a4 4 0 0 1 0 7.75M23 21v-2a4 4 0 0 0-3-3.87" ' + A + '/>',
      userPlus: '<circle cx="9" cy="7" r="4" ' + L + '/><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" ' + L + '/><path d="M19 8v6M22 11h-6" ' + A + '/>',
      sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3" ' + L + '/><path d="M1 14h6M9 8h6M17 16h6" ' + A + '/>',
      refresh: '<path d="M23 4v6h-6" ' + A + '/><path d="M1 20v-6h6" ' + L + '/><path d="M3.5 9a9 9 0 0 1 14.8-3.4L23 10" ' + A + '/><path d="M1 14l4.7 4.4A9 9 0 0 0 20.5 15" ' + L + '/>',
      user: '<circle cx="12" cy="8" r="4" ' + L + '/><path d="M4 20a8 8 0 0 1 16 0" ' + A + '/>',
      lock: '<rect x="4" y="11" width="16" height="10" rx="2" ' + L + '/><path d="M8 11V7a4 4 0 0 1 8 0v4" ' + A + '/>',
    };
    window.HQIconDuoPaths = D;
  })();
  function iconDuo(name, w, accent){
    var p=(window.HQIconDuoPaths||{})[name];
    if(!p) return icon(name, w);
    p=p.split('__ACC__').join(accent||'#e7b24c');
    return '<svg viewBox="0 0 24 24" fill="none" stroke-linecap="round" stroke-linejoin="round" style="width:'+(w||'100%')+';height:'+(w||'100%')+'">'+p+'</svg>';
  }

  var I = {
    home:'<path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/>',
    search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/>',
    image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/>',
    video:'<rect x="2" y="6" width="14" height="12" rx="2"/><path d="M22 8l-6 4 6 4z"/>',
    mic:'<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v4"/>',
    edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    clapper:'<path d="M3 9h18v11H3zM3 9l2-5h16l-2 5M7 4l3 5M14 4l3 5"/><path d="M9 13l5 3-5 3z"/>',
    layers:'<path d="M12 2l9 5-9 5-9-5z"/><path d="M3 12l9 5 9-5M3 17l9 5 9-5"/>',
    folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    coins:'<circle cx="8" cy="8" r="6"/><path d="M18.1 6.6A6 6 0 1 1 9.9 18.5"/><path d="M7 6h1v4M16.7 12.6h1v4"/>',
    sparkles:'<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 14l.7 1.9L22 17l-2.3.6L19 20l-.7-2.4L16 17l2.3-.5z"/>',
    gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 2.6 15a1.6 1.6 0 0 0-1.5-1H1a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 2.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 7 4.6h.1A1.6 1.6 0 0 0 8 3.1V3a2 2 0 1 1 4 0v.1A1.6 1.6 0 0 0 15 4.6a1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V7a1.6 1.6 0 0 0 1.5 1h.1a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>',
    bell:'<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    chevron:'<path d="M9 18l6-6-6-6"/>',
    chevronDown:'<path d="M6 9l6 6 6-6"/>',
    menu:'<path d="M3 12h18M3 6h18M3 18h18"/>',
    arrowMini:'<path d="M5 12h14M13 6l6 6-6 6"/>',
    calendar:'<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    sliders:'<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    refresh:'<path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.8-3.4L23 10M1 14l4.7 4.4A9 9 0 0 0 20.5 15"/>',
    message:'<path d="M21 11.5a8.4 8.4 0 0 1-9 8 9 9 0 0 1-4-1l-5 1 1-4a8.4 8.4 0 0 1-1-4 8.4 8.4 0 0 1 9-8 8.4 8.4 0 0 1 9 8z"/>',
    check:'<path d="M20 6L9 17l-5-5"/>',
    checkCircle:'<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4L12 14.01l-3-3"/>',
    alert:'<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    send:'<path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    trend:'<path d="M3 17l6-6 4 4 7-7"/><path d="M17 8h4v4"/>',
    download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
    play:'<path d="M7 4v16l13-8z"/>',
    plus:'<path d="M12 5v14M5 12h14"/>',
    logout:'<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
    link:'<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'
  };
  function icon(name, w){
    var p=I[name]||I.search;
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:'+(w||'100%')+';height:'+(w||'100%')+'">'+p+'</svg>';
  }
  function escapeHtml(value){
    return String(value == null ? '' : value)
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;')
      .replace(/'/g,'&#39;')
      .replace(/\//g,'&#x2F;');
  }
  function escapeAttr(value){ return escapeHtml(value); }
  function safeUrl(value){
    var s=String(value == null ? '' : value).trim();
    if(!s) return '';
    if(/^\/\//.test(s)) return '#';                                    // 协议相对 //host → 挡(防跳外站)
    if(/^[a-z][a-z0-9+.-]*:/i.test(s)){                                // 带 scheme(有协议冒号)
      return /^(https?|mailto|tel):/i.test(s) ? s : '#';              // 仅放行 http(s)/mailto/tel；js/data/vbscript 等挡
    }
    return s;                                                          // 无 scheme = 相对路径(banana.html/./x/../y//abs) → 放行(修全站导航href变#的回归)
  }

  // 角色分化：仅 今日(运营台)+成本(统计看板) 是管理员专属；其余功能(含获客)所有用户都有；用户侧导以灵感为首页
  var NAV=[
    {k:'dashboard',l:'今日',i:'home', admin:true}, {k:'inspiration',l:'灵感设计',i:'sparkles'},
    {k:'leads',l:'平台获客',i:'search'}, {k:'collect',l:'内容爬取',i:'link'}, {k:'banana',l:'图片生成',i:'image'},
    {k:'video',l:'视频生成',i:'video'}, {k:'audio',l:'音频生成',i:'mic'}, {k:'script',l:'文案编导',i:'edit'},
    {k:'short-drama',l:'短剧创作',i:'clapper'}, {k:'canvas',l:'无限画布',i:'layers'}, {k:'assets',l:'我的资产',i:'folder'}, {k:'pricing',l:'点数价格',i:'coins'}, {k:'invite',l:'邀请中心',i:'users'},
    {k:'cost',l:'成本',i:'coins', admin:true}, {k:'tutorials',l:'教程视频',i:'play'}, {k:'settings',l:'通用设置',i:'gear'}
  ];

  // 管理员判定：已登录则一律以真实账号角色(hq_user.role)为准，忽略测试开关；
  // 仅在"未登录预览"时才用 ?admin=1/0 测试开关(写入 hq_role)。
  function isAdmin(){
    try{
      var q=new URLSearchParams(location.search);
      if(q.get('admin')==='1') localStorage.setItem('hq_role','admin');
      if(q.get('admin')==='0') localStorage.removeItem('hq_role');
      var u=JSON.parse(localStorage.getItem('hq_user')||'null');
      if(u) return u.role==='admin';                    // 已登录：真实角色说了算
      return localStorage.getItem('hq_role')==='admin';  // 未登录：仅预览开关
    }catch(e){ return false; }
  }

  function navHTML(active){
    var admin=isAdmin();
    return NAV.filter(function(it){ return admin || !it.admin; }).map(function(it){
      var on=it.k===active;
      var ntxt=on?'#eaf1fa':'#94a4bb', nbg=on?'rgba(231,178,76,.08)':'transparent', nfg=on?'#e7b24c':'#94a4bb', nbar=on?'1':'0';
      return '<a href="'+escapeAttr(safeUrl(it.k+'.html'))+'" class="hq-navitem" aria-label="'+escapeAttr(it.l)+'" data-nav-label="'+escapeAttr(it.l)+'" style="position:relative; display:flex; align-items:center; gap:12px; padding:10px 13px; border-radius:11px; cursor:pointer; color:'+ntxt+'; background:'+nbg+'; font-size:14px; font-weight:500; transition:.16s;">'+
        '<span class="hq-nav-active-bar" style="position:absolute; left:-12px; top:50%; transform:translateY(-50%); width:3px; height:18px; border-radius:0 3px 3px 0; background:#e7b24c; opacity:'+nbar+';"></span>'+
        '<span class="hq-nav-icon" style="display:flex; width:18px; flex:none; opacity:'+(on?'1':'.55')+'; transition:.16s;">'+iconDuo(it.i)+'</span><span class="hq-nav-label">'+escapeHtml(it.l)+'</span></a>';
    }).join('');
  }

  function ensureNavStyles(){
    if(document.getElementById('hqNavShellStyles')) return;
    var st=document.createElement('style');
    st.id='hqNavShellStyles';
    st.textContent=
      '.hq-aside{--hq-nav-x:50%;--hq-nav-y:50%;position:relative;isolation:isolate;overflow:hidden;transition:width .16s cubic-bezier(.16,1,.3,1),box-shadow .24s ease}'+
      '.hq-aside:before{content:"";position:absolute;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(170px circle at var(--hq-nav-x) var(--hq-nav-y),rgba(231,178,76,.14),rgba(45,212,191,.035) 42%,transparent 72%);opacity:0;transition:opacity .24s ease}'+
      '.hq-aside.hq-nav-awake{box-shadow:inset -1px 0 rgba(231,178,76,.2),12px 0 34px rgba(0,0,0,.12)!important}'+
      '.hq-aside.hq-nav-awake:before{opacity:1}'+
      '.hq-nav-label,.hq-brand-copy,.hq-side-bots-label,.hq-side-bots-arrow,.hq-user-copy,.hq-user-logout{transition:opacity .1s ease}'+
      '.hq-navitem{isolation:isolate;overflow:hidden}'+
      '.hq-navitem:after{content:"";position:absolute;inset:2px;border-radius:9px;background:linear-gradient(105deg,transparent 18%,rgba(231,178,76,.11) 48%,transparent 78%);opacity:0;transform:translateX(-110%);pointer-events:none}'+
      '.hq-navitem:hover,.hq-navitem:focus-visible{background:rgba(231,178,76,.075)!important;color:#eaf1fa!important;outline:none}'+
      '.hq-navitem:hover:after,.hq-navitem:focus-visible:after{animation:hq-nav-scan .72s cubic-bezier(.22,1,.36,1)}'+
      '.hq-navitem:hover .hq-nav-icon,.hq-navitem:focus-visible .hq-nav-icon{opacity:1!important;filter:drop-shadow(0 0 7px rgba(231,178,76,.42));animation:hq-nav-bloom .62s cubic-bezier(.22,1,.36,1)}'+
      '.hq-navitem:hover .hq-nav-icon svg>*,.hq-navitem:focus-visible .hq-nav-icon svg>*{animation:hq-nav-draw .72s cubic-bezier(.22,1,.36,1)}'+
      '.hq-nav-icon{position:relative;z-index:1;transform-origin:center;will-change:transform,filter}'+
      '.hq-nav-icon:before,.hq-nav-icon:after{content:"";position:absolute;left:50%;top:50%;width:3px;height:3px;border-radius:50%;background:#f4cd72;box-shadow:0 0 7px rgba(244,205,114,.8);opacity:0;pointer-events:none}'+
      '.hq-navitem:hover .hq-nav-icon:before,.hq-navitem:focus-visible .hq-nav-icon:before{animation:hq-nav-seed-a .68s ease-out}'+
      '.hq-navitem:hover .hq-nav-icon:after,.hq-navitem:focus-visible .hq-nav-icon:after{animation:hq-nav-seed-b .74s .04s ease-out}'+
      '@keyframes hq-nav-scan{0%{opacity:0;transform:translateX(-110%)}35%{opacity:1}100%{opacity:0;transform:translateX(110%)}}'+
      '@keyframes hq-nav-bloom{0%{transform:scale(.86) rotate(-5deg)}55%{transform:scale(1.18) rotate(3deg)}100%{transform:scale(1) rotate(0)}}'+
      '@keyframes hq-nav-draw{0%{stroke-dasharray:2 32;stroke-dashoffset:18;opacity:.35}100%{stroke-dasharray:48 0;stroke-dashoffset:0;opacity:1}}'+
      '@keyframes hq-nav-seed-a{0%{opacity:0;transform:translate(-50%,-50%) scale(.4)}30%{opacity:1}100%{opacity:0;transform:translate(10px,-12px) scale(1.2)}}'+
      '@keyframes hq-nav-seed-b{0%{opacity:0;transform:translate(-50%,-50%) scale(.4)}30%{opacity:.9}100%{opacity:0;transform:translate(-13px,8px) scale(.8)}}'+
      '.hq-aside-compact{width:68px!important}'+
      '.hq-aside-compact .hq-brand{justify-content:center!important;padding:20px 0 18px!important}'+
      '.hq-aside-compact .hq-brand-copy{display:none!important}'+
      '.hq-aside-compact .hq-side-nav{align-items:center;padding:6px 12px!important}'+
      '.hq-aside-compact .hq-navitem{width:44px;height:42px;justify-content:center;gap:0!important;padding:0!important;box-sizing:border-box}'+
      '.hq-aside-compact .hq-nav-label{display:none!important}'+
      '.hq-aside-compact .hq-nav-active-bar{left:-12px!important}'+
      '.hq-aside-compact .hq-side-footer{align-items:center;padding:12px!important}'+
      '.hq-aside-compact .hq-side-points{display:none!important}'+
      '.hq-aside-compact .hq-side-bots{width:44px;height:42px;justify-content:center;padding:0!important;box-sizing:border-box}'+
      '.hq-aside-compact .hq-side-bots-label,.hq-aside-compact .hq-side-bots-arrow{display:none!important}'+
      '.hq-aside-compact #hqUserCard{width:44px}'+
      '.hq-aside-compact .hq-user-row,.hq-aside-compact .hq-login-row{justify-content:center!important;padding:5px!important}'+
      '.hq-aside-compact .hq-user-copy,.hq-aside-compact .hq-user-logout{display:none!important}'+
      '.hq-account-trigger{color:inherit;font:inherit}.hq-account-trigger:hover,.hq-account-trigger:focus-visible{background:rgba(231,178,76,.08)!important;outline:1px solid rgba(231,178,76,.28)}'+
      '.hq-account-avatar{position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 32% 26%,#f6d488,#e7b24c 46%,#a8721f 100%);color:#1a1206;box-shadow:inset 0 1px 0 rgba(255,255,255,.45),inset 0 -2px 4px rgba(0,0,0,.28),0 2px 8px rgba(0,0,0,.35)}'+
      '.hq-account-avatar img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}'+
      '.hq-account-menu{position:fixed;z-index:9600;width:226px;padding:7px;border:1px solid rgba(148,164,187,.2);border-radius:13px;background:#111827;box-shadow:0 18px 48px rgba(0,0,0,.42);opacity:0;visibility:hidden;transform:translateY(-4px);transition:opacity .12s ease,transform .12s ease,visibility .12s}'+
      '.hq-account-menu.on{opacity:1;visibility:visible;transform:none}'+
      '.hq-account-menu-head{padding:10px 11px 9px;border-bottom:1px solid rgba(148,164,187,.12);margin-bottom:5px}'+
      '.hq-account-menu a,.hq-account-menu button{width:100%;box-sizing:border-box;display:flex;align-items:center;gap:10px;padding:10px 11px;border:0;border-radius:9px;background:transparent;color:#cbd5e1;font:600 13px inherit;text-decoration:none;cursor:pointer;text-align:left}'+
      '.hq-account-menu a:hover,.hq-account-menu a:focus-visible,.hq-account-menu button:hover,.hq-account-menu button:focus-visible{background:rgba(231,178,76,.09);color:#fff;outline:none}'+
      '.hq-account-menu .danger{color:#f490a5}'+
      '.hq-main-scroll{flex:1;min-height:0;overflow-y:auto;padding:26px 30px 40px}'+
      '.hq-main-scroll-flush{overflow:hidden;padding:0}'+
      '.hq-topbar-flush{border-bottom:0!important}'+
      '.hq-nav-tooltip{position:fixed;z-index:9500;max-width:220px;padding:7px 10px;border:1px solid rgba(148,164,187,.18);border-radius:7px;background:#141b28;color:#eaf1fa;box-shadow:0 8px 24px rgba(0,0,0,.3);font:600 12px/1.25 inherit;white-space:nowrap;pointer-events:none;opacity:0;transform:translateX(-4px);transition:opacity .1s ease,transform .1s ease}'+
      '.hq-nav-tooltip.on{opacity:1;transform:none}'+
      '@media(max-width:1100px){.hq-main-scroll-flush{overflow-y:auto}}'+
      '@media(max-width:899px){.hq-aside{width:228px!important}.hq-nav-tooltip{display:none!important}}'+
      '@media(prefers-reduced-motion:reduce){.hq-aside,.hq-aside:before,.hq-nav-tooltip{transition:none!important}.hq-aside:before{display:none}.hq-navitem:after,.hq-nav-icon,.hq-nav-icon svg>*,.hq-nav-icon:before,.hq-nav-icon:after{animation:none!important}}';
    document.head.appendChild(st);
  }

  function navDisplayMode(active,narrow){
    return narrow || active==='inspiration' ? 'expanded' : 'compact';
  }

  function usesFlushWorkspace(active){
    return active==='banana' || active==='video' || active==='audio';
  }

  function bindNavTooltips(aside){
    var tip=document.getElementById('hqNavTooltip');
    if(!tip){
      tip=document.createElement('div');
      tip.id='hqNavTooltip';
      tip.className='hq-nav-tooltip';
      tip.setAttribute('role','tooltip');
      document.body.appendChild(tip);
    }
    function hide(){ tip.classList.remove('on'); }
    function show(item){
      if(!aside.classList.contains('hq-aside-compact')){ hide(); return; }
      var label=item.getAttribute('data-nav-label')||'';
      if(!label){ hide(); return; }
      tip.textContent=label;
      var rect=item.getBoundingClientRect();
      tip.style.left=Math.round(rect.right+10)+'px';
      tip.style.top=Math.round(rect.top+(rect.height-tip.offsetHeight)/2)+'px';
      tip.classList.add('on');
    }
    aside.querySelectorAll('[data-nav-label]').forEach(function(item){
      item.addEventListener('mouseenter',function(){ show(item); });
      item.addEventListener('mouseleave',hide);
      item.addEventListener('focusin',function(){ show(item); });
      item.addEventListener('focusout',hide);
    });
    aside.addEventListener('scroll',hide,true);
    window.addEventListener('resize',hide);
  }

  function bindNavMotion(aside){
    var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)');
    function wake(){ if(!reduce||!reduce.matches) aside.classList.add('hq-nav-awake'); }
    function rest(){ aside.classList.remove('hq-nav-awake'); }
    aside.addEventListener('pointerenter',wake);
    aside.addEventListener('pointermove',function(e){
      if(reduce&&reduce.matches) return;
      var r=aside.getBoundingClientRect();
      aside.style.setProperty('--hq-nav-x',Math.round(e.clientX-r.left)+'px');
      aside.style.setProperty('--hq-nav-y',Math.round(e.clientY-r.top)+'px');
    });
    aside.addEventListener('pointerleave',rest);
    aside.addEventListener('focusin',wake);
    aside.addEventListener('focusout',function(e){ if(!aside.contains(e.relatedTarget)) rest(); });
  }

  function build(){
    var content=document.querySelector('.hq-content');
    if(!content) return;
    ensureNavStyles();
    var active=content.getAttribute('data-active')||'';
    var app=document.querySelector('.hq-app');

    var aside=document.createElement('aside');
    aside.className='hq-aside';
    aside.id='hqSideNav';
    aside.style.cssText='width:228px; flex:none; display:flex; flex-direction:column; border-right:1px solid rgba(148,164,187,.08); background:linear-gradient(180deg, rgba(12,18,32,.95), rgba(8,12,20,.95)); backdrop-filter:blur(12px); z-index:40;';
    var initialMode=navDisplayMode(active,window.innerWidth<900);
    aside.classList.toggle('hq-aside-compact',initialMode==='compact');
    aside.setAttribute('data-nav-mode',initialMode);
    aside.innerHTML=
      '<a href="../index.html" class="hq-brand" aria-label="黄雀 AI 首页" style="display:flex; align-items:center; gap:10px; padding:20px 22px 18px; cursor:pointer;">'+
        '<div style="height:28px; display:flex; align-items:center;"><img src="../assets/cloud/logo-bird.png" alt="黄雀" style="height:100%;width:auto;display:block;filter:drop-shadow(0 0 6px rgba(231,178,76,.4));"></div>'+
        '<div class="hq-brand-copy" style="font-size:17px; font-weight:700; letter-spacing:.4px; white-space:nowrap;">黄雀 <span style="color:#94a4bb; font-weight:400;">AI</span></div></a>'+
      '<nav class="hq-side-nav" style="flex:1; overflow-y:auto; padding:6px 12px; display:flex; flex-direction:column; gap:2px;">'+navHTML(active)+'</nav>'+
      '<div class="hq-side-footer" style="padding:12px 14px; display:flex; flex-direction:column; gap:11px;">'+
        '<div class="hq-side-points" style="position:relative; padding:15px 16px; border:1px solid rgba(231,178,76,.2); border-radius:14px; background:linear-gradient(150deg, rgba(231,178,76,.1), rgba(231,178,76,.02)); overflow:hidden;">'+
          '<div style="position:absolute; right:-14px; top:-10px; width:62px; height:62px; color:rgba(231,178,76,.22);">'+icon('coins','62px')+'</div>'+
          '<div style="font-size:12px; color:#94a4bb;">剩余点数</div>'+
          '<div id="hqPointsSide" class="mono" style="font-size:30px; font-weight:700; color:#e7b24c; line-height:1.1; margin:3px 0 9px;">—</div>'+
          '<div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap;"><a href="recharge.html" style="display:inline-flex; align-items:center; gap:5px; font-size:12.5px; color:#e7b24c; cursor:pointer; font-weight:600;">去充值 <span style="display:flex; width:13px;">'+icon('arrowMini')+'</span></a><button type="button" data-points-detail="1" style="border:0;background:transparent;color:#94a4bb;cursor:pointer;font:700 12.5px inherit;padding:0;">明细</button></div></div>'+
        '<a href="bots.html" class="hq-side-bots" aria-label="Bot 在线" title="Bot 在线" style="display:flex; align-items:center; gap:8px; padding:10px 14px; border:1px solid rgba(45,212,191,.2); border-radius:12px; background:rgba(45,212,191,.05);">'+
          '<span style="width:7px; height:7px; border-radius:50%; background:#2dd4bf; box-shadow:0 0 8px #2dd4bf; animation:hq-pulse 2s infinite;"></span>'+
          '<span class="hq-side-bots-label" style="font-size:13px; color:#94a4bb; flex:1; white-space:nowrap;"><span class="mono" style="color:#2dd4bf; font-weight:600;">34</span> 个 Bot 在线</span>'+
          '<span class="hq-side-bots-arrow" style="display:flex; width:13px; color:#2dd4bf;">'+icon('arrowMini')+'</span></a>'+
        '<div id="hqUserCard"></div>'+
      '</div>';

    var header=document.createElement('header');
    header.className='hq-topbar'+(usesFlushWorkspace(active)?' hq-topbar-flush':'');
    header.style.cssText='flex:none; display:flex; align-items:center; gap:14px; padding:14px 26px; border-bottom:1px solid rgba(148,164,187,.08); background:rgba(7,11,19,.4); backdrop-filter:blur(12px);';
    header.innerHTML=
      '<button type="button" class="hq-burger" aria-label="打开导航" aria-controls="hqSideNav" aria-expanded="false" style="display:none; width:38px; height:38px; align-items:center; justify-content:center; flex:none; color:#94a4bb; background:rgba(148,164,187,.05); border:1px solid rgba(148,164,187,.14); border-radius:11px; cursor:pointer;">'+icon('menu','18px')+'</button>'+
      '<div class="hq-botpill" style="display:flex; align-items:center; gap:8px; padding:8px 13px; border:1px solid rgba(45,212,191,.2); border-radius:11px; background:rgba(45,212,191,.05);">'+
        '<span style="width:7px; height:7px; border-radius:50%; background:#2dd4bf; box-shadow:0 0 8px #2dd4bf; animation:hq-pulse 2s infinite;"></span>'+
        '<span style="font-size:13px; color:#94a4bb;"><span class="mono" style="color:#2dd4bf; font-weight:600;">34</span> 个 Bot 在线</span></div>'+
      '<div style="margin-left:auto; display:flex; align-items:center; gap:12px;">'+
        '<a href="recharge.html" style="display:flex; align-items:center; gap:8px; padding:8px 14px; border:1px solid rgba(231,178,76,.26); border-radius:11px; background:rgba(231,178,76,.07); cursor:pointer;">'+
          '<span style="width:16px; height:16px; border-radius:50%; background:radial-gradient(circle at 35% 30%, #f6d488, #c8902f); flex:none;"></span>'+
          '<span id="hqPointsTop" class="mono" style="font-size:14px; font-weight:700; color:#e7b24c;">—</span></a>'+
        '<button type="button" data-points-detail="1" style="height:36px;padding:0 12px;border-radius:10px;cursor:pointer;font-family:inherit;font-size:12.5px;font-weight:700;color:#94a4bb;background:rgba(148,164,187,.06);border:1px solid rgba(148,164,187,.14);">明细</button>'+
        '<button type="button" class="hq-friends-btn" aria-label="打开好友" title="好友" aria-expanded="false" style="position:relative; width:38px; height:38px; display:flex; align-items:center; justify-content:center; border:1px solid rgba(148,164,187,.14); border-radius:11px; cursor:pointer; color:#94a4bb; background:transparent; font:inherit; padding:0;">'+iconDuo('users','18px','#e7b24c')+'<span class="hq-friends-badge" aria-label="0 条好友申请"></span></button>'+
        '<button type="button" class="hq-notify-btn" aria-label="打开消息中心" title="消息中心" aria-expanded="false" style="position:relative; width:38px; height:38px; display:flex; align-items:center; justify-content:center; border:1px solid rgba(148,164,187,.14); border-radius:11px; cursor:pointer; color:#94a4bb; background:transparent; font:inherit; padding:0;">'+icon('bell','17px')+'<span class="hq-notify-badge" aria-label="0 条未读通知"></span></button>'+
        '<div id="hqAuthArea" style="display:flex; align-items:center; gap:8px;"></div></div>';

    // 重组 DOM：aside | main(topbar + scroll(content))
    var main=document.createElement('main');
    main.style.cssText='flex:1; min-width:0; display:flex; flex-direction:column; height:100vh;';
    var scroll=document.createElement('div');
    scroll.className='hq-main-scroll'+(usesFlushWorkspace(active)?' hq-main-scroll-flush':'');
    content.parentNode.removeChild(content);
    scroll.appendChild(content);
    main.appendChild(header); main.appendChild(scroll);
    app.style.cssText='height:100vh; display:flex; position:relative; z-index:1; overflow:hidden;';
    app.appendChild(aside); app.appendChild(main);
    bindNavTooltips(aside);
    bindNavMotion(aside);

    // 响应式：窄屏抽屉
    var burger=header.querySelector('.hq-burger');
    function applyResp(){
      var narrow=window.innerWidth<900;
      var navMode=navDisplayMode(active,narrow);
      aside.classList.toggle('hq-aside-compact',navMode==='compact');
      aside.setAttribute('data-nav-mode',navMode);
      header.querySelector('.hq-burger').style.display=narrow?'flex':'none';
      header.querySelector('.hq-botpill').style.display=narrow?'none':'flex';
      if(narrow){ aside.style.position='fixed'; aside.style.top=0; aside.style.bottom=0; aside.style.left=0; aside.style.transform='translateX(-100%)'; aside.style.boxShadow='0 0 60px rgba(0,0,0,.6)'; }
      else {
        open=false;
        if(burger) burger.setAttribute('aria-expanded','false');
        aside.style.position='static'; aside.style.transform='none'; aside.style.boxShadow='none';
      }
    }
    var open=false;
    function setNavOpen(next){
      open=!!next;
      aside.style.transform=open?'translateX(0)':'translateX(-100%)';
      if(burger) burger.setAttribute('aria-expanded',open?'true':'false');
    }
    burger.onclick=function(){ setNavOpen(!open); };
    var friendsBtn=header.querySelector('.hq-friends-btn');
    if(friendsBtn) friendsBtn.onclick=openFriendsPanel;
    var notifyBtn=header.querySelector('.hq-notify-btn');
    if(notifyBtn) notifyBtn.onclick=openNotificationPanel;
    window.addEventListener('resize',applyResp); applyResp();
    aside.querySelectorAll('a').forEach(function(a){ a.addEventListener('click',function(){ if(window.innerWidth<900) setNavOpen(false); }); });
    refreshPoints(); renderUser(); refreshNotificationBadge();
  }

  // 拉真实点数填到侧边+顶栏（生成后页面调 window.HQ.refreshPoints() 刷新）
  function authHeaders(extra){
    var h=extra||{};
    return h;
  }
  function refreshPoints(){
    fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store',headers:authHeaders()}).then(function(r){ if(!r.ok) return null; return r.json(); }).then(function(d){
      if(d&&d.user){ _accountAvatar=d.user.avatar||''; try{ localStorage.removeItem('hq_token'); localStorage.setItem('hq_user',JSON.stringify(d.user)); }catch(e){} renderUser(); }
      var p=d&&d.user&&d.user.points; if(p==null) return;
      var a=document.getElementById('hqPointsSide'), b=document.getElementById('hqPointsTop');
      if(a) a.textContent=p; if(b) b.textContent=p;
    }).catch(function(){});
  }

  // ===== 点数消费明细（全站共享入口）=====
  var _pointsState={days:30,kind:'',page:1,totalPages:1,loading:false};
  function ensurePointsModal(){
    if(document.getElementById('hqPointsOv')) return;
    var st=document.createElement('style');
    st.textContent=
      '.hqpo{position:fixed;inset:0;z-index:9100;display:none;align-items:center;justify-content:center;padding:22px;background:rgba(3,7,13,.72);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}'+
      '.hqpo.on{display:flex}'+
      '.hqpm{width:min(920px,96vw);max-height:min(760px,92vh);display:flex;flex-direction:column;border:1px solid rgba(231,178,76,.18);border-radius:16px;background:linear-gradient(180deg,rgba(16,24,39,.98),rgba(9,13,22,.98));box-shadow:0 32px 90px rgba(0,0,0,.54);overflow:hidden}'+
      '.hqph{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 20px;border-bottom:1px solid rgba(148,164,187,.1)}'+
      '.hqpf{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:14px 20px;border-bottom:1px solid rgba(148,164,187,.08);background:rgba(148,164,187,.025)}'+
      '.hqpf select{height:36px;border:1px solid rgba(148,164,187,.16);border-radius:9px;background:rgba(7,11,19,.58);color:#eaf1fa;font:13px inherit;padding:0 10px;outline:0}'+
      '.hqpt{min-height:220px;overflow:auto;padding:0 20px 14px}'+
      '.hqpr{display:grid;grid-template-columns:132px minmax(150px,1fr) 88px 84px 82px;gap:12px;align-items:center;min-height:46px;border-bottom:1px solid rgba(148,164,187,.08);font-size:12.5px;color:#cbd5e1}'+
      '.hqpr.head{position:sticky;top:0;z-index:1;background:#111827;color:#94a4bb;font-weight:800}'+
      '.hqpg{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 20px;border-top:1px solid rgba(148,164,187,.08)}'+
      '.hqpb{height:34px;padding:0 12px;border:1px solid rgba(148,164,187,.14);border-radius:9px;background:rgba(148,164,187,.05);color:#eaf1fa;font:700 12.5px inherit;cursor:pointer}'+
      '.hqpb:disabled{opacity:.45;cursor:not-allowed}'+
      '.hqpx{width:34px;height:34px;border:1px solid rgba(148,164,187,.16);border-radius:10px;background:rgba(148,164,187,.05);color:#94a4bb;cursor:pointer;font-size:20px;line-height:1}'+
      '@media(max-width:760px){.hqpm{width:96vw}.hqpr{grid-template-columns:1fr 62px;gap:6px;padding:10px 0}.hqpr.head{display:none}.hqpr>span:nth-child(1),.hqpr>span:nth-child(2),.hqpr>span:nth-child(4){grid-column:1/2}.hqpr>span:nth-child(3),.hqpr>span:nth-child(5){grid-column:2/3;text-align:right}.hqpt{padding:0 14px 12px}.hqph,.hqpf,.hqpg{padding-left:14px;padding-right:14px}}';
    document.head.appendChild(st);
    var ov=document.createElement('div');
    ov.className='hqpo'; ov.id='hqPointsOv';
    ov.innerHTML='<div class="hqpm" role="dialog" aria-modal="true" aria-label="点数明细">'+
      '<div class="hqph"><div><div style="font-size:18px;font-weight:800;color:#eaf1fa;">点数明细</div><div id="hqPointsSub" style="margin-top:5px;font-size:12px;color:#94a4bb;">查看最近消费记录</div></div><button type="button" class="hqpx" id="hqPointsClose" aria-label="关闭">&times;</button></div>'+
      '<div class="hqpf"><label style="font-size:12px;color:#94a4bb;">时间</label><select id="hqPointsDays"><option value="7">最近7天</option><option value="30" selected>最近30天</option><option value="90">最近90天</option><option value="365">最近一年</option></select><label style="font-size:12px;color:#94a4bb;">功能</label><select id="hqPointsKind"><option value="">全部功能</option></select><button type="button" class="hqpb" id="hqPointsReload">刷新</button></div>'+
      '<div class="hqpt" id="hqPointsRows"></div>'+
      '<div class="hqpg"><div id="hqPointsPage" style="font-size:12px;color:#94a4bb;">第 1 页</div><div style="display:flex;gap:8px;"><button type="button" class="hqpb" id="hqPointsPrev">上一页</button><button type="button" class="hqpb" id="hqPointsNext">下一页</button></div></div>'+
      '</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){ if(e.target===ov) closePointsModal(); });
    document.getElementById('hqPointsClose').onclick=closePointsModal;
    document.getElementById('hqPointsReload').onclick=function(){ _pointsState.page=1; loadPointsHistory(); };
    document.getElementById('hqPointsDays').onchange=function(){ _pointsState.days=this.value; _pointsState.page=1; loadPointsHistory(); };
    document.getElementById('hqPointsKind').onchange=function(){ _pointsState.kind=this.value; _pointsState.page=1; loadPointsHistory(); };
    document.getElementById('hqPointsPrev').onclick=function(){ if(_pointsState.page>1){ _pointsState.page--; loadPointsHistory(); } };
    document.getElementById('hqPointsNext').onclick=function(){ if(_pointsState.page<_pointsState.totalPages){ _pointsState.page++; loadPointsHistory(); } };
  }
  function fmtPointTime(sec){
    if(!sec) return '—';
    var d=new Date(Number(sec)*1000);
    if(isNaN(d.getTime())) return '—';
    return String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
  }
  function renderKindOptions(kinds){
    var sel=document.getElementById('hqPointsKind'); if(!sel) return;
    var cur=_pointsState.kind||'';
    sel.innerHTML='<option value="">全部功能</option>'+(kinds||[]).map(function(k){
      return '<option value="'+escapeAttr(k.kind||'')+'">'+escapeHtml(k.label||k.kind||'未知')+'（'+escapeHtml(k.count||0)+'）</option>';
    }).join('');
    sel.value=cur;
  }
  function renderPointsRows(data){
    var box=document.getElementById('hqPointsRows'); if(!box) return;
    _pointsState.totalPages=data.total_pages||1;
    var sub=document.getElementById('hqPointsSub');
    if(sub) sub.textContent='当前余额 '+(data.points==null?'—':data.points)+' 点 · 最近 '+data.days+' 天共 '+(data.total||0)+' 条';
    renderKindOptions(data.kinds||[]);
    if(!data.items || !data.items.length){
      box.innerHTML='<div style="padding:34px 10px;text-align:center;color:#5c6b82;font-size:13px;">暂无消费记录</div>';
    }else{
      box.innerHTML='<div class="hqpr head"><span>时间</span><span>功能</span><span>消耗</span><span>状态</span><span>任务ID</span></div>'+
        data.items.map(function(x){
          var cost='-'+(x.cost||0);
          var status=x.status_label||x.status||'未知';
          var color=x.refunded?'#2bd576':((x.status==='error'||x.status==='failed')?'#f4708a':'#cbd5e1');
          return '<div class="hqpr"><span class="mono">'+escapeHtml(fmtPointTime(x.created_at))+'</span><span>'+escapeHtml(x.func||x.kind||'未知')+'</span><span class="mono" style="color:#e7b24c;font-weight:800;">'+escapeHtml(cost)+'</span><span style="color:'+color+';">'+escapeHtml(status)+'</span><span class="mono">#'+escapeHtml(x.task_id||'')+'</span></div>';
        }).join('');
    }
    var page=document.getElementById('hqPointsPage');
    if(page) page.textContent='第 '+_pointsState.page+' / '+_pointsState.totalPages+' 页';
    var prev=document.getElementById('hqPointsPrev'), next=document.getElementById('hqPointsNext');
    if(prev) prev.disabled=_pointsState.page<=1;
    if(next) next.disabled=_pointsState.page>=_pointsState.totalPages;
  }
  function loadPointsHistory(){
    ensurePointsModal();
    if(_pointsState.loading) return;
    _pointsState.loading=true;
    var box=document.getElementById('hqPointsRows');
    if(box) box.innerHTML='<div style="padding:34px 10px;text-align:center;color:#94a4bb;font-size:13px;">正在读取点数明细...</div>';
    var q='?days='+encodeURIComponent(_pointsState.days||30)+'&kind='+encodeURIComponent(_pointsState.kind||'')+'&page='+encodeURIComponent(_pointsState.page||1)+'&page_size=20';
    fetch('/api/gen/points/history'+q,{credentials:'same-origin',cache:'no-store',headers:authHeaders()})
      .then(function(r){ if(r.status===401){ openLogin(); throw new Error('请先登录'); } return r.json().then(function(d){return {ok:r.ok,d:d};}); })
      .then(function(res){ if(!res.ok) throw new Error((res.d&&res.d.detail)||'读取失败'); renderPointsRows(res.d||{}); refreshPoints(); })
      .catch(function(e){ if(box) box.innerHTML='<div style="padding:34px 10px;text-align:center;color:#f4708a;font-size:13px;">'+escapeHtml(e.message||'读取失败')+'</div>'; })
      .finally(function(){ _pointsState.loading=false; });
  }
  function openPointsModal(){
    ensurePointsModal();
    var ov=document.getElementById('hqPointsOv');
    if(ov) ov.classList.add('on');
    loadPointsHistory();
  }
  function closePointsModal(){ var ov=document.getElementById('hqPointsOv'); if(ov) ov.classList.remove('on'); }
  document.addEventListener('click',function(e){
    var t=e.target.closest?e.target.closest('[data-points-detail]'):null;
    if(!t) return;
    e.preventDefault();
    openPointsModal();
  });

  // ===== 通知中心：任务结果、点数变化、系统公告 =====
  var _noticeState={kind:'all',items:[],loading:false};
  var _legacyNoticeReadSync={};
  var _announcementState={notice:null,busy:false,returnFocus:null,shown:{}};
  var _friendsPanelHandler=null;
  function registerFriendsPanel(handler){
    _friendsPanelHandler=typeof handler==='function'?handler:null;
  }
  function setFriendsPanelExpanded(on){
    var btn=document.querySelector('.hq-friends-btn');
    if(btn) btn.setAttribute('aria-expanded',on?'true':'false');
  }
  function updateFriendsBadge(count){
    var n=Math.max(0,Number(count||0));
    var badge=document.querySelector('.hq-friends-badge'), btn=document.querySelector('.hq-friends-btn');
    if(badge){
      badge.textContent=n>99?'99+':String(n||'');
      badge.classList.toggle('on',n>0);
      badge.setAttribute('aria-label',n+' 条好友申请');
    }
    if(btn) btn.setAttribute('aria-label',n?'打开好友，'+n+' 条待处理申请':'打开好友');
  }
  function openFriendsPanel(){
    if(_friendsPanelHandler){
      _friendsPanelHandler();
      return;
    }
    if(/settings\.html$/.test(location.pathname)){
      window.dispatchEvent(new CustomEvent('hq:open-friends'));
      return;
    }
    location.href='settings.html#friends';
  }
  var _systemNotices=[{
    id:'system-notification-center-v1',kind:'system',title:'通知中心已启用',
    detail:'生成结果、点数变化和重要系统公告会集中显示在这里。',time:Date.parse('2026-07-10T09:00:00+08:00')
  }];
  function noticeStoreKey(){
    var u=currentUser();
    return 'hq_notification_read_v1:'+(u&&u.username?u.username:'guest');
  }
  function readNoticeIds(){
    try{ var x=JSON.parse(localStorage.getItem(noticeStoreKey())||'[]'); return Array.isArray(x)?x:[]; }catch(e){ return []; }
  }
  function saveNoticeIds(ids){
    try{ localStorage.setItem(noticeStoreKey(),JSON.stringify((ids||[]).slice(-300))); }catch(e){}
  }
  function noticePage(kind){
    var pages={image:'banana.html',video:'video.html',tryon:'video.html',xiaole_video:'video.html',audio:'audio.html',leads:'leads.html',leadgen:'leads.html',copy:'script.html',collect:'collect.html'};
    return pages[kind]||'assets.html';
  }
  // 任务 kind → 资产库分类(catRow 的 data-cat)。已完成的任务，产物都在资产库里。
  function noticeAssetCat(kind){
    var cats={image:'image',video:'video',tryon:'video',xiaole_video:'video',audio:'audio',copy:'copy',leads:'leads',leadgen:'leads',collect:'collect'};
    return cats[kind]||'';
  }
  // 已完成 → 直接跳资产库并定位到这条产物（assets.html 读 ?task= 高亮）；
  // 失败/进行中 → 跳回对应生成页，方便重试或看进度。task_id 必须编码，否则含 & ? # 会串参。
  function noticeHref(x, status){
    var tid=String(x.task_id==null?'':x.task_id);
    var cat=noticeAssetCat(x.kind);
    if(status==='done' && tid && cat){
      return 'assets.html?cat='+encodeURIComponent(cat)+'&task='+encodeURIComponent(tid);
    }
    return noticePage(x.kind);
  }
  function ip12ProgressNotices(payload){
    var openModuleSteps=[5,5,5,5,4,3,3,4], openStepKeys=[];
    openModuleSteps.forEach(function(stepCount,moduleIndex){
      for(var stepIndex=0;stepIndex<stepCount;stepIndex++) openStepKeys.push(moduleIndex+'-'+stepIndex);
    });
    var projects=(payload&&payload.items)||[];
    var notices=[];
    projects.forEach(function(project){
      var questionnaire=project&&project.state&&project.state.questionnaire_state;
      var answers=questionnaire&&questionnaire.answers;
      if(!project||!project.id||!answers||typeof answers!=='object') return;
      var progressed=openStepKeys.filter(function(key){var answer=answers[key];return answer&&(answer.confirmed||answer.skipped);}).length;
      var skipped=openStepKeys.filter(function(key){return answers[key]&&answers[key].skipped;}).sort(function(a,b){
        var aa=a.split('-').map(Number), bb=b.split('-').map(Number); return aa[0]-bb[0]||aa[1]-bb[1];
      });
      if(progressed>=34&&!skipped.length) return;
      var target=skipped.length?skipped[0]:openStepKeys.find(function(key){var answer=answers[key];return !(answer&&(answer.confirmed||answer.skipped));})||'0-0';
      var indexes=target.split('-').map(Number), moduleIndex=indexes[0], stepIndex=indexes[1];
      notices.push({
        id:'ip12-progress-'+project.id+'-'+progressed+'-'+skipped.length,kind:'system',title:skipped.length?'IP12 有 '+skipped.length+' 项待补':'继续完善 IP12',
        detail:(project.title||'数字化 IP')+' · 首轮进度 '+progressed+'/34'+(skipped.length?'，补齐后方案会更准确。':'，继续完成即可解锁定制方案。'),
        time:Number(project.updated_at||project.created_at||0)*1000,
        href:'ip12.html?project='+encodeURIComponent(project.id)+'&module='+encodeURIComponent(moduleIndex+1)+'&step='+encodeURIComponent(stepIndex+1),
        action:skipped.length?'去回补':'继续填写',tone:'info'
      });
    });
    return notices;
  }
  function buildNotices(data){
    var read=readNoticeIds(), items=[];
    var stored=readAccountJson('hq_preferences_v1',currentUser()), prefs=stored.notifications||{};
    function enabled(name, fallback){ return typeof prefs[name]==='boolean'?prefs[name]:fallback; }
    (data&&data.items||[]).forEach(function(x){
      var status=String(x.status||'').toLowerCase();
      var failed=status==='error'||status==='failed', done=status==='done';
      var taskTitle=status==='done'?'生成任务已完成':((status==='error'||status==='failed')?'生成任务失败':(status==='running'?'任务生成中':'任务正在排队'));
      var showTask=(done&&enabled('taskSuccess',true))||(failed&&enabled('taskFailure',true))||(!done&&!failed&&enabled('taskProgress',false));
      if(showTask){
        items.push({id:'task-'+x.task_id+'-'+status+'-'+(x.updated_at||x.created_at||0),kind:'task',title:taskTitle,
          detail:(x.func||x.kind||'生成任务')+' · 任务 #'+(x.task_id||''),time:Number(x.updated_at||x.created_at||0)*1000,
          href:noticeHref(x,status),action:done?'查看产物':'查看任务',tone:failed?'error':(done?'success':'info')});
      }
      if(Number(x.cost||0)>0 && enabled('pointsChanges',true)){
        items.push({id:'points-'+x.task_id+'-'+(x.refunded?'refund':'cost'),kind:'points',title:x.refunded?'任务点数已退回':'点数已扣除',
          detail:(x.func||x.kind||'生成任务')+' · '+(x.refunded?'退回 ':'消耗 ')+Number(x.cost||0)+' 点',time:Number(x.updated_at||x.created_at||0)*1000,
          action:'查看明细',points:true,tone:x.refunded?'success':'points'});
      }
    });
    if(enabled('systemNotices',true)){
      (data&&data.ip12_skips||[]).forEach(function(x){ items.push(x); });
      _systemNotices.forEach(function(x){ items.push(x); });
    }
    (data&&data.system_notices||[]).forEach(function(x){
      var announcement=String(x.kind||'system')==='announcement';
      if(!announcement&&!enabled('systemNotices',true)) return;
      items.push({
        id:'server-notice-'+x.id,serverId:Number(x.id||0),campaignId:Number(x.campaign_id||0),
        kind:'system',serverKind:String(x.kind||'system'),isAnnouncement:announcement,
        title:x.title||'系统通知',detail:x.detail||'',time:Number(x.created_at||0)*1000,
        serverReadAt:x.read_at||null,snoozedUntil:x.popup_snoozed_until||null,
        popupUntil:x.popup_until||null
      });
    });
    items.forEach(function(x){
      x.read=x.serverId?!!x.serverReadAt:read.indexOf(x.id)>=0;
      if(!x.isAnnouncement&&!x.read&&x.serverId&&read.indexOf(x.id)>=0){x.read=true;syncLegacyNoticeRead(x)}
    });
    return items.sort(function(a,b){ return Number(b.time||0)-Number(a.time||0); });
  }
  function formatNoticeTime(ms){
    var d=new Date(Number(ms||0)); if(isNaN(d.getTime())) return '刚刚';
    var now=new Date(), same=now.toDateString()===d.toDateString();
    if(same) return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
    return String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
  }
  function noticeIcon(x){
    if(x.kind==='points') return iconDuo('coins','18px',x.tone==='success'?'#2bd576':'#e7b24c');
    if(x.kind==='system') return iconDuo('message','18px','#60a5fa');
    if(x.tone==='error') return iconDuo('alert','18px','#f4708a');
    return iconDuo(x.tone==='success'?'checkCircle':'clock','18px',x.tone==='success'?'#2bd576':'#60a5fa');
  }
  function ensureNotificationPanel(){
    if(document.getElementById('hqNoticeOv')) return;
    var st=document.createElement('style');
    st.textContent=
      '.hq-notify-badge,.hq-friends-badge{display:none;position:absolute;top:-5px;right:-5px;min-width:14px;height:14px;padding:0 3px;align-items:center;justify-content:center;border:1.5px solid #080d16;border-radius:999px;background:#f4708a;color:#fff;font:800 8px/1 inherit;box-sizing:border-box;pointer-events:none}'+
      '.hq-notify-badge.on,.hq-friends-badge.on{display:flex}'+
      '.hqno{position:fixed;inset:0;z-index:9200;display:none;background:rgba(3,7,13,.46);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}'+
      '.hqno.on{display:block}'+
      '.hqnd{position:absolute;top:0;right:0;width:min(430px,100vw);height:100%;display:flex;flex-direction:column;border-left:1px solid rgba(148,164,187,.14);background:linear-gradient(180deg,#101827,#080d16);box-shadow:-28px 0 80px rgba(0,0,0,.46);animation:hq-notice-in .2s cubic-bezier(.16,1,.3,1)}'+
      '@keyframes hq-notice-in{from{transform:translateX(22px);opacity:.4}to{transform:none;opacity:1}}'+
      '.hqnh{display:flex;align-items:center;gap:12px;padding:20px;border-bottom:1px solid rgba(148,164,187,.1)}'+
      '.hqnt{display:flex;gap:6px;padding:10px 16px;border-bottom:1px solid rgba(148,164,187,.08);overflow:auto}'+
      '.hqnt button{height:32px;padding:0 12px;border:1px solid transparent;border-radius:8px;background:transparent;color:#94a4bb;font:700 12px inherit;white-space:nowrap;cursor:pointer}'+
      '.hqnt button.on{border-color:rgba(231,178,76,.22);background:rgba(231,178,76,.08);color:#e7b24c}'+
      '.hqnl{flex:1;overflow:auto;padding:4px 0}'+
      '.hqni{position:relative;display:grid;grid-template-columns:38px minmax(0,1fr);gap:11px;padding:15px 20px;border-bottom:1px solid rgba(148,164,187,.07);cursor:pointer;transition:background .16s}'+
      '.hqni:hover{background:rgba(148,164,187,.045)}'+
      '.hqni.unread{background:rgba(231,178,76,.035)}'+
      '.hqni.unread:before{content:"";position:absolute;left:8px;top:22px;width:6px;height:6px;border-radius:50%;background:#e7b24c}'+
      '.hqnic{width:36px;height:36px;display:flex;align-items:center;justify-content:center;border:1px solid rgba(148,164,187,.12);border-radius:8px;background:rgba(148,164,187,.045)}'+
      '.hqna{border:0;background:transparent;color:#e7b24c;font:700 12px inherit;cursor:pointer;padding:0}'+
      '.hqnx{width:34px;height:34px;border:1px solid rgba(148,164,187,.16);border-radius:8px;background:rgba(148,164,187,.05);color:#94a4bb;cursor:pointer;font-size:20px;line-height:1}'+
      '.hqne{padding:54px 24px;text-align:center;color:#5c6b82;font-size:13px}'+
      '@media(max-width:520px){.hqnh{padding:16px}.hqni{padding:14px 16px}.hqni.unread:before{left:5px}.hqnt{padding-left:12px;padding-right:12px}}';
    document.head.appendChild(st);
    var ov=document.createElement('div'); ov.className='hqno'; ov.id='hqNoticeOv';
    ov.innerHTML='<section class="hqnd" role="dialog" aria-modal="true" aria-labelledby="hqNoticeTitle">'+
      '<div class="hqnh"><div style="flex:1;min-width:0;"><div id="hqNoticeTitle" style="font-size:18px;font-weight:800;color:#eaf1fa;">消息中心</div><div style="margin-top:4px;font-size:12px;color:#94a4bb;">任务、点数与站内公告</div></div><button type="button" class="hqna" id="hqNoticeReadAll">全部已读</button><button type="button" class="hqnx" id="hqNoticeClose" aria-label="关闭消息中心">&times;</button></div>'+
      '<div class="hqnt" role="tablist"><button data-notice-kind="all" class="on">全部</button><button data-notice-kind="task">任务</button><button data-notice-kind="points">点数</button><button data-notice-kind="system">系统</button></div>'+
      '<div class="hqnl" id="hqNoticeList"><div class="hqne">正在读取通知...</div></div></section>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){ if(e.target===ov) closeNotificationPanel(); });
    document.getElementById('hqNoticeClose').onclick=closeNotificationPanel;
    document.getElementById('hqNoticeReadAll').onclick=markAllNoticesRead;
    ov.querySelectorAll('[data-notice-kind]').forEach(function(btn){ btn.onclick=function(){ _noticeState.kind=this.getAttribute('data-notice-kind')||'all'; renderNotices(); }; });
    document.getElementById('hqNoticeList').onclick=function(e){
      var row=e.target.closest?e.target.closest('[data-notice-id]'):null; if(!row) return;
      var x=_noticeState.items.find(function(it){ return it.id===row.getAttribute('data-notice-id'); }); if(!x) return;
      markNoticeRead(x);
      if(x.points){ closeNotificationPanel(); openPointsModal(); }
      else if(x.href) location.href=safeUrl(x.href);
    };
  }
  function renderNotices(){
    ensureNotificationPanel();
    document.querySelectorAll('[data-notice-kind]').forEach(function(btn){ btn.classList.toggle('on',btn.getAttribute('data-notice-kind')===_noticeState.kind); });
    var list=document.getElementById('hqNoticeList'); if(!list) return;
    var items=_noticeState.items.filter(function(x){ return _noticeState.kind==='all'||x.kind===_noticeState.kind; });
    if(!items.length){ list.innerHTML='<div class="hqne">这里暂时没有通知</div>'; updateNotificationBadge(); return; }
    list.innerHTML=items.map(function(x){
      return '<div class="hqni '+(x.read?'':'unread')+'" data-notice-id="'+escapeAttr(x.id)+'" tabindex="0">'+
        '<div class="hqnic">'+noticeIcon(x)+'</div><div style="min-width:0;"><div style="display:flex;align-items:center;gap:10px;"><div style="flex:1;font-size:13.5px;font-weight:750;color:#eaf1fa;">'+escapeHtml(x.title)+'</div><time class="mono" style="font-size:10.5px;color:#5c6b82;white-space:nowrap;">'+escapeHtml(formatNoticeTime(x.time))+'</time></div>'+
        '<div style="margin-top:5px;font-size:12px;line-height:1.55;color:#94a4bb;white-space:pre-wrap;overflow-wrap:anywhere;">'+escapeHtml(x.detail||'')+'</div>'+(x.action?'<div style="margin-top:8px;font-size:11.5px;font-weight:700;color:#e7b24c;">'+escapeHtml(x.action)+' →</div>':'')+'</div></div>';
    }).join('');
    list.querySelectorAll('[data-notice-id]').forEach(function(row){ row.addEventListener('keydown',function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); row.click(); } }); });
    updateNotificationBadge();
  }
  function loadNotices(){
    ensureNotificationPanel(); if(_noticeState.loading) return;
    _noticeState.loading=true;
    Promise.all([
      fetch('/api/gen/points/history?days=30&page=1&page_size=20',{credentials:'same-origin',cache:'no-store',headers:authHeaders()})
        .then(function(r){ if(r.status===401) return {items:[]}; return r.ok?r.json():Promise.reject(new Error('读取任务通知失败')); }).catch(function(){return {items:[]};}),
      fetch('/api/auth/notifications?limit=50',{credentials:'same-origin',cache:'no-store',headers:authHeaders()})
        .then(function(r){ if(r.status===401) return {items:[]}; return r.ok?r.json():Promise.reject(new Error('读取系统通知失败')); }).catch(function(){return {items:[]};}),
      fetch('/api/gen/digital-ip/projects',{credentials:'same-origin',cache:'no-store',headers:authHeaders()})
        .then(function(r){ if(r.status===401) return {items:[]}; return r.ok?r.json():Promise.reject(new Error('读取 IP12 提醒失败')); }).catch(function(){return {items:[]};})
    ])
      .then(function(all){ var d=all[0]||{}; d.system_notices=(all[1]&&all[1].items)||[]; d.ip12_skips=ip12ProgressNotices(all[2]); _noticeState.items=buildNotices(d); renderNotices(); maybeOpenAnnouncement(); })
      .catch(function(){ _noticeState.items=buildNotices({items:[]}); renderNotices(); })
      .finally(function(){ _noticeState.loading=false; });
  }
  function notificationStateRequest(path){
    return fetch(path,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:'{}'})
      .then(function(r){ return r.json().catch(function(){return {};}).then(function(d){ if(!r.ok){var e=new Error(d.detail||'通知状态保存失败');e.status=r.status;throw e} return d; }); });
  }
  function syncLegacyNoticeRead(x){
    if(!x||!x.serverId||_legacyNoticeReadSync[x.serverId])return;
    _legacyNoticeReadSync[x.serverId]=true;
    notificationStateRequest('/api/auth/notifications/'+encodeURIComponent(x.serverId)+'/read').then(function(d){x.serverReadAt=(d.notification&&d.notification.read_at)||Math.floor(Date.now()/1000)}).catch(function(){delete _legacyNoticeReadSync[x.serverId]});
  }
  function markNoticeRead(notice){
    var x=typeof notice==='string'?_noticeState.items.find(function(it){return it.id===notice;}):notice;
    if(!x) return Promise.resolve();
    if(x.serverId){
      return notificationStateRequest('/api/auth/notifications/'+encodeURIComponent(x.serverId)+'/read').then(function(d){
        x.read=true; x.serverReadAt=(d.notification&&d.notification.read_at)||Math.floor(Date.now()/1000);
        updateNotificationBadge(); renderNotices();
      }).catch(function(){ return null; });
    }
    var ids=readNoticeIds(); if(ids.indexOf(x.id)<0){ ids.push(x.id); saveNoticeIds(ids); }
    x.read=true; updateNotificationBadge(); renderNotices();
    return Promise.resolve();
  }
  function markAllNoticesRead(){
    var btn=document.getElementById('hqNoticeReadAll'); if(btn) btn.disabled=true;
    notificationStateRequest('/api/auth/notifications/read-all').then(function(){ return true; }).catch(function(){return false;}).then(function(serverSaved){
      var ids=readNoticeIds(); _noticeState.items.forEach(function(x){
        if(!x.serverId&&ids.indexOf(x.id)<0) ids.push(x.id);
        if(!x.serverId||serverSaved) x.read=true;
      });
      saveNoticeIds(ids); updateNotificationBadge(); renderNotices();
    }).finally(function(){ if(btn) btn.disabled=false; });
  }
  function notificationEpochMs(value){
    if(value==null||value==='') return 0;
    var numeric=Number(value);
    if(Number.isFinite(numeric)&&numeric>0) return numeric<1000000000000?numeric*1000:numeric;
    var parsed=Date.parse(String(value));
    return Number.isFinite(parsed)?parsed:0;
  }
  function eligibleAnnouncement(x){
    if(!x||!x.isAnnouncement||x.read) return false;
    var now=Date.now(), snoozed=notificationEpochMs(x.snoozedUntil), until=notificationEpochMs(x.popupUntil);
    return (!snoozed||snoozed<=now)&&(!until||until>now);
  }
  function ensureAnnouncementDialog(){
    if(document.getElementById('hqAnnouncementOv')) return;
    var st=document.createElement('style');
    st.id='hqAnnouncementStyles';
    st.textContent=
      '.hqao{position:fixed;inset:0;z-index:9400;display:none;align-items:center;justify-content:center;padding:28px;background:rgba(2,6,12,.78);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}'+
      '.hqao.on{display:flex}'+
      '.hqam{position:relative;width:min(800px,94vw);max-height:min(780px,92vh);display:flex;flex-direction:column;overflow:hidden;border:1px solid rgba(231,178,76,.3);border-radius:20px;background:linear-gradient(145deg,rgba(231,178,76,.1),rgba(16,24,39,.99) 32%,rgba(8,13,23,.99));box-shadow:0 42px 120px rgba(0,0,0,.68),inset 0 1px rgba(255,255,255,.045);animation:hq-announcement-in .24s cubic-bezier(.16,1,.3,1)}'+
      '.hqam:before{content:"";position:absolute;inset:0 0 auto;height:3px;background:linear-gradient(90deg,transparent,#e7b24c 28%,#2dd4bf 72%,transparent);opacity:.88}'+
      '.hqa-head{display:flex;align-items:flex-start;gap:16px;padding:30px 34px 18px}'+
      '.hqa-mark{width:46px;height:46px;display:flex;align-items:center;justify-content:center;flex:none;border:1px solid rgba(231,178,76,.24);border-radius:14px;background:rgba(231,178,76,.09);color:#e7b24c}'+
      '.hqa-eyebrow{color:#e7b24c;font-size:12px;font-weight:800;letter-spacing:.12em}'+
      '.hqa-title{margin:7px 0 0;color:#f5f7fa;font-size:clamp(24px,3vw,34px);line-height:1.2;overflow-wrap:anywhere}'+
      '.hqa-close{width:38px;height:38px;display:grid;place-items:center;flex:none;border:1px solid rgba(148,164,187,.16);border-radius:10px;background:rgba(148,164,187,.06);color:#94a4bb;font:24px/1 inherit;cursor:pointer}'+
      '.hqa-close:hover,.hqa-close:focus-visible{border-color:rgba(231,178,76,.35);color:#f5f7fa;outline:3px solid rgba(231,178,76,.12)}'+
      '.hqa-body{min-height:160px;overflow:auto;margin:0 34px;padding:24px;border:1px solid rgba(148,164,187,.11);border-radius:14px;background:rgba(4,9,17,.4);color:#d5deeb;font-size:16px;line-height:1.85;white-space:pre-wrap;overflow-wrap:anywhere}'+
      '.hqa-foot{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:20px 34px 30px}'+
      '.hqa-note{flex:1;min-width:220px;color:#7f8da3;font-size:12px;line-height:1.55}'+
      '.hqa-actions{display:flex;gap:9px;flex-wrap:wrap;justify-content:flex-end}'+
      '.hqa-btn{min-height:42px;padding:0 15px;border:1px solid rgba(148,164,187,.17);border-radius:10px;background:rgba(148,164,187,.06);color:#d5deeb;font:700 13px inherit;cursor:pointer}'+
      '.hqa-btn.primary{border-color:rgba(231,178,76,.56);background:linear-gradient(135deg,#f5d27f,#e7b24c);color:#211704}'+
      '.hqa-btn:hover,.hqa-btn:focus-visible{transform:translateY(-1px);outline:3px solid rgba(231,178,76,.13)}'+
      '.hqa-btn:disabled,.hqa-close:disabled{opacity:.5;cursor:wait;transform:none}'+
      '.hqa-status{width:100%;min-height:18px;color:#f4708a;font-size:12px;text-align:right}'+
      '@keyframes hq-announcement-in{from{opacity:.25;transform:translateY(18px) scale(.975)}to{opacity:1;transform:none}}'+
      '@media(max-width:640px){.hqao{align-items:flex-end;padding:0}.hqam{width:100%;max-height:96vh;border-radius:20px 20px 0 0}.hqa-head{padding:25px 20px 16px}.hqa-mark{width:40px;height:40px;border-radius:12px}.hqa-title{font-size:24px}.hqa-body{min-height:190px;margin:0 20px;padding:19px;font-size:15px}.hqa-foot{padding:18px 20px 22px}.hqa-note{min-width:100%}.hqa-actions{width:100%;display:grid;grid-template-columns:1fr 1fr}.hqa-btn{padding:0 10px}.hqa-btn.primary{grid-column:1/-1;grid-row:1}}'+
      '@media(prefers-reduced-motion:reduce){.hqam{animation:none}.hqa-btn{transition:none}.hqa-btn:hover,.hqa-btn:focus-visible{transform:none}}';
    document.head.appendChild(st);
    var ov=document.createElement('div');
    ov.className='hqao'; ov.id='hqAnnouncementOv'; ov.setAttribute('aria-hidden','true');
    ov.innerHTML='<section class="hqam" role="dialog" aria-modal="true" aria-labelledby="hqAnnouncementTitle" aria-describedby="hqAnnouncementDetail">'+
      '<div class="hqa-head"><div class="hqa-mark">'+iconDuo('message','25px','#e7b24c')+'</div><div style="flex:1;min-width:0;"><div class="hqa-eyebrow" id="hqAnnouncementDate">今日公告</div><h2 class="hqa-title" id="hqAnnouncementTitle"></h2></div><button type="button" class="hqa-close" id="hqAnnouncementClose" aria-label="今日不再提醒">&times;</button></div>'+
      '<div class="hqa-body" id="hqAnnouncementDetail"></div>'+
      '<div class="hqa-foot"><div class="hqa-note">这条公告也会保存在消息中心。新公告发布后，即使今天已免打扰仍会再次提醒。</div><div class="hqa-actions"><button type="button" class="hqa-btn" data-announcement-action="view">在消息中心查看</button><button type="button" class="hqa-btn" data-announcement-action="snooze">今日不再提醒</button><button type="button" class="hqa-btn primary" data-announcement-action="read">我知道了</button></div><div class="hqa-status" id="hqAnnouncementStatus" aria-live="polite"></div></div></section>';
    document.body.appendChild(ov);
    ov.querySelectorAll('[data-announcement-action]').forEach(function(btn){ btn.onclick=function(){ announcementAction(btn.getAttribute('data-announcement-action')); }; });
    document.getElementById('hqAnnouncementClose').onclick=function(){ announcementAction('snooze'); };
    ov.addEventListener('keydown',function(e){
      if(e.key==='Escape'){
        e.preventDefault(); e.stopPropagation(); announcementAction('snooze'); return;
      }
      if(e.key!=='Tab') return;
      var focusable=Array.prototype.slice.call(ov.querySelectorAll('button:not([disabled])'));
      if(!focusable.length) return;
      var first=focusable[0],last=focusable[focusable.length-1];
      if(e.shiftKey&&document.activeElement===first){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey&&document.activeElement===last){ e.preventDefault(); first.focus(); }
    });
  }
  function formatAnnouncementDate(ms){
    try{ return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(ms||Date.now()))+' 发布'; }
    catch(e){ return '今日公告'; }
  }
  function setAnnouncementBusy(on,message){
    _announcementState.busy=!!on;
    var ov=document.getElementById('hqAnnouncementOv'); if(!ov) return;
    ov.querySelectorAll('button').forEach(function(btn){ btn.disabled=!!on; });
    var status=document.getElementById('hqAnnouncementStatus'); if(status) status.textContent=message||'';
  }
  function openAnnouncement(notice){
    ensureAnnouncementDialog();
    var ov=document.getElementById('hqAnnouncementOv'); if(!ov||!notice) return;
    _announcementState.notice=notice; _announcementState.returnFocus=document.activeElement;
    document.getElementById('hqAnnouncementTitle').textContent=notice.title||'系统公告';
    document.getElementById('hqAnnouncementDetail').textContent=notice.detail||'';
    document.getElementById('hqAnnouncementDate').textContent=formatAnnouncementDate(notice.time);
    setAnnouncementBusy(false,'');
    ov.classList.add('on'); ov.setAttribute('aria-hidden','false');
    setTimeout(function(){ var primary=ov.querySelector('[data-announcement-action="read"]'); if(primary) primary.focus(); },40);
  }
  function closeAnnouncement(){
    var ov=document.getElementById('hqAnnouncementOv'); if(ov){ ov.classList.remove('on'); ov.setAttribute('aria-hidden','true'); }
    var back=_announcementState.returnFocus;
    _announcementState.notice=null; _announcementState.busy=false; _announcementState.returnFocus=null;
    if(back&&typeof back.focus==='function') try{back.focus();}catch(e){}
  }
  function announcementAction(action){
    var x=_announcementState.notice;
    if(!x||!x.serverId||_announcementState.busy) return;
    var read=action==='read';
    setAnnouncementBusy(true,read?'正在确认已读…':'正在保存今日免打扰…');
    var path='/api/auth/notifications/'+encodeURIComponent(x.serverId)+(read?'/read':'/snooze-today');
    notificationStateRequest(path).then(function(d){
      if(read){ x.read=true; x.serverReadAt=(d.notification&&d.notification.read_at)||Math.floor(Date.now()/1000); }
      else x.snoozedUntil=(d.notification&&d.notification.popup_snoozed_until)||Math.floor(Date.now()/1000)+86400;
      closeAnnouncement(); renderNotices(); updateNotificationBadge();
      if(action==='view') setTimeout(function(){ openNotificationPanel(); var row=document.querySelector('[data-notice-id="'+escapeAttr(x.id)+'"]'); if(row) row.scrollIntoView({block:'center'}); },30);
    }).catch(function(err){
      if(err&&err.status===404){closeAnnouncement();loadNotices();return}
      setAnnouncementBusy(false,(err&&err.message)||'保存失败，请重试');
    });
  }
  function maybeOpenAnnouncement(){
    if(_announcementState.notice) return;
    var newest=_noticeState.items.find(function(x){ return x.isAnnouncement; });
    if(!eligibleAnnouncement(newest)||_announcementState.shown[newest.id]) return;
    _announcementState.shown[newest.id]=true;
    openAnnouncement(newest);
  }
  function updateNotificationBadge(){
    var n=_noticeState.items.filter(function(x){ return !x.read; }).length;
    var badge=document.querySelector('.hq-notify-badge'), btn=document.querySelector('.hq-notify-btn');
    if(badge){ badge.textContent=n>99?'99+':String(n||''); badge.classList.toggle('on',n>0); badge.setAttribute('aria-label',n+' 条未读通知'); }
    if(btn) btn.setAttribute('aria-label',n?'打开消息中心，'+n+' 条未读':'打开消息中心');
  }
  function refreshNotificationBadge(){ loadNotices(); }
  function openNotificationPanel(){
    ensureNotificationPanel(); var ov=document.getElementById('hqNoticeOv'); if(ov) ov.classList.add('on');
    var btn=document.querySelector('.hq-notify-btn'); if(btn) btn.setAttribute('aria-expanded','true');
    loadNotices();
  }
  function closeNotificationPanel(){
    var ov=document.getElementById('hqNoticeOv'); if(ov) ov.classList.remove('on');
    var btn=document.querySelector('.hq-notify-btn'); if(btn){ btn.setAttribute('aria-expanded','false'); try{btn.focus();}catch(e){} }
  }
  document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeNotificationPanel(); });

  // ===== 登录弹窗（全站共用，替代跳 /login 页）=====
  var _hqPhone=true, _hqMode='login';
  function buildLoginModal(){
    if(document.getElementById('hqLoginOv')) return;
    var st=document.createElement('style');
    st.textContent=
      '.hqlo{position:fixed;inset:0;z-index:9000;display:none;align-items:center;justify-content:center;padding:24px;background:rgba(4,5,9,.62);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);opacity:0;transition:opacity .22s}'+
      '.hqlo.on{display:flex;opacity:1}'+
      '.hqlm{position:relative;width:100%;max-width:392px;padding:38px;border-radius:22px;background:linear-gradient(180deg,rgba(28,28,32,.94),rgba(14,14,18,.94));border:1px solid rgba(255,255,255,.08);box-shadow:0 40px 90px -30px rgba(0,0,0,.9),0 1px 0 rgba(255,255,255,.06) inset,0 -50px 70px -56px rgba(231,178,76,.22) inset;transform:translateY(10px) scale(.985);transition:transform .24s cubic-bezier(.2,.7,.2,1);font-family:inherit}'+
      '.hqlo.on .hqlm{transform:none}'+
      '.hqlx{position:absolute;top:15px;right:15px;width:30px;height:30px;border:0;border-radius:9px;cursor:pointer;color:#9a9ba2;background:rgba(255,255,255,.05);display:flex;align-items:center;justify-content:center;transition:.16s}'+
      '.hqlx:hover{color:#f4f5f7;background:rgba(255,255,255,.1)}'+
      '.hqlt{display:flex;gap:24px;margin-top:24px;border-bottom:1px solid rgba(255,255,255,.07)}'+
      '.hqlt>button{padding:10px 1px;font-size:14px;font-weight:500;cursor:pointer;color:#9a9ba2;border:0;border-bottom:2px solid transparent;background:transparent;margin-bottom:-1px;transition:.2s;font-family:inherit}'+
      '.hqlt>button.on{color:#f4f5f7;border-bottom-color:#e7b24c}'+
      '.hqlf{display:flex;align-items:center;gap:11px;height:48px;padding:0 15px;border:1px solid rgba(255,255,255,.07);background:rgba(0,0,0,.28);border-radius:13px;transition:.2s}'+
      '.hqlf:focus-within{border-color:rgba(231,178,76,.5);box-shadow:0 0 0 3px rgba(231,178,76,.12)}'+
      '.hqlf input{flex:1;background:transparent;border:0;outline:0;color:#f4f5f7;font-size:14px;font-family:inherit}'+
      '.hqlf input::placeholder{color:#65666c}'+
      '.hqlb{width:100%;height:48px;margin-top:20px;border:0;border-radius:13px;cursor:pointer;font-family:inherit;font-size:15px;font-weight:600;letter-spacing:.18em;color:#1c1402;background:linear-gradient(135deg,#f6d488,#e7b24c);box-shadow:0 14px 30px -12px rgba(231,178,76,.55);transition:.18s}'+
      '.hqlb:hover{transform:translateY(-1px);filter:brightness(1.05)}';
    document.head.appendChild(st);
    var SI='width:16px;height:16px;color:#65666c;display:flex;flex:none;';
    var ov=document.createElement('div'); ov.className='hqlo'; ov.id='hqLoginOv';
    ov.innerHTML=
      '<div class="hqlm" role="dialog" aria-modal="true">'+
      '<button class="hqlx" id="hqLx" aria-label="关闭"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>'+
      '<div style="display:flex;align-items:center;gap:9px;margin-bottom:16px;"><span style="width:8px;height:8px;border-radius:50%;background:#e7b24c;box-shadow:0 0 10px #e7b24c;"></span><span style="font-size:14px;font-weight:600;">黄雀 AI</span></div>'+
      '<div id="hqTitle" style="font-size:20px;font-weight:600;">欢迎回来</div>'+
      '<div id="hqSubtitle" style="font-size:13px;color:#9a9ba2;margin-top:8px;">登录后开启智能获客与内容创作</div>'+
      '<div class="hqlt" id="hqTabs" role="tablist" aria-label="登录方式"><button type="button" role="tab" aria-selected="true" class="on" id="hqTP">手机号登录</button><button type="button" role="tab" aria-selected="false" id="hqTW">密码登录</button></div>'+
      '<form id="hqLoginForm">'+
        '<div style="margin-top:20px;display:flex;flex-direction:column;gap:12px;">'+
          '<div class="hqlf"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg></span><input id="hqU" name="username" type="text" autocomplete="username" maxlength="64" placeholder="请输入手机号 / 账号"></div>'+
          '<div id="hqRP" style="display:flex;gap:10px;"><div class="hqlf" style="flex:1;"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><path d="M22 11v1a10 10 0 1 1-5.9-9.1"/><path d="M22 4L12 14l-3-3"/></svg></span><input id="hqC" name="one-time-code" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="请输入验证码"></div><button type="button" id="hqGc" style="height:48px;padding:0 14px;white-space:nowrap;font-size:13px;color:#e7b24c;background:rgba(231,178,76,.08);border:1px solid rgba(231,178,76,.26);border-radius:13px;cursor:pointer;font-family:inherit;">获取验证码</button></div>'+
          '<div id="hqRW" class="hqlf" style="display:none;"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg></span><input id="hqP" name="password" type="password" autocomplete="current-password" maxlength="128" placeholder="请输入密码" disabled></div>'+
          '<div id="hqRegFields" style="display:none;flex-direction:column;gap:12px;">'+
            '<div class="hqlf"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg></span><input id="hqP2" name="password_confirm" type="password" autocomplete="new-password" maxlength="128" placeholder="请再次输入密码" disabled></div>'+
            '<div class="hqlf"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><circle cx="12" cy="8" r="4"/><path d="M4 20a8 8 0 0 1 16 0"/></svg></span><input id="hqD" name="display_name" type="text" maxlength="32" autocomplete="nickname" placeholder="昵称（可选，最多32字）" disabled></div>'+
          '</div>'+
        '</div>'+
        '<button type="submit" class="hqlb" id="hqSub">登 录</button>'+
        '<div id="hqMsg" style="text-align:center;font-size:12.5px;margin-top:11px;min-height:15px;color:#f4708a;"></div>'+
      '</form>'+
      '<button type="button" id="hqTeam" style="display:block;width:100%;text-align:center;font-size:13.5px;color:#e7b24c;cursor:pointer;font-weight:500;margin-top:6px;border:0;background:transparent;font-family:inherit;padding:0;">团队口令登录 →</button>'+
      '<div style="text-align:center;font-size:11px;color:#65666c;margin-top:16px;">登录即代表您同意《用户协议》与《隐私政策》</div>'+
      '</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){ if(e.target===ov) closeLogin(); });
    document.getElementById('hqLx').onclick=closeLogin;
    var tP=document.getElementById('hqTP'),tW=document.getElementById('hqTW');
    tP.onclick=function(){_hqPhone=true;setLoginMode('login');};
    tW.onclick=function(){_hqPhone=false;setLoginMode('login');};
    document.getElementById('hqGc').onclick=function(){ hqMsg('验证码登录即将上线，请用密码登录','err'); };
    document.getElementById('hqLoginForm').onsubmit=function(e){ e.preventDefault(); if(_hqMode==='register') hqDoRegister(); else hqDoLogin(); };
    document.getElementById('hqTeam').onclick=function(){ if(_hqMode==='register') setLoginMode('login'); else hqDoLogin(); };
  }
  function setLoginMode(mode){
    buildLoginModal(); _hqMode=mode==='register'?'register':'login';
    var title=document.getElementById('hqTitle'), sub=document.getElementById('hqSubtitle'), tabs=document.getElementById('hqTabs');
    var tP=document.getElementById('hqTP'), tW=document.getElementById('hqTW'), rP=document.getElementById('hqRP'), rW=document.getElementById('hqRW');
    var reg=document.getElementById('hqRegFields'), btn=document.getElementById('hqSub'), team=document.getElementById('hqTeam'), u=document.getElementById('hqU'), c=document.getElementById('hqC'), p=document.getElementById('hqP'), p2=document.getElementById('hqP2'), d=document.getElementById('hqD');
    hqMsg('');
    if(_hqMode==='register'){
      if(title) title.textContent='注册账号';
      if(sub) sub.textContent='创建账号后自动登录黄雀 AI 工作台';
      if(tabs) tabs.style.display='none';
      if(rP) rP.style.display='none';
      if(rW) rW.style.display='flex';
      if(reg) reg.style.display='flex';
      if(btn) btn.textContent='注 册';
      if(team) team.textContent='已有账号，返回登录';
      if(u) u.placeholder='请输入账号';
      if(p){ p.placeholder='请输入密码（至少6位）'; p.setAttribute('autocomplete','new-password'); }
      if(c) c.disabled=true;
      if(p) p.disabled=false;
      if(p2) p2.disabled=false;
      if(d) d.disabled=false;
      return;
    }
    if(title) title.textContent='欢迎回来';
    if(sub) sub.textContent='登录后开启智能获客与内容创作';
    if(tabs) tabs.style.display='flex';
    if(reg) reg.style.display='none';
    if(btn) btn.textContent='登 录';
    if(team) team.textContent='团队口令登录 →';
    if(tP&&tW){
      tP.classList.toggle('on',_hqPhone); tW.classList.toggle('on',!_hqPhone);
      tP.setAttribute('aria-selected',_hqPhone?'true':'false');
      tW.setAttribute('aria-selected',!_hqPhone?'true':'false');
    }
    if(rP) rP.style.display=_hqPhone?'flex':'none';
    if(rW) rW.style.display=_hqPhone?'none':'flex';
    if(u) u.placeholder=_hqPhone?'请输入手机号 / 账号':'请输入账号';
    if(p){ p.placeholder='请输入密码'; p.setAttribute('autocomplete','current-password'); }
    if(c) c.disabled=!_hqPhone;
    if(p) p.disabled=_hqPhone;
    if(p2) p2.disabled=true;
    if(d) d.disabled=true;
  }
  function hqMsg(t,k){ var m=document.getElementById('hqMsg'); if(m){ m.textContent=t||''; m.style.color=k==='ok'?'#2bd576':'#f4708a'; } }
  function openLogin(mode){ setLoginMode(mode); var ov=document.getElementById('hqLoginOv'); if(ov){ ov.classList.add('on'); var u=document.getElementById('hqU'); if(u) setTimeout(function(){try{u.focus();}catch(e){}},60); } }
  function openRegister(){ openLogin('register'); }
  function closeLogin(){ var ov=document.getElementById('hqLoginOv'); if(ov) ov.classList.remove('on'); }
  function authSuccess(res,msg){
    try{ localStorage.removeItem('hq_role'); localStorage.removeItem('hq_token'); if(res.d.user) localStorage.setItem('hq_user',JSON.stringify(res.d.user)); }catch(e){}
    hqMsg(msg||'操作成功','ok');
    setTimeout(function(){ closeLogin(); refreshPoints(); renderUser(); },450);
  }
  function hqDoLogin(){
    var username=(document.getElementById('hqU').value||'').trim();
    var secret=_hqPhone?(document.getElementById('hqC').value||''):(document.getElementById('hqP').value||'');
    if(!username||!secret){ hqMsg(_hqPhone?'请填写账号和验证码':'请填写账号和密码','err'); return; }
    var b=document.getElementById('hqSub'); b.disabled=true; b.style.opacity='.7'; hqMsg('登录中…');
    fetch('/api/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username,password:secret})})
    .then(function(r){ if(r.status===404) throw new Error('__nobackend__'); return r.json().then(function(d){return {ok:r.ok,d:d};}); })
    .then(function(res){
      b.disabled=false; b.style.opacity='1';
      if(res.ok&&res.d&&res.d.user){
        authSuccess(res,'登录成功');
      } else { hqMsg((res.d&&res.d.detail)||'账号或密码错误','err'); }
    })
    .catch(function(err){ b.disabled=false; b.style.opacity='1'; hqMsg(err&&err.message==='__nobackend__'?'登录服务即将上线（账号体系开发中）':'网络错误，请重试','err'); });
  }
  function hqDoRegister(){
    var username=(document.getElementById('hqU').value||'').trim();
    var password=(document.getElementById('hqP').value||'');
    var password2=(document.getElementById('hqP2').value||'');
    var displayName=(document.getElementById('hqD').value||'').trim();
    if(!username||!password){ hqMsg('请填写账号和密码','err'); return; }
    if(password.length<6){ hqMsg('密码至少需要 6 位','err'); return; }
    if(password!==password2){ hqMsg('两次输入的密码不一致','err'); return; }
    if(displayName.length>32){ hqMsg('昵称最多 32 个字符','err'); return; }
    var payload={username:username,password:password};
    if(displayName) payload.display_name=displayName;
    var b=document.getElementById('hqSub'); b.disabled=true; b.style.opacity='.7'; hqMsg('注册中…');
    fetch('/api/auth/register',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(function(r){ if(r.status===404) throw new Error('__nobackend__'); return r.json().then(function(d){return {ok:r.ok,status:r.status,d:d};}); })
    .then(function(res){
      b.disabled=false; b.style.opacity='1';
      if(res.ok&&res.d&&res.d.user){ authSuccess(res,'注册成功'); return; }
      var detail=(res.d&&res.d.detail)||'注册失败，请重试';
      if(res.status===409) detail='账号已存在，请换一个账号';
      if(res.status===429) detail='注册太频繁，请稍后再试';
      hqMsg(detail,'err');
    })
    .catch(function(err){ b.disabled=false; b.style.opacity='1'; hqMsg(err&&err.message==='__nobackend__'?'注册服务暂不可用':'网络错误，请重试','err'); });
  }
  document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeLogin(); });

  // ===== 用户登录态显示（左下侧栏卡 + 右上注册/登录）=====
  function currentUser(){ try{ return JSON.parse(localStorage.getItem('hq_user')||'null'); }catch(e){ return null; } }
  var _accountAvatar='';
  function membershipRoleName(user){
    if(user&&user.role==='admin') return '管理员';
    if(!user||!user.membership_active) return '非会员';
    return user.membership_name||({experience:'体验官',partner:'合伙人',initiator:'发起人'}[user.membership_tier])||'会员等级待同步';
  }
  function accountStorageKey(prefix,user){ return prefix+':'+(user&&user.username?user.username:'guest'); }
  function readAccountJson(prefix,user){
    try{ var value=JSON.parse(localStorage.getItem(accountStorageKey(prefix,user))||'{}'); return value&&typeof value==='object'?value:{}; }catch(e){ return {}; }
  }
  function _logout(){
    var h=authHeaders();
    try{ localStorage.removeItem('hq_token'); localStorage.removeItem('hq_user'); localStorage.removeItem('hq_role'); }catch(e){}
    fetch('/api/auth/logout',{method:'POST',credentials:'same-origin',headers:h}).finally(function(){ location.reload(); });
  }
  function avatarHTML(ch,size){
    return '<span class="hq-account-avatar" style="width:'+size+'px;height:'+size+'px;border-radius:'+(size>36?'11px':'50%')+';flex:none;font-size:13px;font-weight:700;">'+escapeHtml(ch)+(_accountAvatar?'<img src="'+escapeAttr(safeUrl(_accountAvatar))+'" alt="">':'')+'</span>';
  }
  function ensureAccountMenu(){
    var menu=document.getElementById('hqAccountMenu');
    if(menu) return menu;
    menu=document.createElement('div');
    menu.id='hqAccountMenu'; menu.className='hq-account-menu'; menu.setAttribute('role','menu');
    menu.innerHTML='<div class="hq-account-menu-head"><div id="hqAccountMenuName" style="font-size:13px;font-weight:800;color:#eaf1fa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"></div><div id="hqAccountMenuRole" style="margin-top:3px;font-size:11px;color:#e7b24c;"></div></div>'+
      '<a href="settings.html" role="menuitem">'+icon('gear','16px')+'<span>账户设置</span></a>'+
      '<a href="recharge.html" role="menuitem">'+icon('coins','16px')+'<span>会员与点数</span></a>'+
      '<a href="invite.html" role="menuitem">'+iconDuo('users','16px','#e7b24c')+'<span>邀请中心</span></a>'+
      '<button type="button" class="danger" data-logout="1" role="menuitem">'+icon('logout','16px')+'<span>退出登录</span></button>';
    document.body.appendChild(menu);
    menu.addEventListener('click',function(e){ if(e.target.closest('[data-logout]')) _logout(); });
    return menu;
  }
  function closeAccountMenu(){
    var menu=document.getElementById('hqAccountMenu'); if(menu) menu.classList.remove('on');
    document.querySelectorAll('[data-account-menu-trigger]').forEach(function(x){ x.setAttribute('aria-expanded','false'); });
  }
  function openAccountMenu(anchor){
    var u=currentUser(); if(!u) return;
    var menu=ensureAccountMenu(), rect=anchor.getBoundingClientRect(), width=226, gap=8;
    document.getElementById('hqAccountMenuName').textContent=u.name||u.nickname||u.username||'我的账号';
    document.getElementById('hqAccountMenuRole').textContent=membershipRoleName(u);
    menu.style.left=Math.max(8,Math.min(window.innerWidth-width-8,rect.right-width))+'px';
    menu.style.top=(rect.top>window.innerHeight/2?Math.max(8,rect.top-menu.offsetHeight-gap):Math.min(window.innerHeight-menu.offsetHeight-8,rect.bottom+gap))+'px';
    menu.classList.add('on'); anchor.setAttribute('aria-expanded','true');
    var first=menu.querySelector('[role="menuitem"]'); if(first) first.focus();
  }
  function renderUser(){
    var u=currentUser(), inn=!!u;
    var profile=readAccountJson('hq_profile_v1',u);
    var card=document.getElementById('hqUserCard'), auth=document.getElementById('hqAuthArea');
    if(inn){
      var name=profile.nickname||(u&&(u.name||u.nickname||u.username))||'我的账号', ch=(String(name).trim()[0]||'我').toUpperCase();
      var safeName=escapeHtml(name);
      var role=membershipRoleName(u);
      if(card) card.innerHTML='<button type="button" class="hq-user-row hq-account-trigger" data-account-menu-trigger="1" aria-label="打开账户菜单" aria-haspopup="menu" aria-expanded="false" aria-controls="hqAccountMenu" title="打开账户菜单" style="width:100%;display:flex;align-items:center;gap:10px;padding:8px 6px;border:0;border-radius:10px;background:transparent;text-align:left;cursor:pointer;">'+
        avatarHTML(ch,34)+
        '<div class="hq-user-copy" style="flex:1;min-width:0;"><div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'+safeName+'</div><div style="font-size:11px;color:#e7b24c;">'+role+'</div></div>'+
        '<span class="hq-user-logout" style="display:flex;width:16px;color:#5c6b82;">'+icon('chevronDown','16px')+'</span></button>';
      if(auth) auth.innerHTML='<button type="button" class="hq-account-trigger" data-account-menu-trigger="1" aria-label="打开账户菜单" aria-haspopup="menu" aria-expanded="false" aria-controls="hqAccountMenu" title="打开账户菜单" style="display:flex;border:0;border-radius:11px;background:transparent;padding:0;cursor:pointer;">'+avatarHTML(ch,38)+'</button>';
    } else {
      if(card) card.innerHTML='<button type="button" class="hq-login-row" data-login="1" aria-label="登录" title="登录" style="width:100%;display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:11px;cursor:pointer;border:1px solid rgba(148,164,187,.16);background:rgba(148,164,187,.04);font-family:inherit;text-align:left;">'+
        '<div style="width:32px;height:32px;border-radius:50%;flex:none;background:rgba(148,164,187,.12);display:flex;align-items:center;justify-content:center;color:#94a4bb;"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 20a8 8 0 0 1 16 0"/></svg></div>'+
        '<div class="hq-user-copy" style="flex:1;min-width:0;"><div style="font-size:13px;font-weight:500;color:#eaf1fa;">未登录</div><div style="font-size:11px;color:#94a4bb;">登录后开启全部功能</div></div>'+
        '<span class="hq-user-logout" style="font-size:12px;color:#e7b24c;font-weight:600;white-space:nowrap;">登录</span></button>';
      if(auth) auth.innerHTML='<button type="button" data-register="1" style="height:36px;padding:0 14px;border-radius:10px;cursor:pointer;font-family:inherit;font-size:13px;color:#94a4bb;background:rgba(148,164,187,.06);border:1px solid rgba(148,164,187,.16);">注册</button>'+
        '<button type="button" data-login="1" style="height:36px;padding:0 16px;border-radius:10px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:600;color:#1c1402;background:linear-gradient(135deg,#f6d488,#e7b24c);border:0;">登录</button>';
    }
    [card,auth].forEach(function(el){ if(!el) return; el.onclick=function(e){
      var t=e.target.closest?e.target.closest('[data-login],[data-register],[data-logout],[data-account-menu-trigger]'):null; if(!t) return;
      if(t.getAttribute('data-logout')) _logout();
      else if(t.getAttribute('data-account-menu-trigger')){ var menu=document.getElementById('hqAccountMenu'); if(menu&&menu.classList.contains('on')) closeAccountMenu(); else openAccountMenu(t); }
      else if(t.getAttribute('data-register')&&window.HQ&&HQ.register) HQ.register();
      else if(window.HQ&&HQ.login) HQ.login();
    };});
  }
  document.addEventListener('click',function(e){
    var menu=document.getElementById('hqAccountMenu');
    if(menu&&menu.classList.contains('on')&&!menu.contains(e.target)&&!e.target.closest('[data-account-menu-trigger]')) closeAccountMenu();
  });
  document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeAccountMenu(); });
  window.addEventListener('resize',closeAccountMenu);

  // 全局任务追踪器独立成 tasks.js，由共享外壳统一加载，避免每个页面各自维护 script 标签。
  // 复用 cloud-shell 的内容戳作为依赖版本：本文件改动时 stamp_assets 会刷新所有页面引用，
  // tasks.js 也随同换 URL，已访问过旧版追踪器的浏览器不会继续命中陈旧缓存。
  function loadTaskTracker(){
    if(window.HQTasks){ try{ window.HQTasks.renderBadge(); }catch(e){} return; }
    if(document.querySelector('script[data-hq-tasks]')) return;
    var version='';
    try{
      var shellScript=document.querySelector('script[src*="cloud-shell.js"]');
      if(shellScript) version=new URL(shellScript.src,location.href).searchParams.get('v')||'';
    }catch(e){}
    var script=document.createElement('script');
    script.setAttribute('data-hq-tasks','1');
    script.src='tasks.js'+(version?('?v='+encodeURIComponent(version)):'');
    document.head.appendChild(script);
  }

  var pricingValues={};
  var pricingListeners=[];
  function fetchPricing(){return fetch('/api/gen/pricing',{cache:'no-store'})
    .then(function(r){return r.ok?r.json():Promise.reject(new Error('pricing unavailable'));})
    .then(function(data){(data.items||[]).forEach(function(item){pricingValues[item.key]=Number(item.points)});return pricingValues;})}
  var pricingReady=fetchPricing().catch(function(){return pricingValues;});
  function onPricing(callback){pricingListeners.push(callback);return pricingReady.then(function(values){callback(values);return values;});}
  function price(key,fallback){var value=Number(pricingValues[key]);return Number.isFinite(value)&&value>0?value:fallback;}
  setInterval(function(){fetchPricing().then(function(values){pricingListeners.forEach(function(callback){callback(values)})}).catch(function(){})},30000);

  window.HQ={ icon:icon, nav:NAV, escapeHtml:escapeHtml, escapeAttr:escapeAttr, safeUrl:safeUrl, isAdmin:isAdmin, refreshPoints:refreshPoints, refreshNotifications:refreshNotificationBadge, setFriendsBadge:updateFriendsBadge, registerFriendsPanel:registerFriendsPanel, setFriendsPanelExpanded:setFriendsPanelExpanded, openFriendsPanel:openFriendsPanel, login:openLogin, register:openRegister, closeLogin:closeLogin, renderUser:renderUser, onPricing:onPricing, price:price };
  function _hqInit(){ build(); buildLoginModal(); loadTaskTracker(); }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',_hqInit); else _hqInit();
})();
