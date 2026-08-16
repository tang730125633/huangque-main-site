(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaStore=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function text(value){ return String(value==null?'':value); }
  function number(value,fallback){
    var result=Number(value);
    return isFinite(result)?result:(fallback==null?0:fallback);
  }
  function clone(value){
    if(Array.isArray(value)) return value.map(clone);
    if(value&&typeof value==='object'){
      var result={};
      Object.keys(value).forEach(function(key){ result[key]=clone(value[key]); });
      return result;
    }
    return value;
  }
  function normalizeBlocker(value){
    value=value&&typeof value==='object'?value:{};
    return {
      code:text(value.code||'unknown'),
      message:text(value.message||value.detail||value.code||'状态未知'),
      shot_id:value.shot_id==null?null:text(value.shot_id),
      line_id:value.line_id==null?null:text(value.line_id)
    };
  }
  function normalizeVersion(value){
    value=value&&typeof value==='object'?value:{};
    return {
      id:text(value.id||value.job_id||''),
      kind:value.kind==='final'?'final':'preview',
      version:number(value.version,0),
      job_id:text(value.job_id),
      status:text(value.status||'unknown'),
      phase:text(value.phase),
      url:text(value.url),
      cover_url:text(value.cover_url),
      asset_id:value.asset_id==null?null:text(value.asset_id),
      duration_ms:value.duration_ms==null?null:number(value.duration_ms,0),
      width:value.width==null?null:number(value.width,0),
      height:value.height==null?null:number(value.height,0),
      created_at:number(value.created_at,0),
      created_by:text(value.created_by),
      cost:number(value.cost,0),
      error_code:text(value.error_code),
      error_message:text(value.error_message)
    };
  }
  function normalizeWorkspace(input){
    input=input&&typeof input==='object'?input:{};
    var versions=(Array.isArray(input.versions)?input.versions:[])
      .map(normalizeVersion)
      .sort(function(left,right){
        return right.created_at-left.created_at||
          right.version-left.version||
          left.kind.localeCompare(right.kind);
      });
    var shots=(Array.isArray(input.shots)?input.shots:[])
      .map(function(value,index){
        value=value&&typeof value==='object'?value:{};
        var voice=value.voice&&typeof value.voice==='object'?value.voice:{};
        var video=value.video&&typeof value.video==='object'?value.video:{};
        return {
          id:text(value.id),
          shot_key:text(value.shot_key||('镜头 '+(index+1))),
          sort_order:number(value.sort_order,index),
          duration:number(value.duration,0),
          ready:value.ready===true,
          voice:{
            locked:voice.locked===true,
            status:text(voice.status||'blocked'),
            timeline_revision:voice.timeline_revision==null?null:
              number(voice.timeline_revision,0),
            lines:Array.isArray(voice.lines)?clone(voice.lines):[]
          },
          video:{
            confirmed:video.confirmed===true,
            status:text(video.status||'blocked'),
            current_version:video.current_version==null?null:
              number(video.current_version,0),
            video_revision:video.video_revision==null?null:
              number(video.video_revision,0)
          },
          blockers:(Array.isArray(value.blockers)?value.blockers:[])
            .map(normalizeBlocker)
        };
      }).sort(function(left,right){ return left.sort_order-right.sort_order; });
    var actions=input.actions&&typeof input.actions==='object'?input.actions:{};
    var readiness=input.readiness&&typeof input.readiness==='object'?
      input.readiness:{};
    var config=input.config&&typeof input.config==='object'?clone(input.config):{};
    config.subtitle=config.subtitle&&typeof config.subtitle==='object'?
      config.subtitle:{enabled:true,preset:'white_outline',position:'bottom'};
    config.bgm=config.bgm&&typeof config.bgm==='object'?
      config.bgm:{asset_id:null,volume:0.18,fade_in_ms:500,fade_out_ms:800};
    config.sound_cues=(Array.isArray(config.sound_cues)?config.sound_cues:[])
      .filter(function(item){ return item&&typeof item==='object'; })
      .map(function(item){ return clone(item); });
    return {
      project_id:text(input.project_id),
      revision:number(input.revision,0),
      stage:text(input.stage||'assembly_review'),
      ratio:text(input.ratio||'9:16'),
      target_duration:number(input.target_duration,0),
      assembly_revision:number(input.assembly_revision,1),
      current_preview_version:input.current_preview_version==null?null:
        number(input.current_preview_version,0),
      current_final_version:input.current_final_version==null?null:
        number(input.current_final_version,0),
      preview_locked:input.preview_locked===true,
      implementation_status:text(input.implementation_status),
      rendering_enabled:input.rendering_enabled===true,
      input_hash:input.input_hash==null?null:text(input.input_hash),
      media_plan:input.media_plan&&typeof input.media_plan==='object'?
        clone(input.media_plan):null,
      audio_subtitle:input.audio_subtitle&&
        typeof input.audio_subtitle==='object'?clone(input.audio_subtitle):{},
      config:config,
      shots:shots,
      versions:versions,
      active_job:input.active_job&&typeof input.active_job==='object'?
        clone(input.active_job):null,
      latest_job:input.latest_job&&typeof input.latest_job==='object'?
        clone(input.latest_job):null,
      completion:input.completion&&typeof input.completion==='object'?
        clone(input.completion):null,
      playback:input.playback&&typeof input.playback==='object'?
        clone(input.playback):{
          current_version:null,versions:[],active_job:null,
          subtitle_toggle_supported:false
        },
      readiness:{
        ready:readiness.ready===true,
        blockers:(Array.isArray(readiness.blockers)?readiness.blockers:[])
          .map(normalizeBlocker)
      },
      actions:{
        can_save_config:actions.can_save_config===true,
        can_preview:actions.can_preview===true,
        can_lock_preview:actions.can_lock_preview===true,
        can_export:actions.can_export===true,
        can_confirm:actions.can_confirm===true
      },
      blockers:(Array.isArray(input.blockers)?input.blockers:[])
        .map(normalizeBlocker)
    };
  }
  function findVersion(workspace,selection){
    if(!workspace) return null;
    return workspace.versions.filter(function(item){
      return selection.versionId&&item.id===selection.versionId;
    })[0]||null;
  }
  function defaultVersion(workspace){
    var finalCurrent=workspace.versions.filter(function(item){
      return item.kind==='final'&&
        item.version===workspace.current_final_version&&
        item.status==='succeeded';
    })[0];
    if(finalCurrent) return finalCurrent;
    var previewCurrent=workspace.versions.filter(function(item){
      return item.kind==='preview'&&
        item.version===workspace.current_preview_version&&
        item.status==='succeeded';
    })[0];
    if(previewCurrent) return previewCurrent;
    return null;
  }
  function isCurrentVersion(workspace,version){
    if(!workspace||!version) return false;
    return version.kind==='final'?
      version.version===workspace.current_final_version:
      version.version===workspace.current_preview_version;
  }
  function createStore(options){
    options=options||{};
    var listeners=[];
    var state={
      context:{
        projectId:text(options.projectId),
        boardId:text(options.boardId),
        canEdit:options.canEdit!==false,
        routeGeneration:0
      },
      project:clone(options.project||{}),
      workspace:null,
      selection:{
        shotId:text(options.shotId),
        versionId:text(options.versionId),
        filter:'all'
      },
      draft:{
        baseRevision:null,
        baseAssemblyRevision:null,
        values:{},
        dirty:false,
        saving:false,
        validation:{}
      },
      ui:{
        loading:true,
        error:'',
        reconnecting:false,
        busyAction:'',
        leftOpen:false,
        rightOpen:false,
        dialog:null,
        toast:'',
        lastUpdatedAt:0
      }
    };
    function emit(){
      listeners.slice().forEach(function(listener){ listener(getState()); });
    }
    function getState(){ return clone(state); }
    function setWorkspace(input){
      var workspace=normalizeWorkspace(input);
      if(state.context.projectId&&workspace.project_id&&
          workspace.project_id!==state.context.projectId){
        return false;
      }
      state.workspace=workspace;
      state.context.routeGeneration+=1;
      var selected=findVersion(workspace,state.selection);
      if(selected&&selected.status!=='succeeded'&&
          !isCurrentVersion(workspace,selected)){
        state.selection.versionId='';
        selected=null;
      }
      if(!selected){
        var version=defaultVersion(workspace);
        state.selection.versionId=version?version.id:'';
      }
      if(!workspace.shots.some(function(shot){
        return shot.id===state.selection.shotId;
      })){
        state.selection.shotId=workspace.shots[0]?workspace.shots[0].id:'';
      }
      state.draft.baseRevision=workspace.revision;
      state.draft.baseAssemblyRevision=workspace.assembly_revision;
      state.draft.values=clone(workspace.config);
      state.draft.dirty=false;
      state.draft.saving=false;
      state.ui.loading=false;
      state.ui.error='';
      state.ui.reconnecting=false;
      state.ui.busyAction='';
      state.ui.lastUpdatedAt=Date.now();
      emit();
      return true;
    }
    function patchUi(patch){
      Object.keys(patch||{}).forEach(function(key){ state.ui[key]=patch[key]; });
      emit();
    }
    function selectShot(id){
      state.selection.shotId=text(id);emit();
    }
    function selectVersion(id){
      state.selection.versionId=text(id);emit();
    }
    function setFilter(filter){
      state.selection.filter=['all','pending','running','failed','unlocked']
        .indexOf(filter)>=0?filter:'all';
      emit();
    }
    function viewedVersion(){
      return findVersion(state.workspace,state.selection);
    }
    function selectors(){
      var workspace=state.workspace;
      var version=viewedVersion();
      var completed=!!workspace&&workspace.stage==='completed';
      var historyOnly=!!version&&!isCurrentVersion(workspace,version);
      return {
        version:clone(version),
        currentVersion:isCurrentVersion(workspace,version),
        historyOnly:historyOnly,
        readOnly:!state.context.canEdit||completed||historyOnly,
        completed:completed,
        canEdit:state.context.canEdit&&!completed&&!historyOnly
      };
    }
    function subscribe(listener){
      if(typeof listener!=='function') return function(){};
      listeners.push(listener);
      return function(){
        listeners=listeners.filter(function(item){ return item!==listener; });
      };
    }
    function destroy(){ listeners=[];state.workspace=null; }
    return {
      getState:getState,
      setWorkspace:setWorkspace,
      patchUi:patchUi,
      selectShot:selectShot,
      selectVersion:selectVersion,
      setFilter:setFilter,
      selectors:selectors,
      subscribe:subscribe,
      destroy:destroy
    };
  }
  return {
    clone:clone,
    normalizeWorkspace:normalizeWorkspace,
    isCurrentVersion:isCurrentVersion,
    createStore:createStore
  };
});
