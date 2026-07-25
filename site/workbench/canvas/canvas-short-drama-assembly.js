(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaAssembly=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var ASSEMBLY_PATH='/api/gen/short-drama/assembly';

  function text(value){ return String(value==null?'':value); }
  function number(value,fallback){
    var result=Number(value);
    return isFinite(result)?result:(fallback==null?0:fallback);
  }
  function clone(value){
    if(Array.isArray(value)) return value.map(clone);
    if(value&&typeof value==='object'){
      var copy={};
      Object.keys(value).forEach(function(key){ copy[key]=clone(value[key]); });
      return copy;
    }
    return value;
  }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function normalizeBlocker(item){
    item=item&&typeof item==='object'?item:{};
    return {
      code:text(item.code||'unknown'),
      message:text(item.message||item.code||'状态未知'),
      shot_id:item.shot_id==null?null:text(item.shot_id)
    };
  }
  function normalizeState(input,options){
    input=input&&typeof input==='object'?input:{};
    options=options&&typeof options==='object'?options:{};
    var shots=(Array.isArray(input.shots)?input.shots:[]).map(function(shot,index){
      shot=shot&&typeof shot==='object'?shot:{};
      var voice=shot.voice&&typeof shot.voice==='object'?shot.voice:{};
      var video=shot.video&&typeof shot.video==='object'?shot.video:{};
      return {
        id:text(shot.id),
        shot_key:text(shot.shot_key||('镜头 '+(index+1))),
        sort_order:number(shot.sort_order,index),
        duration:number(shot.duration,0),
        ready:shot.ready===true,
        voice:{
          locked:voice.locked===true,
          status:text(voice.status||'blocked')
        },
        video:{
          confirmed:video.confirmed===true,
          status:text(video.status||'pending_c3'),
          current_version:video.current_version==null?null:number(video.current_version,0)
        },
        blockers:(Array.isArray(shot.blockers)?shot.blockers:[]).map(normalizeBlocker)
      };
    }).sort(function(left,right){ return left.sort_order-right.sort_order; });
    var readiness=input.readiness&&typeof input.readiness==='object'?input.readiness:{};
    var actions=input.actions&&typeof input.actions==='object'?input.actions:{};
    var config=input.config&&typeof input.config==='object'?clone(input.config):{};
    config.subtitle=config.subtitle&&typeof config.subtitle==='object'?config.subtitle:{};
    config.bgm=config.bgm&&typeof config.bgm==='object'?config.bgm:{};
    return {
      project_id:text(input.project_id),
      revision:number(input.revision,0),
      stage:text(input.stage||'assembly_review'),
      ratio:text(input.ratio||'9:16'),
      target_duration:number(input.target_duration,0),
      assembly_revision:number(input.assembly_revision,1),
      implementation_status:text(input.implementation_status||'contract_only'),
      rendering_enabled:input.rendering_enabled===true,
      config:config,
      shots:shots,
      versions:Array.isArray(input.versions)?input.versions.map(clone):[],
      active_job:input.active_job&&typeof input.active_job==='object'?
        clone(input.active_job):null,
      readiness:{
        ready:readiness.ready===true,
        blockers:(Array.isArray(readiness.blockers)?
          readiness.blockers:[]).map(normalizeBlocker)
      },
      actions:{
        can_save_config:actions.can_save_config===true,
        can_preview:actions.can_preview===true,
        can_lock_preview:actions.can_lock_preview===true,
        can_export:actions.can_export===true,
        can_confirm:actions.can_confirm===true
      },
      busy:options.busy===true,
      error:text(options.error),
      canEdit:options.canEdit!==false
    };
  }
  function uniqueBlockerMessages(items){
    var seen=Object.create(null);
    return (items||[]).map(function(item){ return text(item.message||item.code); })
      .filter(function(message){
        if(!message||seen[message]) return false;
        seen[message]=true;
        return true;
      });
  }
  function disabled(enabled){ return enabled?'':' disabled'; }
  function readinessLabel(ready){ return ready?'已就绪':'待补齐'; }
  function renderWorkspace(input,options){
    var state=normalizeState(input,options);
    if(state.busy&&!state.project_id){
      return '<section class="nc-sda-state" data-state="loading">'+
        '<strong>正在加载合成工作区…</strong><span>正在核对配音、字幕和视频版本</span></section>';
    }
    if(state.error&&!state.project_id){
      return '<section class="nc-sda-state is-error" data-state="error">'+
        '<strong>合成工作区加载失败</strong><span>'+escapeHtml(state.error)+
        '</span><button type="button" data-action="reload">重新加载</button></section>';
    }
    if(!state.shots.length){
      return '<section class="nc-sda-state" data-state="empty">'+
        '<strong>暂无可合成镜头</strong><span>请先完成短剧分镜及前序生产阶段</span></section>';
    }
    var rail=state.shots.map(function(shot){
      return '<article class="nc-sda-shot'+(shot.ready?' is-ready':' is-blocked')+'">'+
        '<div><strong>'+escapeHtml(shot.shot_key)+'</strong><small>'+
        shot.duration+' 秒</small></div><dl><div><dt>配音字幕</dt><dd>'+
        (shot.voice.locked?'已锁定':'未锁定')+'</dd></div><div><dt>电影化身视频</dt><dd>'+
        (shot.video.confirmed?'已确认':'等待 C-3')+'</dd></div></dl>'+
        '<span class="nc-sda-status">'+readinessLabel(shot.ready)+'</span></article>';
    }).join('');
    var blockerMessages=uniqueBlockerMessages(state.readiness.blockers);
    var blockerList=blockerMessages.length?blockerMessages.map(function(message){
      return '<li>'+escapeHtml(message)+'</li>';
    }).join(''):'<li>前序素材已满足合成条件</li>';
    var subtitle=state.config.subtitle||{},bgm=state.config.bgm||{};
    return '<section class="nc-sda-workspace" data-implementation="'+
      escapeHtml(state.implementation_status)+'"><aside class="nc-sda-rail">'+
      '<header><span>D-0 输入契约</span><h2>镜头与素材</h2><small>'+
      state.shots.length+' 镜 · '+state.target_duration+' 秒 · '+escapeHtml(state.ratio)+
      '</small></header><div class="nc-sda-shot-list">'+rail+'</div></aside>'+
      '<main class="nc-sda-preview"><header><span>成片预览</span><h2>项目级合成画布</h2></header>'+
      '<div class="nc-sda-player-placeholder"><div class="nc-sda-play-icon">▶</div>'+
      '<strong>预览渲染将在 D-3 开放</strong><span>D-0 仅建立数据契约与只读工作区，不执行 FFmpeg。</span></div>'+
      '<div class="nc-sda-timeline"><div class="nc-sda-time-head"><span>00:00</span><span>'+
      state.target_duration+'s</span></div><div class="nc-sda-track"><span style="width:100%"></span></div>'+
      '<small>镜头、配音与字幕总时间轴将在媒体计划阶段接入</small></div></main>'+
      '<aside class="nc-sda-console"><header><span>D 阶段</span><h2>合成控制台</h2>'+
      '<small>装配修订 r'+state.assembly_revision+'</small></header>'+
      '<section class="nc-sda-readiness '+(state.readiness.ready?'is-ready':'is-blocked')+'">'+
      '<strong>'+readinessLabel(state.readiness.ready)+'</strong><ul>'+blockerList+'</ul></section>'+
      '<fieldset disabled><legend>装配配置</legend><label>字幕样式<input value="'+
      escapeHtml(subtitle.preset||'white_outline')+'"></label><label>字幕位置<input value="'+
      escapeHtml(subtitle.position||'bottom')+'"></label><label>背景音乐<input value="'+
      escapeHtml(bgm.asset_id||'未选择')+'"></label><label>背景音乐音量<input value="'+
      escapeHtml(bgm.volume==null?'0.18':bgm.volume)+'"></label></fieldset>'+
      '<div class="nc-sda-actions"><button type="button" data-action="save-config"'+
      disabled(state.canEdit&&state.actions.can_save_config)+'>保存装配配置</button>'+
      '<button type="button" data-action="generate-preview"'+
      disabled(state.canEdit&&state.actions.can_preview)+'>生成预览</button>'+
      '<button type="button" data-action="export-final"'+
      disabled(state.canEdit&&state.actions.can_export)+'>正式导出</button>'+
      '<button type="button" class="is-primary" data-action="confirm-completed"'+
      disabled(state.canEdit&&state.actions.can_confirm)+'>确认成片并完成</button></div>'+
      '<p class="nc-sda-contract-note">当前为 D-0 契约骨架。C-3 视频版本接入前，合成与导出保持禁用。</p>'+
      '</aside></section>';
  }
  function createWorkspace(options){
    options=options||{};
    var client=options.client,host=options.host,destroyed=false;
    var snapshot=null,ui={busy:true,error:''},requestGeneration=0;
    if(!client||typeof client.json!=='function'){
      throw new Error('短剧合成工作区缺少已认证 API 客户端');
    }
    function viewOptions(){
      return {
        busy:ui.busy,
        error:ui.error,
        canEdit:options.canEdit!==false&&(!snapshot||snapshot.stage!=='completed')
      };
    }
    function render(){
      var html=renderWorkspace(snapshot||{},viewOptions());
      if(host&&!destroyed) host.innerHTML=html;
      return html;
    }
    function scopedJson(path,requestOptions){
      var scoped=requestOptions?Object.assign({},requestOptions):{};
      if(options.boardId){
        scoped.headers=Object.assign({},scoped.headers||{}, {
          'X-Canvas-Board-Id':String(options.boardId)
        });
      }
      return client.json(path,scoped);
    }
    function reload(){
      if(destroyed) return Promise.resolve(null);
      var generation=++requestGeneration;
      ui.busy=true;ui.error='';render();
      return Promise.resolve(scopedJson(
        ASSEMBLY_PATH+'?project_id='+encodeURIComponent(options.projectId)
      )).then(function(result){
        if(destroyed||generation!==requestGeneration) return null;
        snapshot=result&&typeof result==='object'?result:{};
        ui.busy=false;render();
        if(typeof options.onChange==='function'){
          return Promise.resolve(options.onChange({
            project_id:snapshot.project_id,
            revision:snapshot.revision,
            stage:snapshot.stage,
            ratio:snapshot.ratio
          })).then(function(){ return snapshot; });
        }
        return snapshot;
      }).catch(function(error){
        if(destroyed||generation!==requestGeneration) return null;
        ui.busy=false;ui.error=text(error&&error.message||error);render();
        throw error;
      });
    }
    function onClick(event){
      var target=event&&event.target;
      while(target&&target!==host&&
          !(target.getAttribute&&target.getAttribute('data-action'))){
        target=target.parentNode;
      }
      if(target&&target.getAttribute&&target.getAttribute('data-action')==='reload'){
        reload().catch(function(){});
      }
    }
    if(host&&typeof host.addEventListener==='function'){
      host.addEventListener('click',onClick);
    }
    render();
    var ready=reload();
    return {
      projectId:options.projectId,
      ready:ready,
      render:render,
      reload:reload,
      getState:function(){
        return clone(normalizeState(snapshot||{},viewOptions()));
      },
      destroy:function(){
        if(host&&typeof host.removeEventListener==='function'){
          host.removeEventListener('click',onClick);
        }
        destroyed=true;requestGeneration+=1;host=null;snapshot=null;
        ui.busy=false;ui.error='';
      }
    };
  }
  return {
    normalizeState:normalizeState,
    renderWorkspace:renderWorkspace,
    createWorkspace:createWorkspace
  };
});
