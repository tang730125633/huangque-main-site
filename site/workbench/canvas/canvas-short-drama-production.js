(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.shortDramaProduction=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  var PRODUCTION_PATH='/api/gen/short-drama/production';
  var QUOTE_PATH='/api/gen/short-drama/asset-quote';
  var GENERATE_PATH='/api/gen/short-drama/generate-stills';
  var SELECT_PATH='/api/gen/short-drama/select-asset';
  var CONFIRM_PATH='/api/gen/short-drama/confirm-production-stage';
  var KNOWN_JOB_MISSING_LIMIT=3;
  var BATCH_WAVE_SIZE=5;

  function isActiveJobStatus(status){ return status==='pending'||status==='running'; }

  function clone(value){
    if(Array.isArray(value)) return value.map(clone);
    if(value&&typeof value==='object'){
      var copy={};
      Object.keys(value).forEach(function(key){ copy[key]=clone(value[key]); });
      return copy;
    }
    return value;
  }

  function text(value){ return String(value==null?'':value); }
  function number(value,fallback){
    var result=Number(value);
    return isFinite(result)?result:(fallback==null?0:fallback);
  }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function safeUrl(value){
    var url=text(value).trim();
    if(!url) return '';
    if(/^(?:https?:|blob:|\/|\.\/|\.\.\/)/i.test(url)) return escapeHtml(url);
    return '#';
  }
  function disabledUnless(enabled){ return enabled?'':' disabled'; }
  function checked(value){ return value?' checked':''; }
  function selected(value){ return value?' is-selected':''; }

  function normalizeVersion(version){
    version=version&&typeof version==='object'?version:{};
    return {
      id:version.id,
      version:number(version.version,0),
      job_id:version.job_id,
      url:text(version.url),
      prompt:text(version.prompt),
      ratio:version.ratio==='16:9'?'16:9':(version.ratio==='9:16'?'9:16':''),
      cost:Math.max(0,number(version.cost,0)),
      unit_cost:Math.max(0,number(version.unit_cost,version.cost||0)),
      batch_cost:Math.max(0,number(version.batch_cost,0)),
      provider:text(version.provider),
      model:text(version.model),
      quality:text(version.quality),
      generation_batch_id:text(version.generation_batch_id||version.job_id),
      status:text(version.status||'failed'),
      created_at:number(version.created_at,0)
    };
  }

  function normalizeJob(job){
    if(!job||typeof job!=='object') return null;
    var status=text(job.status||'pending');
    if(['pending','running','done','failed'].indexOf(status)<0) return null;
    return {
      id:job.id,
      job_id:job.job_id,
      kind:text(job.kind||'still'),
      status:status,
      quoted_cost:Math.max(0,number(job.quoted_cost,0)),
      error:text(job.error),
      refunded:!!job.refunded,
      refund_pending:!!job.refund_pending,
      operation_terminal:job.operation_terminal===true
    };
  }

  function normalizeShot(shot,index){
    shot=shot&&typeof shot==='object'?shot:{};
    var still=shot.still&&typeof shot.still==='object'?shot.still:{};
    var references=Array.isArray(shot.references)?shot.references.map(function(item){
      if(item&&typeof item==='object') return {id:item.id,name:text(item.name||item.label),url:text(item.url)};
      return text(item);
    }):[];
    return {
      id:shot.id,
      shot_key:text(shot.shot_key||('镜头 '+(index+1))),
      sort_order:number(shot.sort_order,index),
      duration:Math.max(0,number(shot.duration,0)),
      image_prompt:text(shot.image_prompt),
      image_prompt_hash:text(shot.image_prompt_hash),
      references:references,
      still:{
        asset_id:still.asset_id,
        current_version:still.current_version==null?null:number(still.current_version,0),
        locked:!!still.locked,
        versions:(Array.isArray(still.versions)?still.versions:[]).map(normalizeVersion),
        job:normalizeJob(still.job)
      },
      _order:index
    };
  }

  function normalizeBlockers(input){
    return (Array.isArray(input)?input:[]).map(function(item){
      item=item&&typeof item==='object'?item:{};
      return {
        code:text(item.code),
        message:text(item.message),
        shot_id:item.shot_id==null?null:text(item.shot_id)
      };
    }).filter(function(item){ return item.code&&item.message; });
  }

  function normalizeState(input,options){
    input=input&&typeof input==='object'?input:{};
    options=options&&typeof options==='object'?options:{};
    var shots=(Array.isArray(input.shots)?input.shots:[]).map(normalizeShot);
    shots.sort(function(left,right){
      return left.sort_order-right.sort_order||left._order-right._order;
    });
    shots.forEach(function(shot){ delete shot._order; });
    var requested=Object.prototype.hasOwnProperty.call(options,'selectedShotId')?
      options.selectedShotId:input.selectedShotId;
    var found=shots.some(function(shot){ return shot.id===requested; });
    var promptSource=options.prompts||input.prompts||{};
    var prompts={};
    shots.forEach(function(shot){
      prompts[shot.id]=Object.prototype.hasOwnProperty.call(promptSource,shot.id)?
        text(promptSource[shot.id]):'';
    });
    return {
      project_id:input.project_id,
      revision:Math.max(0,number(input.revision,0)),
      stage:text(input.stage||''),
      ratio:input.ratio==='16:9'?'16:9':'9:16',
      point_budget:Math.max(0,number(input.point_budget,0)),
      spent_points:Math.max(0,number(input.spent_points,0)),
      reserved_points:Math.max(0,number(input.reserved_points,0)),
      handoff_blocked:!!input.handoff_blocked,
      handoff_blockers:normalizeBlockers(input.handoff_blockers),
      shots:shots,
      selectedShotId:found?requested:(shots[0]?shots[0].id:null),
      filter:text(options.filter!=null?options.filter:(input.filter||'all')),
      prompts:prompts,
      canEdit:options.canEdit!=null?options.canEdit!==false:input.canEdit!==false,
      busy:options.busy!=null?!!options.busy:!!input.busy,
      stale:options.stale!=null?!!options.stale:!!input.stale,
      destroyed:options.destroyed!=null?!!options.destroyed:!!input.destroyed,
      error:text(options.error!=null?options.error:input.error),
      quote:clone(options.quote!=null?options.quote:input.quote),
      promptNotices:clone(options.promptNotices!=null?
        options.promptNotices:(input.promptNotices||{})),
      lastMode:text(options.lastMode!=null?options.lastMode:input.lastMode),
      submittedShotIds:(Array.isArray(options.submittedShotIds)?options.submittedShotIds:
        (Array.isArray(input.submittedShotIds)?input.submittedShotIds:[])).slice()
    };
  }

  function summarizeProductionState(input){
    var state=normalizeState(input);
    return {
      project_id:state.project_id,
      revision:state.revision,
      stage:state.stage,
      ratio:state.ratio,
      spent_points:state.spent_points,
      point_budget:state.point_budget,
      reserved_points:state.reserved_points
    };
  }

  function selectedShot(state){
    for(var index=0;index<state.shots.length;index+=1){
      if(state.shots[index].id===state.selectedShotId) return state.shots[index];
    }
    return state.shots[0]||null;
  }

  function shotStatus(shot){
    var job=shot.still.job;
    if(job&&isActiveJobStatus(job.status)) return job.status;
    if(job&&job.status==='failed') return 'failed';
    if(shot.still.locked) return 'locked';
    var current=null;
    shot.still.versions.forEach(function(version){
      if(version.version===shot.still.current_version) current=version;
    });
    if(current&&current.status==='done') return 'done';
    if(shot.still.versions.some(function(version){ return version.status==='failed'; })) return 'failed';
    return 'pending';
  }

  function renderFilters(state){
    var filters=[['all','全部'],['pending','待生成'],['running','生成中'],['done','待锁定'],['failed','失败'],['locked','已锁定']];
    return '<div class="nc-sdp-filters" role="group" aria-label="镜头状态筛选">'+filters.map(function(item){
      return '<button type="button" data-filter="'+item[0]+'" class="'+(state.filter===item[0]?'is-active':'')+'">'+item[1]+'</button>';
    }).join('')+'</div>';
  }

  function renderShotRail(state){
    var visible=state.shots.filter(function(shot){ return state.filter==='all'||shotStatus(shot)===state.filter; });
    return '<aside class="nc-sdp-shot-rail"><header><span class="nc-sdp-kicker">分镜生产</span><h2>镜头列表</h2></header>'+renderFilters(state)+
      '<div class="nc-sdp-shot-list">'+(visible.length?visible.map(function(shot,index){
        var status=shotStatus(shot);
        return '<button type="button" class="nc-sdp-shot-card'+selected(shot.id===state.selectedShotId)+'" data-action="select-shot" data-shot-id="'+escapeHtml(shot.id)+'">'+
          '<span class="nc-sdp-shot-index">#'+String(shot.sort_order+1).padStart(2,'0')+'</span><strong>'+escapeHtml(shot.shot_key)+'</strong><small>'+shot.duration+' 秒</small><i data-status="'+status+'">'+
          ({pending:'待生成',running:'生成中',done:'待锁定',failed:'失败',locked:'已锁定'})[status]+'</i></button>';
      }).join(''):'<p class="nc-sdp-empty">没有符合筛选条件的镜头</p>')+'</div></aside>';
  }

  function renderReferences(shot){
    if(!shot.references.length) return '<p class="nc-sdp-empty">暂无参考素材，生成将使用已确认的角色与分镜上下文。</p>';
    return '<ul class="nc-sdp-reference-list">'+shot.references.map(function(reference){
      if(reference&&typeof reference==='object'){
        return '<li><span>'+escapeHtml(reference.name||reference.id||'参考素材')+'</span>'+
          (reference.url?'<a href="'+safeUrl(reference.url)+'" target="_blank" rel="noopener">查看</a>':'')+'</li>';
      }
      return '<li>'+escapeHtml(reference)+'</li>';
    }).join('')+'</ul>';
  }

  function renderVersionCard(state,shot,version,writable){
    var current=version.version===shot.still.current_version;
    var selectable=writable&&version.status==='done';
    return '<article class="nc-sdp-candidate'+selected(current)+'" data-version-id="'+escapeHtml(version.id)+'">'+
      '<div class="nc-sdp-preview" data-ratio="'+state.ratio+'">'+
      (version.url?'<img src="'+safeUrl(version.url)+'" alt="'+escapeHtml(shot.shot_key)+' 关键帧版本 '+version.version+'">':'<span>无预览</span>')+'</div>'+
      '<div class="nc-sdp-candidate-meta"><strong>版本 '+version.version+(current?' · 当前':'')+'</strong><small>'+
      escapeHtml(version.provider==='banana'?'Nano Banana 2':(version.provider||'历史模型'))+
      (version.quality?' · '+escapeHtml(version.quality.toUpperCase()):'')+' · '+escapeHtml(version.status)+'</small></div>'+
      '<div class="nc-sdp-candidate-actions"><button type="button" data-action="select-version" data-version="'+version.version+'"'+disabledUnless(selectable&&!current)+'>选择</button>'+
      '<button type="button" data-action="lock-version" data-version="'+version.version+'"'+disabledUnless(selectable)+'>选择并锁定</button></div></article>';
  }

  function renderEditor(state){
    var shot=selectedShot(state);
    if(!shot) return '<main class="nc-sdp-editor"><section class="nc-sdp-empty-state"><h2>暂无镜头</h2><p>请先返回分镜阶段创建镜头。</p></section></main>';
    var writable=state.canEdit&&!state.busy&&!state.stale&&!state.destroyed&&state.stage==='stills_review';
    var versions=shot.still.versions;
    var candidates=versions.slice().reverse();
    var seenBatches={};
    var batches=versions.slice().reverse().filter(function(version){
      if(!version.batch_cost&&!version.unit_cost) return false;
      var key=version.generation_batch_id||String(version.job_id||version.version);
      if(seenBatches[key]) return false;
      seenBatches[key]=true;
      return true;
    });
    var direction=text(state.prompts[shot.id]).trim();
    var quote=state.quote&&typeof state.quote==='object'?state.quote:null;
    var quoteMatches=!!(
      quote&&text(quote.shot_id)===text(shot.id)&&
      text(quote.source_prompt_hash)===text(shot.image_prompt_hash)&&
      text(quote.user_direction).trim()===direction
    );
    var promptNotice=state.promptNotices&&state.promptNotices[shot.id];
    return '<main class="nc-sdp-editor"><header class="nc-sdp-editor-header"><div><span class="nc-sdp-kicker">'+escapeHtml(shot.shot_key)+'</span><h2>关键帧候选</h2></div><span class="nc-sdp-ratio">'+state.ratio+'</span></header>'+
      '<section class="nc-sdp-panel nc-sdp-prompt-layers">'+
      '<label class="nc-sdp-prompt">分镜画面提示词 <small>来自已确认分镜，只读</small>'+
      '<textarea readonly>'+escapeHtml(shot.image_prompt)+'</textarea></label>'+
      '<label class="nc-sdp-prompt">本次生成补充要求 <small>可选；不要重复填写上面的分镜提示词</small>'+
      '<textarea data-field="prompt" placeholder="例如：改成夜景，增加轻微俯拍"'+disabledUnless(writable)+'>'+
      escapeHtml(state.prompts[shot.id])+'</textarea></label>'+
      (promptNotice?'<div class="nc-sdp-prompt-notice" role="status">分镜提示词已更新，当前补充要求仍保留。'+
        '<button type="button" data-action="keep-prompt-supplement">保留补充要求</button>'+
        '<button type="button" data-action="clear-prompt-supplement">清空补充要求</button></div>':'')+
      '<details class="nc-sdp-compiled-prompt"'+(quoteMatches?' open':'')+'><summary>最终提交提示词预览</summary>'+
      (quoteMatches?'<pre>'+escapeHtml(quote.compiled_prompt)+'</pre>':
        '<p>点击“询价并生成”后，系统会先展示本次真正发送给模型的完整提示词。</p>')+
      '</details></section>'+
      '<section class="nc-sdp-panel"><h3>参考素材</h3>'+renderReferences(shot)+'</section>'+
      '<section class="nc-sdp-candidate-grid">'+(candidates.length?candidates.map(function(version){ return renderVersionCard(state,shot,version,writable); }).join(''):'<div class="nc-sdp-empty" data-ratio="'+state.ratio+'">尚未生成关键帧候选</div>')+'</section>'+
      (batches.length?'<section class="nc-sdp-panel nc-sdp-history"><h3>生成批次与点数</h3><ol>'+batches.map(function(version){
        var total=version.batch_cost||version.unit_cost*2;
        return '<li><span>'+escapeHtml(version.generation_batch_id||String(version.job_id))+'</span><strong>'+
          escapeHtml(version.provider==='banana'?'Nano Banana 2':(version.provider||'历史模型'))+
          (version.quality?' · '+escapeHtml(version.quality.toUpperCase()):'')+
          '</strong><small>'+version.unit_cost+' 点/张 × 2 = '+total+' 点</small></li>';
      }).join('')+'</ol></section>':'')+
      '<section class="nc-sdp-panel nc-sdp-history"><h3>历史版本</h3>'+(versions.length?'<ol>'+versions.map(function(version){
        return '<li><span>V'+version.version+'</span><strong>'+escapeHtml(version.prompt||'无提示词')+'</strong><small>'+escapeHtml(version.id)+' · '+escapeHtml(version.status)+'</small></li>';
      }).join('')+'</ol>':'<p class="nc-sdp-empty">暂无历史版本</p>')+'</section></main>';
  }

  function allShotsLocked(state){
    return state.shots.length>0&&state.shots.every(function(shot){
      return shot.still.locked&&shotHasCompletedCurrent(state,shot)&&
        !(shot.still.job&&isActiveJobStatus(shot.still.job.status));
    });
  }

  function shotHasCompletedCurrent(state,shot){
    if(!shot||shot.still.current_version==null) return false;
    return shot.still.versions.some(function(version){
      return version.version===shot.still.current_version&&version.status==='done'&&version.ratio===state.ratio;
    });
  }

  function renderInspector(state){
    var shot=selectedShot(state),job=shot&&shot.still.job;
    var writable=state.canEdit&&!state.busy&&!state.stale&&!state.destroyed&&state.stage==='stills_review';
    var quote=state.quote&&typeof state.quote==='object'?number(state.quote.cost,0):number(state.quote,0);
    var budget=state.point_budget===0?'不限':state.point_budget+' 点';
    var confirmable=writable&&!state.handoff_blocked&&allShotsLocked(state);
    var batchable=writable&&state.shots.some(function(item){
      return !item.still.locked&&!shotHasCompletedCurrent(state,item)&&
        state.submittedShotIds.indexOf(item.id)<0&&
        !(item.still.job&&isActiveJobStatus(item.still.job.status));
    });
    return '<aside class="nc-sdp-inspector"><header><span class="nc-sdp-kicker">关键帧生产</span><h2>生成控制台</h2></header>'+
      '<section class="nc-sdp-cost"><span>本次实时报价</span><strong>'+(state.quote==null?'待查询':quote+' 点')+'</strong><small>'+
      (state.quote&&state.quote.shot_count?state.quote.shot_count+' 个镜头 · ':'')+'生成前报价并显式确认；取消不会提交。</small></section>'+
      '<dl><div><dt>项目预算</dt><dd>'+budget+'</dd></div><div><dt>已花费</dt><dd>'+state.spent_points+' 点</dd></div><div><dt>已预留</dt><dd>'+state.reserved_points+' 点</dd></div><div><dt>当前版本</dt><dd>R'+state.revision+'</dd></div></dl>'+
      (job?'<section class="nc-sdp-progress" data-status="'+escapeHtml(job.status)+'"><strong>'+escapeHtml(
        job.status==='running'?'正在生成':job.status==='pending'?'等待生成':job.status==='failed'?'生成失败':'生成完成'
      )+'</strong><small>任务 '+escapeHtml(job.job_id)+' · '+(job.status==='failed'?
        escapeHtml(job.error||'生成失败')+' · '+(job.refunded?'已退款':job.refund_pending?'退款确认中':'请联系客服核对退款'):
        '预留 '+job.quoted_cost+' 点')+'</small></section>':'')+
      (state.handoff_blockers.length?'<section class="nc-sdp-error" role="alert"><strong>暂时无法进入配音</strong><ul>'+state.handoff_blockers.map(function(item){ return '<li>'+escapeHtml(item.message)+'</li>'; }).join('')+'</ul></section>':'')+
      (state.error?'<section class="nc-sdp-error" role="alert"><strong>'+(state.stale?'版本冲突':'操作未完成')+'</strong><p>'+escapeHtml(state.error)+'</p>'+(state.stale?'<button type="button" data-action="refresh">刷新最新版本</button>':'')+'</section>':'')+
      '<div class="nc-sdp-generation-actions"><button type="button" class="is-primary" data-action="generate-current"'+disabledUnless(writable)+'>生成当前镜头</button>'+
      '<button type="button" data-action="retry-current"'+disabledUnless(writable)+'>重试当前镜头</button><button type="button" data-action="generate-batch"'+disabledUnless(batchable)+'>批量模式生成</button></div>'+
      '<button type="button" class="nc-sdp-confirm" data-action="confirm-stage"'+disabledUnless(confirmable)+'>确认全部关键帧并进入配音</button>'+
      (!state.canEdit?'<p class="nc-sdp-readonly">当前为只读模式，所有写操作均已禁用。</p>':'')+
      (state.submittedShotIds.length?'<p class="nc-sdp-readonly">已提交镜头正在同步生产状态，请稍候。</p>':'')+
      (state.stage!=='stills_review'?'<p class="nc-sdp-readonly">当前阶段不可修改关键帧。</p>':'')+'</aside>';
  }

  function renderWorkspace(input,options){
    var state=normalizeState(input,options);
    return '<div class="nc-short-drama-production" data-readonly="'+(!state.canEdit)+'" data-busy="'+state.busy+'" data-stale="'+state.stale+'">'+
      renderShotRail(state)+renderEditor(state)+renderInspector(state)+'</div>';
  }

  function defaultKey(){
    var cryptoObject=typeof globalThis!=='undefined'&&globalThis.crypto;
    if(cryptoObject&&typeof cryptoObject.randomUUID==='function') return cryptoObject.randomUUID();
    return 'still-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  }

  function errorMessage(error){
    if(!error) return '操作失败，请稍后重试';
    if(error.data&&error.data.detail) return text(error.data.detail);
    return text(error.detail||error.message||error);
  }

  function createWorkspace(options){
    options=options||{};
    var client=options.client;
    if(!client||typeof client.json!=='function') throw new Error('production workspace requires a JSON client');
    if(options.projectId==null||options.projectId==='') throw new Error('production workspace requires projectId');
    var confirmHook=typeof options.confirm==='function'?options.confirm:function(){ return false; };
    var onChange=typeof options.onChange==='function'?options.onChange:function(){};
    var onError=typeof options.onError==='function'?options.onError:function(){};
    var keyFactory=typeof options.idempotencyKey==='function'?options.idempotencyKey:defaultKey;
    var later=options.setTimeoutImpl||setTimeout;
    var cancelLater=options.clearTimeoutImpl||clearTimeout;
    var pollInterval=options.pollIntervalMs==null?1500:Math.max(0,number(options.pollIntervalMs,0));
    var host=options.host||null;
    var pendingStorage=options.storage||((typeof sessionStorage!=='undefined'&&sessionStorage)?sessionStorage:null);
    var pendingStorageKey='hq.short-drama.still.pending:'+String(options.projectId);
    var pendingBatchStorageKey='hq.short-drama.still.batch.pending:'+String(options.projectId);
    var serverState=null,destroyed=false,pollTimer=null,pollReject=null,generationPromise=null,lastPublishedSummaryKey=null;
    var submittedGuards=Object.create(null);
    var pendingSingle=null,pendingBatch=[];
    var ui={selectedShotId:options.selectedShotId,filter:'all',prompts:{},
      promptSources:{},promptNotices:{},canEdit:options.canEdit!==false,
      busy:true,stale:false,error:'',quote:null,lastMode:''};

    function ensureAlive(){ if(destroyed) throw new Error('workspace destroyed'); }
    function loadPendingSingle(){
      if(!pendingStorage||typeof pendingStorage.getItem!=='function') return null;
      try{
        var parsed=JSON.parse(pendingStorage.getItem(pendingStorageKey)||'null');
        if(!parsed||parsed.projectId!==String(options.projectId)||!parsed.body||
          typeof parsed.body.shot_id!=='string'||typeof parsed.key!=='string'||!parsed.key) return null;
        parsed.jobId=(typeof parsed.jobId==='number'&&isFinite(parsed.jobId)&&parsed.jobId>0)?parsed.jobId:null;
        return parsed;
      }catch(_error){ return null; }
    }
    function savePendingSingle(){
      if(!pendingStorage||typeof pendingStorage.setItem!=='function'||!pendingSingle) return;
      pendingStorage.setItem(pendingStorageKey,JSON.stringify(pendingSingle));
    }
    function clearPendingSingle(){
      pendingSingle=null;
      if(pendingStorage&&typeof pendingStorage.removeItem==='function') pendingStorage.removeItem(pendingStorageKey);
    }
    function loadPendingBatch(){
      if(!pendingStorage||typeof pendingStorage.getItem!=='function') return [];
      try{
        var parsed=JSON.parse(pendingStorage.getItem(pendingBatchStorageKey)||'[]');
        if(!Array.isArray(parsed)) return [];
        return parsed.filter(function(attempt){
          return attempt&&attempt.projectId===String(options.projectId)&&attempt.body&&
            typeof attempt.body.shot_id==='string'&&typeof attempt.body.quote_token==='string'&&
            typeof attempt.key==='string'&&!!attempt.key;
        }).map(function(attempt){
          attempt.jobId=(typeof attempt.jobId==='number'&&isFinite(attempt.jobId)&&attempt.jobId>0)?
            attempt.jobId:null;
          attempt.cost=Math.max(0,number(attempt.cost,0));
          attempt.expiresAt=Math.max(0,number(attempt.expiresAt,0));
          attempt.started=attempt.started===true;
          return attempt;
        });
      }catch(_error){ return []; }
    }
    function savePendingBatch(){
      if(!pendingStorage) return;
      if(!pendingBatch.length){
        if(typeof pendingStorage.removeItem==='function') pendingStorage.removeItem(pendingBatchStorageKey);
        return;
      }
      if(typeof pendingStorage.setItem==='function'){
        pendingStorage.setItem(pendingBatchStorageKey,JSON.stringify(pendingBatch));
      }
    }
    function removePendingBatchAttempts(attempts){
      var removing=(attempts||[]).map(function(attempt){ return attempt.body.shot_id; });
      pendingBatch=pendingBatch.filter(function(attempt){
        if(removing.indexOf(attempt.body.shot_id)<0) return true;
        if(!pendingSingle||pendingSingle.body.shot_id!==attempt.body.shot_id){
          delete submittedGuards[attempt.body.shot_id];
        }
        return false;
      });
      savePendingBatch();
    }
    function discardUnstartedBatch(){
      removePendingBatchAttempts(pendingBatch.filter(function(attempt){
        return attempt.jobId==null&&attempt.started!==true;
      }));
    }
    pendingSingle=loadPendingSingle();
    if(pendingSingle) submittedGuards[pendingSingle.body.shot_id]=pendingSingle.jobId||true;
    pendingBatch=loadPendingBatch();
    pendingBatch.forEach(function(attempt){
      submittedGuards[attempt.body.shot_id]=attempt.jobId||true;
    });
    function callJson(path,requestOptions){
      return Promise.resolve().then(function(){
        ensureAlive();
        var scoped=requestOptions?Object.assign({},requestOptions):{};
        if(options.boardId){
          scoped.headers=Object.assign({},scoped.headers||{}, {'X-Canvas-Board-Id':String(options.boardId)});
        }
        return client.json(path,scoped);
      }).then(function(value){
        ensureAlive();
        return value;
      });
    }
    function renderOptions(){
      return Object.assign({},ui,{submittedShotIds:Object.keys(submittedGuards)});
    }
    function view(){
      var base=serverState||{project_id:options.projectId,shots:[]};
      return normalizeState(base,renderOptions());
    }
    function paint(){
      ensureAlive();
      var html=renderWorkspace(serverState||{project_id:options.projectId,shots:[]},renderOptions());
      if(host) host.innerHTML=html;
      return html;
    }
    function safePaint(){ if(!destroyed) paint(); }
    function accept(next,keepBusy,notify){
      ensureAlive();
      if(!next||typeof next!=='object') throw new Error('production state is invalid');
      serverState=clone(next);
      var normalized=normalizeState(serverState,{selectedShotId:ui.selectedShotId,prompts:ui.prompts});
      var reconciledBatch=[];
      ui.selectedShotId=normalized.selectedShotId;
      normalized.shots.forEach(function(shot){
        if(!Object.prototype.hasOwnProperty.call(ui.prompts,shot.id)){
          ui.prompts[shot.id]='';
        }
        var previousSource=ui.promptSources[shot.id];
        if(previousSource!=null&&previousSource!==shot.image_prompt&&
            text(ui.prompts[shot.id]).trim()){
          ui.promptNotices[shot.id]=true;
        }
        ui.promptSources[shot.id]=shot.image_prompt;
        var guardedJob=submittedGuards[shot.id];
        var terminalFailed=shot.still.job&&shot.still.job.status==='failed'&&
          guardedJob!==true&&shot.still.job.job_id===guardedJob&&
          (shot.still.job.refunded||shot.still.job.operation_terminal);
        var exactVersion=shot.still.versions.some(function(version){
          return guardedJob!==true&&version.job_id===guardedJob;
        });
        var reconciled=terminalFailed||exactVersion;
        if(reconciled){
          delete submittedGuards[shot.id];
          if(pendingSingle&&pendingSingle.body.shot_id===shot.id&&
            (pendingSingle.jobId==null||pendingSingle.jobId===guardedJob)) clearPendingSingle();
          pendingBatch.forEach(function(attempt){
            if(attempt.body.shot_id===shot.id&&attempt.jobId===guardedJob){
              reconciledBatch.push(attempt);
            }
          });
        }
      });
      if(reconciledBatch.length) removePendingBatchAttempts(reconciledBatch);
      ui.busy=!!keepBusy;ui.stale=false;ui.error='';
      safePaint();
      var summary=summarizeProductionState(serverState),summaryKey=JSON.stringify(summary);
      if(!notify) lastPublishedSummaryKey=summaryKey;
      else if(summaryKey!==lastPublishedSummaryKey){
        lastPublishedSummaryKey=summaryKey;
        onChange(summary);
      }
      return normalized;
    }
    function handleError(error){
      if(destroyed) return;
      ui.busy=false;ui.error=errorMessage(error);
      if(error&&(Number(error.status)===409||error.code==='revision_conflict')) ui.stale=true;
      safePaint();
    }
    function statePath(){ return PRODUCTION_PATH+'?project_id='+encodeURIComponent(options.projectId); }
    function requestState(keepBusy,notify){
      return callJson(statePath()).then(function(next){ return accept(next,keepBusy,notify!==false); });
    }
    function refresh(notify){
      try{ ensureAlive(); }catch(error){ return Promise.reject(error); }
      ui.busy=true;ui.error='';safePaint();
      return requestState(false,notify!==false).catch(function(error){ handleError(error); throw error; });
    }
    function ensureWritable(){
      ensureAlive();
      if(!serverState) throw new Error('production state is not loaded');
      if(ui.stale) throw new Error('workspace is stale; refresh before writing');
      if(!ui.canEdit) throw new Error('read-only workspace');
      if(serverState.stage!=='stills_review') throw new Error('current stage is not stills_review');
      if(ui.busy) throw new Error('workspace busy');
    }
    function currentShot(){ return selectedShot(view()); }
    function mutation(path,body){
      try{ ensureWritable(); }catch(error){ return Promise.reject(error); }
      ui.busy=true;ui.error='';safePaint();
      return callJson(path,{method:'POST',body:body}).then(function(next){
        return accept(next,false,true);
      }).catch(function(error){ handleError(error); throw error; });
    }
    function selectShotById(shotId){
      ensureAlive();
      var normalized=view();
      if(!normalized.shots.some(function(shot){ return shot.id===shotId; })) return false;
      ui.selectedShotId=shotId;ui.quote=null;safePaint();return true;
    }
    function setFilter(filter){
      ensureAlive();
      if(['all','pending','running','done','failed','locked'].indexOf(filter)<0) return false;
      ui.filter=filter;safePaint();return true;
    }
    function setPrompt(prompt,repaint){
      ensureAlive();
      var shot=currentShot();
      if(!shot) return false;
      ui.prompts[shot.id]=text(prompt);
      ui.quote=null;
      if(repaint!==false) safePaint();
      return true;
    }
    function selectVersion(version,lock){
      var shot;
      try{ ensureWritable();shot=currentShot(); }catch(error){ return Promise.reject(error); }
      if(!shot) return Promise.reject(new Error('no shot selected'));
      var requested=number(version,0);
      if(!shot.still.versions.some(function(item){ return item.version===requested&&item.status==='done'; })){
        return Promise.reject(new Error('asset version is unavailable'));
      }
      return mutation(SELECT_PATH,{
        project_id:serverState.project_id,revision:number(serverState.revision,0),
        asset_id:shot.still.asset_id,version:requested,lock:!!lock
      });
    }
    function confirmStage(){
      try{ ensureWritable(); }catch(error){ return Promise.reject(error); }
      var current=view();
      if(current.handoff_blocked){
        return Promise.reject(new Error(
          current.handoff_blockers[0]&&current.handoff_blockers[0].message||'当前无法进入配音阶段'
        ));
      }
      if(!allShotsLocked(current)){
        return Promise.reject(new Error('every shot requires a locked current completed matching-ratio still'));
      }
      return mutation(CONFIRM_PATH,{
        project_id:serverState.project_id,revision:number(serverState.revision,0),stage:'stills_review'
      });
    }
    function stillBodyForShot(shot,mode){
      if(!shot) throw new Error('no shot selected');
      return {
        project_id:serverState.project_id,
        revision:number(serverState.revision,0),
        shot_id:shot.id,
        prompt:text(Object.prototype.hasOwnProperty.call(ui.prompts,shot.id)?
          ui.prompts[shot.id]:'').trim(),
        mode:mode,
        count:2
      };
    }
    function stillBody(mode){ return stillBodyForShot(currentShot(),mode); }
    function submitWithTimeoutRetry(body,key){
      var requestOptions={method:'POST',body:body,headers:{'Idempotency-Key':key}};
      return callJson(GENERATE_PATH,requestOptions).catch(function(error){
        if(error&&error.code==='timeout'){
          ensureAlive();
          return callJson(GENERATE_PATH,requestOptions);
        }
        throw error;
      });
    }
    function ambiguousSubmitError(error){
      return !(error&&(error.operation_terminal===true||
        (error.data&&error.data.operation_terminal===true)));
    }
    function requireStillQuote(quote){
      if(!quote||typeof quote.cost!=='number'||!isFinite(quote.cost)||quote.cost<0||
        typeof quote.quote_token!=='string'||!quote.quote_token){
        throw new Error('still quote is invalid');
      }
      return quote;
    }
    function assertProjectBudget(state,cost){
      if(!state.point_budget) return;
      if(state.spent_points+state.reserved_points+cost<=state.point_budget) return;
      var error=new Error('短剧点数预算不足：请降低批量镜头数或调整项目预算');
      error.code='point_budget_exceeded';
      throw error;
    }
    function baseStillBody(body){
      var copy=Object.assign({},body);
      delete copy.quote_token;
      return copy;
    }
    function pollShots(targets){
      var tracked=(targets||[]).map(function(target){
        return {
          shotId:target&&typeof target==='object'?target.shotId:target,
          jobId:target&&typeof target==='object'?target.jobId:null,
          observed:false,missing:0,done:false,error:null
        };
      });
      return new Promise(function(resolve,reject){
        var settled=false;
        function finish(callback,value){
          if(settled) return;
          settled=true;
          if(pollTimer!=null){ cancelLater(pollTimer);pollTimer=null; }
          if(pollReject===reject) pollReject=null;
          callback(value);
        }
        function tick(){
          pollTimer=null;
          if(destroyed){ finish(reject,new Error('workspace destroyed'));return; }
          requestState(true).then(function(next){
            var pending=tracked.some(function(target){
              if(target.done) return false;
              var shot=null;
              next.shots.forEach(function(item){ if(item.id===target.shotId) shot=item; });
              var job=shot&&shot.still.job;
              var matchingVersion=shot&&target.jobId!=null&&shot.still.versions.some(function(version){
                return version.job_id===target.jobId;
              });
              if(matchingVersion){ target.done=true;return false; }
              var activeJob=job&&isActiveJobStatus(job.status);
              var matchingActive=activeJob&&(target.jobId==null||job.job_id===target.jobId);
              if(matchingActive){
                target.observed=true;target.missing=0;return true;
              }
              if(activeJob){
                if(target.jobId==null) return true;
                target.missing+=1;
                if(target.missing>=KNOWN_JOB_MISSING_LIMIT){
                  target.done=true;target.error=new Error('exact still job disappeared without durable success evidence');return false;
                }
                return true;
              }
              var matchingJob=job&&target.jobId!=null&&job.job_id===target.jobId;
              if(matchingJob&&job.status==='failed'){
                if(job.refund_pending&&!job.refunded) return true;
                if(!job.refunded&&!job.operation_terminal) return true;
                target.done=true;target.error=new Error(job.error||'关键帧生成失败');target.error.durable=true;return false;
              }
              if(matchingJob||target.observed){
                target.done=true;target.error=new Error('关键帧任务结束但没有成功候选图');return false;
              }
              if(target.jobId!=null){
                target.missing+=1;
                if(target.missing>=KNOWN_JOB_MISSING_LIMIT){
                  target.done=true;target.error=new Error('关键帧任务状态缺失且没有成功候选图');return false;
                }
                return true;
              }
              target.missing+=1;
              if(target.missing>=2){ target.done=true;return false; }
              return true;
            });
            if(!pending){
              var failed=null;
              tracked.some(function(target){ if(target.error){ failed=target.error;return true; } return false; });
              ui.busy=false;
              if(failed){ handleError(failed);finish(reject,failed);return; }
              safePaint();finish(resolve,next);return;
            }
            schedule();
          },function(error){ handleError(error);finish(reject,error); });
        }
        function schedule(){
          if(destroyed){ finish(reject,new Error('workspace destroyed'));return; }
          if(pollInterval===0) Promise.resolve().then(tick);
          else pollTimer=later(tick,pollInterval);
        }
        pollReject=reject;
        schedule();
      });
    }
    function pollShot(shotId,jobId){ return pollShots([{shotId:shotId,jobId:jobId}]); }
    function clearSubmittedGuards(submitted){
      (submitted||[]).forEach(function(target){ delete submittedGuards[target.shotId]; });
      safePaint();
    }
    function recoverPartialBatch(submitted,primaryError){
      if(!submitted.length) return Promise.reject(primaryError);
      return pollShots(submitted).then(function(){
        clearSubmittedGuards(submitted);
        throw primaryError;
      },function(){
        if(destroyed) throw primaryError;
        return requestState(true).catch(function(){ return null; }).then(function(){ throw primaryError; });
      });
    }
    function trackGeneration(action){
      generationPromise=action.then(function(result){ generationPromise=null;return result; },function(error){
        generationPromise=null;throw error;
      });
      return generationPromise;
    }
    function resumePendingSingle(){
      if(!pendingSingle) return Promise.resolve(null);
      var attempt=pendingSingle;
      submittedGuards[attempt.body.shot_id]=attempt.jobId||true;
      ui.busy=true;ui.error='';safePaint();
      if(attempt.jobId!=null){
        return pollShot(attempt.body.shot_id,attempt.jobId).then(function(result){
          clearPendingSingle();delete submittedGuards[attempt.body.shot_id];safePaint();return result;
        }).catch(function(error){
          if(error&&error.durable){ clearPendingSingle();delete submittedGuards[attempt.body.shot_id]; }
          handleError(error);throw error;
        });
      }
      return submitWithTimeoutRetry(attempt.body,attempt.key).then(function(response){
        ensureAlive();
        var jobId=response&&response.job_id;
        if(typeof jobId!=='number'||!isFinite(jobId)||Math.floor(jobId)!==jobId||jobId<1){
          throw new Error('successful still submission requires a positive job_id');
        }
        attempt.jobId=jobId;pendingSingle=attempt;savePendingSingle();
        submittedGuards[attempt.body.shot_id]=jobId;safePaint();
        return pollShot(attempt.body.shot_id,jobId).then(function(result){
          clearPendingSingle();delete submittedGuards[attempt.body.shot_id];safePaint();return result;
        });
      }).catch(function(error){
        if((error&&error.durable)||!ambiguousSubmitError(error)){
          clearPendingSingle();delete submittedGuards[attempt.body.shot_id];
        }
        handleError(error);throw error;
      });
    }
    function generate(mode){
      if(generationPromise) return generationPromise;
      var selected;
      try{
        ensureWritable();
        if(['single','retry','batch'].indexOf(mode)<0) throw new Error('invalid generation mode');
        selected=currentShot();
        if(!selected) throw new Error('no shot selected');
        if(pendingBatch.length) throw new Error('a still batch is awaiting reconciliation');
        if(pendingSingle){
          if(pendingSingle.body.shot_id!==selected.id) throw new Error('another still submission is awaiting reconciliation');
          return trackGeneration(resumePendingSingle());
        }
        if(submittedGuards[selected.id]){
          ui.busy=true;ui.error='';safePaint();
          return trackGeneration(pollShot(selected.id,submittedGuards[selected.id]));
        }
        if(selected.still.job&&isActiveJobStatus(selected.still.job.status)){
          submittedGuards[selected.id]=selected.still.job.job_id;
          ui.busy=true;ui.error='';safePaint();
          return trackGeneration(pollShot(selected.id,selected.still.job.job_id));
        }
      }catch(error){ return Promise.reject(error); }
      var body=stillBody(mode),key=keyFactory();
      if(typeof key!=='string'||!key) return Promise.reject(new Error('idempotency key is invalid'));
      ui.busy=true;ui.error='';ui.lastMode=mode;safePaint();
      var action=callJson(QUOTE_PATH,{method:'POST',body:body}).then(function(quote){
        ensureAlive();
        requireStillQuote(quote);
        ui.quote=clone(quote);safePaint();
        return Promise.resolve(confirmHook(quote.cost,clone(quote),clone(body))).then(function(accepted){
          ensureAlive();
          if(!accepted){ ui.busy=false;safePaint();return null; }
          var submittedBody=Object.assign({},body,{quote_token:quote.quote_token});
          pendingSingle={projectId:String(options.projectId),body:clone(submittedBody),key:key,jobId:null};
          submittedGuards[body.shot_id]=true;savePendingSingle();safePaint();
          return resumePendingSingle();
        });
      }).catch(function(error){ handleError(error);throw error; });
      return trackGeneration(action);
    }
    function refreshPendingBatchQuotes(){
      var threshold=Math.floor(Date.now()/1000)+15;
      var chain=Promise.resolve();
      pendingBatch.slice().forEach(function(attempt){
        if(attempt.jobId!=null||attempt.started||attempt.expiresAt>threshold) return;
        chain=chain.then(function(){
          return callJson(QUOTE_PATH,{method:'POST',body:baseStillBody(attempt.body)}).then(function(quote){
            requireStillQuote(quote);
            if(quote.cost!==attempt.cost){
              discardUnstartedBatch();
              var changed=new Error('批量报价已变化，请重新确认后提交');
              changed.code='batch_quote_changed';
              throw changed;
            }
            attempt.body.quote_token=quote.quote_token;
            attempt.expiresAt=Math.max(0,number(
              quote.expires_at,Math.floor(Date.now()/1000)+300));
            savePendingBatch();
          });
        });
      });
      return chain;
    }
    function preparePendingBatch(){
      var latest;
      return requestState(true,false).then(function(next){
        latest=next;
        var unstarted=pendingBatch.filter(function(attempt){
          return attempt.jobId==null&&!attempt.started;
        });
        if(!unstarted.length) return latest;
        if(unstarted.some(function(attempt){
          return number(attempt.body.revision,0)!==latest.revision;
        })){
          var stale=new Error('项目状态已变化，请重新批量报价');
          stale.status=409;stale.code='revision_conflict';
          throw stale;
        }
        var staleAttempts=unstarted.filter(function(attempt){
          var shot=latest.shots.find(function(item){ return item.id===attempt.body.shot_id; });
          return !shot||shot.still.locked||shotHasCompletedCurrent(latest,shot)||
            (shot.still.job&&isActiveJobStatus(shot.still.job.status));
        });
        if(staleAttempts.length) removePendingBatchAttempts(staleAttempts);
        return refreshPendingBatchQuotes();
      }).then(function(){
        assertProjectBudget(latest,pendingBatch.reduce(function(total,attempt){
          return total+(attempt.jobId==null&&!attempt.started?attempt.cost:0);
        },0));
      }).catch(function(error){
        if(error&&(Number(error.status)===409||error.code==='revision_conflict'||
          error.code==='point_budget_exceeded')){
          discardUnstartedBatch();
        }
        throw error;
      }).then(function(){
        return latest;
      });
    }
    function resumePendingBatch(){
      if(!pendingBatch.length) return Promise.resolve(null);
      ui.busy=true;ui.error='';safePaint();
      var latest=null;
      function nextWave(){
        ensureAlive();
        if(!pendingBatch.length){
          ui.busy=false;safePaint();
          return Promise.resolve(latest||view());
        }
        ui.busy=true;safePaint();
        var preparation=preparePendingBatch();
        return preparation.then(function(prepared){
          latest=prepared;
          if(!pendingBatch.length) return nextWave();
          var wave=pendingBatch.slice(0,BATCH_WAVE_SIZE);
          var submitted=[];
          var chain=Promise.resolve();
          wave.forEach(function(attempt){
            chain=chain.then(function(){
              ensureAlive();
              if(attempt.jobId!=null){
                submitted.push({shotId:attempt.body.shot_id,jobId:attempt.jobId});
                return null;
              }
              attempt.started=true;
              submittedGuards[attempt.body.shot_id]=true;
              savePendingBatch();safePaint();
              return submitWithTimeoutRetry(attempt.body,attempt.key).then(function(response){
                ensureAlive();
                var responseJobId=response&&response.job_id;
                if(typeof responseJobId!=='number'||!isFinite(responseJobId)||
                  Math.floor(responseJobId)!==responseJobId||responseJobId<1){
                  throw new Error('successful still submission requires a positive job_id');
                }
                attempt.jobId=responseJobId;
                submittedGuards[attempt.body.shot_id]=responseJobId;
                savePendingBatch();
                submitted.push({shotId:attempt.body.shot_id,jobId:responseJobId});
              }).catch(function(error){
                if(!ambiguousSubmitError(error)){
                  if(error.code==='active_job_cap'){
                    attempt.started=false;
                    savePendingBatch();
                  }else{
                    removePendingBatchAttempts([attempt]);
                    discardUnstartedBatch();
                  }
                }
                throw error;
              });
            });
          });
          return chain.then(function(){
            return pollShots(submitted).then(function(result){
              latest=result;
              removePendingBatchAttempts(wave);
              return nextWave();
            });
          },function(primaryError){
            if(!submitted.length) throw primaryError;
            return pollShots(submitted).then(function(result){
              latest=result;
              if(primaryError.code==='active_job_cap') return nextWave();
              throw primaryError;
            },function(){
              if(destroyed) throw primaryError;
              return requestState(true,false).catch(function(){ return null; }).then(function(){
                throw primaryError;
              });
            });
          });
        });
      }
      return nextWave().catch(function(error){
        handleError(error);
        throw error;
      });
    }
    function generateBatch(){
      if(generationPromise) return generationPromise;
      var eligible,bodies;
      try{
        ensureWritable();
        if(pendingSingle) throw new Error('a single still submission is awaiting reconciliation');
        if(pendingBatch.length) return trackGeneration(resumePendingBatch());
        var normalized=view();
        eligible=normalized.shots.filter(function(shot){
          return !shot.still.locked&&!shotHasCompletedCurrent(normalized,shot)&&!submittedGuards[shot.id]&&
            !(shot.still.job&&isActiveJobStatus(shot.still.job.status));
        });
        if(!eligible.length) return Promise.resolve(null);
        bodies=eligible.map(function(shot){ return stillBodyForShot(shot,'batch'); });
      }catch(error){ return Promise.reject(error); }
      ui.busy=true;ui.error='';ui.lastMode='batch';safePaint();
      var quotes=[],quoteChain=Promise.resolve();
      bodies.forEach(function(body){
        quoteChain=quoteChain.then(function(){
          ensureAlive();
          return callJson(QUOTE_PATH,{method:'POST',body:body}).then(function(quote){
            ensureAlive();quotes.push(requireStillQuote(quote));
          });
        });
      });
      var action=quoteChain.then(function(){
        ensureAlive();
        var total=quotes.reduce(function(sum,quote){ return sum+quote.cost; },0);
        return requestState(true,false).then(function(latest){
          if(bodies.some(function(body){ return number(body.revision,0)!==latest.revision; })){
            var stale=new Error('项目状态已变化，请重新批量报价');
            stale.status=409;stale.code='revision_conflict';
            throw stale;
          }
          assertProjectBudget(latest,total);
          var aggregate={
            cost:total,count:bodies.length*2,kind:'still-batch',shot_count:bodies.length,
            shot_ids:bodies.map(function(body){ return body.shot_id; }),quotes:clone(quotes)
          };
          ui.quote=clone(aggregate);safePaint();
          return Promise.resolve(confirmHook(total,clone(aggregate),clone(bodies))).then(function(accepted){
            ensureAlive();
            if(!accepted){ ui.busy=false;safePaint();return null; }
            pendingBatch=bodies.map(function(body,index){
              var key=keyFactory(body.shot_id,index,'batch');
              if(typeof key!=='string'||!key) throw new Error('idempotency key is invalid');
              submittedGuards[body.shot_id]=true;
              return {
                projectId:String(options.projectId),
                body:Object.assign({},body,{quote_token:quotes[index].quote_token}),
                key:key,jobId:null,cost:quotes[index].cost,
                expiresAt:Math.max(0,number(
                  quotes[index].expires_at,Math.floor(Date.now()/1000)+300)),
                started:false
              };
            });
            savePendingBatch();safePaint();
            return resumePendingBatch();
          });
        });
      }).catch(function(error){ handleError(error);throw error; });
      return trackGeneration(action);
    }
    function destroy(){
      if(destroyed) return;
      destroyed=true;ui.destroyed=true;ui.busy=false;
      if(pollTimer!=null){ cancelLater(pollTimer);pollTimer=null; }
      if(pollReject){ var reject=pollReject;pollReject=null;reject(new Error('workspace destroyed')); }
      if(host&&clickHandler&&typeof host.removeEventListener==='function') host.removeEventListener('click',clickHandler);
      if(host&&inputHandler&&typeof host.removeEventListener==='function') host.removeEventListener('input',inputHandler);
    }
    function actionTarget(node,attribute){
      while(node&&node!==host){ if(node.getAttribute&&node.getAttribute(attribute)!=null) return node;node=node.parentNode; }
      return null;
    }
    function clickHandler(event){
      var filterTarget=actionTarget(event.target,'data-filter');
      if(filterTarget){ setFilter(filterTarget.getAttribute('data-filter'));return; }
      var target=actionTarget(event.target,'data-action');
      if(!target) return;
      var action=target.getAttribute('data-action'),operation=null;
      if(action==='select-shot'){ selectShotById(target.getAttribute('data-shot-id'));return; }
      if(action==='keep-prompt-supplement'){
        var currentNoticeShot=currentShot();
        if(currentNoticeShot) delete ui.promptNotices[currentNoticeShot.id];
        safePaint();return;
      }
      if(action==='clear-prompt-supplement'){
        var noticeShot=currentShot();
        if(noticeShot){
          ui.prompts[noticeShot.id]='';
          delete ui.promptNotices[noticeShot.id];
          ui.quote=null;
        }
        safePaint();return;
      }
      if(action==='select-version') operation=selectVersion(target.getAttribute('data-version'),false);
      else if(action==='lock-version') operation=selectVersion(target.getAttribute('data-version'),true);
      else if(action==='generate-current') operation=generate('single');
      else if(action==='retry-current') operation=generate('retry');
      else if(action==='generate-batch') operation=generateBatch();
      else if(action==='confirm-stage') operation=confirmStage();
      else if(action==='refresh') operation=refresh();
      if(operation&&typeof operation.catch==='function'){
        operation.catch(function(error){ onError(error); });
      }
    }
    function inputHandler(event){
      var target=actionTarget(event.target,'data-field');
      if(target&&target.getAttribute('data-field')==='prompt') setPrompt(target.value,false);
    }

    if(host&&typeof host.addEventListener==='function'){
      host.addEventListener('click',clickHandler);
      host.addEventListener('input',inputHandler);
    }
    safePaint();
    var ready=refresh(false).then(function(){
      if(pendingSingle) return trackGeneration(resumePendingSingle());
      if(pendingBatch.length) return trackGeneration(resumePendingBatch());
      return null;
    }).catch(function(){ return null; });
    return {
      projectId:options.projectId,
      ready:ready,
      render:paint,
      refresh:refresh,
      reload:refresh,
      getState:function(){ ensureAlive();return clone(view()); },
      selectShot:selectShotById,
      setFilter:setFilter,
      setPrompt:setPrompt,
      generateCurrent:function(){ return generate('single'); },
      retryCurrent:function(){ return generate('retry'); },
      generateBatch:generateBatch,
      selectVersion:selectVersion,
      selectAsset:selectVersion,
      confirmStage:confirmStage,
      destroy:destroy
    };
  }

  return {
    normalizeState:normalizeState,
    renderWorkspace:renderWorkspace,
    createWorkspace:createWorkspace
  };
});
