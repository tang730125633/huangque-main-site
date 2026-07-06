/* ============================================================
   黄雀 AI · Cloud Design 落地 — 工作台共享外壳 cloud-shell.js
   注入 228px 侧导 + 顶栏，环绕 .hq-content。各页只写内容 + data-active。
   用法：<div class="hq-app"><div class="hq-content" data-active="dashboard">…内容…</div></div>
   图标集 HQ.icon('name')。登录守卫：未登录可选跳转（默认关，后端接好再开）。
   ============================================================ */
(function(){
  "use strict";
  var I = {
    home:'<path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/>',
    search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/>',
    image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="M21 15l-5-5L5 21"/>',
    video:'<rect x="2" y="6" width="14" height="12" rx="2"/><path d="M22 8l-6 4 6 4z"/>',
    mic:'<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v4"/>',
    edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
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
    {k:'dashboard',l:'今日',i:'home', admin:true}, {k:'inspiration',l:'灵感',i:'sparkles'},
    {k:'leads',l:'获客',i:'search'}, {k:'collect',l:'内容爬取',i:'link'}, {k:'banana',l:'作图',i:'image'},
    {k:'video',l:'视频',i:'video'}, {k:'audio',l:'音频',i:'mic'}, {k:'script',l:'编导',i:'edit'},
    {k:'canvas',l:'画布',i:'layers'}, {k:'assets',l:'资产',i:'folder'},
    {k:'cost',l:'成本',i:'coins', admin:true}, {k:'settings',l:'设置',i:'gear'}
  ];
  // 导航金调3D图标(render3d香槟金,与灵感页能力卡同套)；缺失项回退线性svg
  var NAV3D={dashboard:'home', inspiration:'inspiration', leads:'leads', collect:'collect',
    banana:'banana', video:'video', audio:'audio', script:'script', canvas:'canvas',
    assets:'assets', cost:'recharge', settings:'settings'};

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
      return '<a href="'+escapeAttr(safeUrl(it.k+'.html'))+'" class="hq-navitem" style="position:relative; display:flex; align-items:center; gap:12px; padding:10px 13px; border-radius:11px; cursor:pointer; color:'+ntxt+'; background:'+nbg+'; font-size:14px; font-weight:500; transition:.16s;">'+
        '<span style="position:absolute; left:-12px; top:50%; transform:translateY(-50%); width:3px; height:18px; border-radius:0 3px 3px 0; background:#e7b24c; opacity:'+nbar+';"></span>'+
        '<span style="display:flex; align-items:center; justify-content:center; width:22px; color:'+nfg+';">'+(NAV3D[it.k]?'<img src="../assets/icons/ic_'+NAV3D[it.k]+'.png" alt="" style="width:22px;height:22px;object-fit:contain;flex:none;">':icon(it.i))+'</span>'+escapeHtml(it.l)+'</a>';
    }).join('');
  }

  function build(){
    var content=document.querySelector('.hq-content');
    if(!content) return;
    var active=content.getAttribute('data-active')||'';
    var app=document.querySelector('.hq-app');

    var aside=document.createElement('aside');
    aside.className='hq-aside';
    aside.style.cssText='width:228px; flex:none; display:flex; flex-direction:column; border-right:1px solid rgba(148,164,187,.08); background:linear-gradient(180deg, rgba(12,18,32,.95), rgba(8,12,20,.95)); backdrop-filter:blur(12px); z-index:40;';
    aside.innerHTML=
      '<a href="../index.html" style="display:flex; align-items:center; gap:10px; padding:20px 22px 18px; cursor:pointer;">'+
        '<div style="height:28px; display:flex; align-items:center;"><img src="../assets/cloud/logo-bird.png" alt="黄雀" style="height:100%;width:auto;display:block;filter:drop-shadow(0 0 6px rgba(231,178,76,.4));"></div>'+
        '<div style="font-size:17px; font-weight:700; letter-spacing:.4px;">黄雀 <span style="color:#94a4bb; font-weight:400;">AI</span></div></a>'+
      '<nav style="flex:1; overflow-y:auto; padding:6px 12px; display:flex; flex-direction:column; gap:2px;">'+navHTML(active)+'</nav>'+
      '<div style="padding:12px 14px; display:flex; flex-direction:column; gap:11px;">'+
        '<div style="position:relative; padding:15px 16px; border:1px solid rgba(231,178,76,.2); border-radius:14px; background:linear-gradient(150deg, rgba(231,178,76,.1), rgba(231,178,76,.02)); overflow:hidden;">'+
          '<div style="position:absolute; right:-14px; top:-10px; width:62px; height:62px; color:rgba(231,178,76,.22);">'+icon('coins','62px')+'</div>'+
          '<div style="font-size:12px; color:#94a4bb;">剩余点数</div>'+
          '<div id="hqPointsSide" class="mono" style="font-size:30px; font-weight:700; color:#e7b24c; line-height:1.1; margin:3px 0 9px;">—</div>'+
          '<a href="recharge.html" style="display:inline-flex; align-items:center; gap:5px; font-size:12.5px; color:#e7b24c; cursor:pointer; font-weight:600;">去充值 <span style="display:flex; width:13px;">'+icon('arrowMini')+'</span></a></div>'+
        '<a href="bots.html" style="display:flex; align-items:center; gap:8px; padding:10px 14px; border:1px solid rgba(45,212,191,.2); border-radius:12px; background:rgba(45,212,191,.05);">'+
          '<span style="width:7px; height:7px; border-radius:50%; background:#2dd4bf; box-shadow:0 0 8px #2dd4bf; animation:hq-pulse 2s infinite;"></span>'+
          '<span style="font-size:13px; color:#94a4bb; flex:1;"><span class="mono" style="color:#2dd4bf; font-weight:600;">34</span> 个 Bot 在线</span>'+
          '<span style="display:flex; width:13px; color:#2dd4bf;">'+icon('arrowMini')+'</span></a>'+
        '<div id="hqUserCard"></div>'+
      '</div>';

    var header=document.createElement('header');
    header.className='hq-topbar';
    header.style.cssText='flex:none; display:flex; align-items:center; gap:14px; padding:14px 26px; border-bottom:1px solid rgba(148,164,187,.08); background:rgba(7,11,19,.4); backdrop-filter:blur(12px);';
    header.innerHTML=
      '<button class="hq-burger" style="display:none; width:38px; height:38px; align-items:center; justify-content:center; flex:none; color:#94a4bb; background:rgba(148,164,187,.05); border:1px solid rgba(148,164,187,.14); border-radius:11px; cursor:pointer;">'+icon('menu','18px')+'</button>'+
      '<div id="hqBizSwitch" style="display:none; align-items:center; gap:9px; padding:7px 14px 7px 9px; border:1px solid rgba(148,164,187,.14); border-radius:11px; background:rgba(148,164,187,.04); cursor:pointer;">'+
        '<div style="width:24px; height:24px; border-radius:7px; background:linear-gradient(150deg,#9a6a3a,#5a3d22); display:flex; align-items:center; justify-content:center; color:#fff; font-size:11px; font-weight:600;">仙</div>'+
        '<span style="font-size:13.5px; font-weight:500;">仙颜美容 · 示例</span><span style="display:flex; width:13px; color:#94a4bb;">'+icon('chevronDown')+'</span></div>'+
      '<div class="hq-botpill" style="display:flex; align-items:center; gap:8px; padding:8px 13px; border:1px solid rgba(45,212,191,.2); border-radius:11px; background:rgba(45,212,191,.05);">'+
        '<span style="width:7px; height:7px; border-radius:50%; background:#2dd4bf; box-shadow:0 0 8px #2dd4bf; animation:hq-pulse 2s infinite;"></span>'+
        '<span style="font-size:13px; color:#94a4bb;"><span class="mono" style="color:#2dd4bf; font-weight:600;">34</span> 个 Bot 在线</span></div>'+
      '<div style="margin-left:auto; display:flex; align-items:center; gap:12px;">'+
        '<a href="recharge.html" style="display:flex; align-items:center; gap:8px; padding:8px 14px; border:1px solid rgba(231,178,76,.26); border-radius:11px; background:rgba(231,178,76,.07); cursor:pointer;">'+
          '<span style="width:16px; height:16px; border-radius:50%; background:radial-gradient(circle at 35% 30%, #f6d488, #c8902f); flex:none;"></span>'+
          '<span id="hqPointsTop" class="mono" style="font-size:14px; font-weight:700; color:#e7b24c;">—</span></a>'+
        '<div style="position:relative; width:38px; height:38px; display:flex; align-items:center; justify-content:center; border:1px solid rgba(148,164,187,.14); border-radius:11px; cursor:pointer; color:#94a4bb;">'+icon('bell','17px')+'<span style="position:absolute; top:9px; right:10px; width:7px; height:7px; border-radius:50%; background:#f4708a; border:1.5px solid #070b13;"></span></div>'+
        '<div id="hqAuthArea" style="display:flex; align-items:center; gap:8px;"></div></div>';

    // 重组 DOM：aside | main(topbar + scroll(content))
    var main=document.createElement('main');
    main.style.cssText='flex:1; min-width:0; display:flex; flex-direction:column; height:100vh;';
    var scroll=document.createElement('div');
    scroll.style.cssText='flex:1; overflow-y:auto; padding:26px 30px 40px;';
    content.parentNode.removeChild(content);
    scroll.appendChild(content);
    main.appendChild(header); main.appendChild(scroll);
    app.style.cssText='height:100vh; display:flex; position:relative; z-index:1; overflow:hidden;';
    app.appendChild(aside); app.appendChild(main);

    // 响应式：窄屏抽屉
    var burger=header.querySelector('.hq-burger');
    function applyResp(){
      var narrow=window.innerWidth<900;
      header.querySelector('.hq-burger').style.display=narrow?'flex':'none';
      header.querySelector('.hq-botpill').style.display=narrow?'none':'flex';
      if(narrow){ aside.style.position='fixed'; aside.style.top=0; aside.style.bottom=0; aside.style.left=0; aside.style.transform='translateX(-100%)'; aside.style.boxShadow='0 0 60px rgba(0,0,0,.6)'; }
      else { aside.style.position='static'; aside.style.transform='none'; aside.style.boxShadow='none'; }
    }
    var open=false;
    burger.onclick=function(){ open=!open; aside.style.transform=open?'translateX(0)':'translateX(-100%)'; };
    window.addEventListener('resize',applyResp); applyResp();
    aside.querySelectorAll('a').forEach(function(a){ a.addEventListener('click',function(){ if(window.innerWidth<900){ open=false; aside.style.transform='translateX(-100%)'; } }); });
    refreshPoints(); renderUser();
  }

  // 拉真实点数填到侧边+顶栏（生成后页面调 window.HQ.refreshPoints() 刷新）
  function refreshPoints(){
    var tok=localStorage.getItem('hq_token'); if(!tok) return;
    fetch('/api/auth/me',{headers:{'Authorization':'Bearer '+tok}}).then(function(r){return r.json();}).then(function(d){
      var p=d&&d.user&&d.user.points; if(p==null) return;
      var a=document.getElementById('hqPointsSide'), b=document.getElementById('hqPointsTop');
      if(a) a.textContent=p; if(b) b.textContent=p;
    }).catch(function(){});
  }

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
      '.hqlt>div{padding:10px 1px;font-size:14px;font-weight:500;cursor:pointer;color:#9a9ba2;border-bottom:2px solid transparent;margin-bottom:-1px;transition:.2s}'+
      '.hqlt>div.on{color:#f4f5f7;border-bottom-color:#e7b24c}'+
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
      '<div class="hqlt" id="hqTabs"><div class="on" id="hqTP">手机号登录</div><div id="hqTW">密码登录</div></div>'+
      '<div style="margin-top:20px;display:flex;flex-direction:column;gap:12px;">'+
        '<div class="hqlf"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg></span><input id="hqU" placeholder="请输入手机号 / 账号"></div>'+
        '<div id="hqRP" style="display:flex;gap:10px;"><div class="hqlf" style="flex:1;"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><path d="M22 11v1a10 10 0 1 1-5.9-9.1"/><path d="M22 4L12 14l-3-3"/></svg></span><input id="hqC" placeholder="请输入验证码"></div><button type="button" id="hqGc" style="height:48px;padding:0 14px;white-space:nowrap;font-size:13px;color:#e7b24c;background:rgba(231,178,76,.08);border:1px solid rgba(231,178,76,.26);border-radius:13px;cursor:pointer;font-family:inherit;">获取验证码</button></div>'+
        '<div id="hqRW" class="hqlf" style="display:none;"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg></span><input id="hqP" type="password" placeholder="请输入密码"></div>'+
        '<div id="hqRegFields" style="display:none;flex-direction:column;gap:12px;">'+
          '<div class="hqlf"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg></span><input id="hqP2" type="password" placeholder="请再次输入密码"></div>'+
          '<div class="hqlf"><span style="'+SI+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:100%;height:100%"><circle cx="12" cy="8" r="4"/><path d="M4 20a8 8 0 0 1 16 0"/></svg></span><input id="hqD" maxlength="32" placeholder="昵称（可选，最多32字）"></div>'+
        '</div>'+
      '</div>'+
      '<button type="button" class="hqlb" id="hqSub">登 录</button>'+
      '<div id="hqMsg" style="text-align:center;font-size:12.5px;margin-top:11px;min-height:15px;color:#f4708a;"></div>'+
      '<div id="hqTeam" style="text-align:center;font-size:13.5px;color:#e7b24c;cursor:pointer;font-weight:500;margin-top:6px;">团队口令登录 →</div>'+
      '<div style="text-align:center;font-size:11px;color:#65666c;margin-top:16px;">登录即代表您同意《用户协议》与《隐私政策》</div>'+
      '</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){ if(e.target===ov) closeLogin(); });
    document.getElementById('hqLx').onclick=closeLogin;
    var tP=document.getElementById('hqTP'),tW=document.getElementById('hqTW');
    tP.onclick=function(){_hqPhone=true;setLoginMode('login');};
    tW.onclick=function(){_hqPhone=false;setLoginMode('login');};
    document.getElementById('hqGc').onclick=function(){ hqMsg('验证码登录即将上线，请用密码登录','err'); };
    document.getElementById('hqSub').onclick=function(){ if(_hqMode==='register') hqDoRegister(); else hqDoLogin(); };
    document.getElementById('hqTeam').onclick=function(){ if(_hqMode==='register') setLoginMode('login'); else hqDoLogin(); };
    ['hqU','hqC','hqP','hqP2','hqD'].forEach(function(id){var el=document.getElementById(id);if(el)el.addEventListener('keydown',function(e){if(e.key==='Enter'){ if(_hqMode==='register') hqDoRegister(); else hqDoLogin(); }});});
  }
  function setLoginMode(mode){
    buildLoginModal(); _hqMode=mode==='register'?'register':'login';
    var title=document.getElementById('hqTitle'), sub=document.getElementById('hqSubtitle'), tabs=document.getElementById('hqTabs');
    var tP=document.getElementById('hqTP'), tW=document.getElementById('hqTW'), rP=document.getElementById('hqRP'), rW=document.getElementById('hqRW');
    var reg=document.getElementById('hqRegFields'), btn=document.getElementById('hqSub'), team=document.getElementById('hqTeam'), u=document.getElementById('hqU'), p=document.getElementById('hqP');
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
      if(p) p.placeholder='请输入密码（至少6位）';
      return;
    }
    if(title) title.textContent='欢迎回来';
    if(sub) sub.textContent='登录后开启智能获客与内容创作';
    if(tabs) tabs.style.display='flex';
    if(reg) reg.style.display='none';
    if(btn) btn.textContent='登 录';
    if(team) team.textContent='团队口令登录 →';
    if(tP&&tW){ tP.classList.toggle('on',_hqPhone); tW.classList.toggle('on',!_hqPhone); }
    if(rP) rP.style.display=_hqPhone?'flex':'none';
    if(rW) rW.style.display=_hqPhone?'none':'flex';
    if(u) u.placeholder=_hqPhone?'请输入手机号 / 账号':'请输入账号';
    if(p) p.placeholder='请输入密码';
  }
  function hqMsg(t,k){ var m=document.getElementById('hqMsg'); if(m){ m.textContent=t||''; m.style.color=k==='ok'?'#2bd576':'#f4708a'; } }
  function openLogin(mode){ setLoginMode(mode); var ov=document.getElementById('hqLoginOv'); if(ov){ ov.classList.add('on'); var u=document.getElementById('hqU'); if(u) setTimeout(function(){try{u.focus();}catch(e){}},60); } }
  function openRegister(){ openLogin('register'); }
  function closeLogin(){ var ov=document.getElementById('hqLoginOv'); if(ov) ov.classList.remove('on'); }
  function authSuccess(res,msg){
    try{ localStorage.removeItem('hq_role'); localStorage.setItem('hq_token',res.d.token); if(res.d.user) localStorage.setItem('hq_user',JSON.stringify(res.d.user)); }catch(e){}
    hqMsg(msg||'操作成功','ok');
    setTimeout(function(){ closeLogin(); refreshPoints(); renderUser(); },450);
  }
  function hqDoLogin(){
    var username=(document.getElementById('hqU').value||'').trim();
    var secret=_hqPhone?(document.getElementById('hqC').value||''):(document.getElementById('hqP').value||'');
    if(!username||!secret){ hqMsg(_hqPhone?'请填写账号和验证码':'请填写账号和密码','err'); return; }
    var b=document.getElementById('hqSub'); b.disabled=true; b.style.opacity='.7'; hqMsg('登录中…');
    fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username,password:secret})})
    .then(function(r){ if(r.status===404) throw new Error('__nobackend__'); return r.json().then(function(d){return {ok:r.ok,d:d};}); })
    .then(function(res){
      b.disabled=false; b.style.opacity='1';
      if(res.ok&&res.d&&res.d.token){
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
    fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(function(r){ if(r.status===404) throw new Error('__nobackend__'); return r.json().then(function(d){return {ok:r.ok,status:r.status,d:d};}); })
    .then(function(res){
      b.disabled=false; b.style.opacity='1';
      if(res.ok&&res.d&&res.d.token){ authSuccess(res,'注册成功'); return; }
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
  function _logout(){ try{ localStorage.removeItem('hq_token'); localStorage.removeItem('hq_user'); localStorage.removeItem('hq_role'); }catch(e){} location.reload(); }
  function renderUser(){
    var u=currentUser(), inn=!!localStorage.getItem('hq_token');
    var card=document.getElementById('hqUserCard'), auth=document.getElementById('hqAuthArea'), biz=document.getElementById('hqBizSwitch');
    var av='linear-gradient(150deg,#9a6a3a,#5a3d22)';
    if(inn){
      var name=(u&&(u.nickname||u.username))||'我的账号', ch=(String(name).trim()[0]||'我').toUpperCase();
      var safeName=escapeHtml(name), safeCh=escapeHtml(ch);
      var role=(u&&u.role==='admin')?'管理员':'会员';
      if(card) card.innerHTML='<div style="display:flex;align-items:center;gap:10px;padding:8px 6px;border-radius:10px;">'+
        '<div style="width:34px;height:34px;border-radius:50%;flex:none;background:'+av+';display:flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:600;">'+safeCh+'</div>'+
        '<div style="flex:1;min-width:0;"><div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'+safeName+'</div><div style="font-size:11px;color:#e7b24c;">'+role+'</div></div>'+
        '<span data-logout="1" title="退出登录" style="display:flex;width:16px;color:#5c6b82;cursor:pointer;">'+icon('logout')+'</span></div>';
      if(auth) auth.innerHTML='<div style="width:38px;height:38px;border-radius:11px;background:'+av+';display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;font-weight:600;">'+safeCh+'</div>';
      if(biz) biz.style.display='flex';
    } else {
      if(card) card.innerHTML='<div data-login="1" style="display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:11px;cursor:pointer;border:1px solid rgba(148,164,187,.16);background:rgba(148,164,187,.04);">'+
        '<div style="width:32px;height:32px;border-radius:50%;flex:none;background:rgba(148,164,187,.12);display:flex;align-items:center;justify-content:center;color:#94a4bb;"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 20a8 8 0 0 1 16 0"/></svg></div>'+
        '<div style="flex:1;min-width:0;"><div style="font-size:13px;font-weight:500;color:#eaf1fa;">未登录</div><div style="font-size:11px;color:#94a4bb;">登录后开启全部功能</div></div>'+
        '<span style="font-size:12px;color:#e7b24c;font-weight:600;white-space:nowrap;">登录</span></div>';
      if(auth) auth.innerHTML='<button data-register="1" style="height:36px;padding:0 14px;border-radius:10px;cursor:pointer;font-family:inherit;font-size:13px;color:#94a4bb;background:rgba(148,164,187,.06);border:1px solid rgba(148,164,187,.16);">注册</button>'+
        '<button data-login="1" style="height:36px;padding:0 16px;border-radius:10px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:600;color:#1c1402;background:linear-gradient(135deg,#f6d488,#e7b24c);border:0;">登录</button>';
      if(biz) biz.style.display='none';
    }
    [card,auth].forEach(function(el){ if(!el) return; el.onclick=function(e){
      var t=e.target.closest?e.target.closest('[data-login],[data-register],[data-logout]'):null; if(!t) return;
      if(t.getAttribute('data-logout')) _logout();
      else if(t.getAttribute('data-register')&&window.HQ&&HQ.register) HQ.register();
      else if(window.HQ&&HQ.login) HQ.login();
    };});
  }

  window.HQ={ icon:icon, nav:NAV, escapeHtml:escapeHtml, escapeAttr:escapeAttr, safeUrl:safeUrl, isAdmin:isAdmin, refreshPoints:refreshPoints, login:openLogin, register:openRegister, closeLogin:closeLogin, renderUser:renderUser };
  function _hqInit(){ build(); buildLoginModal(); }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',_hqInit); else _hqInit();
})();
