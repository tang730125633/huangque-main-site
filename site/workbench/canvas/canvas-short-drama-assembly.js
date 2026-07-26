(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{};root.HQCanvas.shortDramaAssembly=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var STATE_PATH='/api/gen/short-drama/assembly';
  var RENDER_PATH='/api/gen/short-drama/render-final';
  var CONFIRM_PATH='/api/gen/short-drama/confirm-assembly';
  function text(value){ return String(value==null?'':value); }
  function number(value,fallback){ var n=Number(value);return isFinite(n)?n:(fallback||0); }
  function clone(value){
    if(Array.isArray(value)) return value.map(clone);
    if(value&&typeof value==='object'){ var copy={};Object.keys(value).forEach(function(key){copy[key]=clone(value[key]);});return copy; }
    return value;
  }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function normalizeBlocker(item){ item=item||{};return {code:text(item.code),message:text(item.message||item.code),shot_id:item.shot_id||null}; }
  function normalizeState(input,options){
    input=input&&typeof input==='object'?input:{};options=options||{};
    var shots=(Array.isArray(input.shots)?input.shots:[]).map(function(shot,index){
      shot=shot||{};return {
        id:text(shot.id),shot_key:text(shot.shot_key||('镜头 '+(index+1))),
        sort_order:number(shot.sort_order,index),duration:number(shot.duration),ready:shot.ready===true,
        voice:clone(shot.voice||{}),video:clone(shot.video||{}),
        blockers:(Array.isArray(shot.blockers)?shot.blockers:[]).map(normalizeBlocker)
      };
    }).sort(function(a,b){return a.sort_order-b.sort_order;});
    var readiness=input.readiness||{},actions=input.actions||{};
    return {
      project_id:text(input.project_id),revision:number(input.revision),stage:text(input.stage||'assembly_review'),
      ratio:text(input.ratio||'9:16'),target_duration:number(input.target_duration),
      assembly_revision:number(input.assembly_revision,1),implementation_status:text(input.implementation_status),
      rendering_enabled:input.rendering_enabled===true,current_final_version:input.current_final_version==null?null:number(input.current_final_version),
      shots:shots,versions:(Array.isArray(input.versions)?input.versions:[]).map(clone),
      active_job:input.active_job?clone(input.active_job):null,
      readiness:{ready:readiness.ready===true,blockers:(Array.isArray(readiness.blockers)?readiness.blockers:[]).map(normalizeBlocker)},
      actions:{can_export:actions.can_export===true,can_confirm:actions.can_confirm===true},
      busy:options.busy===true,error:text(options.error),canEdit:options.canEdit!==false
    };
  }
  function finalVersion(state){
    return state.versions.find(function(version){
      return version.kind==='final'&&version.status==='succeeded'&&number(version.version)===state.current_final_version;
    })||null;
  }
  function renderWorkspace(input,options){
    var state=normalizeState(input,options),final=finalVersion(state);
    if(state.busy&&!state.project_id) return '<section class="nc-sda-state">正在加载合成工作区…</section>';
    if(state.error&&!state.project_id) return '<section class="nc-sda-state is-error"><strong>合成工作区加载失败</strong><span>'+escapeHtml(state.error)+'</span></section>';
    if(!state.shots.length) return '<section class="nc-sda-state">暂无可合成镜头。</section>';
    var rail=state.shots.map(function(shot){
      return '<article class="nc-sda-shot '+(shot.ready?'is-ready':'is-blocked')+'"><div><strong>'+escapeHtml(shot.shot_key)+'</strong><small>'+shot.duration+
        ' 秒</small></div><dl><div><dt>视频版本</dt><dd>'+(shot.video.confirmed?'已锁定':'未锁定')+'</dd></div><div><dt>音轨</dt><dd>'+
        (shot.voice.locked?'已确认':'未确认')+'</dd></div></dl><span class="nc-sda-status">'+(shot.ready?'已就绪':'待补齐')+'</span></article>';
    }).join('');
    var messages=[],seen={};state.readiness.blockers.forEach(function(item){if(item.message&&!seen[item.message]){seen[item.message]=true;messages.push(item.message);}});
    var readiness=messages.length?'<ul>'+messages.map(function(message){return '<li>'+escapeHtml(message)+'</li>';}).join('')+'</ul>':'<p>全部镜头已满足合成条件。</p>';
    var player=final?'<video controls playsinline preload="metadata" src="'+escapeHtml(final.url)+'"></video><div class="nc-sda-media-meta">正式版 v'+
      number(final.version)+' · '+Math.round(number(final.duration_ms)/1000)+' 秒 · '+number(final.width)+'×'+number(final.height)+'</div>':
      '<div class="nc-sda-player-placeholder"><div class="nc-sda-play-icon">▶</div><strong>等待生成正式成片</strong><span>将按镜头顺序拼接已锁定的视频，并保留各段原声。</span></div>';
    var progress=state.active_job?'<section class="nc-sda-progress"><div><strong>'+escapeHtml(state.active_job.phase||'合成中')+'</strong><span>'+number(state.active_job.progress)+'%</span></div><progress max="100" value="'+number(state.active_job.progress)+'"></progress></section>':'';
    return '<section class="nc-sda-workspace"><aside class="nc-sda-rail"><header><span>最终装配</span><h2>镜头清单</h2><small>'+state.shots.length+' 镜 · '+
      state.target_duration+' 秒 · '+escapeHtml(state.ratio)+'</small></header><div class="nc-sda-shot-list">'+rail+'</div></aside><main class="nc-sda-preview"><header><span>成片预览</span><h2>短剧正式版</h2></header>'+player+
      '<div class="nc-sda-timeline"><div class="nc-sda-time-head"><span>00:00</span><span>'+state.target_duration+'s</span></div><div class="nc-sda-track"><span style="width:100%"></span></div><small>首版采用硬切，镜头时长严格跟随分镜。</small></div></main><aside class="nc-sda-console"><header><span>D 阶段</span><h2>合成控制台</h2><small>装配修订 r'+state.assembly_revision+
      '</small></header>'+progress+(state.error?'<p class="nc-sda-error" role="alert">'+escapeHtml(state.error)+'</p>':'')+'<section class="nc-sda-readiness '+(state.readiness.ready?'is-ready':'is-blocked')+'"><strong>'+
      (state.readiness.ready?'已就绪':'待补齐')+'</strong>'+readiness+'</section><div class="nc-sda-actions"><button type="button" data-action="export-final"'+
      ((state.canEdit&&!state.busy&&state.actions.can_export)?'':' disabled')+'>生成 '+state.target_duration+' 秒成片</button><button type="button" class="is-primary" data-action="confirm-completed"'+
      ((state.canEdit&&!state.busy&&state.actions.can_confirm)?'':' disabled')+'>确认成片并完成</button></div><p class="nc-sda-contract-note">合成免费；视频模型生成仍按每个镜头的实时报价扣点。</p></aside></section>';
  }
  function createWorkspace(options){
    options=options||{};
    if(!options.client||typeof options.client.json!=='function') throw new Error('短剧合成工作区缺少已认证 API 客户端');
    var client=options.client,host=options.host||null,destroyed=false,snapshot=null,timer=null;
    var ui={busy:true,error:''},lastSummary='';
    function callJson(path,requestOptions){
      var scoped=requestOptions?Object.assign({},requestOptions):{};
      if(options.boardId) scoped.headers=Object.assign({},scoped.headers||{}, {'X-Canvas-Board-Id':String(options.boardId)});
      return client.json(path,scoped);
    }
    function viewOptions(){ return {busy:ui.busy,error:ui.error,canEdit:options.canEdit!==false&&(!snapshot||snapshot.stage!=='completed')}; }
    function render(){ var html=renderWorkspace(snapshot||{},viewOptions());if(host&&!destroyed) host.innerHTML=html;return html; }
    function publish(){
      if(!snapshot||typeof options.onChange!=='function') return Promise.resolve();
      var summary={project_id:snapshot.project_id,revision:snapshot.revision,stage:snapshot.stage,ratio:snapshot.ratio};
      var key=JSON.stringify(summary);if(key===lastSummary)return Promise.resolve();lastSummary=key;return Promise.resolve(options.onChange(summary));
    }
    function schedule(){
      if(timer!=null){clearTimeout(timer);timer=null;}
      if(destroyed||!snapshot||!snapshot.active_job)return;
      timer=setTimeout(function(){timer=null;reload(false).catch(function(){});},3000);
    }
    function accept(result){ snapshot=result&&typeof result==='object'?result:{};ui.busy=false;ui.error='';render();schedule();return publish().then(function(){return snapshot;}); }
    function fail(error){if(!destroyed){ui.busy=false;ui.error=text(error&&error.message||error);render();schedule();}throw error;}
    function reload(showBusy){
      if(destroyed)return Promise.resolve(null);if(showBusy!==false){ui.busy=true;ui.error='';render();}
      return Promise.resolve(callJson(STATE_PATH+'?project_id='+encodeURIComponent(options.projectId))).then(accept).catch(fail);
    }
    function mutation(path,body){
      if(destroyed||!snapshot||options.canEdit===false||snapshot.stage!=='assembly_review'||ui.busy)return Promise.reject(new Error('当前不能修改成片'));
      ui.busy=true;ui.error='';render();return Promise.resolve(callJson(path,{method:'POST',body:body})).then(accept).catch(fail);
    }
    function renderFinal(){
      return mutation(RENDER_PATH,{project_id:snapshot.project_id,revision:number(snapshot.revision),
        idempotency_key:'sd-final-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10)});
    }
    function confirmCompleted(){ return mutation(CONFIRM_PATH,{project_id:snapshot.project_id,revision:number(snapshot.revision)}); }
    function onClick(event){
      var node=event&&event.target;
      while(node&&node!==host&&!(node.getAttribute&&node.getAttribute('data-action')))node=node.parentNode;
      if(!node||node===host)return;var action=node.getAttribute('data-action');
      if(action==='export-final')renderFinal().catch(function(){});
      else if(action==='confirm-completed')confirmCompleted().catch(function(){});
    }
    if(host&&host.addEventListener)host.addEventListener('click',onClick);
    render();var ready=reload();
    return {projectId:options.projectId,ready:ready,render:render,reload:reload,renderFinal:renderFinal,confirmCompleted:confirmCompleted,
      getState:function(){return clone(normalizeState(snapshot||{},viewOptions()));},destroy:function(){destroyed=true;if(timer!=null)clearTimeout(timer);timer=null;if(host&&host.removeEventListener)host.removeEventListener('click',onClick);host=null;snapshot=null;}};
  }
  return {normalizeState:normalizeState,renderWorkspace:renderWorkspace,createWorkspace:createWorkspace};
});
