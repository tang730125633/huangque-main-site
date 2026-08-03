(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaLipsync=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var BASE='/api/gen/short-drama/lipsync';
  var SNAPSHOT_PATH=BASE+'/snapshot';
  var SPEAKERS_PATH=BASE+'/speakers';
  var QUOTE_PATH=BASE+'/quote';
  var JOBS_PATH=BASE+'/jobs';
  var ACTIVE={prepared:1,queued:1,running:1,cancel_pending:1};
  var RECOVERING={prepared:1,queued:1,running:1,cancel_pending:1,refund_pending:1};

  function text(value){return String(value==null?'':value);}
  function number(value,fallback){
    var result=Number(value);return isFinite(result)?result:Number(fallback)||0;
  }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function clone(value){return JSON.parse(JSON.stringify(value==null?null:value));}
  function unique(items,key){
    var seen={};return (items||[]).filter(function(item){
      var value=key(item);if(!value||seen[value]) return false;
      seen[value]=true;return true;
    });
  }
  function key(prefix){
    return text(prefix||'lipsync')+'-'+Date.now().toString(36)+'-'+
      Math.random().toString(36).slice(2,12);
  }
  function normalizeError(error){
    var result=error instanceof Error?error:new Error(text(error)||'请求失败');
    result.status=number(error&&error.status);
    result.code=text(error&&error.code||error&&error.body&&error.body.code);
    result.detail=text(
      error&&error.detail||error&&error.body&&error.body.detail||
      error&&error.message||error
    );
    return result;
  }
  function errorMessage(error){
    var value=normalizeError(error);
    return ({
      dependency_blocked:'依赖尚未就绪，请先处理阻塞项',
      dependency_changed:'依赖已变化，请刷新并重新报价',
      revision_changed:'项目已更新，请刷新后重试',
      stale_snapshot:'页面快照已过期，请刷新',
      stale_version:'该版本依赖已变化，不能锁定',
      quote_expired:'报价已过期，请重新报价',
      quote_consumed:'该报价已经使用，请刷新任务状态',
      insufficient_points:'项目点数不足',
      refund_pending:'退款处理中，请等待对账完成',
      provider_unavailable:'口型服务暂不可用',
      provider_output_invalid:'服务结果未通过媒体校验',
      media_probe_failed:'媒体规格校验失败',
      forbidden:'当前账号没有此操作权限',
      board_mismatch:'画布身份不匹配',
      lock_conflict:'版本已被其他编辑者锁定',
      lipsync_jobs_disabled:'口型任务尚未开放',
      lipsync_billing_disabled:'口型扣点功能尚未开放',
      lipsync_mutations_disabled:'口型工作区写操作尚未开放'
    })[value.code]||value.detail||'请求失败';
  }
  function provider(snapshot){
    var catalog=snapshot&&snapshot.dependencies&&
      snapshot.dependencies.provider_catalog||{};
    var name=text(catalog.default_provider);
    return (catalog.providers||[]).filter(function(candidate){
      return text(candidate&&candidate.name)===name;
    })[0]||null;
  }
  function allSegments(snapshot){
    var timeline=snapshot&&snapshot.dependencies&&snapshot.dependencies.timeline||{};
    return timeline.segments||timeline.visible_segments||[];
  }
  function shotSegments(snapshot,shotId){
    return allSegments(snapshot).filter(function(item){
      return text(item&&item.shot_id)===text(shotId);
    });
  }
  function targetKey(target){
    return text(target&&target.type)+':'+text(target&&target.value);
  }
  function faceTargets(snapshot,shotId){
    return unique(shotSegments(snapshot,shotId).map(function(item){
      return item&&item.face_target;
    }).filter(function(target){return target&&text(target.value);}),targetKey);
  }
  function blockersFor(snapshot,shotId){
    return (snapshot&&snapshot.blockers||[]).filter(function(item){
      return !item.shot_id||text(item.shot_id)===text(shotId)||
        item.scope==='project';
    });
  }
  function features(snapshot){
    var value=snapshot&&snapshot.features||{};
    return {
      ui:value.ui_enabled!==false,
      mutations:value.mutations_enabled===true,
      batch:value.batch_enabled===true
    };
  }
  function permissions(snapshot,canEdit){
    var value=snapshot&&snapshot.permissions||{};
    return {
      edit:canEdit!==false&&value.can_edit!==false,
      quote:canEdit!==false&&value.quote===true,
      create:canEdit!==false&&value.can_create_job===true,
      select:canEdit!==false&&value.can_select===true,
      lock:canEdit!==false&&value.can_lock===true
    };
  }
  function quotePayload(snapshot,shotId,faceTarget){
    var selected=provider(snapshot);
    var segments=shotSegments(snapshot,shotId).filter(function(item){
      return text(item.speaking_mode||'visible')==='visible';
    });
    if(!snapshot||!snapshot.can_quote||!selected||!segments.length||
        !faceTarget||!text(faceTarget.value)) return null;
    if(!faceTargets(snapshot,shotId).some(function(target){
      return targetKey(target)===targetKey(faceTarget);
    })) return null;
    return {
      project_id:text(snapshot.project_id),
      shot_id:text(shotId),
      expected_revision:number(snapshot.revision),
      expected_input_hash:text(snapshot.input_hash),
      provider:text(selected.name),
      profile:text(selected.profile||'standard'),
      face_target:clone(faceTarget),
      idempotency_key:'lipsync-quote-'+text(snapshot.input_hash).slice(0,16)+'-'+
        text(shotId)+'-'+text(selected.name)+'-'+targetKey(faceTarget)
    };
  }
  function createJobPayload(snapshot,quote){
    if(!snapshot||!quote||quote.chargeable!==true||
        text(quote.input_hash)!==text(snapshot.input_hash)) return null;
    return {
      project_id:text(snapshot.project_id),
      shot_id:text(quote.shot_id),
      quote_id:text(quote.quote_id||quote.id),
      expected_input_hash:text(snapshot.input_hash)
    };
  }
  function shouldPoll(job){
    return !!job&&!!RECOVERING[text(job.state||job.status)];
  }
  function jobStateLabel(state){
    return ({
      prepared:'准备扣点',queued:'排队中',running:'生成中',
      cancel_pending:'取消处理中',succeeded:'已完成',failed:'失败',
      cancelled:'已取消',manual_review:'人工处理中',
      refund_pending:'退款处理中',refunded:'已退款'
    })[text(state)]||text(state)||'未知';
  }
  function renderBlockers(snapshot,shotId){
    var items=blockersFor(snapshot,shotId);
    if(!items.length) return '<p class="nc-sdl-ready">依赖检查通过，可以报价或预览版本。</p>';
    return '<ul class="nc-sdl-blockers">'+items.map(function(item){
      return '<li data-code="'+escapeHtml(item.code)+'"><div><strong>'+
        escapeHtml(item.message||item.code)+'</strong><small>'+
        escapeHtml(item.scope||'project')+' · '+escapeHtml(item.code)+
        '</small></div><span>'+escapeHtml(item.repair_action||'refresh')+
        '</span></li>';
    }).join('')+'</ul>';
  }
  function renderSpeakers(snapshot,shotId,editable,targetIndex){
    var segments=shotSegments(snapshot,shotId);
    var targets=faceTargets(snapshot,shotId);
    if(!segments.length) return '<p class="nc-sdl-empty">当前镜头没有说话人区间。</p>';
    return (targets.length?'<label class="nc-sdl-target">报价人脸目标<select '+
      'data-field="lipsync-face-target">'+targets.map(function(target,index){
        return '<option value="'+index+'"'+
          (number(targetIndex)===index?' selected':'')+'>'+escapeHtml(
          target.label||target.value||('目标 '+(index+1))
        )+'</option>';
      }).join('')+'</select></label>':'')+
      '<div class="nc-sdl-speakers">'+segments.map(function(item){
      var mode=text(item.speaking_mode||'visible');
      var target=item.face_target||{};
      return '<article data-lipsync-segment="'+escapeHtml(item.id)+'">'+
        '<header><strong>'+escapeHtml(item.character_key||'未绑定角色')+
        '</strong><span>'+number(item.start_ms)+'–'+number(item.end_ms)+' ms</span></header>'+
        '<div><label>出声方式<select data-field="lipsync-speaking-mode"'+
        (editable?'':' disabled')+'><option value="visible"'+
        (mode==='visible'?' selected':'')+'>画面内说话</option>'+
        '<option value="offscreen"'+(mode==='offscreen'?' selected':'')+
        '>画外音</option><option value="silent"'+
        (mode==='silent'?' selected':'')+'>静音</option></select></label>'+
        '<label>人脸目标<input data-field="lipsync-face-value" value="'+
        escapeHtml(target.value||'')+'"'+
        (editable&&mode==='visible'?'':' disabled')+'></label></div></article>';
    }).join('')+'</div>'+
      '<button type="button" data-action="save-lipsync-speakers"'+
      (editable?'':' disabled')+'>保存说话人与人脸目标</button>';
  }
  function renderQuote(snapshot,quote,options,selectedTarget){
    var flags=features(snapshot),access=permissions(snapshot,options.canEdit);
    var available=quotePayload(snapshot,options.shotId,selectedTarget);
    var blocked=!available||options.busy||!access.quote;
    var html='<section class="nc-sdl-card"><header><div><span>服务端报价</span>'+
      '<strong>价格与收费身份</strong></div></header>';
    if(quote){
      var cost=quote.cost||{};
      html+='<div class="nc-sdl-quote"><span>'+
        (quote.chargeable?'付费报价':'模拟报价')+'</span><strong>'+
        number(cost.points)+' 点</strong><small>Provider：'+
        escapeHtml(quote.provider||'—')+' · 预计外部成本 '+
        escapeHtml(cost.currency||'USD')+' '+number(cost.external_estimate).toFixed(4)+
        ' · 到期 '+escapeHtml(quote.expires_at||'—')+'</small></div>';
      var payload=createJobPayload(snapshot,quote);
      html+='<button type="button" data-action="create-lipsync-job"'+
        (!flags.mutations||!access.create||!payload||options.busy?' disabled':'')+
        '>确认扣点并生成口型</button>';
    }else{
      html+='<p class="nc-sdl-empty">先获取服务端报价，页面不会自行计算价格。</p>';
    }
    html+='<button type="button" data-action="quote-lipsync"'+
      (blocked?' disabled':'')+'>'+(quote?'重新获取报价':'获取当前镜头报价')+
      '</button></section>';
    return html;
  }
  function renderJobStatus(snapshot,options){
    options=options||{};
    var active=snapshot&&snapshot.active_jobs||[];
    var billing=snapshot&&snapshot.billing||{};
    if(!active.length&&!number(billing.refund_pending)&&
        !number(billing.manual_review)) return '';
    return '<section class="nc-sdl-card nc-sdl-jobs"><header><div><span>PR-F 任务</span>'+
      '<strong>进度、恢复与退款</strong></div></header>'+
      active.map(function(job){
        var actions=job.allowed_actions||{};
        return '<article data-lipsync-job="'+escapeHtml(job.id)+'"><div><strong>'+
          escapeHtml(jobStateLabel(job.state))+'</strong><small>Job '+
          escapeHtml(text(job.id).slice(0,12))+' · '+number(job.progress)+'%</small></div>'+
          '<progress max="100" value="'+number(job.progress)+'"></progress>'+
          '<div class="nc-sdl-job-actions"><button data-action="refresh-lipsync">刷新</button>'+
          '<button data-action="retry-lipsync-job" data-job-id="'+escapeHtml(job.id)+'"'+
          (actions.retry?'':' disabled')+'>安全重试</button>'+
          '<button data-action="cancel-lipsync-job" data-job-id="'+escapeHtml(job.id)+'"'+
          (actions.cancel?'':' disabled')+'>取消</button></div></article>';
      }).join('')+
      (number(billing.refund_pending)?'<p>退款处理中：'+
        number(billing.refund_pending)+' 笔，仅允许刷新。</p>':'')+
      (number(billing.manual_review)?'<p>人工处理：'+
        number(billing.manual_review)+' 笔，请保留 trace_id。</p>':'')+
      '</section>';
  }
  function renderVersions(snapshot,options){
    var versions=(snapshot&&snapshot.versions||[]).filter(function(item){
      return !options.shotId||text(item.shot_id)===text(options.shotId);
    });
    var access=permissions(snapshot,options.canEdit);
    if(!versions.length){
      return '<section class="nc-sdl-card"><header><div><span>不可变版本</span>'+
        '<strong>版本确认</strong></div></header><p class="nc-sdl-empty">'+
        '任务成功并通过媒体校验后，版本会出现在这里。</p></section>';
    }
    var primary=versions.filter(function(item){return item.selected;})[0]||versions[0];
    var comparison=versions.filter(function(item){
      return text(item.id)===text(options.compareVersionId);
    })[0];
    var compareHtml=comparison&&primary&&comparison.id!==primary.id?
      '<div class="nc-sdl-compare"><article><span>A · 当前</span><video controls '+
      'preload="metadata" src="'+escapeHtml(primary.media_url||'')+
      '"></video></article><article><span>B · 候选</span><video controls '+
      'preload="metadata" src="'+escapeHtml(comparison.media_url||'')+
      '"></video></article></div>':'';
    return '<section class="nc-sdl-card"><header><div><span>不可变版本</span>'+
      '<strong>预览、A/B、选择与锁定</strong></div></header>'+
      compareHtml+'<div class="nc-sdl-version-grid">'+versions.map(function(item){
        var media=item.media_spec||{};
        return '<article data-version-id="'+escapeHtml(item.id)+'" class="'+
          (item.selected?'is-selected ':'')+(item.locked?'is-locked ':'')+
          (item.stale?'is-stale':'')+'"><header><div><strong>v'+
          number(item.version)+'</strong><small>'+escapeHtml(item.provider)+
          ' · '+escapeHtml(text(item.id).slice(0,10))+'</small></div><span>'+
          (item.locked?'已锁定':item.selected?'已选择':item.stale?'已过期':'候选')+
          '</span></header><video controls preload="metadata" data-lipsync-player src="'+
          escapeHtml(item.media_url||'')+'"></video><small>'+
          number(media.width)+'×'+number(media.height)+' · '+
          number(media.duration_ms)+' ms · '+number(item.cost&&item.cost.points)+
          ' 点</small><div><button data-action="compare-lipsync-version" '+
          'data-version-id="'+escapeHtml(item.id)+'">'+
          (comparison&&comparison.id===item.id?'B 已选择':'设为 B 对比')+'</button>'+
          '<button data-action="select-lipsync-version" data-version-id="'+
          escapeHtml(item.id)+'"'+
          (!access.select||item.stale||options.busy?' disabled':'')+
          '>选择</button><button data-action="lock-lipsync-version" data-version-id="'+
          escapeHtml(item.id)+'"'+
          (!access.lock||!item.selected||item.stale||options.busy?' disabled':'')+
          '>锁定</button></div></article>';
      }).join('')+'</div></section>';
  }
  function renderPanel(snapshot,quote,options){
    options=options||{};
    if(options.loading){
      return '<section class="nc-sdl-workspace" data-state="loading">'+
        '<p>正在从服务端恢复口型工作区…</p></section>';
    }
    if(!snapshot){
      return '<section class="nc-sdl-workspace" data-state="error"><strong>'+
        '口型工作区暂不可用</strong><p>'+
        escapeHtml(options.error||'无法读取口型快照')+'</p></section>';
    }
    if(features(snapshot).ui===false){
      return '<section class="nc-sdl-workspace" data-state="disabled">'+
        '<strong>口型工作区处于灰度关闭状态</strong>'+
        '<p>已有任务仍由服务端恢复，不会丢失。</p></section>';
    }
    var targets=faceTargets(snapshot,options.shotId);
    var targetIndex=Math.max(0,Math.min(number(options.faceTargetIndex),
      Math.max(0,targets.length-1)));
    var selectedTarget=targets[targetIndex]||null;
    var access=permissions(snapshot,options.canEdit);
    var editable=features(snapshot).mutations&&access.edit&&!options.busy;
    return '<section class="nc-sdl-workspace" data-state="'+
      (snapshot.blockers&&snapshot.blockers.length?'blocked':'ready')+'">'+
      '<header class="nc-sdl-head"><div><span>PR-G 口型画布工作区</span>'+
      '<strong>说话人、付费任务与版本确认</strong><small>Snapshot '+
      escapeHtml(text(snapshot.input_hash).slice(0,12))+' · R'+
      number(snapshot.revision)+'</small></div><button data-action="refresh-lipsync">'+
      '刷新</button></header><div class="nc-sdl-layout"><aside><section class="nc-sdl-card">'+
      '<header><div><span>依赖检查</span><strong>阻塞项与修复入口</strong></div></header>'+
      renderBlockers(snapshot,options.shotId)+'</section></aside><main>'+
      '<section class="nc-sdl-card"><header><div><span>说话人时间轴</span>'+
      '<strong>出声方式与人脸目标</strong></div></header>'+
      renderSpeakers(snapshot,options.shotId,editable,targetIndex)+'</section>'+
      renderVersions(snapshot,options)+'</main><aside>'+
      renderQuote(snapshot,quote,options,selectedTarget)+
      renderJobStatus(snapshot,options)+'</aside></div>'+
      (options.error?'<p class="nc-sdl-error">'+escapeHtml(options.error)+'</p>':'')+
      '<footer>费用、终态、退款、stale、retryable 和 can_lock 均以服务端为准。</footer>'+
      '</section>';
  }
  function createApi(options){
    options=options||{};
    var client=options.client,boardId=text(options.boardId),destroyed=false;
    if(!client||typeof client.json!=='function') throw new Error('缺少认证 API 客户端');
    function request(path,requestOptions){
      if(destroyed) return Promise.reject(new Error('workspace destroyed'));
      var config=Object.assign({},requestOptions||{});
      config.headers=Object.assign({},config.headers||{});
      if(boardId) config.headers['X-Canvas-Board-Id']=boardId;
      return Promise.resolve(client.json(path,config)).catch(function(error){
        throw normalizeError(error);
      });
    }
    return {
      snapshot:function(projectId){
        return request(SNAPSHOT_PATH+'?project_id='+encodeURIComponent(projectId));
      },
      speakers:function(body,pending){
        return request(SPEAKERS_PATH,{method:'PUT',headers:{
          'Idempotency-Key':pending
        },body:body});
      },
      quote:function(body){return request(QUOTE_PATH,{method:'POST',body:body});},
      createJob:function(body,pending){
        return request(JOBS_PATH,{method:'POST',headers:{
          'Idempotency-Key':pending
        },body:body});
      },
      job:function(jobId){return request(JOBS_PATH+'/'+encodeURIComponent(jobId));},
      retry:function(jobId){
        return request(JOBS_PATH+'/'+encodeURIComponent(jobId)+'/retry',{method:'POST',body:{}});
      },
      cancel:function(jobId){
        return request(JOBS_PATH+'/'+encodeURIComponent(jobId)+'/cancel',{method:'POST',body:{}});
      },
      selectVersion:function(versionId,body){
        return request(BASE+'/versions/'+encodeURIComponent(versionId)+'/select',
          {method:'PUT',body:body});
      },
      lockVersion:function(versionId,body){
        return request(BASE+'/versions/'+encodeURIComponent(versionId)+'/lock',
          {method:'POST',body:body});
      },
      destroy:function(){destroyed=true;}
    };
  }
  return {
    SNAPSHOT_PATH:SNAPSHOT_PATH,SPEAKERS_PATH:SPEAKERS_PATH,
    QUOTE_PATH:QUOTE_PATH,JOBS_PATH:JOBS_PATH,
    ACTIVE:ACTIVE,provider:provider,shotSegments:shotSegments,
    faceTargets:faceTargets,quotePayload:quotePayload,
    createJobPayload:createJobPayload,shouldPoll:shouldPoll,
    renderJobStatus:renderJobStatus,renderPanel:renderPanel,
    normalizeError:normalizeError,errorMessage:errorMessage,
    createIdempotencyKey:key,createApi:createApi
  };
});
