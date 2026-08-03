(function(root,factory){
  var api=factory(root);
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaWorkspace=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(root){
  'use strict';
  var STAGES=[
    ['settings','项目设置'],['characters_review','角色确认'],
    ['script_review','剧本确认'],['storyboard_review','分镜确认'],
    ['production','画面确认'],['voice_review','配音字幕'],
    ['video_review','视频确认'],['assembly_review','成片确认'],
    ['completed','已交付']
  ];
  var STAGE_INDEX={
    draft:0,characters_review:1,script_review:2,storyboard_review:3,
    production:4,voice_review:5,video_review:6,assembly_review:7,completed:8
  };
  function text(value){ return String(value==null?'':value); }
  function number(value,fallback){
    var result=Number(value);return isFinite(result)?result:(fallback==null?0:fallback);
  }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function formatTime(timestamp){
    var value=Number(timestamp);
    if(!value) return '时间未知';
    if(value<1000000000000) value*=1000;
    try{
      return new Date(value).toLocaleString('zh-CN',{
        month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'
      });
    }catch(ignore){ return '时间未知'; }
  }
  function formatDuration(ms){
    var seconds=Math.max(0,Math.round(number(ms,0)/1000));
    var minutes=Math.floor(seconds/60);
    return String(minutes).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');
  }
  function uniqueMessages(items){
    var seen=Object.create(null),result=[];
    (items||[]).forEach(function(item){
      var message=text(item&&item.message||item&&item.code);
      if(message&&!seen[message]){ seen[message]=true;result.push(message); }
    });
    return result;
  }
  function button(action,label,enabled,reason,className,extra){
    return '<button type="button" data-action="'+escapeHtml(action)+'"'+
      (className?' class="'+escapeHtml(className)+'"':'')+
      (enabled?'':' disabled')+
      (reason?' title="'+escapeHtml(reason)+'"':'')+
      (extra||'')+'>'+escapeHtml(label)+'</button>'+
      (!enabled&&reason?'<small class="nc-sdw-disabled-reason">'+
        escapeHtml(reason)+'</small>':'');
  }
  function shotVisible(shot,filter){
    if(filter==='pending') return !shot.ready;
    if(filter==='running') return shot.voice.status==='running'||
      shot.video.status==='running';
    if(filter==='failed') return shot.voice.status==='failed'||
      shot.video.status==='failed';
    if(filter==='unlocked') return !shot.voice.locked||!shot.video.confirmed;
    return true;
  }
  function renderStages(workspace){
    var current=STAGE_INDEX[workspace.stage];
    if(current==null) current=7;
    return STAGES.map(function(stage,index){
      var status=index<current?'done':index===current?'active':'waiting';
      return '<li data-stage-state="'+status+'"><span aria-hidden="true">'+
        (status==='done'?'✓':String(index+1))+'</span><div><strong>'+
        escapeHtml(stage[1])+'</strong><small>'+
        (status==='done'?'已完成':status==='active'?'当前阶段':'等待前序')+
        '</small></div></li>';
    }).join('');
  }
  function renderShotRail(workspace,selection){
    var filtered=workspace.shots.filter(function(shot){
      return shotVisible(shot,selection.filter);
    });
    var filters=[
      ['all','全部'],['pending','待补齐'],['running','生成中'],
      ['failed','失败'],['unlocked','待锁定']
    ].map(function(item){
      return '<button type="button" data-action="filter-shots" data-filter="'+
        item[0]+'" aria-pressed="'+(selection.filter===item[0])+'" class="'+
        (selection.filter===item[0]?'is-active':'')+'">'+item[1]+'</button>';
    }).join('');
    var shots=filtered.length?filtered.map(function(shot){
      var selected=selection.shotId===shot.id;
      var blocker=uniqueMessages(shot.blockers)[0]||'素材已就绪';
      return '<button type="button" class="nc-sdw-shot'+
        (selected?' is-selected':'')+(shot.ready?' is-ready':' is-blocked')+
        '" data-action="select-shot" data-shot-id="'+escapeHtml(shot.id)+
        '" aria-current="'+(selected?'true':'false')+'"><span><strong>'+
        escapeHtml(shot.shot_key)+'</strong><small>'+number(shot.duration,0)+
        ' 秒 · '+number(shot.voice.lines.length,0)+' 条台词</small></span>'+
        '<i>'+(shot.voice.locked?'配音✓':'配音—')+' · '+
        (shot.video.confirmed?'视频✓':'视频—')+'</i><em>'+
        escapeHtml(blocker)+'</em></button>';
    }).join(''):'<div class="nc-sdw-rail-empty">当前筛选下没有镜头</div>';
    return '<div class="nc-sdw-filter" aria-label="镜头筛选">'+filters+'</div>'+
      '<div class="nc-sdw-shots">'+shots+'</div>';
  }
  function renderTimeline(workspace,selectedShotId){
    var plan=workspace.media_plan&&Array.isArray(workspace.media_plan.shots)?
      workspace.media_plan.shots:[];
    var duration=number(
      workspace.media_plan&&workspace.media_plan.project_duration_ms,
      workspace.target_duration*1000
    );
    if(!plan.length||!duration){
      return '<div class="nc-sdw-timeline-empty">媒体计划尚未生成</div>';
    }
    return '<div class="nc-sdw-timeline-track">'+plan.map(function(shot,index){
      var width=Math.max(0,number(shot.duration_ms,0)/duration*100);
      return '<button type="button" data-action="select-shot" data-shot-id="'+
        escapeHtml(shot.id)+'" class="'+
        (selectedShotId===text(shot.id)?'is-selected ':'')+
        (index%2?'is-even':'')+'" style="width:'+width.toFixed(4)+'%" title="'+
        escapeHtml(text(shot.id)+' · '+formatDuration(shot.duration_ms))+
        '"><span>'+escapeHtml(text(shot.shot_key||index+1))+'</span></button>';
    }).join('')+'</div><div class="nc-sdw-timeline-time"><span>00:00</span><span>'+
      formatDuration(duration)+'</span></div>';
  }
  function playerVersion(version,workspace){
    var playback=workspace&&workspace.playback;
    var bundle=playback&&playback.current_version;
    if(version&&bundle&&bundle.status==='ready'&&
        bundle.source_version_id===version.id){
      return Object.assign({},version,{
        playback_version_id:bundle.id,
        playback_status:bundle.status,
        media_url:bundle.media_url,
        subtitle_url:bundle.subtitle_url,
        url:bundle.media_url
      });
    }
    return version;
  }
  function finalAssetHref(version,workspace,context){
    if(!version||!version.asset_id) return '';
    var query=[
      'cat=video',
      'asset_id='+encodeURIComponent(version.asset_id),
      'project_id='+encodeURIComponent(workspace&&workspace.project_id||'')
    ];
    if(context&&context.boardId){
      query.push('board_id='+encodeURIComponent(context.boardId));
    }
    return '/workbench/assets?'+query.join('&');
  }
  function renderPlayer(version,workspace,mediaError,canDownload,context){
    version=playerVersion(version,workspace);
    if(!version||version.status!=='succeeded'||!version.url){
      return '<section class="nc-sdw-player-empty" data-state="empty">'+
        '<div aria-hidden="true">▶</div><strong>暂无可播放版本</strong><span>'+
        (workspace.active_job?'任务完成后将在此显示播放器':
          '先生成 720p 预览，再进行正式导出')+'</span></section>';
    }
    var kind=version.kind==='final'?'1080p 正式成片':'720p 预览';
    return '<section class="nc-sdw-player-shell" data-kind="'+version.kind+'">'+
      '<div class="nc-sdw-player-meta"><span>'+escapeHtml(kind)+'</span><strong>v'+
      number(version.version,0)+'</strong><small>'+formatTime(version.created_at)+
      '</small></div><video controls playsinline preload="metadata" '+
      'data-d5-player data-version-id="'+escapeHtml(version.id)+
      '" aria-label="'+escapeHtml(kind+' v'+version.version)+'"></video>'+
      (version.kind==='preview'?'<p class="nc-sdw-preview-note">预览仅用于确认内容，不能作为正式交付文件。'+
        (version.subtitle_url?
          '<button type="button" data-action="toggle-subtitles">字幕：开</button>':
          '<button type="button" data-action="create-playback">生成可切换字幕播放包（0 点）</button>')+
        '</p>':
        (canDownload?'<div class="nc-sdw-delivery-links"><a href="'+escapeHtml(version.url)+
          '" target="_blank" rel="noopener">下载成片</a>'+
          (version.asset_id?'<a href="'+escapeHtml(finalAssetHref(
            version,workspace,context
          ))+'">打开资产</a>':'')+'</div>':
          '<p class="nc-sdw-preview-note">当前权限仅允许播放，不能下载或打开交付资产。</p>'))+
      (mediaError?'<div class="nc-sdw-media-error" role="alert"><span>'+
        escapeHtml(mediaError.message)+'</span><button type="button" '+
        'data-action="retry-media">重新加载</button></div>':'')+'</section>';
  }
  function renderVersions(workspace,selection,versionsModule){
    if(!workspace.versions.length){
      return '<div class="nc-sdw-version-empty">尚无合成版本</div>';
    }
    return workspace.versions.map(function(version){
      var selected=selection.versionId===version.id;
      var current=version.kind==='final'?
        version.version===workspace.current_final_version:
        version.version===workspace.current_preview_version;
      return '<button type="button" data-action="select-version" data-version-id="'+
        escapeHtml(version.id)+'" class="nc-sdw-version'+
        (selected?' is-selected':'')+'" aria-pressed="'+selected+'"><span><strong>'+
        escapeHtml(versionsModule.label(version))+'</strong><small>'+
        escapeHtml(formatTime(version.created_at))+
        (version.created_by?' · '+escapeHtml(version.created_by):'')+
        '</small></span><i data-status="'+escapeHtml(version.status)+'">'+
        escapeHtml(current?'当前':versionsModule.statusLabel(version.status))+
        '</i></button>';
    }).join('');
  }
  function renderTask(job){
    if(!job) return '<section class="nc-sdw-task is-empty"><strong>后台任务</strong>'+
      '<span>当前没有运行中的合成任务</span></section>';
    var progress=Math.max(0,Math.min(100,number(job.progress,0)));
    return '<section class="nc-sdw-task" aria-live="polite"><header><strong>'+
      escapeHtml(job.kind==='final'?'正式导出任务':'预览任务')+
      '</strong><code>#'+escapeHtml(text(job.job_id).slice(-10))+
      '</code></header><div><i style="width:'+progress+'%"></i></div><dl>'+
      '<div><dt>阶段</dt><dd>'+escapeHtml(text(job.phase||'queued'))+'</dd></div>'+
      '<div><dt>进度</dt><dd>'+progress+'%</dd></div>'+
      '<div><dt>状态</dt><dd>'+escapeHtml(text(job.status||'queued'))+'</dd></div>'+
      '</dl>'+(job.error_message?'<p role="alert">'+
        escapeHtml(job.error_message)+'</p>':'')+'</section>';
  }
  function renderSoundDesign(workspace,state,selectors){
    var config=state.ui.configDraft||workspace.config;
    var assets=Array.isArray(state.ui.soundAssets)?state.ui.soundAssets:[];
    var cues=Array.isArray(config.sound_cues)?config.sound_cues:[];
    function assetOptions(selected,emptyLabel){
      return '<option value="">'+escapeHtml(emptyLabel)+'</option>'+
        assets.map(function(asset){
          var id=number(asset.id,0);
          return '<option value="'+id+'"'+(id===number(selected,0)?' selected':'')+'>'+
            escapeHtml(
              asset.name||asset.title||asset.voice_name||
              (asset.text?text(asset.text).slice(0,24):'')||('音频 #'+id)
            )+'</option>';
        }).join('');
    }
    function shotOptions(selected){
      return workspace.shots.map(function(shot){
        return '<option value="'+escapeHtml(shot.id)+'"'+
          (text(selected)===text(shot.id)?' selected':'')+'>'+
          escapeHtml(shot.shot_key)+'</option>';
      }).join('');
    }
    var disabled=selectors.readOnly||!workspace.actions.can_save_config||
      workspace.active_job?' disabled':'';
    return '<section class="nc-sdw-sound-design"><header><div><strong>声音设计（第一批）</strong>'+
      '<small>对白 / 手动音效 / BGM 三轨混音；视频原声保持丢弃</small></div>'+
      '<button type="button" data-action="refresh-audio-assets">刷新音频库</button></header>'+
      '<div class="nc-sdw-sound-bgm"><label>背景音乐<select data-bgm-asset'+disabled+'>'+
      assetOptions(config.bgm&&config.bgm.asset_id,'不使用背景音乐')+
      '</select></label><label>音乐音量<input data-bgm-volume type="number" min="0" max="1" '+
      'step="0.01" value="'+number(config.bgm&&config.bgm.volume,0.18)+'"'+disabled+'></label></div>'+
      '<div class="nc-sdw-sound-cues"><header><strong>手动音效时间线</strong>'+
      '<button type="button" data-action="add-sound-cue"'+disabled+'>添加音效</button></header>'+
      (cues.length?cues.map(function(cue,index){
        return '<div class="nc-sdw-sound-cue" data-sound-cue data-cue-id="'+
          escapeHtml(cue.id)+'"><label>镜头<select data-cue-shot'+disabled+'>'+
          shotOptions(cue.shot_id)+'</select></label><label>类型<select data-cue-kind'+disabled+'>'+
          ['ambience','foley','transition','impact'].map(function(kind){
            var labels={ambience:'环境',foley:'动作',transition:'转场',impact:'强调'};
            return '<option value="'+kind+'"'+(cue.kind===kind?' selected':'')+'>'+
              labels[kind]+'</option>';
          }).join('')+'</select></label><label>音频<select data-cue-asset'+disabled+'>'+
          assetOptions(cue.asset_id,'请选择音频')+'</select></label>'+
          '<label>开始 ms<input data-cue-start type="number" min="0" step="100" value="'+
          number(cue.start_ms,0)+'"'+disabled+'></label><label>结束 ms<input data-cue-end '+
          'type="number" min="1" step="100" value="'+number(cue.end_ms,1000)+'"'+disabled+'></label>'+
          '<label>音量<input data-cue-volume type="number" min="0" max="1" step="0.01" value="'+
          number(cue.volume,0.5)+'"'+disabled+'></label><label class="nc-sdw-sound-check">'+
          '<input data-cue-loop type="checkbox"'+(cue.loop?' checked':'')+disabled+'>循环</label>'+
          '<button type="button" data-action="remove-sound-cue" data-cue-index="'+index+'"'+
          disabled+'>删除</button></div>';
      }).join(''):'<p>尚未添加环境声或音效。对白仍会正常参与主音轨混音。</p>')+
      '</div><footer><span>保存后装配版本会递增，旧预览不再作为当前版本。</span>'+
      '<button type="button" data-action="save-sound-config"'+disabled+'>保存声音配置</button>'+
      '</footer></section>';
  }
  function renderAiSoundDesign(workspace,state,selectors){
    var data=state.ui.aiSoundDesign;
    var busy=state.ui.busyAction;
    var disabled=selectors.readOnly||!workspace.actions.can_save_config||
      workspace.active_job;
    if(!data){
      return '<section class="nc-sdw-ai-sound"><header><div><strong>AI 自动音效</strong>'+
        '<small>先分析，不扣点；确认建议后再报价生成</small></div>'+
        '<button type="button" data-action="analyze-ai-sound"'+
        (disabled||busy?' disabled':'')+'>分析镜头</button></header>'+
        '<p class="nc-sdw-ai-empty">尚未分析镜头中的环境声、动作声和转场声。</p></section>';
    }
    var suggestions=Array.isArray(data.suggestions)?data.suggestions:[];
    var jobs=Array.isArray(data.jobs)?data.jobs:[];
    var provider=data.provider||{};
    var confirmed=suggestions.filter(function(item){
      return item.status==='confirmed';
    });
    var readyJobs=jobs.filter(function(item){
      return item.status==='done'||item.status==='manual_review';
    });
    var active=jobs.some(function(item){
      return item.status==='pending'||item.status==='running';
    });
    function statusLabel(status){
      return {
        suggested:'待确认',confirmed:'已确认',rejected:'已忽略',
        generated:'已生成',applied:'已应用',pending:'排队中',
        running:'生成中',done:'质检通过',manual_review:'需人工确认',
        failed:'失败'
      }[status]||status;
    }
    return '<section class="nc-sdw-ai-sound"><header><div><strong>AI 自动音效</strong>'+
      '<small>分析免费 · '+escapeHtml(provider.provider||'provider')+
      (provider.configured?' 已就绪':' 未配置')+'</small></div>'+
      '<button type="button" data-action="analyze-ai-sound"'+
      (disabled||busy||active?' disabled':'')+'>重新分析</button></header>'+
      (!provider.configured?'<div class="nc-sdw-ai-warning">'+
        escapeHtml(provider.detail||'AI 音效 Provider 尚未配置，当前不会扣点')+
        '</div>':'')+
      '<div class="nc-sdw-ai-list">'+(suggestions.length?suggestions.map(function(item){
        var editable=['suggested','confirmed','rejected'].indexOf(item.status)>=0;
        return '<article data-ai-suggestion data-suggestion-id="'+escapeHtml(item.id)+
          '"><header><span>'+escapeHtml(item.shot_key)+' · '+
          escapeHtml(item.kind)+'</span><i data-status="'+escapeHtml(item.status)+'">'+
          escapeHtml(statusLabel(item.status))+'</i></header>'+
          '<textarea data-ai-prompt maxlength="450"'+
          (!editable||disabled?' disabled':'')+'>'+escapeHtml(item.prompt)+'</textarea>'+
          '<div><label>音量<input data-ai-volume type="number" min="0" max="1" step="0.05" value="'+
          number(item.volume,0.5)+'"'+(!editable||disabled?' disabled':'')+'></label>'+
          '<label><input data-ai-loop type="checkbox"'+(item.loop?' checked':'')+
          (!editable||disabled?' disabled':'')+'>循环</label><span>'+
          Math.round(number(item.duration_ms,0)/100)/10+' 秒</span></div>'+
          (editable?'<footer><button type="button" data-action="set-ai-suggestion" '+
          'data-suggestion-status="rejected"'+(disabled?' disabled':'')+'>忽略</button>'+
          '<button type="button" data-action="set-ai-suggestion" '+
          'data-suggestion-status="confirmed"'+(disabled?' disabled':'')+'>确认建议</button>'+
          '</footer>':'')+'</article>';
      }).join(''):'<p class="nc-sdw-ai-empty">没有找到适合自动生成的音效建议。</p>')+
      '</div>'+
      (jobs.length?'<div class="nc-sdw-ai-jobs"><strong>生成任务</strong>'+
        jobs.map(function(job){
          return '<div data-status="'+escapeHtml(job.status)+'"><span>#'+
            escapeHtml(job.job_id)+' · '+escapeHtml(statusLabel(job.status))+
            '</span><small>'+escapeHtml(job.error||'')+'</small></div>';
        }).join('')+'</div>':'')+
      '<footer><span>已确认 '+confirmed.length+' 条'+
      (active?'，任务处理中':'')+'</span>'+
      '<button type="button" data-action="generate-ai-sound"'+
      (disabled||busy||active||!provider.configured||!confirmed.length?' disabled':'')+
      '>报价并生成</button><button type="button" data-action="apply-ai-sound"'+
      (disabled||busy||active||!readyJobs.length?' disabled':'')+
      '>应用已生成音效</button></footer></section>';
  }
  function renderWorkspace(state,selectors,modules){
    modules=modules||{};
    var workspace=state.workspace;
    if(state.ui.loading&&!workspace){
      return '<section class="nc-sdw-state" data-state="loading" aria-live="polite">'+
        '<span class="nc-sdw-spinner" aria-hidden="true"></span><strong>'+
        '正在恢复短剧工作区</strong><p>加载阶段、版本、锁定与运行中任务…</p></section>';
    }
    if(!workspace){
      return '<section class="nc-sdw-state is-error" data-state="error" role="alert">'+
        '<strong>短剧工作区加载失败</strong><p>'+
        escapeHtml(state.ui.error||'请检查网络后重试')+'</p>'+
        '<button type="button" data-action="reload">重新加载</button></section>';
    }
    var version=selectors.version;
    var lock=modules.locks.lockState(
      workspace,version,state.context.canEdit
    );
    var readiness=uniqueMessages(workspace.readiness.blockers);
    var project=state.project||{};
    var spent=number(project.spent_points,0);
    var budget=number(project.point_budget,0);
    var processing=number(project.reserved_points,0);
    var busy=!!state.ui.busyAction||!!workspace.active_job;
    var previewReason=!state.context.canEdit?'当前为只读权限':
      selectors.completed?'项目已完成':
      selectors.historyOnly?'历史版本只读':
      workspace.active_job?'已有任务运行中':
      readiness[0]||'当前素材未满足预览条件';
    var exportReason=!state.context.canEdit?'当前为只读权限':
      selectors.completed?'项目已完成':
      selectors.historyOnly?'历史版本只读':
      workspace.active_job?'已有任务运行中':
      !workspace.actions.can_export?'请先生成可用的 720p 预览':'';
    var selectedShot=workspace.shots.filter(function(shot){
      return shot.id===state.selection.shotId;
    })[0]||null;
    var historyBanner=selectors.historyOnly?
      '<div class="nc-sdw-history-banner" role="status"><strong>历史版本只读</strong>'+
      '<span>选择版本只改变查看上下文，不会修改项目当前版本。</span>'+
      '<button type="button" data-action="return-current">返回当前工作区</button></div>':'';
    var completedBanner=selectors.completed?
      '<div class="nc-sdw-completed-banner"><strong>项目已交付</strong>'+
      '<span>工作区已永久只读，仍可播放和下载正式资产。</span></div>':'';
    return '<section class="nc-sdw" data-readonly="'+selectors.readOnly+
      '" data-stage="'+escapeHtml(workspace.stage)+'">'+
      '<div class="nc-sdw-mobile-tools"><button type="button" data-action="toggle-left" '+
      'aria-expanded="'+state.ui.leftOpen+'">流程与镜头</button><strong>成片工作台</strong>'+
      '<button type="button" data-action="toggle-right" aria-expanded="'+
      state.ui.rightOpen+'">版本与任务</button></div>'+
      '<aside class="nc-sdw-left '+(state.ui.leftOpen?'is-open':'')+
      '" aria-label="短剧阶段和镜头导航"><header><span>D-5 完整交互</span>'+
      '<h2>流程与镜头</h2><small>'+workspace.shots.length+' 镜 · '+
      workspace.target_duration+' 秒 · '+escapeHtml(workspace.ratio)+
      '</small></header><ol class="nc-sdw-stages">'+renderStages(workspace)+
      '</ol><section><h3>镜头导航</h3>'+renderShotRail(workspace,state.selection)+
      '</section></aside>'+
      '<main class="nc-sdw-main"><header class="nc-sdw-main-head"><div><span>'+
      (version&&version.kind==='final'?'正式成片':'成片预览')+
      '</span><h2>项目级合成与交付</h2><small>项目 R'+workspace.revision+
      ' · 装配 R'+workspace.assembly_revision+'</small></div>'+
      '<button type="button" data-action="reload" class="nc-sdw-refresh" '+
      'aria-label="刷新工作区">↻</button></header>'+
      completedBanner+historyBanner+
      (state.ui.error?'<div class="nc-sdw-inline-error" role="alert"><strong>'+
        (state.ui.reconnecting?'正在重新连接':'操作未完成')+'</strong><span>'+
        escapeHtml(state.ui.error)+'</span></div>':'')+
      renderPlayer(
        version,workspace,state.ui.mediaError,state.context.canEdit,state.context
      )+
      '<section class="nc-sdw-timeline"><header><strong>镜头时间线</strong><small>'+
      (selectedShot?'当前：'+escapeHtml(selectedShot.shot_key):'选择镜头查看')+
      '</small></header>'+renderTimeline(workspace,state.selection.shotId)+
      '</section><section class="nc-sdw-config"><header><strong>装配配置</strong>'+
      '<span>'+(selectors.readOnly?'只读':'由服务端版本控制')+'</span></header>'+
      '<dl><div><dt>字幕</dt><dd>'+
      (workspace.config.subtitle.enabled===false?'关闭':'开启 · '+
        escapeHtml(workspace.config.subtitle.position||'bottom'))+
      '</dd></div><div><dt>背景音乐</dt><dd>'+
      escapeHtml(workspace.config.bgm.asset_id||'未选择')+
      '</dd></div><div><dt>预览规格</dt><dd>720p</dd></div>'+
      '<div><dt>正式规格</dt><dd>1080p</dd></div></dl></section>'+
      renderSoundDesign(workspace,state,selectors)+
      renderAiSoundDesign(workspace,state,selectors)+'</main>'+
      '<aside class="nc-sdw-right '+(state.ui.rightOpen?'is-open':'')+
      '" aria-label="版本、锁定、费用和任务控制台"><header><span>控制台</span>'+
      '<h2>版本与任务</h2><small>状态更新时间 '+formatTime(state.ui.lastUpdatedAt)+
      '</small></header><section class="nc-sdw-version-panel"><div><strong>'+
      '版本</strong><button type="button" data-action="reload" aria-label="刷新版本">↻</button>'+
      '</div>'+renderVersions(workspace,state.selection,modules.versions)+
      '</section><section class="nc-sdw-lock" data-locked="'+lock.locked+'">'+
      '<div><span aria-hidden="true">'+(lock.locked?'🔒':'○')+'</span><strong>'+
      (lock.locked?'当前成果已锁定':'当前成果未锁定')+'</strong></div><p>'+
      escapeHtml(lock.reason)+'</p></section>'+
      '<section class="nc-sdw-metrics"><dl><div><dt>当前阶段</dt><dd>'+
      escapeHtml(workspace.stage==='completed'?'已交付':'成片确认')+
      '</dd></div><div><dt>项目预算</dt><dd>'+budget+' 点</dd></div>'+
      '<div><dt>累计已用</dt><dd>'+spent+' 点</dd></div>'+
      '<div><dt>处理中</dt><dd>'+processing+' 点</dd></div></dl></section>'+
      renderTask(workspace.active_job||(
        workspace.latest_job&&workspace.latest_job.status==='failed'?
          workspace.latest_job:null
      ))+'<section class="nc-sdw-blockers"><strong>门禁检查</strong><ul>'+
      (readiness.length?readiness.slice(0,6).map(function(message){
        return '<li>'+escapeHtml(message)+'</li>';
      }).join(''):'<li class="is-ready">前序素材已满足合成条件</li>')+
      '</ul></section><section class="nc-sdw-actions"><strong>下一步</strong>'+
      button(
        'generate-preview',
        state.ui.busyAction==='preview'?'正在提交…':'生成 720p 预览',
        state.context.canEdit&&!selectors.completed&&!selectors.historyOnly&&
          !busy&&workspace.actions.can_preview,
        previewReason,''
      )+
      '<label>封面时间（毫秒）<input type="number" min="0" step="100" '+
      'data-cover-time value="1000"'+(selectors.readOnly?' disabled':'')+'></label>'+
      button(
        'export-final',
        state.ui.busyAction==='export'?'正在提交…':'导出 1080p 成片',
        state.context.canEdit&&!selectors.completed&&!selectors.historyOnly&&
          !busy&&workspace.actions.can_export,
        exportReason,''
      )+
      modules.completion.render(
        workspace.completion||{},project,state.ui,
        state.context.canEdit&&!selectors.historyOnly,
        busy
      )+'</section>'+
      (state.ui.toast?'<div class="nc-sdw-toast" role="status">'+
        escapeHtml(state.ui.toast)+'</div>':'')+'</aside>'+
      ((state.ui.leftOpen||state.ui.rightOpen)?
        '<button type="button" class="nc-sdw-scrim" data-action="close-drawers" '+
        'aria-label="关闭抽屉"></button>':'')+'</section>';
  }
  function findActionTarget(node,host){
    while(node&&node!==host){
      if(node.getAttribute&&node.getAttribute('data-action')) return node;
      node=node.parentNode;
    }
    return null;
  }
  function createWorkspace(options){
    options=options||{};
    var globals=root&&root.HQCanvas||{};
    var modules={
      store:options.storeModule||globals.shortDramaStore,
      api:options.apiModule||globals.shortDramaD5Api,
      poller:options.pollerModule||globals.shortDramaPoller,
      player:options.playerModule||globals.shortDramaPlayer,
      versions:options.versionsModule||globals.shortDramaVersions,
      locks:options.locksModule||globals.shortDramaLocks,
      forms:options.formsModule||globals.shortDramaForms,
      completion:options.completionModule||globals.shortDramaCompletion
    };
    ['store','api','poller','player','versions','locks','forms','completion']
      .forEach(function(name){
        if(!modules[name]) throw new Error('D-5 模块未加载：'+name);
      });
    var host=options.host,destroyed=false,renderQueued=false,soundTimer=null;
    var loadGeneration=0,unsubscribe=null,playerController=null;
    var mediaError=null,pendingKeys={
      preview:'',export:'',completion:'',playback:'',sound:''
    };
    var confirmHook=typeof options.confirm==='function'?options.confirm:
      (typeof window!=='undefined'&&typeof window.confirm==='function'?
        window.confirm.bind(window):function(){ return false; });
    var storage=Object.prototype.hasOwnProperty.call(options,'storage')?
      options.storage:(typeof localStorage!=='undefined'?localStorage:null);
    var store=modules.store.createStore({
      projectId:options.projectId,boardId:options.boardId,
      canEdit:options.canEdit!==false,project:options.project||{}
    });
    var api=modules.api.createApi({
      client:options.client,boardId:options.boardId
    });
    var mutations=modules.api.createMutationCoordinator();
    var poller=modules.poller.createPoller({
      document:Object.prototype.hasOwnProperty.call(options,'document')?
        options.document:(typeof document!=='undefined'?document:null),
      poll:function(){ return api.load(options.projectId); },
      onResult:function(payload){
        if(destroyed) return false;
        var accepted=store.setWorkspace(payload.result);
        persistSelection();
        return accepted&&payload.result&&payload.result.active_job?true:false;
      },
      onError:function(error){
        if(!destroyed) store.patchUi({
          reconnecting:true,error:text(error&&error.message||error)
        });
      }
    });
    function state(){ return store.getState(); }
    function persistSelection(){
      if(!storage) return;
      try{
        var current=state();
        storage.setItem('hq-short-drama-d5:'+options.projectId,JSON.stringify({
          shotId:current.selection.shotId,
          versionId:current.selection.versionId,
          filter:current.selection.filter
        }));
      }catch(ignore){}
    }
    function restoreSelection(){
      if(!storage) return;
      try{
        var saved=JSON.parse(storage.getItem(
          'hq-short-drama-d5:'+options.projectId
        )||'{}');
        if(saved.filter) store.setFilter(saved.filter);
        if(saved.shotId) store.selectShot(saved.shotId);
        if(saved.versionId) store.selectVersion(saved.versionId);
      }catch(ignore){}
    }
    function queueRender(){
      if(renderQueued||destroyed) return;
      renderQueued=true;
      Promise.resolve().then(function(){ renderQueued=false;render(); });
    }
    function capturePaneScroll(){
      if(!host||typeof host.querySelector!=='function') return [];
      return ['.nc-sdw-left','.nc-sdw-main','.nc-sdw-right'].map(function(selector){
        var pane=host.querySelector(selector);
        return {
          selector:selector,
          top:pane?number(pane.scrollTop,0):0,
          left:pane?number(pane.scrollLeft,0):0
        };
      });
    }
    function restorePaneScroll(positions){
      if(!host||typeof host.querySelector!=='function') return;
      (positions||[]).forEach(function(position){
        var pane=host.querySelector(position.selector);
        if(!pane) return;
        pane.scrollTop=position.top;
        pane.scrollLeft=position.left;
      });
    }
    function hydratePlayer(current,selectors){
      if(!host||typeof host.querySelector!=='function') return;
      var video=host.querySelector('[data-d5-player]');
      if(!video||!selectors.version) return;
      if(!playerController){
        playerController=modules.player.createPlayer({
          api:api,host:host,
          onError:function(error){
            mediaError=error;store.patchUi({mediaError:error});
          },
          onReady:function(){
            mediaError=null;
          }
        });
      }
      playerController.attach(
        video,playerVersion(selectors.version,current.workspace)
      );
    }
    function render(){
      var current=state(),selectors=store.selectors();
      current.ui.mediaError=mediaError;
      var html=renderWorkspace(current,selectors,modules);
      if(host&&!destroyed){
        var paneScroll=capturePaneScroll();
        host.innerHTML=html;
        restorePaneScroll(paneScroll);
        hydratePlayer(current,selectors);
      }
      return html;
    }
    function notifySummary(snapshot){
      if(typeof options.onChange!=='function'||!snapshot) return Promise.resolve();
      return Promise.resolve(options.onChange({
        project_id:snapshot.project_id,
        revision:snapshot.revision,
        stage:snapshot.stage,
        ratio:snapshot.ratio,
        spent_points:number(options.project&&options.project.spent_points,0),
        point_budget:number(options.project&&options.project.point_budget,0),
        reserved_points:number(options.project&&options.project.reserved_points,0)
      }));
    }
    function applyLoaded(payload,generation){
      if(destroyed||generation!==loadGeneration) return null;
      if(!payload||!payload.result||
          payload.result.project_id&&payload.result.project_id!==options.projectId){
        return null;
      }
      store.setWorkspace(payload.result);
      store.patchUi({configDraft:null});
      persistSelection();
      if(payload.result.active_job) poller.start(false);
      else poller.stop();
      return notifySummary(payload.result).then(function(){ return payload.result; });
    }
    function reload(){
      if(destroyed) return Promise.resolve(null);
      var generation=++loadGeneration;
      store.patchUi({loading:true,error:'',reconnecting:false});
      return api.load(options.projectId).then(function(payload){
        return applyLoaded(payload,generation);
      }).catch(function(error){
        if(destroyed||generation!==loadGeneration) return null;
        store.patchUi({
          loading:false,error:text(error&&error.message||error),
          reconnecting:false
        });
        throw error;
      });
    }
    function refreshAudioAssets(){
      return api.audioAssets(options.projectId).then(function(result){
        var items=Array.isArray(result)?result:
          (result&&Array.isArray(result.items)?result.items:[]);
        store.patchUi({soundAssets:items});
        return items;
      }).catch(function(error){
        store.patchUi({error:text(error&&error.message||error)});
        return [];
      });
    }
    function stopSoundPolling(){
      if(soundTimer!==null){
        clearTimeout(soundTimer);soundTimer=null;
      }
    }
    function scheduleSoundPolling(){
      stopSoundPolling();
      if(destroyed) return;
      var data=state().ui.aiSoundDesign;
      var active=data&&Array.isArray(data.jobs)&&data.jobs.some(function(item){
        return item.status==='pending'||item.status==='running';
      });
      if(!active) return;
      soundTimer=setTimeout(function(){
        soundTimer=null;loadSoundDesign().catch(function(){});
      },2500);
    }
    function loadSoundDesign(){
      return api.soundDesign(options.projectId).then(function(result){
        store.patchUi({aiSoundDesign:result});
        scheduleSoundPolling();
        return result;
      }).catch(function(error){
        store.patchUi({aiSoundDesignError:text(error&&error.message||error)});
        throw error;
      });
    }
    function analyzeAiSound(){
      var workspace=state().workspace;
      if(!workspace) return Promise.resolve(null);
      store.patchUi({busyAction:'sound-analyze',error:'',toast:'正在分析镜头音效…'});
      return api.analyzeSoundDesign({
        project_id:workspace.project_id,revision:workspace.revision
      }).then(function(result){
        store.patchUi({
          aiSoundDesign:result,busyAction:'',toast:'镜头音效分析完成，请逐条确认。'
        });
        return result;
      }).catch(function(error){
        store.patchUi({busyAction:'',error:text(error.message||error),toast:''});
        throw error;
      });
    }
    function suggestionRow(node){
      while(node&&node!==host){
        if(node.getAttribute&&node.getAttribute('data-ai-suggestion')!==null){
          return node;
        }
        node=node.parentNode;
      }
      return null;
    }
    function saveAiSuggestion(target){
      var row=suggestionRow(target),workspace=state().workspace;
      if(!row||!workspace) return Promise.resolve(null);
      var prompt=row.querySelector('[data-ai-prompt]');
      var volume=row.querySelector('[data-ai-volume]');
      var loop=row.querySelector('[data-ai-loop]');
      var status=target.getAttribute('data-suggestion-status');
      store.patchUi({busyAction:'sound-edit',error:'',toast:'正在保存音效建议…'});
      return api.saveSoundSuggestions({
        project_id:workspace.project_id,revision:workspace.revision,
        items:[{
          id:row.getAttribute('data-suggestion-id'),
          prompt:text(prompt&&prompt.value),
          status:status,
          volume:Number(volume&&volume.value),
          loop:!!(loop&&loop.checked)
        }]
      }).then(function(result){
        store.patchUi({
          aiSoundDesign:result,busyAction:'',toast:'音效建议已保存。'
        });
        return result;
      }).catch(function(error){
        store.patchUi({busyAction:'',error:text(error.message||error),toast:''});
        throw error;
      });
    }
    function generateAiSound(){
      var current=state(),workspace=current.workspace;
      var data=current.ui.aiSoundDesign||{};
      var ids=(data.suggestions||[]).filter(function(item){
        return item.status==='confirmed';
      }).map(function(item){ return item.id; });
      if(!workspace||!ids.length) return Promise.resolve(null);
      store.patchUi({busyAction:'sound-generate',error:'',toast:'正在获取实时报价…'});
      return api.quoteSoundEffects({
        project_id:workspace.project_id,revision:workspace.revision,
        assembly_revision:workspace.assembly_revision,suggestion_ids:ids
      }).then(function(quote){
        var detail='生成 '+quote.items.length+' 条 AI 音效将消耗 '+
          quote.total_cost+' 点，确认提交吗？';
        if(!confirmHook(detail)){
          store.patchUi({busyAction:'',toast:'已取消生成，不扣点。'});
          return null;
        }
        if(!pendingKeys.sound){
          pendingKeys.sound=modules.api.createIdempotencyKey('sd-sfx');
        }
        return api.generateSoundEffects({
          project_id:workspace.project_id,revision:workspace.revision,
          assembly_revision:workspace.assembly_revision,
          quote_token:quote.quote_token
        },pendingKeys.sound).then(function(result){
          pendingKeys.sound='';
          store.patchUi({busyAction:'',toast:'AI 音效任务已提交，可安全离开页面。'});
          return loadSoundDesign();
        });
      }).catch(function(error){
        store.patchUi({busyAction:'',error:text(error.message||error),toast:''});
        throw error;
      });
    }
    function applyAiSound(){
      var current=state(),workspace=current.workspace;
      var jobs=((current.ui.aiSoundDesign||{}).jobs||[]).filter(function(item){
        return item.status==='done'||item.status==='manual_review';
      });
      if(!workspace||!jobs.length) return Promise.resolve(null);
      var manual=jobs.some(function(item){ return item.status==='manual_review'; });
      if(manual&&!confirmHook(
        '部分音效质检提示需要人工确认。已试听并确认仍要应用吗？'
      )) return Promise.resolve(null);
      store.patchUi({busyAction:'sound-apply',error:'',toast:'正在应用 AI 音效…'});
      return api.applySoundEffects({
        project_id:workspace.project_id,revision:workspace.revision,
        assembly_revision:workspace.assembly_revision,
        job_ids:jobs.map(function(item){ return Number(item.job_id); }),
        approve_manual_review:manual
      }).then(function(){
        store.patchUi({busyAction:'',toast:'AI 音效已加入时间线，旧预览已失效。'});
        return Promise.all([reload(),refreshAudioAssets(),loadSoundDesign()]);
      }).catch(function(error){
        store.patchUi({busyAction:'',error:text(error.message||error),toast:''});
        throw error;
      });
    }
    function coverTime(){
      var input=host&&host.querySelector&&host.querySelector('[data-cover-time]');
      var value=input?Number(input.value):1000;
      return isFinite(value)?Math.max(0,Math.round(value)):1000;
    }
    function draftConfig(){
      var current=state(),workspace=current.workspace;
      return JSON.parse(JSON.stringify(
        current.ui.configDraft||workspace&&workspace.config||{}
      ));
    }
    function readSoundConfig(){
      var current=state(),workspace=current.workspace;
      var config=draftConfig();
      var bgmAsset=host.querySelector('[data-bgm-asset]');
      var bgmVolume=host.querySelector('[data-bgm-volume]');
      config.subtitle=config.subtitle||{
        enabled:true,preset:'white_outline',position:'bottom'
      };
      config.bgm=config.bgm||{
        asset_id:null,volume:0.18,fade_in_ms:500,fade_out_ms:800
      };
      config.bgm.asset_id=bgmAsset&&bgmAsset.value?
        Number(bgmAsset.value):null;
      config.bgm.volume=bgmVolume?Number(bgmVolume.value):0.18;
      config.sound_cues=Array.prototype.slice.call(
        host.querySelectorAll('[data-sound-cue]')
      ).map(function(row){
        function value(selector){ var node=row.querySelector(selector);return node&&node.value; }
        var loop=row.querySelector('[data-cue-loop]');
        return {
          id:row.getAttribute('data-cue-id'),
          shot_id:value('[data-cue-shot]'),
          kind:value('[data-cue-kind]'),
          asset_id:Number(value('[data-cue-asset]')),
          start_ms:Math.round(Number(value('[data-cue-start]'))),
          end_ms:Math.round(Number(value('[data-cue-end]'))),
          loop:!!(loop&&loop.checked),
          volume:Number(value('[data-cue-volume]')),
          fade_in_ms:0,
          fade_out_ms:0,
          enabled:true
        };
      });
      return {
        project_id:workspace.project_id,
        revision:workspace.revision,
        assembly_revision:workspace.assembly_revision,
        config:{
          subtitle:config.subtitle,
          bgm:config.bgm,
          sound_cues:config.sound_cues
        }
      };
    }
    function captureSoundConfigDraft(){
      var current=state(),workspace=current.workspace;
      if(
        !workspace||!host||typeof host.querySelector!=='function'||
        !host.querySelector('[data-bgm-asset]')
      ) return false;
      var request=readSoundConfig();
      store.patchUi({configDraft:request.config});
      return true;
    }
    function isSoundConfigTarget(target){
      if(!target||typeof target.getAttribute!=='function') return false;
      return [
        'data-bgm-asset','data-bgm-volume','data-cue-shot','data-cue-kind',
        'data-cue-asset','data-cue-start','data-cue-end','data-cue-volume',
        'data-cue-loop'
      ].some(function(name){ return target.getAttribute(name)!==null; });
    }
    function addSoundCue(){
      var current=state(),workspace=current.workspace;
      var assets=Array.isArray(current.ui.soundAssets)?
        current.ui.soundAssets:[];
      var shot=workspace.shots.filter(function(item){
        return item.id===current.selection.shotId;
      })[0]||workspace.shots[0];
      if(!shot||!assets.length){
        store.patchUi({error:'请先上传音频资产并刷新音频库。'});
        return;
      }
      var config=draftConfig();
      config.sound_cues=Array.isArray(config.sound_cues)?
        config.sound_cues:[];
      config.sound_cues.push({
        id:'cue-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8),
        shot_id:shot.id,kind:'foley',asset_id:Number(assets[0].id),
        start_ms:0,end_ms:Math.min(1000,number(shot.duration,1)*1000),
        loop:false,volume:0.5,fade_in_ms:0,fade_out_ms:0,enabled:true
      });
      store.patchUi({configDraft:config,error:''});
    }
    function removeSoundCue(index){
      var config=draftConfig();
      config.sound_cues=(config.sound_cues||[]).filter(function(item,i){
        return i!==index;
      });
      store.patchUi({configDraft:config,error:''});
    }
    function saveSoundConfig(){
      var current=state(),workspace=current.workspace,selectors=store.selectors();
      if(!workspace||selectors.readOnly||!workspace.actions.can_save_config){
        return Promise.resolve(null);
      }
      var request;
      try{ request=readSoundConfig(); }
      catch(error){
        store.patchUi({error:text(error&&error.message||error)});
        return Promise.reject(error);
      }
      store.patchUi({busyAction:'config',error:'',toast:'正在保存声音配置…'});
      return api.saveConfig(request).then(function(){
        store.patchUi({configDraft:null,toast:'声音配置已保存，请重新生成预览。'});
        return reload();
      }).catch(function(error){
        store.patchUi({busyAction:'',error:text(error.message||error),toast:''});
        throw error;
      });
    }
    function preview(){
      var current=state(),workspace=current.workspace,selectors=store.selectors();
      if(!workspace||selectors.readOnly||!workspace.actions.can_preview||
          workspace.active_job) return Promise.resolve(null);
      return mutations.run('preview',function(){
        if(!pendingKeys.preview){
          pendingKeys.preview=modules.api.createIdempotencyKey('d5-preview');
        }
        store.patchUi({busyAction:'preview',error:'',toast:'正在提交预览任务…'});
        return api.preview({
          project_id:workspace.project_id,
          revision:workspace.revision,
          assembly_revision:workspace.assembly_revision
        },pendingKeys.preview).then(function(){
          pendingKeys.preview='';
          store.patchUi({toast:'预览任务已提交，可安全离开页面。'});
          return reload();
        }).catch(function(error){
          store.patchUi({
            busyAction:'',error:text(error.message||error),toast:''
          });
          throw error;
        });
      });
    }
    function exportFinal(){
      var current=state(),workspace=current.workspace,selectors=store.selectors();
      if(!workspace||selectors.readOnly||!workspace.actions.can_export||
          workspace.active_job) return Promise.resolve(null);
      var previews=workspace.versions.filter(function(item){
        return item.kind==='preview'&&item.status==='succeeded';
      }).sort(function(a,b){ return b.version-a.version; });
      if(!previews.length) return Promise.resolve(null);
      var request={
        project_id:workspace.project_id,
        revision:workspace.revision,
        assembly_revision:workspace.assembly_revision,
        preview_version:previews[0].version,
        cover_time_ms:coverTime()
      };
      return mutations.run('export',function(){
        store.patchUi({busyAction:'export',error:'',toast:'正在查询实时报价…'});
        return api.quoteFinal(request).then(function(quote){
          if(quote&&quote.can_submit===false){
            throw new Error(quote.message||quote.reason||'当前余额或预算不足');
          }
          return Promise.resolve(confirmHook(
            '确认导出 1080p 正式成片？将扣除 '+
            number(quote&&quote.total_cost,0)+' 点。'
          )).then(function(confirmed){
            if(!confirmed){
              store.patchUi({busyAction:'',toast:''});return null;
            }
            if(!pendingKeys.export){
              pendingKeys.export=modules.api.createIdempotencyKey('d5-export');
            }
            request.quote_token=quote.quote_token;
            store.patchUi({toast:'正在提交正式导出任务…'});
            return api.exportFinal(request,pendingKeys.export);
          });
        }).then(function(result){
          if(!result) return null;
          pendingKeys.export='';
          store.patchUi({toast:'正式导出已提交，可安全离开页面。'});
          return reload();
        }).catch(function(error){
          store.patchUi({
            busyAction:'',error:text(error.message||error),toast:''
          });
          throw error;
        });
      });
    }
    function submitCompletion(){
      var current=state(),workspace=current.workspace,selectors=store.selectors();
      var readiness=workspace&&workspace.completion;
      if(!workspace||selectors.readOnly||!readiness||!readiness.ready||
          !readiness.feature_enabled||current.ui.completionAcknowledged!==true||
          workspace.active_job) return Promise.resolve(null);
      return mutations.run('completion',function(){
        if(!pendingKeys.completion){
          pendingKeys.completion=modules.api.createIdempotencyKey(
            'd6-completion'
          );
        }
        store.patchUi({
          busyAction:'completion',error:'',toast:'正在确认交付…'
        });
        return api.confirmCompletion(
          modules.completion.request(readiness),
          pendingKeys.completion
        ).then(function(result){
          if(!result) return null;
          store.patchUi({
            completionDialog:false,
            completionAcknowledged:false,
            toast:'项目已完成并进入永久只读交付状态。'
          });
          return reload();
        }).catch(function(error){
          if({
            revision_conflict:true,delivery_changed:true,asset_changed:true
          }[error&&error.code]){
            pendingKeys.completion='';
            store.patchUi({
              completionDialog:false,completionAcknowledged:false
            });
          }
          store.patchUi({
            busyAction:'',error:text(error.message||error),toast:''
          });
          throw error;
        });
      });
    }
    function waitPlaybackJob(jobId,attempt){
      if(destroyed) return Promise.resolve(null);
      return api.playbackJob(options.projectId,jobId).then(function(job){
        if(job.status==='succeeded'||job.status==='failed') return reload();
        if(attempt>=120) throw new Error('播放包任务轮询超时，请稍后刷新');
        return new Promise(function(resolve){
          setTimeout(resolve,1000);
        }).then(function(){ return waitPlaybackJob(jobId,attempt+1); });
      });
    }
    function createPlayback(){
      var current=state(),workspace=current.workspace;
      var selected=store.selectors().version;
      if(!workspace||!selected||selected.kind!=='preview'||
          selected.status!=='succeeded') return Promise.resolve(null);
      if(!pendingKeys.playback){
        pendingKeys.playback=modules.api.createIdempotencyKey('pr-d-remux');
      }
      store.patchUi({
        busyAction:'playback',error:'',
        toast:'正在创建可切换字幕播放包（0 点）…'
      });
      return api.remux({
        project_id:workspace.project_id,
        source_version_id:selected.id
      },pendingKeys.playback).then(function(result){
        if(result.status==='succeeded'){
          pendingKeys.playback='';return reload();
        }
        return waitPlaybackJob(result.job_id,0).then(function(value){
          pendingKeys.playback='';
          store.patchUi({toast:'播放包已生成，可独立开关字幕。'});
          return value;
        });
      }).catch(function(error){
        store.patchUi({
          busyAction:'',error:text(error.message||error),toast:''
        });
        throw error;
      });
    }
    function retryMedia(){
      mediaError=null;store.patchUi({mediaError:null,error:''});
      return reload().then(function(){
        var selectors=store.selectors();
        return playerController?
          playerController.retry(selectors.version):null;
      });
    }
    function closeDrawers(){
      store.patchUi({leftOpen:false,rightOpen:false});
    }
    function onClick(event){
      var target=findActionTarget(event&&event.target,host);
      if(!target) return;
      var action=target.getAttribute('data-action');
      if(action!=='reload') captureSoundConfigDraft();
      if(action==='reload') reload().catch(function(){});
      else if(action==='select-shot'){
        store.selectShot(target.getAttribute('data-shot-id'));persistSelection();
      }else if(action==='select-version'){
        mediaError=null;
        store.selectVersion(target.getAttribute('data-version-id'));persistSelection();
      }else if(action==='return-current'){
        mediaError=null;store.selectVersion('');persistSelection();
      }else if(action==='filter-shots'){
        store.setFilter(target.getAttribute('data-filter'));persistSelection();
      }else if(action==='toggle-left'){
        var current=state();store.patchUi({leftOpen:!current.ui.leftOpen,rightOpen:false});
      }else if(action==='toggle-right'){
        var next=state();store.patchUi({rightOpen:!next.ui.rightOpen,leftOpen:false});
      }else if(action==='close-drawers') closeDrawers();
      else if(action==='refresh-audio-assets') refreshAudioAssets();
      else if(action==='add-sound-cue') addSoundCue();
      else if(action==='remove-sound-cue'){
        removeSoundCue(Number(target.getAttribute('data-cue-index')));
      }
      else if(action==='save-sound-config') saveSoundConfig().catch(function(){});
      else if(action==='analyze-ai-sound') analyzeAiSound().catch(function(){});
      else if(action==='set-ai-suggestion'){
        saveAiSuggestion(target).catch(function(){});
      }
      else if(action==='generate-ai-sound') generateAiSound().catch(function(){});
      else if(action==='apply-ai-sound') applyAiSound().catch(function(){});
      else if(action==='generate-preview') preview().catch(function(){});
      else if(action==='export-final') exportFinal().catch(function(){});
      else if(action==='open-completion') store.patchUi({
        completionDialog:true,completionAcknowledged:false
      });
      else if(action==='cancel-completion') store.patchUi({
        completionDialog:false,completionAcknowledged:false
      });
      else if(action==='toggle-completion-ack'){
        var completionState=state();
        store.patchUi({
          completionAcknowledged:
            completionState.ui.completionAcknowledged!==true
        });
      }else if(action==='submit-completion'){
        submitCompletion().catch(function(){});
      }
      else if(action==='create-playback'){
        createPlayback().catch(function(){});
      }
      else if(action==='toggle-subtitles'&&playerController){
        var visible=playerController.toggleSubtitles();
        target.textContent='字幕：'+(visible?'开':'关');
      }
      else if(action==='retry-media') retryMedia().catch(function(){});
    }
    function onChange(event){
      if(isSoundConfigTarget(event&&event.target)){
        captureSoundConfigDraft();
      }
    }
    function onKeyDown(event){
      if(event&&event.key==='Escape'){
        closeDrawers();
        store.patchUi({
          completionDialog:false,completionAcknowledged:false
        });
      }
    }
    if(host&&typeof host.addEventListener==='function'){
      host.addEventListener('click',onClick);
      host.addEventListener('change',onChange);
      host.addEventListener('keydown',onKeyDown);
    }
    restoreSelection();
    unsubscribe=store.subscribe(queueRender);
    render();
    var ready=reload().then(function(result){
      return Promise.all([
        refreshAudioAssets(),loadSoundDesign().catch(function(){ return null; })
      ]).then(function(){ return result; });
    });
    return {
      projectId:options.projectId,
      ready:ready,
      render:render,
      reload:reload,
      getState:function(){ return state(); },
      destroy:function(){
        if(destroyed) return;
        destroyed=true;loadGeneration+=1;
        if(host&&typeof host.removeEventListener==='function'){
          host.removeEventListener('click',onClick);
          host.removeEventListener('change',onChange);
          host.removeEventListener('keydown',onKeyDown);
        }
        if(unsubscribe) unsubscribe();
        stopSoundPolling();
        poller.destroy();mutations.destroy();api.destroy();
        if(playerController) playerController.destroy();
        store.destroy();host=null;
      }
    };
  }
  return {
    renderWorkspace:renderWorkspace,
    playerVersion:playerVersion,
    finalAssetHref:finalAssetHref,
    createWorkspace:createWorkspace,
    formatTime:formatTime,
    formatDuration:formatDuration
  };
});
