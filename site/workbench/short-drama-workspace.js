(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root) root.HQShortDramaWorkspace=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function text(value){return String(value==null?'':value);}
  function escapeHtml(value){return text(value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function key(prefix){
    if(typeof crypto!=='undefined'&&crypto.randomUUID)return prefix+'-'+crypto.randomUUID();
    return prefix+'-'+Date.now()+'-'+Math.random().toString(16).slice(2);
  }
  function avatarCreateUrl(){
    return '/workbench/video.html?function=cinematic&action=create-avatar';
  }
  function createClient(fetchImpl){
    fetchImpl=fetchImpl||(typeof fetch==='function'?fetch.bind(globalThis):null);
    if(!fetchImpl)throw new Error('fetch unavailable');
    function request(path,options){
      options=options||{};
      var headers=Object.assign({'Accept':'application/json','Authorization':'Bearer __cookie__'},options.headers||{});
      var body=options.body;
      if(body!==undefined){headers['Content-Type']='application/json';body=JSON.stringify(body);}
      return fetchImpl(path,{method:options.method||'GET',credentials:'same-origin',cache:'no-store',headers:headers,body:body})
        .then(function(response){return response.text().then(function(raw){
          var data={};try{data=raw?JSON.parse(raw):{};}catch(ignore){data={detail:raw};}
          if(!response.ok){var error=new Error(data.detail||('HTTP '+response.status));error.status=response.status;error.code=data.code;throw error;}
          return data;
        });});
    }
    function mutate(path,payload,prefix){return request(path,{method:'POST',headers:{'Idempotency-Key':key(prefix)},body:payload});}
    return {
      workspace:function(id){return request('/api/gen/short-drama/conversation?project_id='+encodeURIComponent(id));},
      message:function(payload){return mutate('/api/gen/short-drama/conversation/messages',payload,'message');},
      generate:function(payload){return mutate('/api/gen/short-drama/conversation/script/generate',payload,'generate');},
      updateShot:function(payload){return mutate('/api/gen/short-drama/conversation/script/shot/update',payload,'shot-update');},
      regenerateShot:function(payload){return mutate('/api/gen/short-drama/conversation/script/shot/regenerate',payload,'shot-regenerate');},
      setShotLock:function(payload){return mutate('/api/gen/short-drama/conversation/script/shot/lock',payload,'shot-lock');},
      restore:function(payload){return mutate('/api/gen/short-drama/conversation/script/restore',payload,'restore');},
      lock:function(payload){return mutate('/api/gen/short-drama/conversation/script/lock',payload,'lock');},
      characterStudio:function(id){return request('/api/gen/short-drama/character-studio?project_id='+encodeURIComponent(id));},
      saveCharacterProfile:function(payload){return mutate('/api/gen/short-drama/character-studio/profile',payload,'character-profile');},
      bindCharacterAvatar:function(payload){return mutate('/api/gen/short-drama/character-studio/bind-avatar',payload,'character-avatar');},
      generateCharacterImage:function(payload){return mutate('/api/gen/short-drama/generate-character-reference',payload,'character-image');},
      preflight:function(id){return request('/api/gen/short-drama/preflight?project_id='+encodeURIComponent(id));},
      prepare:function(payload){return mutate('/api/gen/short-drama/preflight/generate',payload,'preflight');},
      confirmPlan:function(payload){return mutate('/api/gen/short-drama/preflight/confirm',payload,'confirm-plan');},
      autodraft:function(id){return request('/api/gen/short-drama/autodraft?project_id='+encodeURIComponent(id));},
      providerPreflight:function(payload){return mutate('/api/gen/short-drama/autodraft/provider-preflight',payload,'provider-preflight');},
      providerQuote:function(payload){return mutate('/api/gen/short-drama/autodraft/provider-quote',payload,'provider-quote');},
      startProviderJob:function(payload){return mutate('/api/gen/short-drama/autodraft/provider-jobs',payload,'provider-shot');},
      providerJob:function(projectId,jobId){return request('/api/gen/short-drama/autodraft/provider-jobs/'+encodeURIComponent(jobId)+'?project_id='+encodeURIComponent(projectId));},
      startDraft:function(payload){return mutate('/api/gen/short-drama/autodraft/jobs',payload,'autodraft');},
      draftJob:function(projectId,jobId){return request('/api/gen/short-drama/autodraft/jobs/'+encodeURIComponent(jobId)+'?project_id='+encodeURIComponent(projectId));},
      refinement:function(id){return request('/api/gen/short-drama/refinement?project_id='+encodeURIComponent(id));},
      previewRefinement:function(payload){return mutate('/api/gen/short-drama/refinement/changes/preview',payload,'refinement-preview');},
      refineShot:function(payload){return mutate('/api/gen/short-drama/refinement/jobs',payload,'refinement-shot');},
      markRefinementIssue:function(payload){return mutate('/api/gen/short-drama/refinement/issues',payload,'refinement-issue');},
      refinementJob:function(projectId,jobId){return request('/api/gen/short-drama/refinement/jobs/'+encodeURIComponent(jobId)+'?project_id='+encodeURIComponent(projectId));},
      confirmRefinement:function(payload){return mutate('/api/gen/short-drama/refinement/confirm',payload,'refinement-confirm');},
      restoreRefinement:function(payload){return mutate('/api/gen/short-drama/refinement/restore',payload,'refinement-restore');},
      deliveryQuote:function(payload){return mutate('/api/gen/short-drama/delivery/quote',payload,'delivery-quote');},
      startDelivery:function(payload){return mutate('/api/gen/short-drama/delivery/jobs',payload,'delivery');},
      deliveryJob:function(projectId,jobId){return request('/api/gen/short-drama/delivery/jobs/'+encodeURIComponent(jobId)+'?project_id='+encodeURIComponent(projectId));},
      createProject:function(payload){return mutate('/api/gen/short-drama/projects',payload,'project-clone');}
    };
  }
  function cloneProjectPayload(project){
    project=project||{};
    return {
      title:(text(project.title).trim()||'短剧项目')+' · 新版本',
      synopsis:text(project.synopsis).trim(),
      ratio:text(project.ratio).trim()||'16:9',
      target_duration:Number(project.target_duration||30),
      shot_count:Number(project.shot_count||6),
      visual_style:text(project.visual_style).trim()||'电影感写实',
      target_platform:text(project.target_platform).trim()||'抖音',
      point_budget:Number(project.point_budget||0)
    };
  }
  function normalize(raw){
    raw=raw||{};
    return {
      project:raw.project||{},
      conversation:raw.conversation||{state:'idea_intake',revision:1,understanding:{}},
      messages:Array.isArray(raw.messages)?raw.messages:[],
      current_script:raw.current_script||null,
      versions:Array.isArray(raw.versions)?raw.versions:[],
      script_import:raw.script_import||null,
      permissions:raw.permissions||{can_edit:false},
      billing:raw.billing||{cost:0,charged:false}
    };
  }
  function quickReplyPresentation(value,index){
    value=text(value).trim();
    var normalized=value.replace(/[，。！？\s]/g,'');
    var result={icon:'💬',title:value,description:'把这个选择告诉创作助手，继续完善故事。',primary:index===0};
    if(/确认|采用|就这个|锁定/.test(normalized)){
      result.icon='✓';result.description='确认当前创作方向，进入后续剧本生成与制作准备。';
    }else if(/推荐|方向|方案/.test(normalized)){
      result.icon='✨';result.description='根据当前想法，整理几套不同的故事方向供你比较。';
    }else if(/悬疑|反转|推理|线索/.test(normalized)){
      result.icon='🔍';result.description='围绕谜题、线索和反转，继续补齐人物与冲突。';
    }else if(/温暖|治愈|情感|成长/.test(normalized)){
      result.icon='🌤️';result.description='围绕人物关系、情绪变化和成长落点继续创作。';
    }else if(/补充|调整|修改|继续/.test(normalized)){
      result.icon='✎';result.description='暂不确认，继续补充人物、冲突、情绪或结局要求。';
    }else if(/结局|收尾/.test(normalized)){
      result.icon='🎬';result.description='明确故事最终落点，让前面的冲突能够自然收束。';
    }else if(/人物|角色/.test(normalized)){
      result.icon='👤';result.description='继续完善主角关系、性格动机和关键选择。';
    }
    return result;
  }
  function messageHtml(item){
    var metadata=item&&item.metadata||{},recommendations=Array.isArray(metadata.recommendations)?metadata.recommendations:[],quickReplies=Array.isArray(metadata.quick_replies)?metadata.quick_replies:[];
    var recommendationTitles=recommendations.map(function(item){return text(item.title);});
    quickReplies=quickReplies.filter(function(value){return recommendationTitles.indexOf(text(value))<0;});
    var cards=recommendations.length?'<div class="sd-advisor-recommendations">'+recommendations.map(function(option){
      return '<button type="button" data-action="quick-reply" data-message="'+escapeHtml(option.title||'')+'"><span>'+escapeHtml(option.title||'创作方案')+'</span><b>'+escapeHtml(option.hook||'')+'</b><small>'+escapeHtml(option.summary||'')+'</small></button>';
    }).join('')+'</div>':'';
    var replies=quickReplies.length?'<section class="sd-advisor-actions" aria-label="创作助手下一步建议"><header><b>你可以这样继续</b><small>选择一项，助手会接着理解你的想法</small></header><div class="sd-advisor-quick">'+quickReplies.map(function(value,index){
      var option=quickReplyPresentation(value,index);
      return '<button type="button" class="'+(option.primary?'primary':'')+'" data-action="quick-reply" data-message="'+escapeHtml(value)+'"><span class="sd-advisor-quick-icon" aria-hidden="true">'+escapeHtml(option.icon)+'</span><span class="sd-advisor-quick-copy"><b>'+escapeHtml(option.title)+'</b><small>'+escapeHtml(option.description)+'</small></span><span class="sd-advisor-quick-arrow" aria-hidden="true">›</span></button>';
    }).join('')+'</div>'+(quickReplies.length>1?'<button type="button" class="sd-advisor-refresh" data-action="quick-reply" data-message="请结合我们已经聊过的内容，再推荐三个不同方向">↻ 换一批建议</button>':'')+'</section>':'';
    return '<article class="sd-chat-message '+escapeHtml(item.role||'assistant')+'"><b>'+
      (item.role==='user'?'你':'创作助手')+'</b><p>'+escapeHtml(item.content)+'</p>'+cards+replies+'</article>';
  }
  function importContractHtml(contract){
    contract=contract||{};
    if(!contract.source_hash)return '';
    var mode=contract.import_mode==='optimize'?'AI 协助优化':'尊重原稿';
    var characters=(contract.characters||[]).map(escapeHtml).join('、')||'待确认';
    var points=(contract.plot_points||[]).map(function(item){return '<li><b>'+escapeHtml({start:'开场',middle:'中段',end:'结尾'}[item.position]||item.position||'剧情节点')+'</b><span>'+escapeHtml(item.excerpt||'')+'</span></li>';}).join('');
    var dialogues=(contract.key_dialogues||[]).map(function(item){return '<li><b>'+escapeHtml(item.speaker||'人物')+'</b><span>'+escapeHtml(item.text||'')+'</span></li>';}).join('');
    var changes=(contract.proposed_changes||[]).map(function(item){var status=item.status||'pending';var statusText=status==='confirmed'?'已确认':(status==='denied'?'已排除':'待确认');return '<li class="'+escapeHtml(status)+'"><b>'+escapeHtml(item.label||'优化项')+'</b><span>'+escapeHtml(item.summary||'')+'</span><em>'+statusText+'</em></li>';}).join('');
    var preservations=(contract.required_preservations||[]).map(function(item){return '<li class="confirmed"><b>'+escapeHtml(item.kind==='dialogue'?'必保对白':'必保内容')+'</b><span>'+escapeHtml(item.source||'')+'</span><em>原稿位置 '+Number(item.source_offset||0)+'</em></li>';}).join('');
    return '<dt class="sd-import-contract-label">原稿理解快照</dt><dd class="sd-import-contract"><header><b>完整原稿处理契约</b><em>'+escapeHtml(mode)+'</em></header><p><span>契约版本</span><b>第 '+Number(contract.revision||1)+' 版</b></p><p><span>契约哈希</span><code>'+escapeHtml(contract.contract_hash||'待生成')+'</code></p><p><span>原稿哈希</span><code>'+escapeHtml(contract.source_hash)+'</code></p><p><span>识别人物</span><b>'+characters+'</b></p>'+(points?'<h4>首 / 中 / 尾剧情节点</h4><ul>'+points+'</ul>':'')+(dialogues?'<h4>关键对白</h4><ul>'+dialogues+'</ul>':'')+(preservations?'<h4>用户追加的必须保留内容</h4><ul>'+preservations+'</ul>':'')+(changes?'<h4>重要优化边界</h4><ul>'+changes+'</ul>':'')+'</dd>';
  }
  function shotMediaIndex(autodraft){
    autodraft=autodraft||{};
    var index={};
    (autodraft.provider_versions||[]).forEach(function(item){
      var shotKey=text(item&&item.shot_key);
      if(!shotKey)return;
      if(!index[shotKey])index[shotKey]={versions:[],job:null};
      index[shotKey].versions.push(item);
    });
    var job=autodraft.provider_job,jobShotKey=text(job&&job.shot_key);
    if(jobShotKey){
      if(!index[jobShotKey])index[jobShotKey]={versions:[],job:null};
      index[jobShotKey].job=job;
    }
    return index;
  }
  function shotMediaHtml(shot,media){
    media=media||{versions:[],job:null};
    var versions=media.versions||[],current=versions[0]||null,job=media.job||null;
    var active=job&&['billing','queued','submitting','running'].indexOf(job.status)>=0;
    var failed=job&&['failed','submit_unknown','canceled'].indexOf(job.status)>=0;
    var statusHtml='';
    if(active){
      statusHtml='<div class="sd-shot-media-status working"><b>正在生成视频 · '+Number(job.progress||0)+'%</b><span>'+escapeHtml(job.provider||'Provider')+' · 任务 '+escapeHtml(job.id||'')+'</span><div class="sd-progress"><i style="width:'+Math.max(0,Math.min(100,Number(job.progress||0)))+'%"></i></div></div>';
    }else if(failed&&!current){
      statusHtml='<div class="sd-shot-media-status failed"><b>本镜头生成失败</b><span>'+escapeHtml(job.error&&job.error.detail||'请在右侧查看失败原因后重试')+'</span></div>';
    }else if(!current){
      statusHtml='<div class="sd-shot-media-status empty"><b>尚未生成镜头视频</b><span>在右侧选择当前镜头，完成预检、报价和生成。</span></div>';
    }
    if(!current)return '<section class="sd-shot-media">'+statusHtml+'</section>';
    var history=versions.length>1?'<details class="sd-shot-media-history"><summary>历史视频版本（'+versions.length+'）</summary><div>'+versions.map(function(item,index){return '<a href="'+escapeHtml(item.url||'')+'" target="_blank" rel="noopener"><b>v'+Number(item.version||0)+(index===0?' · 当前':'')+'</b><span>'+escapeHtml(item.provider||'Provider')+' · '+escapeHtml(item.created_at||'')+'</span></a>';}).join('')+'</div></details>':'';
    return '<section class="sd-shot-media ready"><header><div><b>镜头视频 · v'+Number(current.version||0)+'</b><span>'+escapeHtml(current.provider||'Provider')+' 已生成</span></div><a href="'+escapeHtml(current.url||'')+'" target="_blank" rel="noopener">单独打开</a></header><video controls preload="metadata" src="'+escapeHtml(current.url||'')+'"></video>'+statusHtml+history+'</section>';
  }
  function scriptHtml(version,canEdit,autodraft){
    if(!version||!version.script)return '<div class="sd-script-empty"><strong>还没有剧本</strong><p>先在左侧补充创作方向，然后生成第一版结构化剧本。</p></div>';
    var script=version.script,overview=script.overview||{},mediaByShot=shotMediaIndex(autodraft);
    var legacy=version.model_version==='conversation-script-v2'?'<div class="sd-preflight-stale">该版本由旧通用模板生成，镜头可能与故事摘要不一致。请基于当前项目创建新版本后重新生成剧本。</div>':'';
    var dialogueById={};(script.dialogue_lines||[]).forEach(function(line){dialogueById[text(line.id)]=line;});
    var quality=script.quality_gate||{},qualityStatus=quality.status||'unknown';
    var qualityMetrics=quality.metrics||{},shotCount=Number(qualityMetrics.shot_count);
    if(!shotCount)shotCount=(script.shots||[]).length;
    var providerReady=Number(qualityMetrics.provider_ready_shots);
    if(!providerReady)providerReady=(script.shots||[]).filter(function(shot){return !!text(shot.provider_prompt);}).length;
    var qualityHtml=quality.status?'<section class="sd-storyboard-quality '+escapeHtml(qualityStatus)+'"><header><div><span>分镜质量门禁</span><b>'+escapeHtml(qualityStatus==='pass'?'可以锁定':qualityStatus==='warning'?'建议人工复核':'存在阻塞项')+'</b></div><em>'+providerReady+' / '+shotCount+' 镜可提交 Provider</em></header>'+((quality.blockers||[]).concat(quality.warnings||[]).map(function(item){return '<p>'+escapeHtml(item.shot_key?item.shot_key+' · ':'')+escapeHtml(item.message||item.code||'待检查')+'</p>';}).join('')||'<p>镜头时长、对白、剧情推进和 Provider 提示词检查通过。</p>')+'</section>':'';
    return '<header class="sd-script-head"><div><span>结构化剧本 · v'+Number(version.version||0)+'</span><h2>'+escapeHtml(overview.title||'未命名剧本')+'</h2><p>'+escapeHtml(overview.logline||'')+'</p></div><em>'+escapeHtml(version.status||'draft')+'</em></header>'+legacy+
      qualityHtml+
      '<section class="sd-script-block"><h3>角色</h3><div class="sd-character-list">'+(script.characters||[]).map(function(item){return '<article><b>'+escapeHtml(item.name)+'</b><span>'+escapeHtml(item.identity)+'</span><p>'+escapeHtml(item.personality)+'</p></article>';}).join('')+'</div></section>'+
      '<section class="sd-script-block"><h3>三幕结构</h3>'+(script.acts||[]).map(function(item){return '<article class="sd-act"><b>第'+Number(item.act)+'幕 · '+escapeHtml(item.name)+'</b><p>'+escapeHtml(item.summary)+'</p></article>';}).join('')+'</section>'+
      '<section class="sd-script-block"><header class="sd-block-heading"><div><h3>镜头与台词</h3><p>每个镜头的视频、任务状态和历史版本均与对应分镜绑定。</p></div></header>'+(script.shots||[]).map(function(shot,index){var line=dialogueById[text((shot.dialogue_line_ids||[])[0])]||{},lineLabel=line.kind==='silence'?'静默表演':line.kind==='on_screen_text'?'画面文字：'+text(line.text):(line.speaker||'旁白')+'：'+text(line.text);return '<article class="sd-shot '+(shot.locked?'locked':'')+'" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"><header><span>#'+Number(shot.sort_order||index+1)+' · '+Number(shot.duration_seconds||0)+'s · '+escapeHtml(shot.beat||'')+'</span><div>'+(shot.locked?'<em>已锁定</em>':'')+(canEdit?'<button type="button" data-action="edit-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'">编辑</button><button type="button" data-action="regenerate-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"'+(shot.locked?' disabled':'')+'>重生成</button><button type="button" data-action="toggle-shot-lock" data-shot-key="'+escapeHtml(shot.shot_key||'')+'" data-locked="'+(shot.locked?'1':'0')+'">'+(shot.locked?'解锁':'锁定')+'</button>':'')+'</div></header><small>'+escapeHtml(shot.purpose||'剧情推进')+'</small><b>'+escapeHtml(shot.visual)+'</b><p>'+escapeHtml(lineLabel)+'</p>'+shotMediaHtml(shot,mediaByShot[text(shot.shot_key)])+'<details><summary>查看镜头执行信息</summary><dl><dt>场景</dt><dd>'+escapeHtml(shot.scene||'')+'</dd><dt>机位</dt><dd>'+escapeHtml(shot.camera||'')+'</dd><dt>连续性</dt><dd>'+escapeHtml(shot.continuity||'')+'</dd><dt>Provider 提示词</dt><dd>'+escapeHtml(shot.provider_prompt||'')+'</dd></dl></details></article>';}).join('')+'</section>';
  }
  function versionHtml(item,currentId){
    return '<button type="button" class="sd-version '+(item.id===currentId?'current':'')+'" data-version-id="'+escapeHtml(item.id)+'"><span>v'+Number(item.version)+'</span><b>'+escapeHtml(item.change_summary||'剧本版本')+'</b><em>'+escapeHtml(item.status)+'</em></button>';
  }
  function preflightHtml(conversation,preflight,canEdit){
    var locked=conversation.state==='script_locked';
    if(!locked){
      var understanding=conversation.understanding||{},hasScript=!!conversation.current_version_id,confirmed=!!understanding.direction_confirmed;
      var questions=(understanding.open_questions||[]).map(function(value){return '<li>'+escapeHtml(value)+'</li>';}).join('');
      var gate=hasScript||confirmed?
        '<div class="sd-direction-gate ready"><b>'+(hasScript?'当前剧本可继续修改':'创作方向已确认')+'</b><p>'+(hasScript?'本次要求将生成新的剧本版本。':'现在可以生成首版结构化剧本。')+'</p></div>':
        '<div class="sd-direction-gate pending"><b>'+(understanding.confirmation_invalidated?'修改后需要重新确认':'请先确认创作方向')+'</b><p>助手会先理解想法、给出建议并与你确认，确认后才生成首版剧本。</p>'+(questions?'<ul>'+questions+'</ul>':'')+'</div>';
      return '<section><h2>下一步</h2>'+gate+'<textarea id="sdInstruction" maxlength="2000" placeholder="可选：补充本次生成或修改要求"></textarea><button data-action="generate" type="button">生成 / 修改剧本</button><button data-action="lock" class="secondary" type="button">锁定当前剧本</button><p class="sd-free">本阶段不扣点</p></section>';
    }
    preflight=preflight||{};
    var current=preflight.current_plan,plan=current&&current.plan;
    if(!plan)return '<section class="sd-preflight"><span class="sd-stage-label">PR-3 · 制作准备</span><h2>制作前自动体检</h2><p>检查时长、素材、复杂度和预算，并把锁定剧本转换为可执行制作计划。</p><label>制作路线<select id="sdQualityRoute"><option value="quick_draft">快速草稿 · 720p</option><option value="formal">正式制作 · 1080p</option></select></label><button data-action="prepare" type="button"'+(canEdit?'':' disabled')+'>生成制作方案</button><p class="sd-free">只估算，不扣点</p></section>';
    var confirmed=current.status==='confirmed',stale=!!preflight.stale;
    var checks=(plan.checks||[]).map(function(item){return '<article class="sd-check '+escapeHtml(item.status)+'"><span>'+escapeHtml(item.status==='pass'?'通过':item.status==='blocker'?'阻塞':'需确认')+'</span><b>'+escapeHtml(item.label)+'</b><p>'+escapeHtml(item.summary)+'</p>'+(item.suggestion?'<small>'+escapeHtml(item.suggestion)+'</small>':'')+'</article>';}).join('');
    var routeOptions=(plan.route_options||[]).map(function(item){return '<option value="'+escapeHtml(item.key)+'"'+(item.key===plan.quality_route?' selected':'')+'>'+escapeHtml(item.name)+' · '+Number(item.estimated_points)+' 点估算</option>';}).join('');
    var acceptance=(plan.required_acceptance||[]).length?'<label class="sd-accept"><input id="sdAcceptAdjustments" type="checkbox"> 我已了解并接受 '+Number(plan.required_acceptance.length)+' 项系统建议</label>':'';
    return '<section class="sd-preflight"><span class="sd-stage-label">PR-3 · 制作准备</span><h2>'+(confirmed?'制作方案已确认':'制作方案 v'+Number(current.version)+' 待确认')+'</h2>'+(stale?'<div class="sd-preflight-stale">剧本或项目规格已变化，请重新体检后再确认。</div>':'')+'<div class="sd-estimate"><strong>'+Number(plan.estimate&&plan.estimate.points||0)+' 点</strong><span>'+escapeHtml(plan.estimate&&plan.estimate.resolution||'')+' · 约 '+Number(plan.estimate&&plan.estimate.minutes||0)+' 分钟</span></div><label>制作路线<select id="sdQualityRoute"'+(confirmed||!canEdit?' disabled':'')+'>'+routeOptions+'</select></label><div class="sd-checks">'+checks+'</div><p class="sd-plan-meta">'+Number(plan.duration&&plan.duration.shots&&plan.duration.shots.length||0)+' 镜 · '+Number(plan.duration&&plan.duration.target_ms||0)/1000+' 秒 · '+Number((plan.assets||[]).length)+' 项推荐素材</p>'+(confirmed?'<div class="sd-confirmed">已锁定制作方案 v'+Number(current.version)+'，下一阶段可据此生成自动草稿。</div>':acceptance+'<button data-action="confirm-plan" class="secondary" type="button"'+(plan.ready&&!stale&&canEdit?'':' disabled')+'>确认制作方案</button><button data-action="prepare" type="button"'+(canEdit?'':' disabled')+'>按当前路线重新体检</button>')+'<p class="sd-free">当前仅为估算，本阶段不扣点</p></section>';
  }
  function autodraftActionsHtml(autodraft,canEdit){
    autodraft=autodraft||{};
    var job=autodraft.current_job,version=autodraft.current_version,billing=autodraft.billing||{},production=autodraft.production||{};
    if(version){
      var issues=(version.manifest&&version.manifest.issues)||[];
      if(version.is_demo)return '<section class="sd-autodraft-actions"><span class="sd-stage-label">PR-4 · 演示模式</span><h2>演示视频 v'+Number(version.version||0)+'</h2><div class="sd-preflight-stale">这只是界面联调用的固定示例，不是根据当前剧本生成的短剧，不能作为项目成片交付。</div></section>';
      return '<section class="sd-autodraft-actions"><span class="sd-stage-label">PR-4 · 自动草稿</span><h2>可播放草稿 v'+Number(version.version||0)+'</h2><div class="sd-draft-ready">草稿已交付'+(version.status==='degraded'?'，含 '+issues.length+' 个待优化镜头':'')+'</div><p>下一阶段可继续修改问题镜头并生成新版本。</p></section>';
    }
    if(job&&(['queued','running'].indexOf(job.status)>=0)){
      return '<section class="sd-autodraft-actions"><span class="sd-stage-label">PR-4 · 自动草稿</span><h2>后台正在制作</h2><div class="sd-progress"><i style="width:'+Math.max(0,Math.min(100,Number(job.progress||0)))+'%"></i></div><strong>'+Number(job.progress||0)+'%</strong><p>'+escapeHtml(job.phase||'queued')+' · 可离开页面，任务会继续执行。</p></section>';
    }
    var plan=autodraft.confirmed_plan;
    if(!plan)return '';
    if(production.ready===false){
      var poc=autodraft.provider_poc||{},preview=autodraft.provider_preview||null,quote=autodraft.provider_quote||null,shotJob=autodraft.provider_job||null,shots=poc.shots||[],characters=poc.characters||[],provider=production.provider||{},providerName=provider.selected||poc.provider||'heygen_cinematic',providerState=provider.configured?'配置已就绪':'尚未配置';
      var boundCharacters=characters.filter(function(item){return item.binding_ready;});
      var missingCharacters=characters.filter(function(item){return !item.binding_ready;});
      var allRolesBound=characters.length>0&&boundCharacters.length===characters.length;
      var firstShot=shots[0]||null;
      var shotOptions=shots.map(function(item){return '<option value="'+escapeHtml(item.shot_key)+'">#'+Number(item.sort_order||0)+' · '+escapeHtml(item.scene||item.shot_key)+' · '+Math.ceil(Number(item.duration_ms||0)/1000)+'s</option>';}).join('');
      var result=preview?'<div class="sd-check '+(preview.ready?'pass':'warning')+'"><b>'+escapeHtml(preview.message||'预检完成')+'</b><p>'+escapeHtml(preview.request&&preview.request.prompt||'')+'</p><small>'+escapeHtml(preview.request&&preview.request.ratio||'')+' · '+escapeHtml(preview.request&&preview.request.resolution||'')+' · '+Number(preview.request&&preview.request.duration_seconds||0)+' 秒<br>'+escapeHtml(preview.next_action||'')+'</small></div>':'';
      var quoteHtml=quote?'<div class="sd-estimate"><strong>'+Number(quote.cost||0)+' 点</strong><span>报价 '+escapeHtml(quote.shot&&quote.shot.shot_key||'')+' · 5 分钟内有效</span></div>':'';
      var providerJobHtml='';
      if(shotJob){
        var jobError=shotJob.error&&shotJob.error.detail||'';
        var jobMessage=jobError||(shotJob.status==='succeeded'?
          '镜头 '+(shotJob.shot_key||'')+' 已由 '+(shotJob.provider||providerName)+' 生成完成':
          '镜头 '+(shotJob.shot_key||'')+' 正在由 '+(shotJob.provider||providerName)+' 处理');
        providerJobHtml='<div class="sd-check '+(shotJob.status==='succeeded'?'pass':(['failed','submit_unknown'].indexOf(shotJob.status)>=0?'warning':''))+'"><b>镜头任务 · '+escapeHtml(shotJob.status||'')+' · '+Number(shotJob.progress||0)+'%</b><p>'+escapeHtml(jobMessage)+'</p><button type="button" class="sd-shot-jump" data-action="jump-to-shot" data-shot-key="'+escapeHtml(shotJob.shot_key||'')+'">在镜头与台词中查看</button></div>';
      }
      var bindingSummary=allRolesBound?
        '<div class="sd-check pass" id="sdProviderBindingStatus"><b>'+boundCharacters.length+'/'+characters.length+' 个角色已锁定，可开始检查镜头</b><p>人物形象统一由左侧角色卡管理，当前镜头会自动使用对应角色的已锁定形象。</p></div>':
        '<div class="sd-check warning" id="sdProviderBindingStatus"><b>角色形象尚未准备完整</b><p>'+(missingCharacters.length?'未绑定：'+escapeHtml(missingCharacters.map(function(item){return item.name||item.character_key;}).join('、'))+'。':'角色资料仍在加载。')+' 请点击左侧角色卡完成形象生成、选择与锁定。</p></div>';
      var active=shotJob&&['billing','queued','submitting','running'].indexOf(shotJob.status)>=0;
      return '<section class="sd-autodraft-actions"><span class="sd-stage-label">PR-4 · Provider 接入</span><h2>单镜头真实生成</h2><div class="sd-preflight-stale">'+escapeHtml(production.message||'当前不能生成与剧本一致的短剧。')+'</div><div class="sd-estimate"><strong>'+escapeHtml(providerName)+'</strong><span>'+escapeHtml(providerState)+'</span></div>'+bindingSummary+'<p>选择镜头后，系统会自动读取该镜头所需角色及其已锁定形象，先免费检查参数，再报价并由你确认扣点。</p><label>当前镜头<select id="sdProviderShot"'+(shots.length&&!active?'':' disabled')+'>'+shotOptions+'</select></label><div class="sd-check" id="sdProviderShotCharacter"><b>正在读取镜头角色</b></div><button data-action="provider-preflight" type="button"'+(canEdit&&firstShot&&firstShot.binding_ready&&!active?'':' disabled')+'>免费检查当前镜头</button>'+result+(preview&&preview.ready&&!quote?'<button data-action="provider-quote" type="button"'+(canEdit&&!active?'':' disabled')+'>获取付费报价</button>':'')+quoteHtml+(quote?'<button data-action="provider-start" type="button"'+(canEdit&&!active?'':' disabled')+'>确认扣 '+Number(quote.cost||0)+' 点并生成</button>':'')+providerJobHtml+'<p class="sd-free">预检和报价不扣点；只有确认生成后扣点，提交前失败自动退回。</p></section>';
    }
    if(production.mode==='demo')return '<section class="sd-autodraft-actions"><span class="sd-stage-label">PR-4 · 演示模式</span><h2>生成界面联调示例</h2><p>该模式只验证任务、轮询和播放器，不会根据剧本生成真实画面。</p><div class="sd-estimate"><strong>0 点</strong><span>固定示例 · 不可交付</span></div><button data-action="start-draft" type="button"'+(canEdit?'':' disabled')+'>生成演示草稿</button></section>';
    var assembling=production.mode==='provider_poc'&&production.assembly&&production.assembly.all_ready;
    return '<section class="sd-autodraft-actions"><span class="sd-stage-label">PR-4 · 合成预览</span><h2>'+(assembling?'全部镜头已完成':'一键生成可播放草稿')+'</h2><p>'+(assembling?'将 '+Number(production.assembly.ready_count||0)+' 个真实 Provider 镜头按剧本顺序合成为 720p 全片预览；不会重复扣除镜头生成费用。':'自动准备素材、画面、配音、字幕与基础口型；个别镜头失败时会安全降级，优先交付完整草稿。')+'</p><div class="sd-estimate"><strong>'+Number(billing.cost||0)+' 点</strong><span>720p · 后台任务</span></div><button data-action="start-draft" type="button"'+(canEdit?'':' disabled')+'>'+(assembling?'合成 720p 预览':'开始自动制作')+'</button><p class="sd-free">'+(billing.mode==='provider_assets_already_charged'?'镜头费用已结算，本次合成不重复扣点':billing.mode==='development_free'?'本地开发模式：不扣点':'提交后扣点；建单失败自动退款')+'</p></section>';
  }
  function draftHtml(autodraft){
    var version=autodraft&&autodraft.current_version;
    if(!version)return '';
    var manifest=version.manifest||{},shots=manifest.shots||[],issues=manifest.issues||[];
    if(version.is_demo)return '<section class="sd-draft"><header><div><span>PR-4 · 演示模式</span><h2>固定界面联调视频</h2><p>该视频与当前剧本无关，仅用于验证播放器，不能作为项目成果。</p></div><em>demo</em></header><video controls preload="metadata" src="'+escapeHtml(version.url||'')+'"></video></section>';
    return '<section class="sd-draft"><header><div><span>PR-4 · 720p 自动草稿</span><h2>可播放草稿 v'+Number(version.version||0)+'</h2><p>'+(version.status==='degraded'?'已安全降级交付，可继续优化问题镜头。':'全部镜头已完成。')+'</p></div><em>'+escapeHtml(version.status||'ready')+'</em></header><video controls preload="metadata" src="'+escapeHtml(version.url||'')+'"></video><div class="sd-draft-summary"><strong>'+shots.length+' 个镜头</strong><strong>'+issues.length+' 个待优化</strong><strong>'+Math.round(Number(manifest.duration_ms||0)/1000)+' 秒</strong></div><h3>镜头状态</h3><div class="sd-draft-shots">'+shots.map(function(shot){return '<article class="'+escapeHtml(shot.status||'ready')+'"><b>#'+Number(shot.sort_order||0)+' · '+escapeHtml(shot.shot_key||'')+'</b><span>'+escapeHtml(shot.status==='degraded'?'安全替代':'已就绪')+'</span><p>'+escapeHtml(shot.issue&&shot.issue.message||'画面、配音和字幕已装配。')+'</p></article>';}).join('')+'</div></section>';
  }
  function refinementHtml(refinement){
    refinement=refinement||{};
    var delivery=refinement.current_delivery,current=refinement.current_refinement;
    if(delivery){
      var snapshot=delivery.snapshot||{};
      var deliverable=snapshot.deliverable===true;
      return '<section class="sd-draft sd-delivery"><header><div><span>PR-5 · '+(deliverable?'正式交付':'开发演示')+'</span><h2>'+(deliverable?'1080p 正式成片':'本地演示预览')+' v'+Number(delivery.version||0)+'</h2><p>'+(deliverable?'交付快照已固化，素材计划、精修版本和输出哈希均可追溯。':'该视频复用现有素材，仅用于本地流程验收，不是 1080p 正式交付文件。')+'</p></div><em>'+(deliverable?'ready':'demo')+'</em></header><video controls preload="metadata" src="'+escapeHtml(delivery.url||'')+'"></video><div class="sd-delivery-proof"><b>'+(deliverable?'不可变交付快照':'不可交付的演示快照')+'</b><span>精修 v'+Number(snapshot.refinement_version||0)+'</span><code>'+escapeHtml(delivery.input_hash||'')+'</code></div></section>';
    }
    if(!current)return '';
    var shots=current.shots||[],issues=current.issues||[];
    return '<section class="sd-draft sd-refinement"><header><div><span>PR-5 · 智能精修</span><h2>精修工作副本 v'+Number(current.version||0)+'</h2><p>'+(issues.length?'逐个修复问题镜头，确认后再生成正式成片。':'问题镜头已处理完，可确认精修版本。')+'</p></div><em>'+escapeHtml(current.status||'draft')+'</em></header><video controls preload="metadata" src="'+escapeHtml(current.url||'')+'"></video><div class="sd-draft-summary"><strong>'+shots.length+' 个镜头</strong><strong>'+issues.length+' 个待处理</strong><strong>'+Number((refinement.refinement_versions||[]).length)+' 个精修版本</strong></div><h3>镜头精修</h3><div class="sd-draft-shots">'+shots.map(function(shot){var degraded=shot.status==='degraded';return '<article class="'+escapeHtml(shot.status||'ready')+'"><b>#'+Number(shot.sort_order||0)+' · '+escapeHtml(shot.shot_key||'')+'</b><span>'+escapeHtml(degraded?'待修复':'已就绪')+'</span><p>'+escapeHtml(shot.issue&&shot.issue.message||'该镜头已通过精修检查。')+'</p>'+(degraded?'<button type="button" data-action="refine-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'">预览并重做这个镜头</button>':'<button type="button" data-action="mark-refinement-issue" data-shot-key="'+escapeHtml(shot.shot_key||'')+'">标记这个镜头有问题</button>')+'</article>';}).join('')+'</div></section>';
  }
  function refinementActionsHtml(refinement,canEdit){
    refinement=refinement||{};
    var current=refinement.current_refinement,refineJob=refinement.current_refinement_job,deliveryJob=refinement.current_delivery_job,billing=refinement.billing||{};
    if(refinement.current_delivery){
      var delivered=refinement.current_delivery.snapshot&&refinement.current_delivery.snapshot.deliverable===true;
      return '<section><span class="sd-stage-label">PR-5 · '+(delivered?'已交付':'开发演示')+'</span><h2>'+(delivered?'正式成片已生成':'演示预览已生成')+'</h2><p class="sd-free">'+(delivered?'交付快照不可修改，可继续查看历史版本。':'仅供本地验收，不扣点、不可作为正式交付文件。')+'</p></section>';
    }
    if(deliveryJob&&['queued','running'].indexOf(deliveryJob.status)>=0){
      var demoJob=billing.mode==='development_free';
      return '<section><span class="sd-stage-label">PR-5 · '+(demoJob?'开发演示':'正式导出')+'</span><h2>'+(demoJob?'正在准备免费演示预览':'正在合成 1080p 成片')+'</h2><div class="sd-progress"><i style="width:'+Number(deliveryJob.progress||0)+'%"></i></div><strong>'+Number(deliveryJob.progress||0)+'%</strong><p>'+escapeHtml(deliveryJob.phase||'queued')+' · 任务可恢复，可离开页面。</p></section>';
    }
    if(refineJob&&['queued','running'].indexOf(refineJob.status)>=0)return '<section><span class="sd-stage-label">PR-5 · 镜头精修</span><h2>正在重做 '+escapeHtml(refineJob.shot_key||'镜头')+'</h2><div class="sd-progress"><i style="width:'+Number(refineJob.progress||0)+'%"></i></div><strong>'+Number(refineJob.progress||0)+'%</strong></section>';
    if(!current)return '';
    var issues=current.issues||[],confirmed=current.status==='confirmed';
    if(confirmed){
      if(billing.delivery_enabled!==true)return '<section><span class="sd-stage-label">PR-5 · 正式交付</span><h2>真实 1080p 交付暂未启用</h2><p>真实渲染执行器尚未接入，系统不会询价、建单或扣点。</p><button type="button" disabled>正式交付不可用</button><p class="sd-free">精修版本已安全保留，执行器启用后可继续。</p></section>';
      var demo=billing.mode==='development_free',localRender=billing.mode==='local_ffmpeg';
      return '<section><span class="sd-stage-label">PR-5 · '+(demo?'开发演示':'正式交付')+'</span><h2>精修版本已确认</h2><p>'+(demo?'生成一个复用现有素材的本地流程预览。':localRender?'使用已锁定的 720p 预览生成本地 1080p 正式成片，并固化不可变交付快照。':'正式导出前会重新报价并校验确认版本。')+'</p><div class="sd-estimate"><strong>'+Number(billing.formal_cost||0)+' 点</strong><span>'+(demo?'源规格 · 不可交付':'1080p · 不可变快照')+'</span></div><button type="button" data-action="start-delivery"'+(canEdit?'':' disabled')+'>'+(demo?'生成免费演示预览':localRender?'生成 1080p 正式成片':'询价并生成正式成片')+'</button><p class="sd-free">'+(demo?'本地开发模式：不扣点、不可交付':localRender?'本地 FFmpeg 导出：不重复扣点，输出文件与版本证据一并固化':'按报价扣点，建单失败自动退款')+'</p></section>';
    }
    var acceptanceChecks=[['story_continuity','剧情与镜头顺序连贯'],['character_consistency','人物形象一致且无串脸'],['audio_video_sync','音画同步且无异常静音'],['subtitle_timing','字幕未越界且时间正确'],['visual_integrity','无黑帧、花屏或明显生成瑕疵'],['transition_quality','转场自然并符合节奏']];
    return '<section class="sd-full-acceptance"><span class="sd-stage-label">PR-5 · 全片验收</span><h2>'+(issues.length?'还有 '+issues.length+' 个问题镜头':'全片可以验收并锁定')+'</h2><p>请完整播放 720p 预览并检查以下项目。验收通过后将锁定镜头、音轨、字幕和素材版本，用于 1080p 导出。</p><div class="sd-checks">'+acceptanceChecks.map(function(item){return '<label><input type="checkbox" data-acceptance-check="'+item[0]+'"'+(issues.length?' disabled':'')+'> '+escapeHtml(item[1])+'</label>';}).join('')+'</div><button type="button" data-action="confirm-refinement" disabled>全片验收通过并锁定</button><small>'+(issues.length?'请先重做全部问题镜头。':'勾选全部验收项后可锁定当前精修版本。')+'</small></section>';
  }
  function refinementProviderHtml(autodraft,refinement,canEdit){
    var current=refinement&&refinement.current_refinement,issues=current&&current.issues||[];
    if(!issues.length)return '';
    var issueKeys=issues.map(function(item){return item.shot_key;});
    var poc=autodraft&&autodraft.provider_poc||{},shots=(poc.shots||[]).filter(function(item){return issueKeys.indexOf(item.shot_key)>=0;});
    var preview=autodraft&&autodraft.provider_preview,quote=autodraft&&autodraft.provider_quote,job=autodraft&&autodraft.provider_job;
    var options=shots.map(function(item){return '<option value="'+escapeHtml(item.shot_key)+'">#'+Number(item.sort_order||0)+' · '+escapeHtml(item.scene||item.shot_key)+'</option>';}).join('');
    var active=job&&['billing','queued','submitting','running'].indexOf(job.status)>=0;
    var status=job?'<div class="sd-check '+(job.status==='succeeded'?'pass':'')+'"><b>Provider 镜头任务 · '+escapeHtml(job.status||'')+' · '+Number(job.progress||0)+'%</b><p>'+escapeHtml(job.error&&job.error.detail||'真实镜头生成完成后，可点击上方“预览并重做这个镜头”重新装配全片。')+'</p></div>':'';
    var quoteHtml=quote?'<div class="sd-estimate"><strong>'+Number(quote.cost||0)+' 点</strong><span>确认后才会扣点并调用真实 Provider</span></div><button data-action="provider-start" type="button"'+(canEdit&&!active?'':' disabled')+'>确认扣点并生成新镜头</button>':'';
    var previewHtml=preview&&preview.ready?'<div class="sd-check pass"><b>真实 Provider 请求预检通过</b><p>'+escapeHtml(preview.request&&preview.request.prompt||'')+'</p></div>'+(quote?'':'<button data-action="provider-quote" type="button"'+(canEdit&&!active?'':' disabled')+'>获取付费报价</button>'):'';
    return '<section class="sd-autodraft-actions sd-refinement-provider"><span class="sd-stage-label">PR-5 · 问题镜头真实重生成</span><h2>先生成新媒体，再重新装配</h2><p>精修不会复用旧视频或只改状态。请为问题镜头完成预检、报价和真实 Provider 生成；成功后重新点击对应镜头的重做按钮。</p><label>问题镜头<select id="sdProviderShot"'+(shots.length&&!active?'':' disabled')+'>'+options+'</select></label><div class="sd-check" id="sdProviderShotCharacter"><b>正在读取镜头角色</b></div><button data-action="provider-preflight" type="button"'+(canEdit&&shots.length&&!active?'':' disabled')+'>免费检查当前镜头</button>'+previewHtml+quoteHtml+status+'</section>';
  }
  function shellHtml(){
    return '<div class="sd-workspace-top"><a href="short-drama.html">← 返回项目</a><div><span id="sdWorkspaceState"></span><b id="sdWorkspaceTitle"></b></div><div class="sd-workspace-top-actions"><button type="button" class="sd-inspector-button" data-action="toggle-inspector" id="sdInspectorButton" aria-expanded="true">收起摘要</button><button type="button" class="sd-history-button" data-action="toggle-history" id="sdHistoryButton" hidden>创作记录</button></div></div>'+
      '<div class="sd-workspace-grid" id="sdWorkspaceGrid">'+
      '<aside class="sd-chat"><header><button type="button" class="sd-chat-toggle" data-action="toggle-history" id="sdChatToggle" hidden aria-expanded="false">展开历史记录</button><h2 id="sdChatTitle">和创作助手对话</h2><p id="sdChatDescription">说清人物、冲突、情绪和结局。</p></header><div id="sdMessages"></div><form id="sdMessageForm"><textarea name="message" maxlength="8000" placeholder="例如：结尾要反转，但不要悲剧" required></textarea><button type="submit">发送</button></form><div class="sd-chat-locked-actions" id="sdChatLockedActions" hidden><p>这里仅保留本项目的历史沟通记录，不会修改已锁定或已交付内容。</p><button type="button" data-action="clone-project">基于当前项目创建新版本</button><small>将复制创作规格并建立新项目，当前交付快照保持不变。</small></div></aside>'+
      '<main class="sd-script" id="sdScript"></main>'+
      '<aside class="sd-inspector"><section><h2>理解摘要</h2><dl id="sdUnderstanding"></dl></section><div id="sdActions"></div><section><h2>版本历史</h2><div id="sdVersions"></div></section><div class="sd-workspace-notice" id="sdWorkspaceNotice" hidden></div></aside>'+
      '</div>';
  }
  function mount(doc,options){
    options=options||{};
    var projectId=text(options.projectId).trim(),client=options.client||createClient(options.fetchImpl),state=normalize({}),preflight={state:'script_required',current_plan:null,versions:[]},autodraft={state:'plan_required',versions:[]},refinement=null,characterStudio=null,selectedCharacterKey='',selectedShotKey='',selectedProviderShotKey='',pollTimer=null,historyExpanded=false,inspectorExpanded=!(doc.defaultView&&doc.defaultView.innerWidth<=1050);
    var root=doc.getElementById('shortDramaWorkspace');
    if(!root||!projectId)throw new Error('workspace target unavailable');
    root.innerHTML=shellHtml();root.insertAdjacentHTML('beforeend','<div class="sd-character-modal" id="sdCharacterModal" hidden><div class="sd-character-modal-backdrop" data-action="close-character"></div><section role="dialog" aria-modal="true" aria-labelledby="sdCharacterModalTitle"><header><div><span>角色形象工作室</span><h2 id="sdCharacterModalTitle">准备角色</h2></div><button type="button" data-action="close-character" aria-label="关闭">×</button></header><div id="sdCharacterModalBody"></div></section></div><div class="sd-character-modal sd-shot-modal" id="sdShotModal" hidden><div class="sd-character-modal-backdrop" data-action="close-shot-editor"></div><section role="dialog" aria-modal="true" aria-labelledby="sdShotModalTitle"><header><div><span>单镜头编辑器</span><h2 id="sdShotModalTitle">编辑镜头</h2></div><button type="button" data-action="close-shot-editor" aria-label="关闭">×</button></header><div id="sdShotModalBody"></div></section></div>');root.hidden=false;
    var notice=doc.getElementById('sdWorkspaceNotice');
    function busy(flag){root.classList.toggle('busy',!!flag);root.querySelectorAll('button,textarea').forEach(function(node){var readOnlyAction=node.getAttribute('data-action')==='toggle-history';node.disabled=!!flag||(!readOnlyAction&&!state.permissions.can_edit&&node.closest('form,section'));});}
    function show(message,error){notice.textContent=message||'';notice.classList.toggle('error',!!error);notice.hidden=!message;}
    function studioCharacter(characterKey){
      return characterStudio&&characterStudio.characters&&characterStudio.characters.filter(function(item){return item.character_key===characterKey;})[0];
    }
    function currentShot(shotKey){
      var script=state.current_script&&state.current_script.script;
      return script&&(script.shots||[]).filter(function(item){return item.shot_key===shotKey;})[0];
    }
    function currentShotLine(shot){
      var script=state.current_script&&state.current_script.script;
      var lineId=shot&&shot.dialogue_line_ids&&shot.dialogue_line_ids[0];
      return script&&(script.dialogue_lines||[]).filter(function(item){return item.id===lineId;})[0];
    }
    function renderShotModal(){
      var modal=doc.getElementById('sdShotModal'),body=doc.getElementById('sdShotModalBody'),shot=currentShot(selectedShotKey);
      if(!selectedShotKey||!shot||!state.current_script){modal.hidden=true;return;}
      var script=state.current_script.script,line=currentShotLine(shot)||{},locked=!!shot.locked;
      modal.hidden=false;
      doc.getElementById('sdShotModalTitle').textContent='镜头 #'+Number(shot.sort_order||0)+(locked?' · 已锁定':'');
      var characterOptions=(script.characters||[]).map(function(item){return '<option value="'+escapeHtml(item.character_key||'')+'"'+(item.character_key===line.character_key?' selected':'')+'>'+escapeHtml(item.name||'角色')+'</option>';}).join('');
      body.innerHTML='<form id="sdShotEditor" class="sd-shot-editor"><div class="sd-shot-editor-grid">'+
        '<label>剧情任务<textarea name="purpose" required maxlength="160">'+escapeHtml(shot.purpose||'')+'</textarea></label>'+
        '<label>镜头时长（秒）<input type="number" name="duration_seconds" min="1" max="'+Number((script.overview&&script.overview.duration_seconds)||60)+'" value="'+Number(shot.duration_seconds||1)+'" required><small>修改后系统会自动平衡其他未锁定镜头，总时长保持不变。</small></label>'+
        '<label>场景<input name="scene" maxlength="80" value="'+escapeHtml(shot.scene||'')+'"></label>'+
        '<label class="wide">具体画面与动作<textarea name="visual" required maxlength="360">'+escapeHtml(shot.visual||'')+'</textarea></label>'+
        '<label class="wide">机位与运镜<textarea name="camera" maxlength="180">'+escapeHtml(shot.camera||'')+'</textarea></label>'+
        '<label class="wide">连续性要求<textarea name="continuity" maxlength="220">'+escapeHtml(shot.continuity||'')+'</textarea></label>'+
        '<label>内容类型<select name="dialogue_kind"><option value="dialogue"'+(line.kind==='dialogue'?' selected':'')+'>人物对白</option><option value="voiceover"'+(line.kind==='voiceover'?' selected':'')+'>旁白</option><option value="on_screen_text"'+(line.kind==='on_screen_text'?' selected':'')+'>画面文字</option><option value="silence"'+(line.kind==='silence'?' selected':'')+'>静默表演</option></select></label>'+
        '<label>说话角色<select name="character_key"><option value="">不指定</option>'+characterOptions+'</select></label>'+
        '<label class="wide">台词 / 旁白 / 画面文字<textarea name="dialogue_text" maxlength="120" placeholder="静默表演时留空">'+escapeHtml(line.text||'')+'</textarea><small>建议按每秒 3—4 个汉字控制长度。</small></label>'+
        '<label class="wide">Provider 提示词<textarea name="provider_prompt" required maxlength="1200">'+escapeHtml(shot.provider_prompt||'')+'</textarea></label>'+
        '<label class="wide">禁止项<textarea name="negative_prompt" maxlength="500">'+escapeHtml(shot.negative_prompt||'')+'</textarea></label>'+
        '</div><footer>'+(locked?'<p>当前镜头已锁定；解锁后才能编辑或重新生成。</p>':'<button type="submit">保存为新剧本版本</button>')+'<button type="button" class="secondary" data-action="close-shot-editor">取消</button></footer></form>';
    }
    function renderCharacterCards(){
      var list=doc.querySelector('.sd-character-list');
      if(!list||!state.current_script)return;
      var scriptLocked=state.conversation.state==='script_locked';
      var scriptCharacters=state.current_script.script.characters||[];
      var known={};scriptCharacters.forEach(function(item){known[item.character_key]=item;});
      var items=characterStudio&&characterStudio.characters&&characterStudio.characters.length?characterStudio.characters:scriptCharacters;
      list.innerHTML=items.map(function(item){
        var prepared=studioCharacter(item.character_key),image=prepared&&prepared.image_url;
        var status=prepared?(prepared.binding_ready?'已绑定电影化身':image?'已有角色形象':'待准备'):(scriptLocked?'正在加载':'锁定后可选择');
        return '<button type="button" class="sd-character-card" data-action="open-character" data-character-key="'+escapeHtml(item.character_key||'')+'">'+
          (image?'<img src="'+escapeHtml(image)+'" alt="'+escapeHtml(item.name||'角色')+'">':'<i>'+escapeHtml((item.name||'?').slice(0,1))+'</i>')+
          '<span><b>'+escapeHtml(item.name||'未命名角色')+'</b><em>'+escapeHtml(status)+'</em><small>'+escapeHtml((prepared&&prepared.identity_text)||item.identity||'')+'</small><p>'+escapeHtml((prepared&&prepared.personality)||item.personality||'')+'</p></span></button>';
      }).join('');
    }
    function renderCharacterModal(){
      var modal=doc.getElementById('sdCharacterModal'),body=doc.getElementById('sdCharacterModalBody'),character=studioCharacter(selectedCharacterKey);
      if(!selectedCharacterKey){modal.hidden=true;return;}
      if(!character){
        var fallback=state.current_script&&state.current_script.script&&(state.current_script.script.characters||[]).filter(function(item){return item.character_key===selectedCharacterKey;})[0];
        modal.hidden=false;
        doc.getElementById('sdCharacterModalTitle').textContent=(fallback&&fallback.name)||'准备角色';
        if(state.conversation.state!=='script_locked'){
          body.innerHTML='<section class="sd-character-prerequisite"><i>'+escapeHtml(((fallback&&fallback.name)||'?').slice(0,1))+'</i><h3>先锁定剧本，再选择人物形象</h3><p>角色名称已经从当前剧本识别出来，但人物档案和形象绑定必须关联一个不会继续变化的剧本版本。锁定后将自动打开该角色的形象工作室。</p><button type="button" data-action="lock-script-for-character">锁定当前剧本并继续</button><button type="button" class="secondary" data-action="close-character">暂不锁定</button></section>';
        }else{
          body.innerHTML='<section class="sd-character-prerequisite"><i>'+escapeHtml(((fallback&&fallback.name)||'?').slice(0,1))+'</i><h3>正在加载角色形象资料</h3><p>剧本已经锁定，正在同步角色档案和电影化身库。</p><button type="button" data-action="retry-character-studio">重新加载</button><button type="button" class="secondary" data-action="close-character">关闭</button></section>';
        }
        return;
      }
      modal.hidden=false;doc.getElementById('sdCharacterModalTitle').textContent=character.name||'准备角色';
      var avatars=(characterStudio&&characterStudio.avatars)||[];
      var affected=(character.affected_shots||[]).map(function(item){return '#'+Number(item.sort_order||0);}).join('、')||'暂无镜头';
      body.innerHTML='<div class="sd-character-editor"><div class="sd-character-preview">'+
        (character.image_url?'<img src="'+escapeHtml(character.image_url)+'" alt="'+escapeHtml(character.name)+'">':'<i>'+escapeHtml((character.name||'?').slice(0,1))+'</i>')+
        '<b>'+escapeHtml(character.binding_ready?'已绑定电影化身':character.image_url?'形象已准备':'尚未准备形象')+'</b><small>影响镜头：'+escapeHtml(affected)+'</small></div>'+
        '<form id="sdCharacterProfile"><label>角色身份<textarea name="identity_text" required>'+escapeHtml(character.identity_text||'')+'</textarea></label><label>人物性格<textarea name="personality" required>'+escapeHtml(character.personality||'')+'</textarea></label><label>外貌特征<textarea name="appearance_prompt" required placeholder="年龄、脸型、发型、体态、辨识特征">'+escapeHtml(character.appearance_prompt||'')+'</textarea></label><label>服装穿着<textarea name="wardrobe_prompt" required placeholder="服装风格、颜色、材质、配饰">'+escapeHtml(character.wardrobe_prompt||'')+'</textarea></label><button type="submit">保存角色档案</button><button type="button" class="secondary" data-action="generate-character-image">生成角色形象图（按现有规则扣点）</button></form></div>'+
        '<section class="sd-character-library"><header><div><b>我的电影化身库</b><small>选择后会绑定到该角色，并用于对应镜头预检</small></div><button type="button" data-action="refresh-character-library">刷新</button></header><div>'+avatars.map(function(avatar){return '<button type="button" class="'+(String(character.avatar_id||'')===String(avatar.id)?'selected':'')+'" data-action="bind-character-avatar" data-avatar-id="'+escapeHtml(avatar.id)+'">'+(avatar.image_url?'<img src="'+escapeHtml(avatar.image_url)+'" alt="'+escapeHtml(avatar.name)+'">':'<i>像</i>')+'<span>'+escapeHtml(avatar.name)+'</span></button>';}).join('')+'</div>'+(avatars.length?'':'<p>暂无可用电影化身。请先创建并等待状态变为可用。</p>')+'<button type="button" class="secondary" data-action="create-character-avatar">去视频生成创建电影化身</button>'+(character.avatar_id?'<button type="button" class="ghost" data-action="unbind-character-avatar">解除当前绑定</button>':'')+'</section>';
    }
    function enhanceProviderPreflight(){
      var poc=autodraft&&autodraft.provider_poc,shotField=doc.getElementById('sdProviderShot'),summary=doc.getElementById('sdProviderShotCharacter');
      if(!poc||!shotField)return;
      if(selectedProviderShotKey&&(poc.shots||[]).some(function(item){return item.shot_key===selectedProviderShotKey;}))shotField.value=selectedProviderShotKey;
      selectedProviderShotKey=shotField.value;
      var shot=(poc.shots||[]).filter(function(item){return item.shot_key===shotField.value;})[0]||(poc.shots||[])[0];
      var requiredKeys=shot&&shot.character_keys||[];
      var requiredCharacters=requiredKeys.map(function(key){
        return (poc.characters||[]).filter(function(item){return item.character_key===key;})[0]||{character_key:key,name:key,binding_ready:false};
      });
      if(summary){
        var ready=!!(shot&&shot.binding_ready);
        summary.className='sd-check '+(ready?'pass':'warning');
        summary.innerHTML=ready?
          '<b>本镜头角色已就绪</b><p>'+escapeHtml(requiredCharacters.map(function(item){return item.name;}).join('、')||'无需绑定角色')+' · 将自动使用左侧锁定形象</p>':
          '<b>本镜头暂不能生成</b><p>'+(requiredCharacters.length?'请先在左侧完成 '+escapeHtml(requiredCharacters.filter(function(item){return !item.binding_ready;}).map(function(item){return item.name;}).join('、'))+' 的形象绑定。':'该镜头尚未关联角色，请先检查剧本镜头配置。')+'</p>';
      }
      var button=root.querySelector('[data-action="provider-preflight"]');
      var active=autodraft.provider_job&&['billing','queued','submitting','running'].indexOf(autodraft.provider_job.status)>=0;
      if(button)button.disabled=!(state.permissions.can_edit&&shot&&shot.binding_ready&&!active);
    }
    function render(){
      state=normalize(state);
      doc.getElementById('sdWorkspaceTitle').textContent=state.project.title||'短剧项目';
      doc.getElementById('sdWorkspaceState').textContent=state.conversation.state;
      doc.getElementById('sdMessages').innerHTML=state.messages.map(messageHtml).join('')||'<p class="sd-placeholder">从一句创作想法开始吧。</p>';
      doc.getElementById('sdScript').innerHTML=refinementHtml(refinement)||draftHtml(autodraft)||scriptHtml(state.current_script,state.permissions.can_edit&&state.conversation.state!=='script_locked',autodraft);
      if(!refinement&&!autodraft.current_version)renderCharacterCards();
      var understanding=state.conversation.understanding||{};
      var phaseLabel={discovering:'正在了解想法',recommending:'正在选择方向',refining:'修改后待确认',import_review:'原稿理解待确认',direction_ready:'创作方向已确认'}[understanding.phase]||'等待创作想法';
      var selected=(understanding.recommendations||[]).filter(function(item){return item.id===understanding.selected_recommendation_id;})[0];
      var selectedDirection=understanding.selected_direction||selected||{};
      var missing=(understanding.missing_fields||[]).join('、');
      doc.getElementById('sdUnderstanding').innerHTML='<dt>助手状态</dt><dd><span class="sd-advisor-state '+escapeHtml(understanding.phase||'discovering')+'">'+escapeHtml(phaseLabel)+'</span></dd><dt>核心故事</dt><dd>'+escapeHtml(understanding.premise||state.project.synopsis||'待补充')+'</dd>'+(selectedDirection.title?'<dt>助手建议</dt><dd>'+escapeHtml(selectedDirection.title)+'<small>'+escapeHtml(selectedDirection.summary||'')+'</small></dd>':'')+'<dt>用户补充</dt><dd>'+escapeHtml((understanding.story_notes||[]).join('；')||'待补充')+'</dd><dt>风格</dt><dd>'+escapeHtml(understanding.tone||state.project.visual_style||'待补充')+'</dd>'+(missing?'<dt>仍需了解</dt><dd>'+escapeHtml(missing)+'</dd>':'')+'<dt>规格</dt><dd>'+Number(understanding.duration_seconds||state.project.target_duration||0)+' 秒 · '+escapeHtml(understanding.ratio||state.project.ratio||'')+'</dd>'+importContractHtml(understanding.import_contract);
      doc.getElementById('sdVersions').innerHTML=state.versions.map(function(item){return versionHtml(item,state.conversation.current_version_id);}).join('')||'<p class="sd-placeholder">暂无版本</p>';
      var locked=state.conversation.state==='script_locked';
      var grid=doc.getElementById('sdWorkspaceGrid'),chatToggle=doc.getElementById('sdChatToggle'),historyButton=doc.getElementById('sdHistoryButton'),inspectorButton=doc.getElementById('sdInspectorButton'),lockedActions=doc.getElementById('sdChatLockedActions'),messageForm=doc.getElementById('sdMessageForm');
      grid.classList.toggle('chat-readonly',locked);
      grid.classList.toggle('project-ready',locked);
      grid.classList.toggle('history-open',locked&&historyExpanded);
      grid.classList.toggle('inspector-collapsed',!inspectorExpanded);
      inspectorButton.textContent=inspectorExpanded?'收起摘要':'查看摘要';
      inspectorButton.setAttribute('aria-expanded',inspectorExpanded?'true':'false');
      doc.getElementById('sdChatTitle').textContent=locked?'历史创作记录（只读）':'和创作助手对话';
      doc.getElementById('sdChatDescription').textContent=locked?'剧本已锁定，以下内容仅供追溯。':'说清人物、冲突、情绪和结局。';
      chatToggle.hidden=!locked;
      chatToggle.textContent='关闭创作记录';
      chatToggle.setAttribute('aria-expanded',historyExpanded?'true':'false');
      historyButton.hidden=!locked;
      historyButton.textContent=historyExpanded?'关闭创作记录':'创作记录';
      historyButton.setAttribute('aria-expanded',historyExpanded?'true':'false');
      lockedActions.hidden=!locked;
      messageForm.hidden=locked;
      doc.getElementById('sdActions').innerHTML=refinement?(refinementActionsHtml(refinement,state.permissions.can_edit)+refinementProviderHtml(autodraft,refinement,state.permissions.can_edit)):(autodraft.confirmed_plan?autodraftActionsHtml(autodraft,state.permissions.can_edit):preflightHtml(state.conversation,preflight,state.permissions.can_edit));
      enhanceProviderPreflight();
      renderCharacterModal();
      renderShotModal();
      var generate=root.querySelector('[data-action="generate"]'),lock=root.querySelector('[data-action="lock"]');
      if(generate)generate.disabled=locked||!state.permissions.can_edit||(!state.conversation.current_version_id&&!understanding.direction_confirmed);
      if(lock)lock.disabled=locked||!state.current_script||!state.permissions.can_edit||!!(state.current_script&&state.current_script.script&&state.current_script.script.quality_gate&&state.current_script.script.quality_gate.status==='blocked');
      root.querySelector('#sdMessageForm textarea').disabled=locked||!state.permissions.can_edit;
      root.querySelector('#sdMessageForm button').disabled=locked||!state.permissions.can_edit;
      if(!locked){
        root.querySelector('#sdMessageForm textarea').placeholder=understanding.phase==='direction_ready'?'方向已确认；还可以补充一条硬性要求':understanding.phase==='import_review'?'核对原稿理解，或补充必须保留 / 允许优化的内容':understanding.phase==='refining'?'继续调整，或发送“确认调整后的方向”':'说说人物、冲突、情绪、结局，或让助手推荐方向';
      }
    }
    function payload(extra){return Object.assign({project_id:projectId,conversation_revision:Number(state.conversation.revision)},extra||{});}
    function apply(promise,success){
      busy(true);show('',false);
      return promise.then(function(result){state=normalize(result);render();show(success,false);return state;})
        .catch(function(error){show(error.message||'操作失败',true);if(error.status===409)return client.workspace(projectId).then(function(result){state=normalize(result);render();});throw error;})
        .finally(function(){busy(false);render();});
    }
    function sendConversationMessage(value){
      value=text(value).trim();
      if(!value)return Promise.resolve(state);
      var field=doc.getElementById('sdMessageForm').elements.message;
      return apply(client.message(payload({message:value})),'创作助手已更新理解')
        .then(function(result){field.value='';var messages=doc.getElementById('sdMessages');messages.scrollTop=messages.scrollHeight;return result;});
    }
    function loadPreflight(){
      return client.preflight(projectId).then(function(result){preflight=result||{};render();return preflight;});
    }
    function applyPreflight(promise,success){
      busy(true);show('',false);
      return promise.then(function(result){preflight=result||{};render();show(success,false);return preflight;})
        .catch(function(error){show(error.message||'制作准备操作失败',true);if(error.status===409)return loadPreflight();throw error;})
        .finally(function(){busy(false);render();});
    }
    function schedulePoll(){
      if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}
      var refinementJob=refinement&&refinement.current_refinement_job,deliveryJob=refinement&&refinement.current_delivery_job;
      if(refinementJob&&['queued','running'].indexOf(refinementJob.status)>=0){
        pollTimer=setTimeout(function(){
          client.refinementJob(projectId,refinementJob.id).then(function(result){
            refinement.current_refinement_job=result;
            if(result.result)return client.refinement(projectId).then(function(workspace){refinement=workspace||null;show('镜头精修已完成，已生成新版本',false);});
          }).then(function(){render();schedulePoll();}).catch(function(error){show(error.message||'精修任务状态更新失败',true);});
        },900);
        return;
      }
      if(deliveryJob&&['queued','running'].indexOf(deliveryJob.status)>=0){
        pollTimer=setTimeout(function(){
          client.deliveryJob(projectId,deliveryJob.id).then(function(result){
            refinement.current_delivery_job=result;
            if(result.result)return client.refinement(projectId).then(function(workspace){refinement=workspace||null;show(refinement.billing&&refinement.billing.mode==='development_free'?'免费演示预览已生成，不可作为正式交付':'1080p 正式成片已生成并固化交付快照',false);});
          }).then(function(){render();schedulePoll();}).catch(function(error){show(error.message||'正式导出状态更新失败',true);});
        },900);
        return;
      }
      var providerJob=autodraft&&autodraft.provider_job;
      if(providerJob&&['billing','queued','submitting','running'].indexOf(providerJob.status)>=0){
        pollTimer=setTimeout(function(){
          client.providerJob(projectId,providerJob.id).then(function(result){
            autodraft.provider_job=result;
            if(result.status==='succeeded')return client.autodraft(projectId).then(function(workspace){
              autodraft=workspace||{};
              show('当前镜头已生成并保存为可复用版本',false);
            });
            if(['failed','submit_unknown'].indexOf(result.status)>=0){
              show(result.error&&result.error.detail||'单镜头生成失败；请查看退款或人工对账状态',true);
            }
          }).then(function(){render();schedulePoll();}).catch(function(error){
            show(error.message||'单镜头任务状态更新失败',true);
            schedulePoll();
          });
        },1500);
        return;
      }
      var job=autodraft&&autodraft.current_job;
      if(!job||['queued','running'].indexOf(job.status)<0)return;
      pollTimer=setTimeout(function(){
        client.draftJob(projectId,job.id).then(function(result){
          autodraft.current_job=result;
          if(result.result)return client.autodraft(projectId).then(function(workspace){
            autodraft=workspace||{};
            if(autodraft.current_version)return client.refinement(projectId).then(function(next){refinement=next||null;});
          });
        }).then(function(){render();schedulePoll();}).catch(function(error){show(error.message||'自动草稿状态更新失败',true);});
      },900);
    }
    function loadAutodraft(){
      return client.autodraft(projectId).then(function(result){autodraft=result||{};render();schedulePoll();return autodraft;});
    }
    function loadRefinement(){
      return client.refinement(projectId).then(function(result){refinement=result||null;render();schedulePoll();return refinement;});
    }
    function loadCharacterStudio(silent){
      return client.characterStudio(projectId).then(function(result){
        characterStudio=result||null;
        render();
        return characterStudio;
      }).catch(function(error){
        if(!silent)show(error.message||'角色形象工作室加载失败',true);
        throw error;
      });
    }
    function refreshCharacterContext(message){
      return Promise.all([
        loadCharacterStudio(true),
        client.preflight(projectId).catch(function(){return preflight;}),
        client.autodraft(projectId).catch(function(){return autodraft;})
      ]).then(function(results){
        characterStudio=results[0]||characterStudio;
        preflight=results[1]||preflight;
        autodraft=results[2]||autodraft;
        render();
        if(message)show(message,false);
        return characterStudio;
      });
    }
    function saveCharacterProfile(character,form){
      var fields=form.elements;
      return client.saveCharacterProfile({
        project_id:projectId,
        project_revision:Number(characterStudio.project_revision),
        character_key:character.character_key,
        identity_text:text(fields.identity_text.value).trim(),
        personality:text(fields.personality.value).trim(),
        appearance_prompt:text(fields.appearance_prompt.value).trim(),
        wardrobe_prompt:text(fields.wardrobe_prompt.value).trim()
      });
    }
    root.addEventListener('submit',function(event){
      if(event.target.id!=='sdCharacterProfile')return;
      event.preventDefault();
      var character=studioCharacter(selectedCharacterKey);
      if(!character)return;
      busy(true);show('',false);
      saveCharacterProfile(character,event.target)
        .then(function(){return refreshCharacterContext('角色档案已保存，相关制作计划已标记为需要重新确认');})
        .catch(function(error){show(error.message||'角色档案保存失败',true);})
        .finally(function(){busy(false);render();});
    });
    root.addEventListener('submit',function(event){
      if(event.target.id!=='sdShotEditor')return;
      event.preventDefault();
      var shot=currentShot(selectedShotKey),fields=event.target.elements;
      if(!shot||!state.current_script)return;
      var kind=fields.dialogue_kind.value,characterKey=fields.character_key.value;
      if((kind==='dialogue'||kind==='voiceover')&&!characterKey){show('人物对白或旁白必须选择说话角色',true);return;}
      var changes={
        purpose:text(fields.purpose.value).trim(),
        duration_seconds:Number(fields.duration_seconds.value),
        scene:text(fields.scene.value).trim(),
        visual:text(fields.visual.value).trim(),
        camera:text(fields.camera.value).trim(),
        continuity:text(fields.continuity.value).trim(),
        provider_prompt:text(fields.provider_prompt.value).trim(),
        negative_prompt:text(fields.negative_prompt.value).trim(),
        dialogue:{
          kind:kind,
          character_key:characterKey,
          text:text(fields.dialogue_text.value).trim()
        }
      };
      apply(client.updateShot(payload({
        version_id:state.current_script.id,
        shot_key:shot.shot_key,
        changes:changes
      })),'镜头已保存为新的剧本版本').then(function(){
        selectedShotKey='';
        render();
      });
    });
    doc.getElementById('sdMessageForm').addEventListener('submit',function(event){
      event.preventDefault();var field=event.currentTarget.elements.message,value=text(field.value).trim();if(!value)return;
      sendConversationMessage(value);
    });
    root.addEventListener('click',function(event){
      var action=event.target.closest('[data-action]');
      if(action&&action.getAttribute('data-action')==='edit-shot'){
        selectedShotKey=action.getAttribute('data-shot-key')||'';
        renderShotModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='close-shot-editor'){
        selectedShotKey='';
        renderShotModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='regenerate-shot'){
        var regenerateKey=action.getAttribute('data-shot-key')||'',promptWindow=doc.defaultView;
        var instruction=promptWindow&&typeof promptWindow.prompt==='function'?promptWindow.prompt('可选：说明这个镜头需要怎样调整',''):'';
        if(instruction===null)return;
        apply(client.regenerateShot(payload({
          version_id:state.current_script.id,
          shot_key:regenerateKey,
          instruction:text(instruction).trim()
        })),'当前镜头已重新生成，其他镜头保持不变');
        return;
      }
      if(action&&action.getAttribute('data-action')==='toggle-shot-lock'){
        var lockKey=action.getAttribute('data-shot-key')||'',willLock=action.getAttribute('data-locked')!=='1';
        apply(client.setShotLock(payload({
          version_id:state.current_script.id,
          shot_key:lockKey,
          locked:willLock
        })),willLock?'镜头已锁定':'镜头已解锁');
        return;
      }
      if(action&&action.getAttribute('data-action')==='open-character'){
        selectedCharacterKey=action.getAttribute('data-character-key')||'';
        renderCharacterModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='lock-script-for-character'){
        if(!state.current_script)return;
        apply(client.lock(payload({version_id:state.current_script.id})),'剧本已锁定，正在打开角色形象工作室')
          .then(loadPreflight)
          .then(function(){return loadCharacterStudio();})
          .then(function(){render();renderCharacterModal();})
          .catch(function(error){show(error.message||'剧本锁定或角色资料加载失败',true);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='retry-character-studio'){
        busy(true);show('',false);
        loadCharacterStudio().then(function(){render();renderCharacterModal();})
          .catch(function(error){show(error.message||'角色形象工作室加载失败',true);})
          .finally(function(){busy(false);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='close-character'){
        selectedCharacterKey='';
        renderCharacterModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='refresh-character-library'){
        busy(true);show('',false);
        loadCharacterStudio().then(function(){
          var count=Number(characterStudio&&characterStudio.avatars&&characterStudio.avatars.length||0);
          show(count?'形象库已刷新，共 '+count+' 个可用电影化身':'形象库中暂无可用电影化身',!count);
        }).finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='create-character-avatar'){
        var characterForCreate=studioCharacter(selectedCharacterKey);
        var returnTo=doc.defaultView&&doc.defaultView.location?doc.defaultView.location.href:'';
        var target=avatarCreateUrl()+'&return_to='+encodeURIComponent(returnTo)+'&project_id='+encodeURIComponent(projectId)+'&character_key='+encodeURIComponent(characterForCreate&&characterForCreate.character_key||'');
        var opened=doc.defaultView&&typeof doc.defaultView.open==='function'?doc.defaultView.open(target,'_blank','noopener'):null;
        if(!opened&&doc.defaultView&&doc.defaultView.location)doc.defaultView.location.href=target;
        show('请在视频生成页完成电影化身创建，返回后刷新形象库并绑定',false);
        return;
      }
      if(action&&action.getAttribute('data-action')==='bind-character-avatar'){
        var characterForBind=studioCharacter(selectedCharacterKey);
        if(!characterForBind)return;
        busy(true);show('',false);
        client.bindCharacterAvatar({
          project_id:projectId,
          project_revision:Number(characterStudio.project_revision),
          character_key:characterForBind.character_key,
          avatar_id:action.getAttribute('data-avatar-id')||''
        }).then(function(){return refreshCharacterContext('电影化身已绑定到 '+characterForBind.name+'，相关镜头会自动使用该形象');})
          .catch(function(error){show(error.message||'电影化身绑定失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='unbind-character-avatar'){
        var characterForUnbind=studioCharacter(selectedCharacterKey);
        if(!characterForUnbind)return;
        busy(true);show('',false);
        client.bindCharacterAvatar({
          project_id:projectId,
          project_revision:Number(characterStudio.project_revision),
          character_key:characterForUnbind.character_key,
          avatar_id:''
        }).then(function(){return refreshCharacterContext('已解除 '+characterForUnbind.name+' 的电影化身绑定');})
          .catch(function(error){show(error.message||'解除绑定失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='generate-character-image'){
        var characterForImage=studioCharacter(selectedCharacterKey),profileForm=doc.getElementById('sdCharacterProfile');
        if(!characterForImage||!profileForm)return;
        if(!doc.defaultView.confirm('生成角色形象将按现有计费规则执行，确认继续吗？'))return;
        busy(true);show('',false);
        saveCharacterProfile(characterForImage,profileForm).then(function(result){
          characterStudio.project_revision=Number(result.project_revision);
          return client.generateCharacterImage({
            project_id:projectId,
            revision:Number(characterStudio.project_revision),
            character_key:characterForImage.character_key
          });
        }).then(function(){
          show('角色形象生成任务已提交，完成后会显示在角色卡旁',false);
          var attempts=0;
          function poll(){
            attempts+=1;
            return loadCharacterStudio(true).then(function(){
              var current=studioCharacter(characterForImage.character_key);
              if(current&&current.image_url)return current;
              if(attempts>=30)return null;
              return new Promise(function(resolve){setTimeout(resolve,1000);}).then(poll);
            });
          }
          return poll();
        }).then(function(result){
          if(result)show('角色形象已生成，可继续创建或绑定电影化身',false);
        }).catch(function(error){show(error.message||'角色形象生成失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='quick-reply'){
        var actionGroup=action.closest('.sd-advisor-actions');
        if(actionGroup){
          action.classList.add('selected');
          actionGroup.querySelectorAll('button').forEach(function(button){button.disabled=true;});
        }
        sendConversationMessage(action.getAttribute('data-message'));
        return;
      }
      if(action&&action.getAttribute('data-action')==='toggle-history'){
        historyExpanded=!historyExpanded;
        render();
        return;
      }
      if(action&&action.getAttribute('data-action')==='toggle-inspector'){
        inspectorExpanded=!inspectorExpanded;
        render();
        return;
      }
      if(action&&action.getAttribute('data-action')==='clone-project'){
        busy(true);show('',false);
        client.createProject(cloneProjectPayload(state.project)).then(function(project){
          show('新版本项目已创建，正在打开',false);
          var target='short-drama.html?project='+encodeURIComponent(project.id);
          if(doc.defaultView&&doc.defaultView.location)doc.defaultView.location.href=target;
        }).catch(function(error){show(error.message||'创建新版本项目失败',true);})
          .finally(function(){busy(false);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='generate'){
        apply(client.generate(payload({instruction:text(doc.getElementById('sdInstruction').value).trim()})),'新剧本版本已生成');
        return;
      }
      if(action&&action.getAttribute('data-action')==='lock'){
        if(!state.current_script)return;
        apply(client.lock(payload({version_id:state.current_script.id})),'剧本已锁定，可进入制作准备').then(loadPreflight);
        return;
      }
      if(action&&action.getAttribute('data-action')==='prepare'){
        var route=doc.getElementById('sdQualityRoute');
        applyPreflight(client.prepare({project_id:projectId,conversation_revision:Number(state.conversation.revision),quality_route:route?route.value:'quick_draft'}),'制作前体检已完成');
        return;
      }
      if(action&&action.getAttribute('data-action')==='confirm-plan'){
        var current=preflight.current_plan,plan=current&&current.plan,accept=doc.getElementById('sdAcceptAdjustments');
        if(!current)return;
        if((plan.required_acceptance||[]).length&&(!accept||!accept.checked)){show('请先勾选接受系统建议',true);return;}
        applyPreflight(client.confirmPlan({project_id:projectId,plan_id:current.id,plan_version:Number(current.version),accepted_issue_keys:plan.required_acceptance||[]}), '制作方案已确认').then(loadAutodraft);
        return;
      }
      if(action&&action.getAttribute('data-action')==='start-draft'){
        var confirmed=autodraft.confirmed_plan;
        if(!confirmed)return;
        busy(true);show('',false);
        client.startDraft({project_id:projectId,plan_id:confirmed.id}).then(function(result){
          autodraft.current_job=result;render();show('自动草稿任务已提交，可离开页面等待完成',false);schedulePoll();
        }).catch(function(error){show(error.message||'自动草稿任务提交失败',true);}).finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='provider-preflight'){
        var confirmedPlan=autodraft.confirmed_plan,shotField=doc.getElementById('sdProviderShot');
        if(!confirmedPlan||!shotField)return;
        selectedProviderShotKey=shotField.value;
        var providerShot=((autodraft.provider_poc&&autodraft.provider_poc.shots)||[]).filter(function(item){return item.shot_key===shotField.value;})[0];
        if(!providerShot||!providerShot.binding_ready){show('请先点击左侧角色卡，完成当前镜头全部角色的形象绑定',true);return;}
        busy(true);show('',false);
        client.providerPreflight({project_id:projectId,plan_id:confirmedPlan.id,shot_key:shotField.value,avatar_id:providerShot.primary_avatar_id||'',character_key:providerShot.primary_character_key||''}).then(function(result){
          autodraft.provider_preview=result;autodraft.provider_quote=null;render();show('单镜头请求预检完成：没有扣点，也没有调用外部 Provider',false);
        }).catch(function(error){show(error.message||'单镜头请求预检失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='provider-quote'){
        var preview=autodraft.provider_preview,planForQuote=autodraft.confirmed_plan;
        if(!preview||!planForQuote)return;
        busy(true);show('',false);
        client.providerQuote({
          project_id:projectId,
          plan_id:planForQuote.id,
          shot_key:preview.shot.shot_key,
          avatar_id:preview.avatar.id,
          character_key:preview.character_key
        }).then(function(result){
          autodraft.provider_quote=result;render();
          show('报价已生成；确认后才会扣点并提交外部任务',false);
        }).catch(function(error){show(error.message||'单镜头报价失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='provider-start'){
        var quote=autodraft.provider_quote;
        if(!quote)return;
        var confirmWindow=doc.defaultView;
        if(confirmWindow&&typeof confirmWindow.confirm==='function'&&!confirmWindow.confirm('确认扣除 '+Number(quote.cost||0)+' 点，生成镜头 '+text(quote.shot&&quote.shot.shot_key)+'？'))return;
        busy(true);show('',false);
        client.startProviderJob({project_id:projectId,quote_token:quote.quote_token}).then(function(result){
          autodraft.provider_job=result;autodraft.provider_quote=null;render();
          show('已完成扣点并创建单镜头任务；页面会自动更新进度',false);
          schedulePoll();
        }).catch(function(error){show(error.message||'单镜头任务提交失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='jump-to-shot'){
        var jumpKey=action.getAttribute('data-shot-key')||'';
        var targetShot=Array.prototype.filter.call(root.querySelectorAll('.sd-shot[data-shot-key]'),function(item){return item.getAttribute('data-shot-key')===jumpKey;})[0];
        if(targetShot){
          targetShot.scrollIntoView({behavior:'smooth',block:'center'});
          targetShot.classList.remove('focused');
          void targetShot.offsetWidth;
          targetShot.classList.add('focused');
        }
        return;
      }
      if(action&&action.getAttribute('data-action')==='refine-shot'){
        var shotKey=action.getAttribute('data-shot-key');
        busy(true);show('',false);
        client.previewRefinement({project_id:projectId,shot_key:shotKey}).then(function(preview){
          if(preview.replacement_ready!==true){throw new Error(preview.replacement_error&&preview.replacement_error.message||'请先在镜头生成区通过真实 Provider 生成该镜头的新版本');}
          show('将重做 '+preview.affected_shots.join('、')+'，预计 '+Number(preview.estimated_seconds||0)+' 秒；已建立恢复点',false);
          return client.refineShot({project_id:projectId,shot_key:shotKey,source_version_id:preview.source_version_id,replacement_provider_version_id:preview.replacement_provider_version_id});
        }).then(function(result){refinement.current_refinement_job=result;render();schedulePoll();})
          .catch(function(error){show(error.message||'镜头精修提交失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='mark-refinement-issue'){
        var issueVersion=refinement&&refinement.current_refinement;if(!issueVersion)return;
        var issueShot=action.getAttribute('data-shot-key');
        var issueMessage=window.prompt('请简要说明该镜头的问题','验收发现该镜头需要重做');
        if(issueMessage===null)return;
        busy(true);show('',false);
        client.markRefinementIssue({project_id:projectId,version_id:issueVersion.id,shot_key:issueShot,issue_code:'user_reported_issue',message:issueMessage})
          .then(loadRefinement).then(function(){show('问题已记录，请重做该镜头后重新验收',false);})
          .catch(function(error){show(error.message||'标记问题镜头失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='confirm-refinement'){
        var currentRefinement=refinement&&refinement.current_refinement;if(!currentRefinement)return;
        var requirements=refinement&&refinement.acceptance_requirements||{};
        var checklist={};Array.prototype.slice.call(root.querySelectorAll('[data-acceptance-check]')).forEach(function(item){checklist[item.getAttribute('data-acceptance-check')]=item.checked===true;});
        busy(true);show('',false);
        client.confirmRefinement({project_id:projectId,version_id:currentRefinement.id,checklist:checklist,source_hashes:requirements.source_hashes||{}}).then(loadRefinement)
          .then(function(){show('全片验收通过，精修版本已锁定，可以导出 1080p 正式成片',false);})
          .catch(function(error){show(error.message||'精修确认失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='start-delivery'){
        var deliverySource=refinement&&refinement.current_refinement;if(!deliverySource)return;
        if(!refinement.billing||refinement.billing.delivery_enabled!==true){show('真实 1080p 正式交付执行器尚未启用，本次不会扣点',true);return;}
        busy(true);show('',false);
        client.deliveryQuote({project_id:projectId,version_id:deliverySource.id}).then(function(quote){
          show('报价 '+Number(quote.cost||0)+' 点，有效期 5 分钟，正在提交正式导出',false);
          return client.startDelivery({project_id:projectId,quote_token:quote.quote_token});
        }).then(function(result){refinement.current_delivery_job=result;render();schedulePoll();})
          .catch(function(error){show(error.message||'正式导出提交失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      var node=event.target.closest('[data-version-id]');if(!node||node.classList.contains('current'))return;
      apply(client.restore(payload({version_id:node.getAttribute('data-version-id')})),'历史版本已恢复为新版本');
    });
    root.addEventListener('change',function(event){
      if(event.target&&event.target.hasAttribute('data-acceptance-check')){
        var checks=Array.prototype.slice.call(root.querySelectorAll('[data-acceptance-check]'));
        var confirmButton=root.querySelector('[data-action="confirm-refinement"]');
        if(confirmButton)confirmButton.disabled=!checks.length||checks.some(function(item){return !item.checked;});
        return;
      }
      if(event.target&&event.target.id==='sdProviderShot'){
        selectedProviderShotKey=event.target.value;
        autodraft.provider_preview=null;
        autodraft.provider_quote=null;
        render();
      }
    });
    busy(true);
    Promise.all([client.workspace(projectId),client.preflight(projectId),client.autodraft(projectId)]).then(function(results){
      state=normalize(results[0]);preflight=results[1]||{};autodraft=results[2]||{};
      var tasks=[];
      if(state.conversation.state==='script_locked')tasks.push(client.characterStudio(projectId).then(function(value){characterStudio=value||null;}));
      if(autodraft.current_version)tasks.push(client.refinement(projectId).then(function(value){refinement=value||null;}));
      return Promise.all(tasks);
    }).then(function(){render();schedulePoll();}).catch(function(error){show(error.message||'工作区加载失败',true);}).finally(function(){busy(false);render();});
    return {render:render,getState:function(){return state;},getPreflight:function(){return preflight;},getAutodraft:function(){return autodraft;},getRefinement:function(){return refinement;}};
  }
  return {createClient:createClient,avatarCreateUrl:avatarCreateUrl,cloneProjectPayload:cloneProjectPayload,normalize:normalize,quickReplyPresentation:quickReplyPresentation,messageHtml:messageHtml,importContractHtml:importContractHtml,shotMediaIndex:shotMediaIndex,shotMediaHtml:shotMediaHtml,scriptHtml:scriptHtml,versionHtml:versionHtml,preflightHtml:preflightHtml,autodraftActionsHtml:autodraftActionsHtml,draftHtml:draftHtml,refinementHtml:refinementHtml,refinementActionsHtml:refinementActionsHtml,refinementProviderHtml:refinementProviderHtml,shellHtml:shellHtml,mount:mount};
});
