(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.shortDramaVideo=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var STATE_PATH='/api/gen/short-drama/video';
  var QUOTE_PATH='/api/gen/short-drama/video-quote';
  var GENERATE_PATH='/api/gen/short-drama/generate-video';
  var SELECT_PATH='/api/gen/short-drama/select-video';
  var CONFIRM_PATH='/api/gen/short-drama/confirm-video-stage';

  function text(value){ return String(value==null?'':value); }
  function number(value,fallback){ var n=Number(value);return isFinite(n)?n:(fallback||0); }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function clone(value){
    if(Array.isArray(value)) return value.map(clone);
    if(value&&typeof value==='object'){
      var copy={};Object.keys(value).forEach(function(key){ copy[key]=clone(value[key]); });return copy;
    }
    return value;
  }
  function active(job){ return !!job&&(job.status==='pending'||job.status==='running'); }
  function normalizeState(input,options){
    input=input&&typeof input==='object'?input:{};options=options||{};
    var models=(Array.isArray(input.models)?input.models:[]).map(function(model){
      return {
        channel:text(model.channel),label:text(model.label||model.channel),
        model:text(model.model),resolution:text(model.resolution),
        upscale:model.upscale===true,enabled:model.enabled===true
      };
    });
    var shots=(Array.isArray(input.shots)?input.shots:[]).map(function(shot,index){
      shot=shot&&typeof shot==='object'?shot:{};
      var video=shot.video&&typeof shot.video==='object'?shot.video:{};
      var versions=(Array.isArray(video.versions)?video.versions:[]).map(clone);
      return {
        id:text(shot.id),shot_key:text(shot.shot_key||('镜头 '+(index+1))),
        sort_order:number(shot.sort_order,index),duration:number(shot.duration,0),
        scene_description:text(shot.scene_description),video_prompt:text(shot.video_prompt),
        video:{asset_id:video.asset_id||null,current_version:video.current_version==null?null:number(video.current_version),
          locked:video.locked===true,versions:versions,job:video.job?clone(video.job):null}
      };
    }).sort(function(left,right){ return left.sort_order-right.sort_order; });
    var selected=options.selectedShotId||input.selectedShotId;
    if(!shots.some(function(shot){ return shot.id===selected; })) selected=shots[0]&&shots[0].id;
    var selectedModelKey=text(options.modelKey);
    if(!models.some(function(model){ return model.channel===selectedModelKey&&model.enabled; })){
      var first=models.find(function(model){ return model.enabled; });selectedModelKey=first?first.channel:'';
    }
    return {
      project_id:text(input.project_id),revision:number(input.revision),stage:text(input.stage||'video_review'),
      ratio:text(input.ratio||'9:16'),target_duration:number(input.target_duration),
      point_budget:number(input.point_budget),spent_points:number(input.spent_points),
      reserved_points:number(input.reserved_points),models:models,shots:shots,
      ready:input.ready===true,selectedShotId:selected,modelKey:selectedModelKey,
      prompts:options.prompts||{},busy:options.busy===true,error:text(options.error),
      canEdit:options.canEdit!==false
    };
  }
  function selectedShot(state){
    return state.shots.find(function(shot){ return shot.id===state.selectedShotId; })||null;
  }
  function currentVersion(shot){
    if(!shot) return null;
    return shot.video.versions.find(function(version){
      return number(version.version)===shot.video.current_version;
    })||shot.video.versions[0]||null;
  }
  function statusLabel(shot){
    if(shot.video.locked) return '已锁定';
    if(active(shot.video.job)) return '生成中';
    if(shot.video.versions.length) return '待选择';
    if(shot.video.job&&shot.video.job.status==='failed') return '生成失败';
    return '待生成';
  }
  function renderWorkspace(input,options){
    var state=normalizeState(input,options),shot=selectedShot(state);
    if(state.busy&&!state.project_id){
      return '<section class="nc-sdvideo-state">正在加载视频工作台…</section>';
    }
    if(state.error&&!state.project_id){
      return '<section class="nc-sdvideo-state is-error"><strong>视频工作台加载失败</strong><span>'+escapeHtml(state.error)+'</span></section>';
    }
    if(!shot){ return '<section class="nc-sdvideo-state">暂无可生成镜头。</section>'; }
    var rail=state.shots.map(function(item){
      return '<button type="button" class="nc-sdvideo-shot'+(item.id===shot.id?' is-selected':'')+
        '" data-shot-id="'+escapeHtml(item.id)+'"><span>'+escapeHtml(item.shot_key)+'</span><small>'+item.duration+
        ' 秒 · '+statusLabel(item)+'</small></button>';
    }).join('');
    var chosen=currentVersion(shot);
    var player=chosen?'<video controls playsinline preload="metadata" src="'+escapeHtml(chosen.url)+'"></video>':
      '<div class="nc-sdvideo-empty"><span>▶</span><strong>还没有镜头视频</strong><small>锁定关键帧后，选择模型开始生成。</small></div>';
    var versions=shot.video.versions.map(function(version){
      var isCurrent=number(version.version)===shot.video.current_version;
      return '<article class="nc-sdvideo-version'+(isCurrent?' is-current':'')+'"><video controls playsinline preload="metadata" src="'+
        escapeHtml(version.url)+'"></video><div><strong>v'+number(version.version)+'</strong><small>'+escapeHtml(version.channel)+' · '+
        escapeHtml(version.resolution)+'</small></div><button type="button" data-version="'+number(version.version)+
        '" data-lock="false"'+((state.canEdit&&!state.busy&&!shot.video.locked)?'':' disabled')+'>选中</button><button type="button" data-version="'+
        number(version.version)+'" data-lock="true"'+((state.canEdit&&!state.busy&&!shot.video.locked)?'':' disabled')+'>选中并锁定</button></article>';
    }).join('');
    var modelOptions=state.models.map(function(model){
      return '<option value="'+escapeHtml(model.channel)+'"'+(model.channel===state.modelKey?' selected':'')+
        (model.enabled?'':' disabled')+'>'+escapeHtml(model.label)+(model.enabled?'':'（暂不可用）')+'</option>';
    }).join('');
    var prompt=Object.prototype.hasOwnProperty.call(state.prompts,shot.id)?state.prompts[shot.id]:shot.video_prompt;
    var job=shot.video.job;
    var jobInfo=job?'<section class="nc-sdvideo-job '+(job.status==='failed'?'is-error':'')+'"><strong>'+escapeHtml(job.phase||job.status)+
      '</strong><span>任务 #'+number(job.job_id)+(job.error?' · '+escapeHtml(job.error):'')+'</span></section>':'';
    return '<section class="nc-sdvideo-workspace"><aside class="nc-sdvideo-rail"><header><span>C-3 视频工作台</span><h2>分镜任务</h2><small>'+
      state.shots.length+' 镜 · '+state.target_duration+' 秒</small></header>'+rail+'</aside><main class="nc-sdvideo-main"><header><span>'+escapeHtml(shot.shot_key)+
      '</span><h2>'+escapeHtml(shot.scene_description||'镜头预览')+'</h2></header><div class="nc-sdvideo-player">'+player+'</div><section class="nc-sdvideo-versions"><header><h3>候选版本</h3><small>'+
      shot.video.versions.length+' 个</small></header>'+(versions||'<p>生成成功后会在这里保留每个版本。</p>')+'</section></main><aside class="nc-sdvideo-console"><header><span>模型与提示词</span><h2>生成控制台</h2></header>'+jobInfo+
      (state.error?'<p class="nc-sdvideo-error" role="alert">'+escapeHtml(state.error)+'</p>':'')+'<label>视频模型<select data-field="model"'+
      ((state.canEdit&&!state.busy&&!shot.video.locked)?'':' disabled')+'>'+modelOptions+'</select></label><label>动作与镜头提示<textarea data-field="prompt"'+
      ((state.canEdit&&!state.busy&&!shot.video.locked)?'':' disabled')+'>'+escapeHtml(prompt)+'</textarea></label><dl><div><dt>已使用</dt><dd>'+state.spent_points+
      ' 点</dd></div><div><dt>生成中预留</dt><dd>'+state.reserved_points+' 点</dd></div><div><dt>项目预算</dt><dd>'+state.point_budget+
      ' 点</dd></div></dl><button type="button" data-action="generate"'+((state.canEdit&&!state.busy&&!shot.video.locked&&!active(job)&&state.modelKey)?'':' disabled')+'>'+
      (shot.video.versions.length?'再生成一个版本':'生成当前镜头')+'</button><button type="button" data-action="unlock"'+((state.canEdit&&!state.busy&&shot.video.locked)?'':' disabled')+'>解锁当前版本</button><button type="button" class="is-primary" data-action="confirm-stage"'+
      ((state.canEdit&&!state.busy&&state.ready)?'':' disabled')+'>全部锁定，进入成片合成</button><p>每次提交前都会显示实时报价；Seedance 默认走 480P + AI 超清。</p></aside></section>';
  }
  function createWorkspace(options){
    options=options||{};
    if(!options.client||typeof options.client.json!=='function') throw new Error('视频工作台缺少已认证 API 客户端');
    if(!options.projectId) throw new Error('视频工作台缺少短剧项目');
    var client=options.client,host=options.host||null,destroyed=false,snapshot=null,timer=null;
    var ui={busy:true,error:'',selectedShotId:null,modelKey:'',prompts:{}};
    var lastSummary='';
    function callJson(path,requestOptions){
      var scoped=requestOptions?Object.assign({},requestOptions):{};
      if(options.boardId){ scoped.headers=Object.assign({},scoped.headers||{}, {'X-Canvas-Board-Id':String(options.boardId)}); }
      return client.json(path,scoped);
    }
    function view(){ return normalizeState(snapshot||{},Object.assign({},ui,{canEdit:options.canEdit!==false})); }
    function render(){ var html=renderWorkspace(snapshot||{},Object.assign({},ui,{canEdit:options.canEdit!==false}));if(host&&!destroyed) host.innerHTML=html;return html; }
    function publish(){
      if(!snapshot||typeof options.onChange!=='function') return Promise.resolve();
      var summary={project_id:snapshot.project_id,revision:snapshot.revision,stage:snapshot.stage,ratio:snapshot.ratio,
        point_budget:snapshot.point_budget,spent_points:snapshot.spent_points,reserved_points:snapshot.reserved_points};
      var key=JSON.stringify(summary);if(key===lastSummary) return Promise.resolve();lastSummary=key;
      return Promise.resolve(options.onChange(summary));
    }
    function schedule(){
      if(timer!=null){ clearTimeout(timer);timer=null; }
      if(destroyed||!snapshot||!snapshot.shots.some(function(shot){ return active(shot.video&&shot.video.job); })) return;
      timer=setTimeout(function(){ timer=null;reload(false).catch(function(){}); },4000);
    }
    function accept(result){
      snapshot=result&&typeof result==='object'?result:{};
      var normalized=view();ui.selectedShotId=normalized.selectedShotId;ui.modelKey=normalized.modelKey;
      normalized.shots.forEach(function(shot){ if(!Object.prototype.hasOwnProperty.call(ui.prompts,shot.id)) ui.prompts[shot.id]=shot.video_prompt; });
      ui.busy=false;ui.error='';render();schedule();return publish().then(function(){ return snapshot; });
    }
    function fail(error){ if(!destroyed){ ui.busy=false;ui.error=text(error&&error.message||error);render();schedule(); }throw error; }
    function reload(showBusy){
      if(destroyed) return Promise.resolve(null);if(showBusy!==false){ ui.busy=true;ui.error='';render(); }
      return Promise.resolve(callJson(STATE_PATH+'?project_id='+encodeURIComponent(options.projectId))).then(accept).catch(fail);
    }
    function writable(){
      if(destroyed||!snapshot) throw new Error('视频工作台尚未加载');
      if(options.canEdit===false||snapshot.stage!=='video_review') throw new Error('当前视频工作台不可编辑');
      if(ui.busy) throw new Error('请等待当前操作完成');
    }
    function selected(){ return selectedShot(view()); }
    function mutation(path,body,headers){
      try{ writable(); }catch(error){ return Promise.reject(error); }
      ui.busy=true;ui.error='';render();
      return Promise.resolve(callJson(path,{method:'POST',body:body,headers:headers||{}})).then(accept).catch(fail);
    }
    function selectVersion(version,lock){
      var shot=selected();if(!shot||!shot.video.asset_id) return Promise.reject(new Error('视频版本不存在'));
      return mutation(SELECT_PATH,{project_id:snapshot.project_id,revision:number(snapshot.revision),asset_id:shot.video.asset_id,version:number(version),lock:!!lock});
    }
    function generate(){
      try{ writable(); }catch(error){ return Promise.reject(error); }
      var state=view(),shot=selectedShot(state),model=state.models.find(function(item){ return item.channel===state.modelKey&&item.enabled; });
      if(!shot||!model) return Promise.reject(new Error('请选择当前可用的视频模型'));
      if(shot.video.locked||active(shot.video.job)) return Promise.reject(new Error('当前镜头不能重复提交'));
      var body={project_id:state.project_id,revision:state.revision,shot_id:shot.id,channel:model.channel,model:model.model,
        prompt:text(ui.prompts[shot.id]||shot.video_prompt).trim(),resolution:model.resolution,upscale:model.upscale,generate_audio:true};
      if(!body.prompt) return Promise.reject(new Error('请填写动作与镜头提示词'));
      ui.busy=true;ui.error='';render();
      return Promise.resolve(callJson(QUOTE_PATH,{method:'POST',body:body})).then(function(quote){
        if(!quote||typeof quote.cost!=='number'||!quote.quote_token) throw new Error('视频实时报价无效');
        if(typeof options.confirm==='function'&&options.confirm(quote.cost,{kind:'video',model:model.label},body)===false){
          ui.busy=false;render();return null;
        }
        var submitted=Object.assign({},body,{quote_token:quote.quote_token});
        var key='sd-video-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10);
        return callJson(GENERATE_PATH,{method:'POST',body:submitted,headers:{'Idempotency-Key':key}}).then(function(){ return reload(false); });
      }).catch(fail);
    }
    function confirmStage(){
      var state=view();if(!state.ready) return Promise.reject(new Error('请先锁定全部镜头的视频版本'));
      return mutation(CONFIRM_PATH,{project_id:state.project_id,revision:state.revision,stage:'video_review'});
    }
    function onClick(event){
      var node=event&&event.target;
      while(node&&node!==host){
        if(node.getAttribute&&node.getAttribute('data-shot-id')!=null){ ui.selectedShotId=node.getAttribute('data-shot-id');render();return; }
        if(node.getAttribute&&node.getAttribute('data-version')!=null){ selectVersion(node.getAttribute('data-version'),node.getAttribute('data-lock')==='true').catch(function(){});return; }
        if(node.getAttribute&&node.getAttribute('data-action')){
          var action=node.getAttribute('data-action');
          if(action==='generate') generate().catch(function(){});
          else if(action==='unlock'){
            var shot=selected(),version=shot&&shot.video.current_version;
            if(version) selectVersion(version,false).catch(function(){});
          }else if(action==='confirm-stage') confirmStage().catch(function(){});
          return;
        }
        node=node.parentNode;
      }
    }
    function onInput(event){
      var target=event&&event.target,field=target&&target.getAttribute&&target.getAttribute('data-field');
      if(field==='prompt'){ var shot=selected();if(shot) ui.prompts[shot.id]=target.value; }
      else if(field==='model'){ ui.modelKey=target.value; }
    }
    if(host&&host.addEventListener){ host.addEventListener('click',onClick);host.addEventListener('input',onInput);host.addEventListener('change',onInput); }
    render();var ready=reload();
    return {projectId:options.projectId,ready:ready,render:render,reload:reload,generate:generate,selectVersion:selectVersion,
      confirmStage:confirmStage,getState:function(){ return clone(view()); },destroy:function(){
        destroyed=true;if(timer!=null) clearTimeout(timer);timer=null;
        if(host&&host.removeEventListener){ host.removeEventListener('click',onClick);host.removeEventListener('input',onInput);host.removeEventListener('change',onInput); }
        host=null;snapshot=null;
      }};
  }
  return {normalizeState:normalizeState,renderWorkspace:renderWorkspace,createWorkspace:createWorkspace};
});
