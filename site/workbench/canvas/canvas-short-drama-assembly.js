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
  var PREVIEW_PATH=ASSEMBLY_PATH+'/preview';
  var FINAL_QUOTE_PATH=ASSEMBLY_PATH+'/final-quote';
  var FINAL_EXPORT_PATH=ASSEMBLY_PATH+'/export';
  var FINAL_CONFIRM_PATH=ASSEMBLY_PATH+'/confirm';

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
      shot_id:item.shot_id==null?null:text(item.shot_id),
      line_id:item.line_id==null?null:text(item.line_id)
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
          status:text(voice.status||'blocked'),
          timeline_revision:voice.timeline_revision==null?null:
            number(voice.timeline_revision,0),
          lines:Array.isArray(voice.lines)?voice.lines.map(clone):[]
        },
        video:{
          confirmed:video.confirmed===true,
          status:text(video.status||'blocked'),
          current_version:video.current_version==null?null:number(video.current_version,0),
          video_revision:video.video_revision==null?null:number(video.video_revision,0),
          probe:video.probe&&typeof video.probe==='object'?clone(video.probe):null,
          source_kind:text(video.source_kind||'standard'),
          lipsync:video.lipsync&&typeof video.lipsync==='object'?
            clone(video.lipsync):null
        },
        blockers:(Array.isArray(shot.blockers)?shot.blockers:[]).map(normalizeBlocker)
      };
    }).sort(function(left,right){ return left.sort_order-right.sort_order; });
    var readiness=input.readiness&&typeof input.readiness==='object'?input.readiness:{};
    var audioSubtitle=input.audio_subtitle&&
      typeof input.audio_subtitle==='object'?input.audio_subtitle:{};
    var masterAudio=input.master_audio&&
      typeof input.master_audio==='object'?input.master_audio:{};
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
      implementation_status:text(
        input.implementation_status||'audio_subtitle_engine'
      ),
      rendering_enabled:input.rendering_enabled===true,
      planner_version:text(input.planner_version),
      input_hash:input.input_hash==null?null:text(input.input_hash),
      media_plan:input.media_plan&&typeof input.media_plan==='object'?
        clone(input.media_plan):null,
      audio_subtitle:{
        engine_version:text(audioSubtitle.engine_version),
        input_hash:audioSubtitle.input_hash==null?null:
          text(audioSubtitle.input_hash),
        status:text(audioSubtitle.status||'not_built'),
        error_code:text(audioSubtitle.error_code),
        artifacts:Array.isArray(audioSubtitle.artifacts)?
          audioSubtitle.artifacts.map(clone):[],
        blockers:(Array.isArray(audioSubtitle.blockers)?
          audioSubtitle.blockers:[]).map(normalizeBlocker)
      },
      master_audio:{
        engine_version:text(masterAudio.engine_version),
        contract_version:text(masterAudio.contract_version),
        master_audio_hash:masterAudio.master_audio_hash==null?null:
          text(masterAudio.master_audio_hash),
        status:text(masterAudio.status||'not_built'),
        cache_hit:masterAudio.cache_hit===true,
        duration_ms:number(masterAudio.duration_ms,0),
        sample_rate:number(masterAudio.sample_rate,0),
        channels:number(masterAudio.channels,0),
        codec:text(masterAudio.codec),
        artifact:masterAudio.artifact&&typeof masterAudio.artifact==='object'?
          clone(masterAudio.artifact):null,
        timeline:masterAudio.timeline&&typeof masterAudio.timeline==='object'?
          clone(masterAudio.timeline):null,
        blockers:(Array.isArray(masterAudio.blockers)?
          masterAudio.blockers:[]).map(normalizeBlocker)
      },
      lipsync_assembly:input.lipsync_assembly&&
        typeof input.lipsync_assembly==='object'?
        clone(input.lipsync_assembly):null,
      config:config,
      shots:shots,
      versions:Array.isArray(input.versions)?input.versions.map(clone):[],
      active_job:input.active_job&&typeof input.active_job==='object'?
        clone(input.active_job):null,
      latest_job:input.latest_job&&typeof input.latest_job==='object'?
        clone(input.latest_job):null,
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
  function engineStatusLabel(status){
    return ({
      blocked:'输入未就绪',
      not_built:'引擎就绪',
      building:'处理中',
      ready:'中间产物已就绪',
      failed:'处理失败',
      stale:'产物已过期'
    })[status]||'状态未知';
  }
  function phaseLabel(phase){
    return ({
      queued:'等待调度',preparing:'准备素材',rendering_shots:'渲染镜头',
      concatenating:'拼接视频',cover:'生成封面',finalizing:'校验成片',
      rendering:'1080p 重渲染',probing:'质量校验',
      uploading_video:'上传正式成片',uploading_cover:'上传封面',
      archiving:'归档资产',
      completed:'预览已完成',failed:'预览失败'
    })[phase]||'处理中';
  }
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
        (shot.video.source_kind==='lipsync'&&shot.video.lipsync?
          '口型成片 v'+escapeHtml(shot.video.lipsync.version):
          (shot.video.confirmed?'已确认 v'+shot.video.current_version:'未确认'))+
        '</dd></div></dl>'+
        '<span class="nc-sda-status">'+readinessLabel(shot.ready)+'</span></article>';
    }).join('');
    var blockerMessages=uniqueBlockerMessages(state.readiness.blockers);
    var blockerList=blockerMessages.length?blockerMessages.map(function(message){
      return '<li>'+escapeHtml(message)+'</li>';
    }).join(''):'<li>前序素材已满足合成条件</li>';
    var subtitle=state.config.subtitle||{},bgm=state.config.bgm||{};
    var planShots=state.media_plan&&Array.isArray(state.media_plan.shots)?
      state.media_plan.shots:[];
    var planDuration=state.media_plan?
      number(state.media_plan.project_duration_ms,0):0;
    var timeline=planShots.length&&planDuration>0?planShots.map(function(shot,index){
      var width=Math.max(0,number(shot.duration_ms,0)/planDuration*100);
      return '<span class="nc-sda-plan-shot is-'+(index%2?'even':'odd')+
        '" style="width:'+width.toFixed(4)+'%" title="'+
        escapeHtml(text(shot.id)+' · '+(number(shot.duration_ms,0)/1000)+'s')+
        '"></span>';
    }).join(''):'<span class="nc-sda-plan-empty" style="width:100%"></span>';
    var planLabel=state.media_plan?
      'D-1 媒体计划已生成 · 输入哈希 '+escapeHtml((state.input_hash||'').slice(0,12)):
      '等待前序锁定素材通过探测与时间线校验';
    var engineBlockers=uniqueBlockerMessages(state.audio_subtitle.blockers);
    var engineDetail=engineBlockers.length?
      engineBlockers.join('；'):
      state.audio_subtitle.status==='not_built'?
        '等待 D-3 预览任务调用，不会因刷新页面自动生成':
        state.audio_subtitle.artifacts.length+' 个中间产物';
    var master=state.master_audio||{};
    var masterHash=master.master_audio_hash?
      escapeHtml(master.master_audio_hash.slice(0,12)):'待生成';
    var masterSpec=master.sample_rate&&master.channels?
      Math.round(master.sample_rate/1000)+'kHz · '+master.channels+
      ' 声道 · '+escapeHtml(master.codec||'pcm_s16le'):'规格待确认';
    var masterCache=master.cache_hit?'缓存已命中':
      master.status==='ready'?'母带已就绪':'等待构建';
    var previewVersions=state.versions.filter(function(item){
      return item&&item.kind==='preview'&&item.status==='succeeded'&&item.url;
    }).sort(function(left,right){ return number(right.version,0)-number(left.version,0); });
    var currentPreview=previewVersions.filter(function(item){
      return number(item.version,0)===number(input.current_preview_version,0);
    })[0]||previewVersions[0]||null;
    var finalVersions=state.versions.filter(function(item){
      return item&&item.kind==='final'&&item.status==='succeeded'&&item.url;
    }).sort(function(left,right){ return number(right.version,0)-number(left.version,0); });
    var currentFinal=finalVersions.filter(function(item){
      return number(item.version,0)===number(input.current_final_version,0);
    })[0]||finalVersions[0]||null;
    var playback=currentFinal||currentPreview;
    var player=playback?
      '<div class="nc-sda-player"><video controls preload="metadata" data-preview-url="'+
      escapeHtml(playback.url)+'"></video><span>'+
      (currentFinal?'1080p 正式成片':'720p 预览')+' · v'+
      number(playback.version,1)+'</span></div>':
      '<div class="nc-sda-player-placeholder"><div class="nc-sda-play-icon">▶</div>'+
      '<strong>尚未生成 720p 预览</strong><span>素材校验通过后可提交免费预览任务。</span></div>';
    var job=state.active_job||(
      state.latest_job&&state.latest_job.status==='failed'?state.latest_job:null
    );
    var jobPanel=job?
      '<section class="nc-sda-job"><strong>'+escapeHtml(phaseLabel(job.phase))+
      '</strong><div><i style="width:'+Math.max(0,Math.min(100,number(job.progress,0)))+
      '%"></i></div><span>'+number(job.progress,0)+'% · 任务 #'+
      escapeHtml(job.job_id)+'</span></section>':'';
    if(job&&job.status==='failed'&&job.error_message){
      jobPanel=jobPanel.replace(
        '</section>',
        '<em>'+escapeHtml(job.error_message)+'</em></section>'
      );
    }
    var history=previewVersions.length?
      '<section class="nc-sda-history"><strong>预览版本</strong>'+
      previewVersions.slice(0,10).map(function(item){
        return '<a href="'+escapeHtml(item.url)+'" target="_blank" rel="noopener">v'+
          number(item.version,1)+' · '+(number(item.duration_ms,0)/1000).toFixed(1)+'s</a>';
      }).join('')+'</section>':'';
    var finalHistory=finalVersions.length?
      '<section class="nc-sda-history"><strong>正式资产</strong>'+
      finalVersions.slice(0,10).map(function(item){
        return '<a href="'+escapeHtml(item.url)+'" target="_blank" rel="noopener">v'+
          number(item.version,1)+' · 1080p'+
          (item.asset_id?' · 已归档':'')+'</a>';
      }).join('')+'</section>':'';
    return '<section class="nc-sda-workspace" data-implementation="'+
      escapeHtml(state.implementation_status)+'"><aside class="nc-sda-rail">'+
      '<header><span>D-2 音频与字幕</span><h2>镜头与素材</h2><small>'+
      state.shots.length+' 镜 · '+state.target_duration+' 秒 · '+escapeHtml(state.ratio)+
      '</small></header><div class="nc-sda-shot-list">'+rail+'</div></aside>'+
      '<main class="nc-sda-preview"><header><span>成片预览</span><h2>项目级合成画布</h2></header>'+
      player+
      '<div class="nc-sda-timeline"><div class="nc-sda-time-head"><span>00:00</span><span>'+
      state.target_duration+'s</span></div><div class="nc-sda-track">'+timeline+'</div>'+
      '<small>'+planLabel+'</small></div></main>'+
      '<aside class="nc-sda-console"><header><span>D 阶段</span><h2>合成控制台</h2>'+
      '<small>装配修订 r'+state.assembly_revision+'</small></header>'+
      '<section class="nc-sda-readiness '+(state.readiness.ready?'is-ready':'is-blocked')+'">'+
      '<strong>'+readinessLabel(state.readiness.ready)+'</strong><ul>'+blockerList+'</ul></section>'+
      '<section class="nc-sda-engine" data-status="'+
      escapeHtml(state.audio_subtitle.status)+'"><strong>音频与字幕引擎 · '+
      escapeHtml(engineStatusLabel(state.audio_subtitle.status))+'</strong><span>'+
      escapeHtml(engineDetail)+'</span></section>'+
      '<section class="nc-sda-engine nc-sda-master-audio" data-status="'+
      escapeHtml(master.status||'not_built')+'"><strong>主音轨 · '+
      escapeHtml(engineStatusLabel(master.status||'not_built'))+
      '</strong><span>'+masterSpec+'</span><small>哈希 '+masterHash+
      ' · '+escapeHtml(masterCache)+' · '+master.duration_ms+'ms</small></section>'+
      jobPanel+history+finalHistory+
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
      '<label class="nc-sda-cover-time">封面时间（毫秒）<input type="number" min="0" step="100" data-cover-time value="1000"></label>'+
      '<p class="nc-sda-contract-note">D-4 将选定预览的锁定输入重新渲染为 1080p，完成私有上传、封面与资产归档后才允许确认项目完成。</p>'+
      '</aside></section>';
  }
  function createWorkspace(options){
    options=options||{};
    var client=options.client,host=options.host,destroyed=false;
    var snapshot=null,ui={busy:true,error:'',pendingKey:''},requestGeneration=0;
    var pollTimer=null,pollDelay=2000;
    var playerObjectUrl='';
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
      if(host&&!destroyed){
        if(playerObjectUrl&&typeof URL!=='undefined'&&URL.revokeObjectURL){
          URL.revokeObjectURL(playerObjectUrl);playerObjectUrl='';
        }
        host.innerHTML=html;
        hydratePlayer();
      }
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
    function hydratePlayer(){
      if(!host||typeof host.querySelector!=='function') return;
      var video=host.querySelector('video[data-preview-url]');
      if(!video) return;
      var source=text(video.getAttribute('data-preview-url'));
      if(source.indexOf('/api/gen/file/')!==0){
        video.src=source;return;
      }
      if(typeof client.asset!=='function') return;
      var requestOptions={};
      if(options.boardId){
        requestOptions.headers={'X-Canvas-Board-Id':String(options.boardId)};
      }
      Promise.resolve(client.asset(source,requestOptions)).then(function(blob){
        if(destroyed||!video||typeof URL==='undefined'||!URL.createObjectURL) return;
        playerObjectUrl=URL.createObjectURL(blob);video.src=playerObjectUrl;
      }).catch(function(){});
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
        if(snapshot.active_job){
          schedulePoll();
        }else{
          clearPoll();ui.pendingKey='';pollDelay=2000;
        }
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
    function clearPoll(){
      if(pollTimer){ clearTimeout(pollTimer);pollTimer=null; }
    }
    function schedulePoll(){
      if(destroyed||pollTimer) return;
      pollTimer=setTimeout(function(){
        pollTimer=null;
        pollActiveJob().catch(function(){});
        pollDelay=Math.min(12000,Math.round(pollDelay*1.5));
      },pollDelay);
    }
    function pollActiveJob(){
      var job=snapshot&&snapshot.active_job;
      if(!job||!job.job_id) return reload();
      return Promise.resolve(scopedJson(
        '/api/gen/job/'+encodeURIComponent(job.job_id)
      )).then(function(result){
        var status=text(result&&result.status);
        if(status==='pending'||status==='running'){
          job.status=status==='pending'?'queued':'running';
          job.phase=text(result.composition_phase||job.phase);
          job.progress=number(result.progress,job.progress);
          render();schedulePoll();return result;
        }
        return reload();
      }).catch(function(){
        // A shared editor/viewer may not own the generic job. Fall back to the
        // permission-aware assembly read model, then continue backoff polling.
        return reload();
      });
    }
    function idempotencyKey(){
      if(ui.pendingKey) return ui.pendingKey;
      ui.pendingKey='d3-'+Date.now().toString(36)+'-'+
        Math.random().toString(36).slice(2,12);
      return ui.pendingKey;
    }
    function resetIdempotency(prefix){
      ui.pendingKey=prefix+'-'+Date.now().toString(36)+'-'+
        Math.random().toString(36).slice(2,12);
      return ui.pendingKey;
    }
    function generatePreview(){
      if(!snapshot||!snapshot.actions||!snapshot.actions.can_preview||ui.busy){
        return Promise.resolve(null);
      }
      ui.busy=true;ui.error='';render();
      return Promise.resolve(scopedJson(PREVIEW_PATH,{
        method:'POST',
        headers:{'Idempotency-Key':idempotencyKey()},
        body:{
          project_id:snapshot.project_id,
          revision:snapshot.revision,
          assembly_revision:snapshot.assembly_revision
        }
      })).then(function(){
        pollDelay=2000;
        return reload();
      }).catch(function(error){
        ui.busy=false;ui.error=text(error&&error.message||error);render();
        throw error;
      });
    }
    function coverTime(){
      var input=host&&host.querySelector&&host.querySelector('[data-cover-time]');
      var value=input?Number(input.value):1000;
      return Number.isFinite(value)?Math.max(0,Math.round(value)):1000;
    }
    function exportFinal(){
      if(!snapshot||!snapshot.actions.can_export||ui.busy) return Promise.resolve(null);
      var previews=snapshot.versions.filter(function(item){
        return item&&item.kind==='preview'&&item.status==='succeeded';
      }).sort(function(a,b){ return number(b.version,0)-number(a.version,0); });
      if(!previews.length) return Promise.resolve(null);
      var request={
        project_id:snapshot.project_id,revision:snapshot.revision,
        assembly_revision:snapshot.assembly_revision,
        preview_version:number(previews[0].version,0),
        cover_time_ms:coverTime()
      };
      ui.busy=true;ui.error='';render();
      return Promise.resolve(scopedJson(FINAL_QUOTE_PATH,{
        method:'POST',body:request
      })).then(function(quote){
        if(quote&&quote.can_submit===false){
          throw new Error(quote.message||quote.reason||'项目预算或账户余额不足，暂不能导出');
        }
        var message='确认导出 1080p 正式成片？将扣除 '+
          number(quote.total_cost,0)+' 点。';
        var confirmFn=typeof options.confirmExport==='function'?
          options.confirmExport:function(textValue){
            return typeof window==='undefined'||typeof window.confirm!=='function'?
              true:window.confirm(textValue);
          };
        return Promise.resolve(confirmFn(message,quote)).then(function(confirmed){
          if(!confirmed){ ui.busy=false;render();return null; }
          request.quote_token=quote.quote_token;
          return scopedJson(FINAL_EXPORT_PATH,{
            method:'POST',
            headers:{'Idempotency-Key':resetIdempotency('d4')},
            body:request
          });
        });
      }).then(function(result){
        if(!result) return null;
        pollDelay=2000;return reload();
      }).catch(function(error){
        ui.busy=false;ui.error=text(error&&error.message||error);render();throw error;
      });
    }
    function confirmCompleted(){
      if(!snapshot||!snapshot.actions.can_confirm||ui.busy) return Promise.resolve(null);
      var finals=snapshot.versions.filter(function(item){
        return item&&item.kind==='final'&&item.status==='succeeded'&&item.asset_id;
      }).sort(function(a,b){ return number(b.version,0)-number(a.version,0); });
      if(!finals.length) return Promise.resolve(null);
      ui.busy=true;ui.error='';render();
      return Promise.resolve(scopedJson(FINAL_CONFIRM_PATH,{
        method:'POST',body:{
          project_id:snapshot.project_id,revision:snapshot.revision,
          final_version:number(finals[0].version,0)
        }
      })).then(function(){ return reload(); }).catch(function(error){
        ui.busy=false;ui.error=text(error&&error.message||error);render();throw error;
      });
    }
    function onClick(event){
      var target=event&&event.target;
      while(target&&target!==host&&
          !(target.getAttribute&&target.getAttribute('data-action'))){
        target=target.parentNode;
      }
      var action=target&&target.getAttribute&&target.getAttribute('data-action');
      if(action==='reload'){
        reload().catch(function(){});
      }else if(action==='generate-preview'){
        generatePreview().catch(function(){});
      }else if(action==='export-final'){
        exportFinal().catch(function(){});
      }else if(action==='confirm-completed'){
        confirmCompleted().catch(function(){});
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
        clearPoll();
        if(playerObjectUrl&&typeof URL!=='undefined'&&URL.revokeObjectURL){
          URL.revokeObjectURL(playerObjectUrl);playerObjectUrl='';
        }
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
