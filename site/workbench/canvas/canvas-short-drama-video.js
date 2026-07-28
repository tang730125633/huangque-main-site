(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaVideo=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var VIDEO_PATH='/api/gen/short-drama/video';
  var QUOTE_PATH='/api/gen/short-drama/video-quote';
  var GENERATE_PATH='/api/gen/short-drama/generate-video';
  var SELECT_PATH='/api/gen/short-drama/select-video-version';
  var LOCK_PATH='/api/gen/short-drama/set-video-shot-lock';
  var CONFIRM_PATH='/api/gen/short-drama/confirm-production-stage';
  var CAST_PATH='/api/gen/short-drama/video-cast';
  var AVATAR_PATH='/api/gen/short-drama/video-cast/avatars';
  var POLL_BASE=1800;
  var ACTIVE={pending:1,running:1,uploading:1,submitted:1,downloading:1,metadata_pending:1};

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
    return {code:text(item.code||'unknown'),message:text(item.message||item.code||'状态异常')};
  }
  function normalizeAvatar(item){
    item=item&&typeof item==='object'?item:{};
    return {
      id:number(item.id,0),name:text(item.name||('形象 '+item.id)),
      image_url:text(item.image_url),image_file:text(item.image_file),
      status:text(item.status)
    };
  }
  function normalizeState(input,options){
    input=input&&typeof input==='object'?input:{};
    options=options&&typeof options==='object'?options:{};
    var shots=(Array.isArray(input.shots)?input.shots:[]).map(function(shot,index){
      shot=shot&&typeof shot==='object'?shot:{};
      var versions=(Array.isArray(shot.versions)?shot.versions:[]).map(function(version){
        version=version&&typeof version==='object'?version:{};
        return {
          version:number(version.version,0),status:text(version.status),
          url:text(version.url),file:text(version.file),cover_url:text(version.cover_url),
          duration_ms:number(version.duration_ms,0),ratio:text(version.ratio),
          prompt:text(version.prompt),cost:number(version.cost,0),
          semantic_status:text(version.semantic_status||'legacy'),
          semantic_report:version.semantic_report&&typeof version.semantic_report==='object'?
            clone(version.semantic_report):{},
          created_at:number(version.created_at,0)
        };
      });
      var current=versions.filter(function(item){
        return item.version===number(shot.current_version,-1);
      })[0]||null;
      var job=shot.job&&typeof shot.job==='object'?clone(shot.job):null;
      return {
        id:text(shot.id),shot_key:text(shot.shot_key||('镜头 '+(index+1))),
        sort_order:number(shot.sort_order,index),duration:number(shot.duration,0),
        prompt:text(shot.video_prompt),video_revision:number(shot.video_revision,1),
        current_version:shot.current_version==null?null:number(shot.current_version,0),
        locked:shot.locked===true,status:text(shot.status||'empty'),
        lockable:shot.lockable===true,
        blockers:(Array.isArray(shot.lock_blockers)?shot.lock_blockers:[]).map(normalizeBlocker),
        versions:versions,current:current,job:job,
        still:shot.still&&typeof shot.still==='object'?clone(shot.still):null,
        avatar_ids:Array.isArray(shot.avatar_ids)?shot.avatar_ids.map(text):[],
        voice_tracks:Array.isArray(shot.voice_tracks)?shot.voice_tracks.map(clone):[]
      };
    }).sort(function(a,b){ return a.sort_order-b.sort_order; });
    var selected=text(options.selectedShotId||input.selectedShotId);
    if(!shots.some(function(shot){ return shot.id===selected; })) selected=shots[0]?shots[0].id:'';
    var castCharacters=(Array.isArray(input.cast_characters)?input.cast_characters:[]).map(function(item){
      item=item&&typeof item==='object'?item:{};
      return {
        character_key:text(item.character_key),name:text(item.name||item.character_key),
        reference_url:text(item.reference_url),reference_file:text(item.reference_file),
        source_type:text(item.source_type),binding_source:text(item.binding_source||'missing'),
        avatar_id:item.avatar_id==null?null:number(item.avatar_id,0),
        avatar_name:text(item.avatar_name),avatar_status:text(item.avatar_status),
        valid:item.valid===true,shot_count:number(item.shot_count,0),
        blocker:item.blocker?normalizeBlocker(item.blocker):null
      };
    });
    var avatars=(Array.isArray(options.avatars)?options.avatars:[]).map(normalizeAvatar).filter(function(item){
      return item.id>0&&item.status==='ready';
    });
    return {
      project_id:text(input.project_id),revision:number(input.revision,0),
      stage:text(input.stage||'video_review'),ratio:text(input.ratio||'9:16'),
      point_budget:number(input.point_budget,0),spent_points:number(input.spent_points,0),
      reserved_points:number(input.reserved_points,0),shots:shots,selectedShotId:selected,
      unlocked_shot_count:number(input.unlocked_shot_count,shots.filter(function(s){return !s.locked;}).length),
      handoff_blocked:input.handoff_blocked!==false,
      handoff_blockers:(Array.isArray(input.handoff_blockers)?input.handoff_blockers:[]).map(normalizeBlocker),
      busy:options.busy===true,operationBusy:options.operationBusy===true,
      error:text(options.error),canEdit:options.canEdit!==false&&input.stage!=='assembly_review',
      enhancePrompt:false,
      castCharacters:castCharacters,avatars:avatars,
      castSelections:clone(options.castSelections||{}),
      castDirty:options.castDirty===true,
      castConflicts:clone(options.castConflicts||{}),
      canCreateAvatar:options.canCreateAvatar===true,
      avatarBusy:options.avatarBusy===true
    };
  }
  function selectedShot(state){
    return state.shots.filter(function(shot){ return shot.id===state.selectedShotId; })[0]||state.shots[0];
  }
  function statusLabel(status){
    return ({
      empty:'待生成',blocked:'依赖未就绪',pending:'排队中',running:'生成中',
      uploading:'上传参考图',submitted:'云端生成',downloading:'下载成片',
      metadata_pending:'整理版本',done:'待确认',locked:'已锁定',failed:'生成失败'
    })[status]||status;
  }
  function videoSource(version){
    return version&&text(version.url||version.file);
  }
  function renderCast(state,readonly){
    if(!state.castCharacters.length) return '';
    var conflictKeys=Object.keys(state.castConflicts||{});
    var cards=state.castCharacters.map(function(character){
      var selected=Object.prototype.hasOwnProperty.call(state.castSelections,character.character_key)?
        number(state.castSelections[character.character_key],0):number(character.avatar_id,0);
      var current=state.avatars.filter(function(item){return item.id===selected;})[0]||null;
      var conflicted=conflictKeys.indexOf(character.character_key)>=0;
      var image=text(character.reference_url||character.reference_file);
      var source=character.binding_source==='video_cast'?'C-3 补绑':
        (character.binding_source==='character'?'角色原生':'未绑定');
      var options='<option value="">请选择电影化身</option>'+state.avatars.map(function(avatar){
        return '<option value="'+avatar.id+'"'+(avatar.id===selected?' selected':'')+'>'+
          escapeHtml(avatar.name)+'</option>';
      }).join('');
      if(selected&&!current){
        options+='<option value="'+selected+'" selected disabled>'+
          escapeHtml(character.avatar_name||('不可用形象 #'+selected))+'</option>';
      }
      return '<article class="nc-sdv-cast-card'+(character.valid?'':' is-invalid')+
        (conflicted?' is-conflicted':'')+'">'+
        '<div class="nc-sdv-cast-reference">'+(image?'<img src="'+escapeHtml(image)+
        '" alt="'+escapeHtml(character.name)+'参考图">':'<span>暂无参考图</span>')+'</div>'+
        '<div class="nc-sdv-cast-detail"><strong>'+escapeHtml(character.name)+'</strong>'+
        '<small>'+character.shot_count+' 个镜头 · '+escapeHtml(source)+'</small>'+
        '<select data-field="cast" data-character-key="'+escapeHtml(character.character_key)+'"'+
        (readonly||state.operationBusy?' disabled':'')+'>'+options+'</select>'+
        (conflicted?'<em>协作者已更新此角色的绑定，请确认保留本地选择或采用最新绑定</em>':
          (character.blocker?'<em>'+escapeHtml(character.blocker.message)+'</em>':''))+
        '</div></article>';
    }).join('');
    var conflictNotice=conflictKeys.length?
      '<div class="nc-sdv-cast-conflict"><span>检测到协作者更新了 '+conflictKeys.length+
      ' 个角色。为避免静默覆盖，保存前请确认处理方式。</span><div>'+
      '<button type="button" data-action="keep-local-cast">保留我的选择</button>'+
      '<button type="button" data-action="reload-cast">采用最新绑定</button></div></div>':'';
    var createDisabled=readonly||!state.canCreateAvatar;
    return '<section class="nc-sdv-cast"><header><div><span>C-3 角色补绑</span>'+
      '<h3>角色选角</h3></div><div><button type="button" data-action="refresh-avatars"'+
      (readonly||state.avatarBusy?' disabled':'')+'>刷新形象库</button><button type="button" '+
      'data-action="create-avatar"'+(createDisabled?' disabled':'')+
      (state.canCreateAvatar?'':' title="仅项目所有者可以创建新电影化身"')+
      '>创建新电影化身</button></div></header>'+conflictNotice+
      '<div class="nc-sdv-cast-grid">'+cards+'</div><button type="button" class="nc-sdv-cast-save" '+
      'data-action="save-cast"'+
      (readonly||state.operationBusy||!state.castDirty||conflictKeys.length?' disabled':'')+
      '>保存角色绑定</button></section>';
  }
  function renderWorkspace(input,options){
    var state=normalizeState(input,options);
    if(state.busy&&!state.project_id){
      return '<section class="nc-sdv-state"><strong>正在加载电影化身视频工作区…</strong></section>';
    }
    if(state.error&&!state.project_id){
      return '<section class="nc-sdv-state is-error"><strong>视频工作区加载失败</strong><span>'+
        escapeHtml(state.error)+'</span><button data-action="reload">重新加载</button></section>';
    }
    if(!state.shots.length){
      return '<section class="nc-sdv-state"><strong>暂无可生成镜头</strong></section>';
    }
    var shot=selectedShot(state),readonly=!state.canEdit;
    var rail=state.shots.map(function(item){
      return '<button type="button" class="nc-sdv-shot'+(item.id===shot.id?' is-active':'')+
        '" data-action="select-shot" data-shot-id="'+escapeHtml(item.id)+'"><span>'+
        escapeHtml(item.shot_key)+'</span><small>'+item.duration+' 秒 · '+
        escapeHtml(statusLabel(item.status))+'</small></button>';
    }).join('');
    var source=videoSource(shot.current);
    var tracks=(shot.voice_tracks||[]).filter(function(item){return item.subtitle_visible!==false;});
    var audioTracks=(shot.voice_tracks||[]).map(function(item,index){
      var source=text(item.audio_url||item.audio_file);
      return source?'<audio preload="metadata" data-voice-track="'+index+'" data-start-ms="'+
        number(item.start_ms,0)+'" data-end-ms="'+number(item.end_ms,0)+'" src="'+
        escapeHtml(source)+'"></audio>':'';
    }).join('');
    var subtitle=tracks.length?'<div class="nc-sdv-subtitle" data-subtitle-overlay>'+
      escapeHtml(tracks[0].subtitle_text||'')+'</div>':'';
    var player=source?'<div class="nc-sdv-player-wrap"><video controls playsinline muted data-video-player src="'+
      escapeHtml(source)+'"></video>'+subtitle+audioTracks+'</div>':
      '<div class="nc-sdv-empty-preview"><strong>尚未生成视频</strong><span>锁定的关键帧、配音与字幕将作为本次生成依据</span></div>';
    var versions=shot.versions.length?shot.versions.map(function(version){
      return '<button type="button" class="nc-sdv-version'+
        (version.version===shot.current_version?' is-current':'')+
        '" data-action="select-version" data-version="'+version.version+
        '"'+(readonly||shot.locked?' disabled':'')+'><span>V'+version.version+
        '</span><small>'+Math.round(version.duration_ms/100)/10+' 秒 · '+version.cost+' 点</small></button>';
    }).join(''):'<span class="nc-sdv-muted">生成成功后会在这里保留全部版本</span>';
    var blockers=shot.blockers.length?'<ul class="nc-sdv-blockers">'+shot.blockers.map(function(item){
      return '<li>'+escapeHtml(item.message)+'</li>';
    }).join('')+'</ul>':'';
    var semantic=shot.current&&shot.current.semantic_report||{};
    var semanticDecision=text(semantic.decision||shot.current&&shot.current.semantic_status);
    var semanticLabel={
      accepted:'语义检查通过',rejected_visual:'语义检查未通过',
      manual_review:'语义检查需复核',skipped:'语义检查已关闭',
      unavailable:'语义检查暂不可用',legacy:'历史版本未检查'
    }[semanticDecision]||'';
    var semanticReasons=Array.isArray(semantic.reasons)?semantic.reasons:[];
    var semanticNotice=semanticLabel?
      '<section class="nc-sdv-semantic" data-decision="'+escapeHtml(semanticDecision)+'">'+
      '<strong>'+escapeHtml(semanticLabel)+'</strong>'+
      (semantic.mode==='shadow'?'<span>当前为观察模式，不阻断版本锁定。</span>':'')+
      (semanticDecision==='manual_review'?
        '<span>检查服务暂时无法给出结论，建议人工确认后使用；当前不阻断版本锁定。</span>':'')+
      (semanticReasons.length?'<ul>'+semanticReasons.map(function(reason){
        return '<li>'+escapeHtml(reason)+'</li>';
      }).join('')+'</ul>':'')+'</section>':'';
    var active=shot.job&&ACTIVE[text(shot.job.status)];
    var budget=Math.max(0,state.point_budget-state.spent_points-state.reserved_points);
    return '<section class="nc-sdv-workspace" data-stage="'+escapeHtml(state.stage)+'">'+
      '<aside class="nc-sdv-rail"><header><span>C-3 镜头队列</span><h2>电影化身视频</h2>'+
      '<small>'+state.shots.length+' 镜头 · '+escapeHtml(state.ratio)+'</small></header>'+
      '<div class="nc-sdv-shot-list">'+rail+'</div><button type="button" data-action="batch-generate"'+
      (readonly||state.operationBusy?' disabled':'')+'>批量生成未完成镜头</button></aside>'+
      '<main class="nc-sdv-preview"><header><div><span>'+escapeHtml(shot.shot_key)+'</span>'+
      '<h2>成片预览</h2></div><strong>'+escapeHtml(statusLabel(shot.status))+'</strong></header>'+
      renderCast(state,readonly)+player+
      '<section class="nc-sdv-versions"><h3>生成版本</h3><div>'+versions+'</div></section></main>'+
      '<aside class="nc-sdv-console"><header><span>C-3 控制台</span><h2>生成与确认</h2></header>'+
      '<dl><div><dt>项目预算</dt><dd>'+budget+' 点</dd></div><div><dt>已支出</dt><dd>'+
      state.spent_points+' 点</dd></div><div><dt>处理中</dt><dd>'+state.reserved_points+' 点</dd></div></dl>'+
      '<label>画面提示词<textarea data-field="prompt"'+(readonly||shot.locked?' disabled':'')+'>'+
      escapeHtml(shot.prompt)+'</textarea></label>'+
      '<p class="nc-sdv-prompt-note">系统会结合锁定的角色、场景、动作和镜头自动编译提示词；'+
      '人物对白、模型原声及供应商二次改写均已关闭。明确引用角色时请使用 @角色名。</p>'+
      '<div class="nc-sdv-meta"><span>开放模式</span><span>'+shot.avatar_ids.length+
      ' 个电影化身</span><span>'+shot.duration+' 秒</span></div>'+semanticNotice+blockers+
      (active?'<p class="nc-sdv-progress">任务正在'+escapeHtml(statusLabel(shot.job.status))+
        '，页面会自动恢复和轮询，不会重复提交。</p>':'')+
      (state.error?'<p class="nc-sdv-error">'+escapeHtml(state.error)+'</p>':'')+
      '<div class="nc-sdv-actions"><button type="button" data-action="generate"'+
      (readonly||shot.locked||active||state.operationBusy?' disabled':'')+'>询价并生成</button>'+
      '<button type="button" data-action="toggle-lock"'+
      (readonly||active||state.operationBusy||(!shot.locked&&!shot.lockable)?' disabled':'')+'>'+
      (shot.locked?'解除锁定':'锁定当前版本')+'</button>'+
      '<button type="button" class="is-primary" data-action="confirm-stage"'+
      (readonly||state.handoff_blocked||state.operationBusy?' disabled':'')+
      '>确认视频并进入合成</button></div></aside></section>';
  }
  function createWorkspace(options){
    options=options||{};
    var client=options.client,host=options.host,snapshot=null,destroyed=false;
    var requestGeneration=0,pollTimer=null,pollFailures=0;
    var ui={busy:true,operationBusy:false,error:'',selectedShotId:'',enhancePrompt:false,
      avatars:[],avatarBusy:false,canCreateAvatar:false,
      castSelections:{},castBaseline:{},castDirtyKeys:{},castConflicts:{},castDirty:false};
    if(!client||typeof client.json!=='function') throw new Error('视频工作区缺少已认证 API 客户端');
    function viewOptions(){
      return {
        busy:ui.busy,operationBusy:ui.operationBusy,error:ui.error,
        selectedShotId:ui.selectedShotId,enhancePrompt:ui.enhancePrompt,
        canEdit:options.canEdit!==false,avatars:ui.avatars,avatarBusy:ui.avatarBusy,
        canCreateAvatar:ui.canCreateAvatar,
        castSelections:ui.castSelections,castDirty:ui.castDirty,
        castConflicts:ui.castConflicts
      };
    }
    function state(){ return normalizeState(snapshot||{},viewOptions()); }
    function castMapFromSnapshot(){
      var result={};
      ((snapshot&&snapshot.cast_characters)||[]).forEach(function(character){
        var key=text(character.character_key);
        if(key&&character.avatar_id!=null){
          result[key]=number(character.avatar_id,0);
        }
      });
      return result;
    }
    function castValue(map,key){
      return Object.prototype.hasOwnProperty.call(map,key)?number(map[key],0):0;
    }
    function recalculateCastDirty(){
      var keys=Object.keys(ui.castBaseline).concat(Object.keys(ui.castSelections))
        .filter(function(key,index,list){return list.indexOf(key)===index;});
      ui.castDirtyKeys={};
      keys.forEach(function(key){
        if(castValue(ui.castBaseline,key)!==castValue(ui.castSelections,key)){
          ui.castDirtyKeys[key]=true;
        }
      });
      ui.castDirty=Object.keys(ui.castDirtyKeys).length>0;
    }
    function syncCastFromSnapshot(force){
      var remote=castMapFromSnapshot();
      if(force||!ui.castDirty){
        ui.castSelections=clone(remote);
        ui.castBaseline=clone(remote);
        ui.castDirtyKeys={};
        ui.castConflicts={};
        ui.castDirty=false;
        return;
      }
      var oldBaseline=ui.castBaseline;
      var validKeys={};
      ((snapshot&&snapshot.cast_characters)||[]).forEach(function(character){
        validKeys[text(character.character_key)]=true;
      });
      Object.keys(ui.castSelections).forEach(function(key){
        if(!validKeys[key]) delete ui.castSelections[key];
      });
      Object.keys(ui.castDirtyKeys).forEach(function(key){
        if(!validKeys[key]) delete ui.castDirtyKeys[key];
      });
      Object.keys(ui.castConflicts).forEach(function(key){
        if(!validKeys[key]) delete ui.castConflicts[key];
      });
      Object.keys(validKeys).forEach(function(key){
        if(ui.castDirtyKeys[key]){
          if(castValue(oldBaseline,key)!==castValue(remote,key)
              && castValue(ui.castSelections,key)!==castValue(remote,key)){
            ui.castConflicts[key]=true;
          }
          return;
        }
        if(castValue(remote,key)>0) ui.castSelections[key]=castValue(remote,key);
        else delete ui.castSelections[key];
      });
      ui.castBaseline=clone(remote);
      recalculateCastDirty();
      Object.keys(ui.castConflicts).forEach(function(key){
        if(!ui.castDirtyKeys[key]) delete ui.castConflicts[key];
      });
    }
    function render(){
      var html=renderWorkspace(snapshot||{},viewOptions());
      if(host&&!destroyed) host.innerHTML=html;
      return html;
    }
    function scopedJson(path,requestOptions){
      var request=requestOptions?Object.assign({},requestOptions):{};
      if(options.boardId){
        request.headers=Object.assign({},request.headers||{},{
          'X-Canvas-Board-Id':String(options.boardId)
        });
      }
      return client.json(path,request);
    }
    function avatarPath(){
      return AVATAR_PATH+'?project_id='+encodeURIComponent(options.projectId);
    }
    function notify(){
      if(typeof options.onChange!=='function'||!snapshot) return Promise.resolve(snapshot);
      return Promise.resolve(options.onChange({
        project_id:snapshot.project_id,revision:snapshot.revision,
        stage:snapshot.stage,ratio:snapshot.ratio,
        spent_points:snapshot.spent_points,reserved_points:snapshot.reserved_points,
        point_budget:snapshot.point_budget
      })).then(function(){return snapshot;});
    }
    function clearPoll(){
      if(pollTimer!=null&&typeof clearTimeout==='function') clearTimeout(pollTimer);
      pollTimer=null;
    }
    function hasActive(){
      return state().shots.some(function(shot){ return shot.job&&ACTIVE[text(shot.job.status)]; });
    }
    function schedulePoll(){
      clearPoll();
      if(destroyed||!hasActive()||typeof setTimeout!=='function') return;
      var delay=Math.min(12000,POLL_BASE*Math.pow(1.7,pollFailures));
      pollTimer=setTimeout(function(){
        pollTimer=null;
        reload({quiet:true}).then(function(){ pollFailures=0;schedulePoll(); })
          .catch(function(){ pollFailures+=1;schedulePoll(); });
      },delay);
    }
    function reload(config){
      config=config||{};
      if(destroyed) return Promise.resolve(null);
      var generation=++requestGeneration;
      if(!config.quiet){ui.busy=true;ui.error='';render();}
      return Promise.all([
        Promise.resolve(scopedJson(
          VIDEO_PATH+'?project_id='+encodeURIComponent(options.projectId)
        )),
        (options.canEdit===false?Promise.resolve({items:[],can_create_avatar:false}):
          Promise.resolve(scopedJson(avatarPath())).catch(function(){return {items:[]};}))
      ]).then(function(results){
        if(destroyed||generation!==requestGeneration) return null;
        var result=results[0],avatarResult=results[1];
        snapshot=result&&typeof result==='object'?result:{};
        ui.avatars=Array.isArray(avatarResult&&avatarResult.items)?avatarResult.items:[];
        ui.canCreateAvatar=avatarResult&&avatarResult.can_create_avatar===true;
        syncCastFromSnapshot();
        ui.busy=false;ui.error='';
        if(!ui.selectedShotId||!state().shots.some(function(s){return s.id===ui.selectedShotId;})){
          ui.selectedShotId=state().shots[0]&&state().shots[0].id||'';
        }
        render();schedulePoll();
        return notify();
      }).catch(function(error){
        if(destroyed||generation!==requestGeneration) return null;
        ui.busy=false;ui.error=text(error&&error.message||error);render();
        if(typeof options.onError==='function') options.onError(error);
        throw error;
      });
    }
    function refreshAvatars(){
      if(destroyed||ui.avatarBusy||options.canEdit===false) return Promise.resolve(null);
      ui.avatarBusy=true;ui.error='';render();
      return Promise.resolve(scopedJson(avatarPath())).then(function(result){
        if(destroyed) return null;
        ui.avatarBusy=false;
        ui.avatars=Array.isArray(result&&result.items)?result.items:[];
        ui.canCreateAvatar=result&&result.can_create_avatar===true;
        render();
        return ui.avatars;
      }).catch(function(error){
        if(destroyed) return null;
        ui.avatarBusy=false;ui.error=text(error&&error.message||error);render();
        throw error;
      });
    }
    function setCastSelection(characterKey,avatarId){
      characterKey=text(characterKey);
      if(!characterKey) return;
      var next=number(avatarId,0);
      if(next>0) ui.castSelections[characterKey]=next;
      else delete ui.castSelections[characterKey];
      delete ui.castConflicts[characterKey];
      recalculateCastDirty();
      render();
    }
    function keepLocalCast(){
      ui.castConflicts={};
      render();
    }
    function reloadCast(){
      syncCastFromSnapshot(true);
      render();
    }
    function saveCast(){
      if(destroyed||!snapshot||!ui.castDirty) return Promise.resolve(null);
      var bindings=Object.keys(ui.castSelections).sort().filter(function(key){
        return number(ui.castSelections[key],0)>0;
      }).map(function(key){
        return {character_key:key,avatar_id:number(ui.castSelections[key],0)};
      });
      return call(CAST_PATH,{
        project_id:snapshot.project_id,revision:snapshot.revision,bindings:bindings
      });
    }
    function call(path,body,headers){
      if(destroyed||ui.operationBusy) return Promise.resolve(null);
      ui.operationBusy=true;ui.error='';render();
      return Promise.resolve(scopedJson(path,{
        method:'POST',headers:Object.assign({'Content-Type':'application/json'},headers||{}),
        body:body
      })).then(function(result){
        if(destroyed) return null;
        ui.operationBusy=false;
        if(result&&Array.isArray(result.shots)){
          snapshot=result;syncCastFromSnapshot(true);render();schedulePoll();return notify();
        }
        return reload();
      }).catch(function(error){
        if(destroyed) return null;
        ui.operationBusy=false;ui.error=text(error&&error.message||error);render();
        if(typeof options.onError==='function') options.onError(error);
        throw error;
      });
    }
    function operationBody(shot){
      return {
        project_id:snapshot.project_id,revision:snapshot.revision,
        shot_id:shot.id,video_revision:shot.video_revision
      };
    }
    function generate(shot,prompt){
      var quoteBody=operationBody(shot);
      quoteBody.prompt=prompt;quoteBody.enhance_prompt=ui.enhancePrompt;
      ui.operationBusy=true;ui.error='';render();
      return Promise.resolve(scopedJson(QUOTE_PATH,{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:quoteBody
      })).then(function(quote){
        var accepted=typeof options.confirm==='function'?
          options.confirm(number(quote.total_cost,0),Object.assign({kind:'video'},quote),quoteBody):true;
        return Promise.resolve(accepted).then(function(ok){
          if(!ok){ui.operationBusy=false;render();return null;}
          var key='sdv-'+snapshot.project_id+'-'+shot.id+'-'+Date.now()+'-'+
            Math.random().toString(16).slice(2);
          return scopedJson(GENERATE_PATH,{
            method:'POST',
            headers:{'Content-Type':'application/json','Idempotency-Key':key},
            body:{
              project_id:snapshot.project_id,revision:snapshot.revision,
              shot_id:shot.id,video_revision:shot.video_revision,
              quote_token:quote.quote_token
            }
          }).then(function(){ui.operationBusy=false;return reload();});
        });
      }).catch(function(error){
        ui.operationBusy=false;ui.error=text(error&&error.message||error);render();
        if(typeof options.onError==='function') options.onError(error);
        throw error;
      });
    }
    function onClick(event){
      var target=event&&event.target;
      while(target&&target!==host&&!(target.getAttribute&&target.getAttribute('data-action'))){
        target=target.parentNode;
      }
      if(!target||target===host) return;
      var action=target.getAttribute('data-action'),current=selectedShot(state());
      if(action==='reload') reload().catch(function(){});
      if(action==='refresh-avatars') refreshAvatars().catch(function(){});
      if(action==='create-avatar'){
        var creatorUrl='/workbench/video.html?function=cinematic&action=create-avatar';
        if(typeof options.openAvatarCreator==='function') options.openAvatarCreator(creatorUrl);
        else if(typeof window!=='undefined'&&typeof window.open==='function'){
          if(typeof window.addEventListener==='function'){
            window.addEventListener('focus',function(){
              refreshAvatars().catch(function(){});
            },{once:true});
          }
          window.open(creatorUrl,'_blank','noopener');
        }
      }
      if(action==='keep-local-cast') keepLocalCast();
      if(action==='reload-cast') reloadCast();
      if(action==='save-cast') saveCast().catch(function(){});
      if(action==='select-shot'){
        ui.selectedShotId=text(target.getAttribute('data-shot-id'));ui.error='';render();
      }
      if(!current) return;
      if(action==='generate'){
        var promptNode=host&&host.querySelector('[data-field="prompt"]');
        generate(current,text(promptNode&&promptNode.value)).catch(function(){});
      }
      if(action==='select-version'){
        var body=operationBody(current);body.version=number(target.getAttribute('data-version'),0);
        call(SELECT_PATH,body).catch(function(){});
      }
      if(action==='toggle-lock'){
        var lockBody=operationBody(current);lockBody.lock=!current.locked;
        call(LOCK_PATH,lockBody).catch(function(){});
      }
      if(action==='confirm-stage'){
        call(CONFIRM_PATH,{
          project_id:snapshot.project_id,revision:snapshot.revision,stage:'video_review'
        }).catch(function(){});
      }
      if(action==='batch-generate'){
        var queue=state().shots.filter(function(shot){
          return !shot.locked&&!shot.current&&!(shot.job&&ACTIVE[text(shot.job.status)])&&
            !shot.blockers.some(function(item){return item.code!=='missing_current_version';});
        });
        var chain=Promise.resolve();
        queue.forEach(function(shot){
          chain=chain.then(function(){return generate(shot,shot.prompt);});
        });
        chain.catch(function(){});
      }
    }
    function onChange(event){
      var target=event&&event.target;
      if(target&&target.getAttribute&&target.getAttribute('data-field')==='enhance'){
        ui.enhancePrompt=!!target.checked;
      }
      if(target&&target.getAttribute&&target.getAttribute('data-field')==='cast'){
        setCastSelection(
          target.getAttribute('data-character-key'),target.value
        );
      }
    }
    function onTimeUpdate(event){
      var player=event&&event.target;
      if(!player||!player.matches||!player.matches('[data-video-player]')) return;
      var overlay=host&&host.querySelector('[data-subtitle-overlay]');
      var shot=selectedShot(state());
      if(!shot) return;
      var ms=number(player.currentTime,0)*1000,active=(shot.voice_tracks||[]).filter(function(line){
        return line.subtitle_visible!==false&&ms>=number(line.start_ms,0)&&ms<=number(line.end_ms,0);
      })[0];
      if(overlay){
        overlay.textContent=active?text(active.subtitle_text):'';
        overlay.hidden=!active;
      }
      if(!host||typeof host.querySelectorAll!=='function') return;
      Array.prototype.forEach.call(host.querySelectorAll('[data-voice-track]'),function(audio){
        var start=number(audio.getAttribute('data-start-ms'),0);
        var end=number(audio.getAttribute('data-end-ms'),0);
        var isActive=ms>=start&&ms<=end;
        if(!isActive||player.paused){
          if(typeof audio.pause==='function') audio.pause();
          return;
        }
        var desired=Math.max(0,(ms-start)/1000);
        if(Math.abs(number(audio.currentTime,0)-desired)>.35) audio.currentTime=desired;
        if(typeof audio.play==='function'){
          var playing=audio.play();
          if(playing&&typeof playing.catch==='function') playing.catch(function(){});
        }
      });
    }
    if(host&&typeof host.addEventListener==='function'){
      host.addEventListener('click',onClick);
      host.addEventListener('change',onChange);
      host.addEventListener('timeupdate',onTimeUpdate,true);
    }
    render();
    var ready=reload();
    return {
      projectId:options.projectId,ready:ready,render:render,reload:reload,
      refreshAvatars:refreshAvatars,setCastSelection:setCastSelection,saveCast:saveCast,
      keepLocalCast:keepLocalCast,reloadCast:reloadCast,
      getState:function(){return clone(state());},
      destroy:function(){
        destroyed=true;requestGeneration+=1;clearPoll();
        if(host&&typeof host.removeEventListener==='function'){
          host.removeEventListener('click',onClick);
          host.removeEventListener('change',onChange);
          host.removeEventListener('timeupdate',onTimeUpdate,true);
        }
        host=null;snapshot=null;
      }
    };
  }
  return {normalizeState:normalizeState,renderWorkspace:renderWorkspace,createWorkspace:createWorkspace};
});
