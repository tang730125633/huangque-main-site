(function(root,factory){
  var api=factory(root);
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.shortDrama=api; }
})(typeof window!=='undefined'?window:null,function(root){
  'use strict';
  var STAGES=['draft','characters_review','script_review','storyboard_review','stills_review',
    'voice_review','video_review','assembly_review','completed'];
  var PRODUCTION_STAGE_INDEX=STAGES.indexOf('stills_review');
  var MAX_CHARACTERS=20,MAX_DIALOGUE_LINES=120;
  var PLACEHOLDER_SYNOPSIS='请在短剧工作区完善故事梗概';
  var STAGE_LABELS={
    settings:'项目设置',characters_review:'角色确认',script_review:'剧本确认',
    storyboard_review:'分镜确认',stills_review:'画面确认',voice_review:'配音字幕',
    video_review:'视频确认',assembly_review:'成片确认',completed:'已交付'
  };

  function stageIndex(stage){ return Math.max(0,STAGES.indexOf(stage)); }
  function stageProgress(stage){ return Math.round(stageIndex(stage)*100/(STAGES.length-1)); }
  function isProductionStage(stage){ return STAGES.indexOf(stage)>=PRODUCTION_STAGE_INDEX; }

  function validShotCounts(duration){
    duration=Number(duration);
    return [6,7,8,9,10].filter(function(count){
      return 5*count<=duration&&duration<=10*count;
    });
  }

  function normalizeSettings(input){
    var value=Object.assign({
      ratio:'9:16',target_duration:30,shot_count:6,
      visual_style:'电影写实',target_platform:'抖音'
    },input||{});
    if(value.ratio!=='9:16'&&value.ratio!=='16:9') value.ratio='9:16';
    if([30,45,60].indexOf(Number(value.target_duration))<0) value.target_duration=30;
    else value.target_duration=Number(value.target_duration);
    var shotCount=Number(value.shot_count);
    if(!isFinite(shotCount)||Math.floor(shotCount)!==shotCount) shotCount=6;
    shotCount=Math.max(6,Math.min(10,shotCount));
    var allowed=validShotCounts(value.target_duration);
    value.shot_count=allowed.indexOf(shotCount)>=0?shotCount:allowed[0];
    return value;
  }

  function summarizeProject(project){
    project=project||{};
    return normalizeNodeParams({
      project_id:project.id||project.project_id||null,
      title:project.title||'新短剧',ratio:project.ratio||'9:16',
      target_duration:project.target_duration||30,stage:project.stage||'draft',
      progress:project.progress==null?stageProgress(project.stage):project.progress,
      spent_points:project.spent_points||0,estimated_points:project.estimated_points||0
    });
  }

  function normalizeNodeParams(input){
    var value=input||{}, duration=Number(value.target_duration);
    if([30,45,60].indexOf(duration)<0) duration=30;
    return {
      project_id:value.project_id||value.id||null,
      title:String(value.title||'新短剧').slice(0,80),
      ratio:value.ratio==='16:9'?'16:9':'9:16',
      target_duration:duration,
      stage:String(value.stage||'draft'),
      progress:Math.max(0,Math.min(100,Number(value.progress)||0)),
      spent_points:Math.max(0,Number(value.spent_points)||0),
      estimated_points:Math.max(0,Number(value.estimated_points)||0)
    };
  }

  function cloneValue(value){
    if(Array.isArray(value)) return value.map(cloneValue);
    if(value&&typeof value==='object'){
      var copy={};
      Object.keys(value).forEach(function(key){ copy[key]=cloneValue(value[key]); });
      return copy;
    }
    return value;
  }

  function dialogueClientToken(){
    if(root&&root.crypto&&typeof root.crypto.randomUUID==='function'){
      return root.crypto.randomUUID();
    }
    return 'dialogue-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,12);
  }

  function sanitizeNodeData(node){
    var copy=cloneValue(node||{});
    if(copy.type==='shortDrama'){
      copy.params=normalizeNodeParams(copy.params);
      copy.outputs={};
    }
    return copy;
  }

  function creationPayload(params){
    var summary=normalizeNodeParams(params);
    return {
      title:summary.title,
      synopsis:PLACEHOLDER_SYNOPSIS,
      ratio:summary.ratio,
      target_duration:summary.target_duration,
      shot_count:6
    };
  }

  function canOpenNode(params,canEdit){
    return !!(params&&params.project_id)||!!canEdit;
  }

  function createProjectCoordinator(options){
    options=options||{};
    if(typeof options.getNode!=='function'||typeof options.create!=='function'||typeof options.apply!=='function'){
      throw new Error('short drama project coordinator requires getNode, create, and apply methods');
    }
    var pending=Object.create(null);
    var completed=Object.create(null);
    var discardedScopes=Object.create(null);
    function itemKey(scopeKey,nodeId){ return JSON.stringify([String(scopeKey||''),String(nodeId||'')]); }
    function linkedProject(node){ return node&&node.params&&node.params.project_id||null; }
    function scopeHasPending(scopeKey){
      return Object.keys(pending).some(function(key){ return pending[key].scopeKey===scopeKey; });
    }
    function consume(scopeKey,nodeId,entry){
      var key=itemKey(scopeKey,nodeId), live=options.getNode(scopeKey,nodeId);
      if(!live) return entry.projectId;
      var linked=linkedProject(live);
      if(linked!==entry.expectedProjectId){
        delete completed[key];
        return linked||entry.projectId;
      }
      options.apply(live,entry.project);
      delete completed[key];
      return entry.projectId;
    }
    function ensure(scopeKey,nodeId,payload,canCreate,expectedProjectId){
      scopeKey=String(scopeKey||'');
      nodeId=String(nodeId||'');
      expectedProjectId=expectedProjectId||null;
      var key=itemKey(scopeKey,nodeId);
      if(pending[key]) return pending[key].promise;
      if(completed[key]) return Promise.resolve(consume(scopeKey,nodeId,completed[key]));
      var current=options.getNode(scopeKey,nodeId);
      var projectId=linkedProject(current);
      if(projectId) return Promise.resolve(projectId);
      if(!canCreate) return Promise.reject(new Error('当前画布为只读，无法创建短剧项目'));
      var request=Promise.resolve().then(function(){ return options.create(payload); }).then(function(project){
        var createdId=project&&(project.id||project.project_id);
        if(!createdId) throw new Error('创建短剧项目失败');
        if(discardedScopes[scopeKey]) return createdId;
        var entry={scopeKey:scopeKey,nodeId:nodeId,projectId:createdId,expectedProjectId:expectedProjectId,project:project};
        completed[key]=entry;
        return consume(scopeKey,nodeId,entry);
      });
      pending[key]={scopeKey:scopeKey,promise:request};
      function clear(){
        if(pending[key]&&pending[key].promise===request) delete pending[key];
        if(discardedScopes[scopeKey]&&!scopeHasPending(scopeKey)) delete discardedScopes[scopeKey];
      }
      request.then(clear,clear);
      return request;
    }
    function cleanupScope(scopeKey){
      scopeKey=String(scopeKey||'');
      Object.keys(completed).forEach(function(key){ if(completed[key].scopeKey===scopeKey) delete completed[key]; });
      if(scopeHasPending(scopeKey)) discardedScopes[scopeKey]=true;
    }
    return {
      ensure:ensure,
      cleanupScope:cleanupScope,
      hasPending:function(scopeKey,nodeId){ return !!pending[itemKey(scopeKey,nodeId)]; },
      hasCompleted:function(scopeKey,nodeId){ return !!completed[itemKey(scopeKey,nodeId)]; }
    };
  }

  function planningPayload(project){
    var settings=normalizeSettings(project);
    return {
      format:'short_drama',
      project_id:project&&project.id,
      project_revision:project&&project.revision,
      prompt:settings.synopsis||'',
      dur:String(settings.target_duration)+'s',
      ratio:settings.ratio,
      shot_count:settings.shot_count,
      style:settings.visual_style,
      platform:settings.target_platform
    };
  }

  function projectPath(id){ return '/api/gen/short-drama/project?id='+encodeURIComponent(id); }

  function jobError(data){
    var error=new Error(data&&data.error||data&&data.detail||'短剧策划生成失败');
    error.code=data&&data.code||'job_failed';
    error.data=data||null;
    return error;
  }

  function parseJobResult(data){
    var result=data&&data.result;
    return typeof result==='string'?JSON.parse(result):result;
  }

  function createClient(apiClient,pollFn,boardId){
    pollFn=pollFn||apiClient&&apiClient.poll;
    if(!apiClient||typeof apiClient.json!=='function'||typeof pollFn!=='function'){
      throw new Error('short drama client requires json and poll methods');
    }
    function json(path,requestOptions){
      if(!boardId&&!requestOptions) return apiClient.json(path);
      var scoped=requestOptions?Object.assign({},requestOptions):{};
      if(boardId) scoped.headers=Object.assign({},scoped.headers||{}, {'X-Canvas-Board-Id':String(boardId)});
      return apiClient.json(path,scoped);
    }
    function applyPlan(projectId,revision,jobId){
      return json('/api/gen/short-drama/apply-plan',{
        method:'POST',body:{project_id:projectId,revision:revision,job_id:jobId}
      });
    }
    return {
      list:function(page,pageSize){
        page=page==null?1:Number(page);pageSize=pageSize==null?20:Number(pageSize);
        return json('/api/gen/short-drama/projects?page='+encodeURIComponent(page)+'&page_size='+encodeURIComponent(pageSize));
      },
      get:function(projectId){ return json(projectPath(projectId)); },
      getAvatarCandidates:function(projectId,force){
        var path='/api/gen/short-drama/avatar-candidates?project_id='+
          encodeURIComponent(projectId);
        if(force) path+='&refresh='+encodeURIComponent(Date.now());
        return json(path).catch(function(error){
          if(!error||Number(error.status)!==404) throw error;
          return json('/api/gen/short-drama/video-cast/avatars?project_id='+
            encodeURIComponent(projectId));
        });
      },
      getAssetGraph:function(projectId){
        return json('/api/gen/short-drama/asset-graph?project_id='+
          encodeURIComponent(projectId));
      },
      syncAssetGraph:function(projectId,graphRevision){
        return json('/api/gen/short-drama/asset-graph/sync',{
          method:'POST',body:{project_id:projectId,graph_revision:graphRevision}
        });
      },
      createAsset:function(projectId,graphRevision,asset){
        return json('/api/gen/short-drama/asset-graph/assets',{
          method:'POST',body:Object.assign({
            project_id:projectId,graph_revision:graphRevision
          },asset||{})
        });
      },
      bindAsset:function(projectId,graphRevision,binding){
        return json('/api/gen/short-drama/asset-graph/bindings',{
          method:'POST',body:Object.assign({
            project_id:projectId,graph_revision:graphRevision
          },binding||{})
        });
      },
      lockAssetVersion:function(projectId,graphRevision,versionId){
        return json('/api/gen/short-drama/asset-graph/versions/lock',{
          method:'POST',body:{project_id:projectId,graph_revision:graphRevision,version_id:versionId}
        });
      },
      buildAssetSnapshot:function(projectId,graphRevision,shotId){
        return json('/api/gen/short-drama/asset-graph/snapshots',{
          method:'POST',body:{project_id:projectId,graph_revision:graphRevision,shot_id:shotId}
        });
      },
      getPlanningQuote:function(){ return json('/api/gen/short-drama/planning-quote'); },
      getRecoverablePlanningJob:function(projectId){
        return json('/api/gen/short-drama/planning-job?project_id='+encodeURIComponent(projectId));
      },
      create:function(project){ return json('/api/gen/short-drama/projects',{method:'POST',body:project}); },
      update:function(projectId,revision,patch){
        return json(projectPath(projectId),{method:'PUT',body:Object.assign({},patch||{},{revision:revision})});
      },
      delete:function(projectId,revision){
        return json('/api/gen/short-drama/project/delete',{
          method:'POST',body:{project_id:projectId,revision:revision}
        });
      },
      applyPlan:applyPlan,
      confirm:function(projectId,revision,stage){
        return json('/api/gen/short-drama/confirm',{
          method:'POST',body:{project_id:projectId,revision:revision,stage:stage}
        });
      },
      generatePlan:function(project,hooks){
        hooks=hooks||{};
        function submitOrRecover(){
          return json('/api/gen/short-drama/planning-job?project_id='+encodeURIComponent(project.id))
            .then(function(recovered){
              if(recovered&&recovered.job_id) return recovered;
              return json('/api/gen/copy',{method:'POST',body:planningPayload(project)});
            });
        }
        return submitOrRecover().then(function(created){
            if(!created||!created.job_id) throw jobError(created);
            if(typeof hooks.onCost==='function'&&created.cost!=null) hooks.onCost(Number(created.cost));
            if(typeof hooks.onProgress==='function') hooks.onProgress({status:'pending',percent:20,label:'规划任务已入队'});
            return pollFn({
              request:function(){ return json('/api/gen/job/'+created.job_id); },
              intervalMs:3000,
              maxMs:420000,
              inspect:function(job){
                if(typeof hooks.onProgress==='function') hooks.onProgress({
                  status:job&&job.status||'pending',
                  percent:job&&job.status==='done'?80:Math.max(25,Math.min(75,Number(job&&job.progress)||45)),
                  label:job&&(job.phase||job.status)||'正在生成'
                });
                if(job&&job.status==='done') return {done:true,value:parseJobResult(job)};
                if(job&&(job.status==='error'||job.status==='failed')) return {error:jobError(job)};
                return {pending:true};
              }
            }).then(function(){ return applyPlan(project.id,project.revision,created.job_id); });
          });
      },
      generateCharacterReference:function(project,character){
        var key='short-drama-character-'+Date.now()+'-'+Math.random().toString(16).slice(2);
        return json('/api/gen/short-drama/generate-character-reference',{
          method:'POST',headers:{'Idempotency-Key':key},body:{
          project_id:project.id,revision:project.revision,
          character_key:character.character_key
        }}).then(function(created){
          if(!created||!created.job_id) throw jobError(created);
          return pollFn({
            request:function(){ return json('/api/gen/job/'+created.job_id); },
            intervalMs:3000,maxMs:420000,
            inspect:function(job){
              if(job&&job.status==='done') return {done:true,value:{
                job_id:created.job_id,result:parseJobResult(job),cost:Number(created.cost||35)
              }};
              if(job&&(job.status==='error'||job.status==='failed')) return {error:jobError(job)};
              return {pending:true};
            }
          });
        });
      }
    };
  }

  function escapeHtml(value){
    return String(value==null?'':value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function safeImageUrl(value){
    value=String(value||'').trim();
    return /^(https?:\/\/|\/)/i.test(value)?escapeHtml(value):'';
  }

  function isStageEnabled(project,stage){
    if(stage==='settings') return true;
    if(!project||STAGES.indexOf(stage)<1) return false;
    return STAGES.indexOf(stage)<=STAGES.indexOf(project.stage);
  }

  function isRoleDowngrade(previousRole,nextRole){
    var wasEditable=previousRole==='owner'||previousRole==='editor';
    var remainsEditable=nextRole==='owner'||nextRole==='editor';
    return wasEditable&&!remainsEditable;
  }

  function isStageEditable(project,stage,canEdit){
    if(!canEdit||!project) return false;
    if(stage==='settings') return project.stage==='draft';
    return ['characters_review','script_review','storyboard_review'].indexOf(stage)>=0&&
      project.stage===stage&&isStageEnabled(project,stage);
  }

  function cleanText(value){ return String(value==null?'':value).trim(); }
  function optionalText(value){ value=cleanText(value); return value||null; }
  function asList(value){
    if(Array.isArray(value)) return value.map(cleanText).filter(Boolean);
    return cleanText(value).split(',').map(cleanText).filter(Boolean);
  }

  function makeSettingsPatch(value){
    value=value||{};
    return {
      title:cleanText(value.title),synopsis:cleanText(value.synopsis),
      ratio:value.ratio==='16:9'?'16:9':'9:16',
      target_duration:Number(value.target_duration),shot_count:Number(value.shot_count),
      visual_style:cleanText(value.visual_style),target_platform:cleanText(value.target_platform),
      point_budget:Math.max(0,Number(value.point_budget)||0)
    };
  }

  function makeCharactersPatch(characters){
    return {characters:(characters||[]).map(function(character){
      var sourceType=character.source_type==='cinematic_avatar'?
        'cinematic_avatar':'ai_character';
      return {
        character_key:cleanText(character.character_key||character.key),name:cleanText(character.name),
        identity_text:cleanText(character.identity_text||character.identity),
        personality:cleanText(character.personality),
        source_type:sourceType,
        avatar_id:sourceType==='cinematic_avatar'?optionalText(character.avatar_id):null,
        appearance_prompt:cleanText(character.appearance_prompt),
        wardrobe_prompt:cleanText(character.wardrobe_prompt),
        reference_job_id:character.reference_job_id?Number(character.reference_job_id):null,
        reference_locked:character.reference_locked===true||character.reference_locked==='true',
        voice_key:optionalText(character.voice_key),
        voice_settings:cloneValue(character.voice_settings||{})
      };
    })};
  }

  function valuesEqual(left,right){
    return JSON.stringify(left==null?null:left)===JSON.stringify(right==null?null:right);
  }

  function mergeCharacterDrafts(baseCharacters,latestCharacters,draftCharacters){
    var base=baseCharacters||[],latest=latestCharacters||[],draft=draftCharacters||[];
    var fields=[
      'character_key','name','identity_text','personality','source_type','avatar_id',
      'appearance_prompt','wardrobe_prompt','reference_job_id',
      'reference_locked','voice_key','voice_settings'
    ],conflicts=[],merged=[];
    function alignToBase(items,preferStableOrder){
      var aligned=new Array(base.length),used=[];
      if(preferStableOrder&&items.length===base.length){
        base.forEach(function(_item,index){
          aligned[index]=items[index];
          used[index]=true;
        });
        return {items:aligned,used:used};
      }
      base.forEach(function(baseItem,baseIndex){
        var key=cleanText(baseItem.character_key),matched=-1;
        items.some(function(item,index){
          if(!used[index]&&cleanText(item.character_key)===key){
            matched=index;
            return true;
          }
          return false;
        });
        if(matched>=0){
          aligned[baseIndex]=items[matched];
          used[matched]=true;
        }
      });
      if(items.length===base.length){
        base.forEach(function(_item,index){
          if(!aligned[index]&&!used[index]){
            aligned[index]=items[index];
            used[index]=true;
          }
        });
      }
      return {items:aligned,used:used};
    }
    function changedFromBase(baseItem,item){
      return fields.some(function(field){
        return !valuesEqual(item&&item[field],baseItem&&baseItem[field]);
      });
    }
    var latestAligned=alignToBase(latest,false);
    var draftAligned=alignToBase(draft,true);
    base.forEach(function(baseItem,index){
      var latestItem=latestAligned.items[index],draftItem=draftAligned.items[index];
      if(!draftItem){
        if(latestItem){
          merged.push(cloneValue(latestItem));
          conflicts.push({
            character_key:cleanText(latestItem.character_key),
            field:'character_key',reason:'removed_local'
          });
        }
        return;
      }
      if(!latestItem){
        if(changedFromBase(baseItem,draftItem)){
          merged.push(cloneValue(draftItem));
          conflicts.push({
            character_key:cleanText(draftItem.character_key),
            field:'character_key',reason:'removed'
          });
        }
        return;
      }
      var result=Object.assign({},latestItem),itemConflicts=[];
      fields.forEach(function(field){
        var before=baseItem[field],remote=latestItem[field],local=draftItem[field];
        if(valuesEqual(local,before)) result[field]=cloneValue(remote);
        else if(valuesEqual(remote,before)||valuesEqual(local,remote)) result[field]=cloneValue(local);
        else{
          result[field]=cloneValue(local);
          itemConflicts.push({field:field,reason:'changed'});
        }
      });
      merged.push(result);
      itemConflicts.forEach(function(conflict){
        conflicts.push({
          character_key:cleanText(result.character_key),
          field:conflict.field,reason:conflict.reason
        });
      });
    });
    draft.forEach(function(item,index){
      if(!draftAligned.used[index]){
        merged.push(cloneValue(item));
        conflicts.push({
          character_key:cleanText(item.character_key),
          field:'character_key',reason:'added_local'
        });
      }
    });
    latest.forEach(function(item,index){
      if(!latestAligned.used[index]){
        var key=cleanText(item.character_key);
        if(!merged.some(function(candidate){
          return cleanText(candidate.character_key)===key;
        })){
          merged.push(cloneValue(item));
          conflicts.push({character_key:key,field:'character_key',reason:'added'});
        }
      }
    });
    return {characters:merged,conflicts:conflicts};
  }

  function scriptLineKey(line){
    var id=cleanText(line&&line.id),token=cleanText(line&&line.client_token);
    return token?'token:'+token:(id?'id:'+id:'');
  }

  function attachDialogueReceipts(script,receipts){
    var value=cloneValue(script||{}),tokenById=Object.create(null);
    Object.keys(receipts||{}).forEach(function(token){
      var id=cleanText(receipts[token]);
      if(id) tokenById[id]=token;
    });
    value.dialogue_lines=(value.dialogue_lines||[]).map(function(line){
      var item=cloneValue(line),token=tokenById[cleanText(item.id)];
      if(token) item.client_token=token;
      return item;
    });
    return value;
  }

  function mergePreferredOrder(baseOrder,preferred){
    var result=(preferred||[]).filter(function(key,index,items){
      return baseOrder.indexOf(key)>=0&&items.indexOf(key)===index;
    });
    baseOrder.forEach(function(key,index){
      if(result.indexOf(key)>=0) return;
      var previous=null,next=null;
      for(var before=index-1;before>=0;before-=1){
        if(result.indexOf(baseOrder[before])>=0){ previous=baseOrder[before];break; }
      }
      for(var after=index+1;after<baseOrder.length;after+=1){
        if(result.indexOf(baseOrder[after])>=0){ next=baseOrder[after];break; }
      }
      if(previous) result.splice(result.indexOf(previous)+1,0,key);
      else if(next) result.splice(result.indexOf(next),0,key);
      else result.push(key);
    });
    return result;
  }

  function mergeScriptDrafts(baseScript,latestScript,draftScript,receipts){
    var base=attachDialogueReceipts(baseScript,receipts);
    var latest=attachDialogueReceipts(latestScript,receipts);
    var draft=attachDialogueReceipts(draftScript,receipts),conflicts=[];
    var merged=cloneValue(latest);
    var scriptFields=['title','logline','hook','conflict_text','turn_text','ending'];
    var lineFields=['character_key','text','subtitle_enabled','voice_overrides'];
    scriptFields.forEach(function(field){
      var before=base[field],remote=latest[field],local=draft[field];
      if(valuesEqual(local,before)) merged[field]=cloneValue(remote);
      else if(valuesEqual(remote,before)||valuesEqual(local,remote)) merged[field]=cloneValue(local);
      else{
        merged[field]=cloneValue(local);
        conflicts.push({
          scope:'script',key:'script',field:field,local:cloneValue(local),remote:cloneValue(remote)
        });
      }
    });

    var baseLines=base.dialogue_lines||[],latestLines=latest.dialogue_lines||[];
    var draftLines=draft.dialogue_lines||[],remoteByKey=Object.create(null);
    var localByKey=Object.create(null),baseKeys=Object.create(null);
    var mergedByKey=Object.create(null),baseOrder=[];
    latestLines.forEach(function(line){ var key=scriptLineKey(line);if(key) remoteByKey[key]=line; });
    draftLines.forEach(function(line){ var key=scriptLineKey(line);if(key) localByKey[key]=line; });
    baseLines.forEach(function(baseLine){
      var key=scriptLineKey(baseLine),remote=remoteByKey[key],local=localByKey[key];
      baseKeys[key]=true;
      baseOrder.push(key);
      if(!local&&remote){
        var remoteChanged=lineFields.some(function(field){
          return !valuesEqual(remote[field],baseLine[field]);
        });
        if(remoteChanged){
          conflicts.push({
            scope:'dialogue',key:key,field:'__line__',reason:'removed_local',
            local:null,remote:cloneValue(remote)
          });
        }
        return;
      }
      if(local&&!remote){
        var localChanged=lineFields.some(function(field){
          return !valuesEqual(local[field],baseLine[field]);
        });
        if(localChanged){
          mergedByKey[key]=cloneValue(local);
          conflicts.push({
            scope:'dialogue',key:key,field:'__line__',reason:'removed_remote',
            local:cloneValue(local),remote:null
          });
        }
        return;
      }
      if(!local&&!remote) return;
      var mergedLine=cloneValue(remote);
      lineFields.forEach(function(field){
        var before=baseLine[field],remoteValue=remote[field],localValue=local[field];
        if(valuesEqual(localValue,before)) mergedLine[field]=cloneValue(remoteValue);
        else if(valuesEqual(remoteValue,before)||valuesEqual(localValue,remoteValue)){
          mergedLine[field]=cloneValue(localValue);
        }else{
          mergedLine[field]=cloneValue(localValue);
          conflicts.push({
            scope:'dialogue',key:key,field:field,
            local:cloneValue(localValue),remote:cloneValue(remoteValue)
          });
        }
      });
      mergedByKey[key]=mergedLine;
    });

    var localAdded=[],remoteAdded=[];
    draftLines.forEach(function(line){
      var key=scriptLineKey(line);if(key&&!baseKeys[key]) localAdded.push(key);
    });
    latestLines.forEach(function(line){
      var key=scriptLineKey(line);if(key&&!baseKeys[key]) remoteAdded.push(key);
    });
    localAdded.concat(remoteAdded).forEach(function(key){
      if(mergedByKey[key]) return;
      var local=localByKey[key],remote=remoteByKey[key];
      if(local&&remote){
        var mergedLine=cloneValue(remote);
        lineFields.forEach(function(field){
          if(valuesEqual(local[field],remote[field])) return;
          mergedLine[field]=cloneValue(local[field]);
          conflicts.push({
            scope:'dialogue',key:key,field:field,
            local:cloneValue(local[field]),remote:cloneValue(remote[field])
          });
        });
        mergedByKey[key]=mergedLine;
      }else{
        mergedByKey[key]=cloneValue(local||remote);
      }
    });

    baseOrder=baseOrder.filter(function(key){ return !!mergedByKey[key]; });
    var commonBase=baseOrder.filter(function(key){ return localByKey[key]&&remoteByKey[key]; });
    function projected(lines,allowed){
      return lines.map(scriptLineKey).filter(function(key){
        return allowed.indexOf(key)>=0;
      });
    }
    function sameOrder(left,right){ return valuesEqual(left,right); }
    var baseCommon=baseOrder.filter(function(key){ return commonBase.indexOf(key)>=0; });
    var localCommon=projected(draftLines,commonBase);
    var remoteCommon=projected(latestLines,commonBase);
    var localReordered=!sameOrder(localCommon,baseCommon);
    var remoteReordered=!sameOrder(remoteCommon,baseCommon);
    var orderConflict=false;
    if(localReordered&&remoteReordered&&!sameOrder(localCommon,remoteCommon)){
      orderConflict=true;
    }
    function anchors(lines,key){
      var index=lines.map(scriptLineKey).indexOf(key),previous='',next='';
      for(var before=index-1;before>=0;before-=1){
        var beforeKey=scriptLineKey(lines[before]);
        if(baseKeys[beforeKey]){ previous=beforeKey;break; }
      }
      for(var after=index+1;after<lines.length;after+=1){
        var afterKey=scriptLineKey(lines[after]);
        if(baseKeys[afterKey]){ next=afterKey;break; }
      }
      return [previous,next];
    }
    localAdded.forEach(function(key){
      if(remoteAdded.indexOf(key)>=0&&
          !sameOrder(anchors(draftLines,key),anchors(latestLines,key))){
        orderConflict=true;
      }
    });
    var localBaseOrder=projected(draftLines,baseOrder);
    var remoteBaseOrder=projected(latestLines,baseOrder);
    var chosenBaseOrder=baseOrder;
    if(localReordered) chosenBaseOrder=mergePreferredOrder(baseOrder,localBaseOrder);
    else if(remoteReordered) chosenBaseOrder=mergePreferredOrder(baseOrder,remoteBaseOrder);

    function gapIndex(lines,key){
      var anchor=anchors(lines,key),previous=chosenBaseOrder.indexOf(anchor[0]);
      var next=chosenBaseOrder.indexOf(anchor[1]);
      if(previous>=0) return previous+1;
      if(next>=0) return next;
      return chosenBaseOrder.length;
    }
    var buckets=Array.from({length:chosenBaseOrder.length+1},function(){ return []; });
    remoteAdded.forEach(function(key){
      if(localAdded.indexOf(key)<0) buckets[gapIndex(latestLines,key)].push(key);
    });
    localAdded.forEach(function(key){
      buckets[gapIndex(draftLines,key)].push(key);
    });
    var orderedKeys=[];
    buckets[0].forEach(function(key){ if(orderedKeys.indexOf(key)<0) orderedKeys.push(key); });
    chosenBaseOrder.forEach(function(key,index){
      if(orderedKeys.indexOf(key)<0) orderedKeys.push(key);
      buckets[index+1].forEach(function(addedKey){
        if(orderedKeys.indexOf(addedKey)<0) orderedKeys.push(addedKey);
      });
    });
    if(orderConflict){
      conflicts.push({
        scope:'dialogue',key:'sequence',field:'__order__',reason:'reordered',
        local:projected(draftLines,Object.keys(mergedByKey)),
        remote:projected(latestLines,Object.keys(mergedByKey))
      });
    }
    merged.dialogue_lines=orderedKeys.map(function(key){ return mergedByKey[key]; });
    merged.version=latest.version;
    return {script:merged,conflicts:conflicts};
  }

  function makeScriptPatch(script){
    script=script||{};
    return {script:{
      title:cleanText(script.title),logline:cleanText(script.logline),hook:cleanText(script.hook),
      conflict_text:cleanText(script.conflict_text||script.conflict),
      turn_text:cleanText(script.turn_text||script.turn),ending:cleanText(script.ending),
      dialogue_lines:(script.dialogue_lines||[]).map(function(line){
        var item={id:cleanText(line.id),character_key:cleanText(line.character_key),text:cleanText(line.text)};
        if(!item.id&&cleanText(line.client_token)) item.client_token=cleanText(line.client_token);
        return item;
      })
    }};
  }

  function makeShotsPatch(shots){
    return {shots:(shots||[]).map(function(shot){
      return {
        shot_key:cleanText(shot.shot_key||shot.key),duration:Number(shot.duration),
        scene_description:cleanText(shot.scene_description),camera_description:cleanText(shot.camera_description),
        character_keys:asList(shot.character_keys),dialogue_line_ids:asList(shot.dialogue_line_ids),
        image_prompt:cleanText(shot.image_prompt),video_prompt:cleanText(shot.video_prompt)
      };
    })};
  }

  function validateSettings(value){
    var patch=makeSettingsPatch(value),errors=[];
    if(!patch.title) errors.push('请输入短剧名称');
    if(patch.synopsis.length<8) errors.push('故事梗概至少需要 8 个字');
    if([30,45,60].indexOf(patch.target_duration)<0) errors.push('目标时长必须为 30、45 或 60 秒');
    if(patch.shot_count<6||patch.shot_count>10||Math.floor(patch.shot_count)!==patch.shot_count){
      errors.push('分镜数量必须为 6–10 个');
    }
    if(validShotCounts(patch.target_duration).indexOf(patch.shot_count)<0){
      errors.push('目标时长与分镜数量不匹配');
    }
    if(!patch.visual_style) errors.push('请输入视觉风格');
    if(!patch.target_platform) errors.push('请输入目标平台');
    return errors;
  }

  function validateCharacters(characters){
    var errors=[],seen=Object.create(null);
    if((characters||[]).length>MAX_CHARACTERS) errors.push('短剧角色数量不能超过 '+MAX_CHARACTERS+' 个');
    makeCharactersPatch(characters).characters.forEach(function(character,index){
      var label='角色 '+(index+1)+'：';
      ['character_key','name','identity_text','personality','appearance_prompt','wardrobe_prompt'].forEach(function(field){
        if(!character[field]) errors.push(label+'缺少 '+field);
      });
      if(seen[character.character_key]) errors.push(label+'角色标识重复');
      seen[character.character_key]=true;
      if(character.source_type==='cinematic_avatar'&&!character.avatar_id) errors.push(label+'请选择电影化形象');
      if(!character.voice_settings||typeof character.voice_settings!=='object'||Array.isArray(character.voice_settings)){
        errors.push(label+'语音设置必须是 JSON 对象');
      }
    });
    return errors;
  }

  function validateScript(script,project){
    var value=makeScriptPatch(script).script,errors=[];
    if(value.dialogue_lines.length>MAX_DIALOGUE_LINES){
      errors.push('剧本台词数量不能超过 '+MAX_DIALOGUE_LINES+' 条');
    }
    ['title','logline','hook','conflict_text','turn_text','ending'].forEach(function(field){
      if(!value[field]) errors.push('剧本缺少 '+field);
    });
    var characterKeys=(project&&project.characters||[]).map(function(item){ return item.character_key; });
    if(characterKeys.indexOf('narrator')<0) characterKeys.push('narrator');
    var ids=Object.create(null);
    value.dialogue_lines.forEach(function(line,index){
      var label='台词 '+(index+1)+'：';
      if(!line.character_key) errors.push(label+'请选择说话角色');
      if(!line.text) errors.push(label+'请填写台词内容');
      if(!line.id&&!line.client_token) errors.push(label+'新增台词缺少客户端请求标识');
      if(line.id&&ids[line.id]) errors.push(label+'台词标识不能重复');
      if(line.id) ids[line.id]=true;
      if(line.character_key&&characterKeys.indexOf(line.character_key)<0){
        errors.push(label+'引用了未知角色 '+line.character_key);
      }
    });
    return errors;
  }

  function validateShots(shots,project){
    var value=makeShotsPatch(shots).shots,errors=[];
    if(value.length<6||value.length>10) errors.push('分镜卡片必须为 6–10 张');
    if(project&&Number(project.shot_count)!==value.length) errors.push('分镜数量必须与项目设置一致');
    var characterKeys=(project&&project.characters||[]).map(function(item){ return item.character_key; });
    var scripts=project&&project.script_versions||[],latest=scripts[scripts.length-1]||{};
    var dialogueIds=(latest.dialogue_lines||[]).map(function(item){ return item.id; });
    var keys=Object.create(null),duration=0;
    value.forEach(function(shot,index){
      var label='分镜 '+(index+1)+'：';
      if(!shot.shot_key) errors.push(label+'缺少分镜标识');
      if(keys[shot.shot_key]) errors.push(label+'分镜标识重复');
      keys[shot.shot_key]=true;
      if([5,10].indexOf(shot.duration)<0) errors.push(label+'时长只能是 5 或 10 秒');
      duration+=shot.duration;
      if(!shot.scene_description) errors.push(label+'缺少场景描述');
      if(!shot.camera_description) errors.push(label+'缺少镜头描述');
      if(!shot.image_prompt) errors.push(label+'缺少画面提示词');
      if(!shot.video_prompt) errors.push(label+'缺少视频提示词');
      shot.character_keys.forEach(function(key){ if(characterKeys.indexOf(key)<0) errors.push(label+'引用了未知角色'); });
      shot.dialogue_line_ids.forEach(function(id){ if(dialogueIds.indexOf(id)<0) errors.push(label+'引用了未知台词'); });
    });
    if(project&&duration!==Number(project.target_duration)) errors.push('分镜总时长必须等于项目目标时长');
    return errors;
  }

  function canGeneratePlan(project,synopsisSaved){
    if(!project||project.stage!=='draft'||synopsisSaved===false) return false;
    var synopsis=cleanText(project.synopsis);
    return synopsis.length>=8&&synopsis!==PLACEHOLDER_SYNOPSIS;
  }

  function workspaceErrorMessage(error){
    if(error&&error.code==='revision_conflict'){
      return '项目已在其他页面更新，请刷新后重试';
    }
    var message=error&&error.message||'短剧工作区操作失败';
    if(/短剧规划.*(?:字段|JSON)|剧本格式不完整/.test(message)){
      return 'AI 返回的剧本格式不完整，系统自动修复失败。本次失败会自动退款，请重新生成';
    }
    return message;
  }

  function disabledUnless(enabled){ return enabled?'':' disabled'; }
  function selected(value,expected){ return String(value)===String(expected)?' selected':''; }
  function fieldValue(value){ return escapeHtml(value==null?'':value); }

  function renderStageNavigation(project,state){
    return ['settings'].concat(STAGES.slice(1)).map(function(stage){
      var enabled=isStageEnabled(project,stage),active=state.activeStage===stage;
      if(isProductionStage(stage)) enabled=stage===project.stage;
      return '<button type="button" class="nc-short-drama-stage'+(active?' is-active':'')+'" data-tab="'+stage+'"'+
        disabledUnless(enabled)+' aria-current="'+(active?'step':'false')+'"><span>'+escapeHtml(STAGE_LABELS[stage])+'</span></button>';
    }).join('');
  }

  function renderCharacterRail(project){
    var characters=project.characters||[];
    return '<aside class="nc-short-drama-character-rail"><div class="nc-short-drama-section-title">角色列表 <span>'+characters.length+'</span></div>'+
      (characters.length?characters.map(function(character,index){
        return '<button type="button" class="nc-short-drama-character-chip" data-character-jump="'+index+'">'+
          '<span class="nc-short-drama-avatar">'+escapeHtml((character.name||'?').slice(0,1))+'</span><span><strong>'+escapeHtml(character.name)+'</strong><small>'+escapeHtml(character.identity_text||character.identity||'')+'</small></span></button>';
      }).join(''):'<p class="nc-short-drama-empty">生成策划后将在这里显示角色。</p>')+'</aside>';
  }

  function renderSettingsEditor(project,state){
    var editable=isStageEditable(project,'settings',state.canEdit)&&!state.busy;
    var deletable=state.canEdit&&!state.busy;
    var shotOptions=validShotCounts(project.target_duration).map(function(count){
      return '<option value="'+count+'"'+selected(project.shot_count,count)+'>'+count+' 镜</option>';
    }).join('');
    return '<section class="nc-short-drama-panel nc-short-drama-settings-form"><header><div><span class="nc-short-drama-kicker">免费编辑</span><h2>项目设置</h2></div><div class="nc-short-drama-actions"><button type="button" class="is-danger" data-action="delete-project"'+disabledUnless(deletable)+'>删除项目</button><button type="button" data-action="save-settings"'+disabledUnless(editable)+'>保存设置</button></div></header>'+
      '<div class="nc-short-drama-form-grid"><label>短剧名称<input data-field="title" value="'+fieldValue(project.title)+'"'+disabledUnless(editable)+'></label>'+
      '<label class="is-wide">故事梗概<textarea data-field="synopsis" rows="7"'+disabledUnless(editable)+'>'+fieldValue(project.synopsis)+'</textarea><small>至少 8 个字；保存后才能生成策划。</small></label>'+
      '<label>画面比例<select data-field="ratio"'+disabledUnless(editable)+'><option'+selected(project.ratio,'9:16')+'>9:16</option><option'+selected(project.ratio,'16:9')+'>16:9</option></select></label>'+
      '<label>目标时长<select data-field="target_duration"'+disabledUnless(editable)+'><option value="30"'+selected(project.target_duration,30)+'>30 秒</option><option value="45"'+selected(project.target_duration,45)+'>45 秒</option><option value="60"'+selected(project.target_duration,60)+'>60 秒</option></select></label>'+
      '<label>分镜数量<select data-field="shot_count"'+disabledUnless(editable)+'>'+shotOptions+'</select></label>'+
      '<label>视觉风格<input data-field="visual_style" value="'+fieldValue(project.visual_style)+'"'+disabledUnless(editable)+'></label>'+
      '<label>目标平台<input data-field="target_platform" value="'+fieldValue(project.target_platform)+'"'+disabledUnless(editable)+'></label>'+
      '<label>点数预算<input type="number" min="0" data-field="point_budget" value="'+fieldValue(project.point_budget||0)+'"'+disabledUnless(editable)+'></label></div></section>';
  }

  function renderAvatarSelector(character,index,state,editable){
    var candidates=state.avatarCandidates||{},items=Array.isArray(candidates.items)?
      candidates.items:[],current=cleanText(character.avatar_id),matched=false;
    var cards=items.map(function(item){
      var id=cleanText(item.id),active=id===current;
      if(active) matched=true;
      var image=safeImageUrl(item.image_url);
      return '<button type="button" class="nc-short-drama-avatar-option'+
        (active?' is-selected':'')+'" data-action="select-character-avatar" '+
        'data-avatar-index="'+index+'" data-avatar-id="'+fieldValue(id)+'"'+
        disabledUnless(editable)+'>'+
        (image?'<img src="'+image+'" alt="'+escapeHtml(item.name||'电影化形象')+'">':
          '<span class="nc-short-drama-avatar is-large">'+
          escapeHtml((item.name||'?').slice(0,1))+'</span>')+
        '<span class="nc-short-drama-avatar-copy"><strong>'+
        escapeHtml(item.name||'未命名形象')+'</strong>'+
        (active?'<small>已选择</small>':'')+'</span></button>';
    }).join('');
    var content='';
    if(candidates.loading) content='<p class="nc-short-drama-avatar-state">正在加载电影化形象……</p>';
    else if(candidates.error) content='<p class="nc-short-drama-avatar-state is-error">'+
      escapeHtml(candidates.error)+'</p>';
    else if(candidates.loaded&&!items.length) content=
      '<p class="nc-short-drama-avatar-state">暂时没有可用的电影化形象，请先创建电影化身</p>';
    else content='<div class="nc-short-drama-avatar-options">'+cards+'</div>';
    if(current&&candidates.loaded&&!matched){
      content+='<p class="nc-short-drama-avatar-state is-error">'+
        '原来绑定的形象已删除或不可用，请重新选择</p>';
    }
    return '<input type="hidden" data-field="avatar_id" value="'+fieldValue(current)+'">'+
      '<div class="is-wide nc-short-drama-avatar-picker"><div class="nc-short-drama-avatar-picker-head">'+
      '<strong>选择电影化形象</strong><span>'+(
        current&&matched?'已选择':'请选择电影化形象'
      )+'</span></div>'+content+
      '<div class="nc-short-drama-avatar-picker-actions">'+
      (candidates.canCreate?'<button type="button" data-action="create-character-avatar"'+
        disabledUnless(editable)+'>创建新电影化身</button>':'')+
      '<button type="button" data-action="refresh-character-avatars"'+
      disabledUnless(editable||state.canEdit)+'>刷新形象库</button></div></div>';
  }

  function renderCharactersEditor(project,state){
    var editable=isStageEditable(project,'characters_review',state.canEdit)&&!state.busy;
    var referenceEditable=state.canEdit&&!state.busy&&
      ['characters_review','script_review','storyboard_review','stills_review'].indexOf(project.stage)>=0;
    var characters=state.characterDrafts||project.characters||[];
    return '<section class="nc-short-drama-panel"><header><div><span class="nc-short-drama-kicker">角色资产准备</span><h2>角色确认</h2></div><div class="nc-short-drama-actions"><button type="button" data-action="save-characters"'+disabledUnless(editable)+'>保存角色</button><button type="button" class="is-primary" data-confirm-stage="characters_review"'+disabledUnless(editable)+'>确认角色并继续</button></div></header><div class="nc-short-drama-character-cards">'+
      characters.map(function(character,index){
        return '<article class="nc-short-drama-character-card" data-character-index="'+index+'"><div class="nc-short-drama-card-heading"><span class="nc-short-drama-avatar is-large">'+escapeHtml((character.name||'?').slice(0,1))+'</span><div><strong>'+escapeHtml(character.name)+'</strong><small>'+escapeHtml(character.character_key)+'</small></div></div>'+
          '<div class="nc-short-drama-form-grid"><label>角色标识<input data-field="character_key" value="'+fieldValue(character.character_key)+'"'+disabledUnless(editable)+'></label><label>角色名称<input data-field="name" value="'+fieldValue(character.name)+'"'+disabledUnless(editable)+'></label>'+
          '<label>来源<select data-field="source_type"'+disabledUnless(editable)+'><option value="ai_character"'+selected(character.source_type,'ai_character')+'>AI 角色</option><option value="cinematic_avatar"'+selected(character.source_type,'cinematic_avatar')+'>电影化形象</option></select></label>'+
          (character.source_type==='cinematic_avatar'?
            renderAvatarSelector(character,index,state,editable):
            '<input type="hidden" data-field="avatar_id" value="">')+
          '<label class="is-wide">身份<textarea data-field="identity_text"'+disabledUnless(editable)+'>'+fieldValue(character.identity_text)+'</textarea></label><label class="is-wide">性格<textarea data-field="personality"'+disabledUnless(editable)+'>'+fieldValue(character.personality)+'</textarea></label>'+
          '<label class="is-wide">外观提示词<textarea data-field="appearance_prompt"'+disabledUnless(editable)+'>'+fieldValue(character.appearance_prompt)+'</textarea></label><label class="is-wide">服装提示词<textarea data-field="wardrobe_prompt"'+disabledUnless(editable)+'>'+fieldValue(character.wardrobe_prompt)+'</textarea></label>'+
          '<div class="is-wide nc-short-drama-character-reference">'+
          (safeImageUrl(character.reference_url)?'<img src="'+safeImageUrl(character.reference_url)+'" alt="'+escapeHtml(character.name)+' 角色标准图">':'<span>尚未生成角色标准图</span>')+
          '<input type="hidden" data-field="reference_job_id" value="'+fieldValue(character.reference_job_id)+'">'+
          '<input type="hidden" data-field="reference_locked" value="'+(character.reference_locked?'true':'false')+'">'+
          (character.source_type==='ai_character'?'<button type="button" data-action="generate-character-reference" data-reference-index="'+index+'"'+disabledUnless(referenceEditable)+'>'+
            (character.reference_locked?'重新生成标准图（35点）':'生成并锁定标准图（35点）')+'</button>':'<small>电影化形象将自动使用已归属的形象图</small>')+
          '</div>'+
          '<label>音色 Key<input data-field="voice_key" value="'+fieldValue(character.voice_key)+'"'+disabledUnless(editable)+'></label><label>语音设置 JSON<textarea data-field="voice_settings"'+disabledUnless(editable)+'>'+fieldValue(
            character._voice_settings_raw!=null?character._voice_settings_raw:
              JSON.stringify(character.voice_settings||{},null,2)
          )+'</textarea></label></div></article>';
      }).join('')+'</div></section>';
  }

  function selectedScript(project,state){
    var versions=project.script_versions||[],selectedVersion=Number(state.scriptVersion),match=null;
    versions.forEach(function(version){ if(Number(version.version)===selectedVersion) match=version; });
    return match||versions[versions.length-1]||{};
  }

  function renderScriptConflicts(state){
    var conflicts=state.scriptConflicts||[];
    if(!conflicts.length) return '';
    function preview(value){
      if(value==null) return '删除';
      if(typeof value==='object') return JSON.stringify(value);
      return String(value);
    }
    return '<aside class="nc-short-drama-script-conflicts" role="alert"><strong>检测到并发编辑冲突</strong>'+
      '<p>保存已暂停。请逐项选择要保留的内容。</p>'+
      conflicts.map(function(conflict,index){
        var label=conflict.scope==='script'?conflict.field:
          conflict.key.replace(/^id:|^token:/,'')+' / '+conflict.field;
        return '<div class="nc-short-drama-script-conflict"><span>'+escapeHtml(label)+'</span>'+
          '<small>我的：'+escapeHtml(preview(conflict.local))+
          '；服务器：'+escapeHtml(preview(conflict.remote))+'</small>'+
          '<button type="button" data-action="resolve-script-conflict" data-conflict-index="'+index+
          '" data-conflict-choice="local">保留我的</button>'+
          '<button type="button" data-action="resolve-script-conflict" data-conflict-index="'+index+
          '" data-conflict-choice="remote">采用服务器</button></div>';
      }).join('')+'</aside>';
  }

  function renderScriptEditor(project,state){
    var versions=project.script_versions||[],selectedValue=selectedScript(project,state),latest=versions[versions.length-1]||{};
    var script=state.scriptDraftDirty&&state.scriptDraft&&
      Number(selectedValue.version)===Number(latest.version)?state.scriptDraft:selectedValue;
    var editable=isStageEditable(project,'script_review',state.canEdit)&&Number(script.version)===Number(latest.version)&&!state.busy;
    var characters=(project.characters||[]).slice();
    if(!characters.some(function(item){ return item.character_key==='narrator'; })){
      characters.push({character_key:'narrator',name:'旁白',identity_text:'不驱动口型'});
    }
    function speakerOptions(line){
      return characters.map(function(character){
        var label=cleanText(character.name||character.character_key);
        var identity=cleanText(character.identity_text);
        return '<option value="'+fieldValue(character.character_key)+'"'+
          selected(line.character_key,character.character_key)+'>'+
          escapeHtml(label+(identity?'｜'+identity:''))+'</option>';
      }).join('');
    }
    var dialogueRows=(script.dialogue_lines||[]).map(function(line,index){
      var displayId=cleanText(line.id);
      if(!displayId||displayId.indexOf('tmp_')===0) displayId='保存后由系统生成';
      return '<article class="nc-short-drama-dialogue" data-dialogue-index="'+index+'">'+
        '<label>台词编号<input data-field="id" value="'+fieldValue(displayId)+'" readonly aria-readonly="true"></label>'+
        '<label>说话角色<select data-field="character_key"'+disabledUnless(editable)+'>'+
          speakerOptions(line)+'</select></label>'+
        '<label class="is-wide">台词内容<textarea data-field="text"'+disabledUnless(editable)+'>'+
          fieldValue(line.text)+'</textarea></label>'+
        '<div class="nc-short-drama-dialogue-actions">'+
          '<button type="button" data-action="copy-dialogue" data-dialogue-index="'+index+'"'+disabledUnless(editable)+'>复制</button>'+
          '<button type="button" data-action="delete-dialogue" data-dialogue-index="'+index+'"'+disabledUnless(editable)+'>删除</button>'+
        '</div>'+
        '<input type="hidden" data-field="client_token" value="'+fieldValue(line.client_token)+'">'+
        '<input type="hidden" data-field="speaker_name_snapshot" value="'+fieldValue(line.speaker_name_snapshot)+'">'+
      '</article>';
    }).join('');
    return '<section class="nc-short-drama-panel nc-short-drama-script-form"><header><div><span class="nc-short-drama-kicker">版本化剧本</span><h2>剧本确认</h2></div><div class="nc-short-drama-actions"><button type="button" data-action="save-script"'+disabledUnless(editable)+'>保存为新版本</button><button type="button" class="is-primary" data-confirm-stage="script_review"'+disabledUnless(editable)+'>确认剧本并继续</button></div></header>'+
      renderScriptConflicts(state)+
      '<div class="nc-short-drama-version-tabs">'+versions.map(function(version){ return '<button type="button" data-script-version="'+version.version+'" class="'+(Number(version.version)===Number(script.version)?'is-active':'')+'">版本 '+version.version+'</button>'; }).join('')+'</div>'+
      '<div class="nc-short-drama-form-grid"><label>剧本标题<input data-field="title" value="'+fieldValue(script.title)+'"'+disabledUnless(editable)+'></label><label class="is-wide">一句话故事<textarea data-field="logline"'+disabledUnless(editable)+'>'+fieldValue(script.logline)+'</textarea></label>'+
      '<label class="is-wide">Hook<textarea data-field="hook"'+disabledUnless(editable)+'>'+fieldValue(script.hook)+'</textarea></label><label class="is-wide">冲突<textarea data-field="conflict_text"'+disabledUnless(editable)+'>'+fieldValue(script.conflict_text||script.conflict)+'</textarea></label>'+
      '<label class="is-wide">反转<textarea data-field="turn_text"'+disabledUnless(editable)+'>'+fieldValue(script.turn_text||script.turn)+'</textarea></label><label class="is-wide">结局<textarea data-field="ending"'+disabledUnless(editable)+'>'+fieldValue(script.ending)+'</textarea></label></div>'+
      '<div class="nc-short-drama-dialogue-list"><header><div><h3>台词</h3><small>页面显示角色名称，系统仍用稳定角色标识关联配音、字幕与口型。</small></div>'+
      '<button type="button" data-action="add-dialogue"'+disabledUnless(editable)+'>新增台词</button></header>'+
      dialogueRows+'</div></section>';
  }

  function renderStoryboardEditor(project,state){
    var editable=isStageEditable(project,'storyboard_review',state.canEdit)&&!state.busy;
    var characters=Object.create(null),dialogue=Object.create(null);
    (project.characters||[]).forEach(function(character){ characters[character.character_key]=character.name; });
    var scripts=project.script_versions||[],latest=scripts[scripts.length-1]||{};
    (latest.dialogue_lines||[]).forEach(function(line){ dialogue[line.id]=line.text; });
    return '<section class="nc-short-drama-panel"><header><div><span class="nc-short-drama-kicker">6–10 张可执行分镜</span><h2>分镜确认</h2></div><div class="nc-short-drama-actions"><button type="button" data-action="save-shots"'+disabledUnless(editable)+'>保存分镜</button><button type="button" class="is-primary" data-confirm-stage="storyboard_review"'+disabledUnless(editable)+'>确认分镜</button></div></header><div class="nc-short-drama-shot-list">'+
      (project.shots||[]).map(function(shot,index){
        var names=(shot.character_keys||[]).map(function(key){ return characters[key]||key; }).join('、')||'无';
        var lines=(shot.dialogue_line_ids||[]).map(function(id){ return dialogue[id]||id; }).join(' / ')||'无台词';
        return '<article class="nc-short-drama-shot-card" data-shot-key="'+fieldValue(shot.shot_key)+'" data-shot-index="'+index+'"><div class="nc-short-drama-shot-number"><span>#'+String(index+1).padStart(2,'0')+'</span><strong>'+fieldValue(shot.duration)+'秒</strong></div>'+
          '<div class="nc-short-drama-shot-fields"><label>时长<select data-field="duration"'+disabledUnless(editable)+'><option value="5"'+selected(shot.duration,5)+'>5秒</option><option value="10"'+selected(shot.duration,10)+'>10秒</option></select></label><label>分镜标识<input data-field="shot_key" value="'+fieldValue(shot.shot_key)+'"'+disabledUnless(editable)+'></label>'+
          '<label class="is-wide">场景描述<textarea data-field="scene_description"'+disabledUnless(editable)+'>'+fieldValue(shot.scene_description)+'</textarea></label><label class="is-wide">镜头描述<textarea data-field="camera_description"'+disabledUnless(editable)+'>'+fieldValue(shot.camera_description)+'</textarea></label>'+
          '<label class="is-wide">角色 <small>'+escapeHtml(names)+'</small><input data-field="character_keys" value="'+fieldValue((shot.character_keys||[]).join(','))+'"'+disabledUnless(editable)+'></label><label class="is-wide">台词摘要 <small>'+escapeHtml(lines)+'</small><input data-field="dialogue_line_ids" value="'+fieldValue((shot.dialogue_line_ids||[]).join(','))+'"'+disabledUnless(editable)+'></label>'+
          '<label class="is-wide">画面提示词<textarea data-field="image_prompt"'+disabledUnless(editable)+'>'+fieldValue(shot.image_prompt)+'</textarea></label><label class="is-wide">视频提示词<textarea data-field="video_prompt"'+disabledUnless(editable)+'>'+fieldValue(shot.video_prompt)+'</textarea></label></div></article>';
      }).join('')+'</div>'+renderAssetGraph(project,state)+'</section>';
  }

  function renderAssetGraph(project,state){
    var graph=state.assetGraph||{},entities=Array.isArray(graph.entities)?graph.entities:[];
    var typeLabels={character:'角色',scene:'场景',costume:'服装',makeup:'妆容',
      prop:'道具',vehicle:'车辆',clue:'线索'};
    var counts={};
    entities.forEach(function(entity){ counts[entity.asset_type]=(counts[entity.asset_type]||0)+1; });
    var summary=Object.keys(typeLabels).filter(function(type){ return counts[type]; }).map(function(type){
      return '<span>'+typeLabels[type]+' '+counts[type]+'</span>';
    }).join('')||'<span>尚未建立资产</span>';
    var rows=entities.map(function(entity){
      var versions=Array.isArray(entity.versions)?entity.versions:[];
      var current=versions.filter(function(item){ return item.id===entity.current_version_id; })[0];
      var draft=versions.filter(function(item){ return item.status==='draft'; })[0];
      return '<li><span><strong>'+escapeHtml(entity.name)+'</strong><small>'+escapeHtml(
        typeLabels[entity.asset_type]||entity.asset_type
      )+' · '+(current?'已锁定 v'+current.version:'待锁定')+'</small></span>'+(
        draft?'<button type="button" data-action="lock-asset-version" data-version-id="'+
          fieldValue(draft.id)+'"'+disabledUnless(state.canEdit&&!state.busy)+'>锁定 v'+
          Number(draft.version)+'</button>':''
      )+'</li>';
    }).join('');
    var shots=Array.isArray(project.shots)?project.shots:[];
    var bindable=entities.filter(function(entity){
      return entity.asset_type!=='character'&&entity.asset_type!=='scene';
    });
    return '<section class="nc-short-drama-asset-graph"><header><div><span class="nc-short-drama-kicker">资产一致性</span><h3>短剧资产图谱</h3></div><div class="nc-short-drama-actions">'+
      '<button type="button" data-action="sync-asset-graph"'+disabledUnless(state.canEdit&&!state.busy)+'>同步基础资产</button>'+(
        entities.length?'<button type="button" data-action="build-asset-snapshots"'+
        disabledUnless(state.canEdit&&!state.busy)+'>生成镜头资产快照</button>':''
      )+'</div></header><div class="nc-short-drama-asset-summary">'+summary+'</div>'+(
        state.assetGraphLoading?'<p>正在读取资产图谱…</p>':
        state.assetGraphError?'<p class="nc-short-drama-error">'+escapeHtml(state.assetGraphError)+'</p>':
        '<ul>'+rows+'</ul>'
      )+'<div class="nc-short-drama-asset-create"><select data-asset-field="asset_type"'+
        disabledUnless(state.canEdit&&!state.busy)+'>'+Object.keys(typeLabels).filter(function(type){
          return type!=='character'&&type!=='scene';
        }).map(function(type){ return '<option value="'+type+'">'+typeLabels[type]+'</option>'; }).join('')+
        '</select><input data-asset-field="name" maxlength="200" placeholder="资产名称，例如：黑色雨伞"'+
        disabledUnless(state.canEdit&&!state.busy)+'><input data-asset-field="description" maxlength="4000" placeholder="外观、用途与连续性要求"'+
        disabledUnless(state.canEdit&&!state.busy)+'><button type="button" data-action="create-asset"'+
        disabledUnless(state.canEdit&&!state.busy)+'>新增资产</button></div>'+(
        shots.length&&bindable.length?'<div class="nc-short-drama-asset-bind"><select data-asset-bind="shot_id"'+
        disabledUnless(state.canEdit&&!state.busy)+'>'+shots.map(function(shot){ return '<option value="'+
          fieldValue(shot.id)+'">'+escapeHtml(shot.shot_key||shot.id)+'</option>'; }).join('')+
        '</select><select data-asset-bind="entity_id"'+disabledUnless(state.canEdit&&!state.busy)+'>'+bindable.map(function(entity){
          return '<option value="'+fieldValue(entity.id)+'">'+escapeHtml(entity.name)+' · '+
            escapeHtml(typeLabels[entity.asset_type]||entity.asset_type)+'</option>';
        }).join('')+'</select><select data-asset-bind="relation_type"'+disabledUnless(state.canEdit&&!state.busy)+
        '><option value="uses">使用</option><option value="wears">穿戴</option><option value="drives">驾驶</option><option value="reveals">揭示线索</option><option value="related">其他关联</option></select><button type="button" data-action="bind-asset"'+
        disabledUnless(state.canEdit&&!state.busy)+'>绑定到镜头</button></div>':''
      )+(
        entities.length?'<p class="nc-short-drama-asset-hint">新增资产后先锁定版本，再选择镜头和关系完成绑定。</p>':''
      )+'<small>镜头生成只读取已锁定版本；后续修改会创建新版本，不会污染历史快照。</small></section>';
  }

  function renderProductionUnavailablePanel(){
    return '<section class="nc-short-drama-complete"><span class="nc-short-drama-complete-mark">!</span><p class="nc-short-drama-kicker">生产阶段</p><h2>生产工作区未加载</h2><p>请刷新页面重试；若问题持续，请联系管理员检查画布资源。</p><div class="nc-short-drama-load-actions"><button type="button" data-action="reload">重试</button><button type="button" data-action="close">关闭</button></div></section>';
  }

  function renderWorkspaceTopbar(project,state){
    return '<header class="nc-short-drama-topbar"><div class="nc-short-drama-brand"><span>HQ</span><div><strong>'+escapeHtml(project.title)+'</strong><small>'+escapeHtml(project.id)+' · R'+Number(project.revision||0)+'</small></div></div><nav>'+renderStageNavigation(project,state)+'</nav><button type="button" class="nc-short-drama-close" data-action="close" aria-label="关闭">×</button></header>';
  }

  function renderProductionFrame(project,state,content){
    return '<div class="nc-short-drama-workspace nc-short-drama-production-workspace" role="dialog" aria-modal="true" aria-label="短剧生产工作区" data-readonly="'+(!state.canEdit)+'">'+renderWorkspaceTopbar(project,state)+
      (!state.canEdit?'<div class="nc-short-drama-readonly">当前画布为只读模式，所有生产写操作均已禁用。</div>':'')+
      '<div class="nc-short-drama-production-slot" data-production-host>'+content+'</div></div>';
  }

  function renderInspector(project,state){
    var planning=state.planning||{},ready=canGeneratePlan(project,state.synopsisSaved)&&state.canEdit&&!state.busy;
    var costText=planning.cost==null?'待查询':Number(planning.cost)+' 点';
    return '<aside class="nc-short-drama-inspector"><div class="nc-short-drama-section-title">制作控制台</div>'+
      '<div class="nc-short-drama-cost-card"><span>本次策划成本</span><strong>'+costText+'</strong><p>提交前查询实时价格并确认。保存和确认不扣点。</p></div>'+
      '<dl><div><dt>当前阶段</dt><dd>'+escapeHtml(STAGE_LABELS[project.stage]||'草稿')+'</dd></div><div><dt>累计已扣</dt><dd>'+Number(project.spent_points||0)+' 点</dd></div><div><dt>项目预算</dt><dd>'+Number(project.point_budget||0)+' 点</dd></div><div><dt>交付规格</dt><dd>'+escapeHtml(project.ratio)+' · '+Number(project.target_duration)+'秒 · '+Number(project.shot_count)+'镜</dd></div></dl>'+
      (project.stage==='draft'?'<button type="button" class="nc-short-drama-plan-button" data-action="generate-plan"'+disabledUnless(ready)+'><span>生成短剧策划（按实时报价）</span><small>'+(ready?'生成角色、剧本和分镜':'请先保存有效故事梗概')+'</small></button>':'')+
      (planning.running?'<div class="nc-short-drama-progress"><div><span style="width:'+Number(planning.percent||0)+'%"></span></div><strong>正在生成策划… '+Number(planning.percent||0)+'%</strong><small>'+escapeHtml(planning.label||'正在等待规划任务')+'</small></div>':'')+
      (state.error?'<div class="nc-short-drama-error" role="alert"><strong>'+(state.stale?'版本冲突':'操作未完成')+'</strong><p>'+escapeHtml(state.error)+'</p>'+(state.stale?'<button type="button" data-action="reload">刷新项目</button>':'')+'</div>':'')+'</aside>';
  }

  function renderLoadState(state){
    state=Object.assign({canEdit:true,busy:false,loadFailed:false,loadStatus:0,error:''},state||{});
    var ownerOnly=!state.canEdit&&Number(state.loadStatus)===404;
    if(state.busy&&!state.loadFailed){
      return '<div class="nc-short-drama-workspace" role="dialog" aria-modal="true" data-readonly="'+(!state.canEdit)+'"><button type="button" class="nc-short-drama-close nc-short-drama-load-close" data-action="close" aria-label="关闭">×</button><div class="nc-short-drama-loading">正在加载短剧项目…</div></div>';
    }
    var message=ownerOnly?'仅项目创建者可查看短剧详情':(state.error||'短剧项目加载失败');
    return '<div class="nc-short-drama-workspace" role="dialog" aria-modal="true" data-readonly="'+(!state.canEdit)+'"><section class="nc-short-drama-load-state" role="alert"><span class="nc-short-drama-load-mark">!</span><h2>'+(ownerOnly?'无法查看项目':'项目加载失败')+'</h2><p>'+escapeHtml(message)+'</p><div class="nc-short-drama-load-actions"><button type="button" data-action="reload">重试</button><button type="button" data-action="close">关闭</button></div></section></div>';
  }

  function renderWorkspace(project,state){
    state=Object.assign({activeStage:project&&project.stage==='draft'?'settings':project&&project.stage||'settings',canEdit:true,busy:false,error:'',stale:false,synopsisSaved:true,planning:{},loadFailed:false,loadStatus:0},state||{});
    if(!project) return renderLoadState(state);
    if(!isStageEnabled(project,state.activeStage)) state.activeStage=project.stage==='draft'?'settings':project.stage;
    var center=state.activeStage==='settings'?renderSettingsEditor(project,state):
      state.activeStage==='characters_review'?renderCharactersEditor(project,state):
      state.activeStage==='script_review'?renderScriptEditor(project,state):
      state.activeStage==='storyboard_review'?renderStoryboardEditor(project,state):renderProductionUnavailablePanel();
    return '<div class="nc-short-drama-workspace" role="dialog" aria-modal="true" aria-label="短剧策划工作区" data-readonly="'+(!state.canEdit)+'">'+renderWorkspaceTopbar(project,state)+
      (!state.canEdit?'<div class="nc-short-drama-readonly">当前画布为只读模式，可查看已完成内容，不能保存或确认。</div>':'')+
      '<div class="nc-short-drama-layout">'+renderCharacterRail(project)+'<main class="nc-short-drama-editor">'+center+'</main>'+renderInspector(project,state)+'</div></div>';
  }

  function findActionTarget(node,attribute,host){
    while(node&&node!==host){ if(node.getAttribute&&node.getAttribute(attribute)!=null) return node; node=node.parentNode; }
    return null;
  }

  function createWorkspace(options){
    options=options||{};
    var client=options.client||createClient(options.apiClient,options.poll,options.boardId);
    var project=null,destroyed=false,host=null,productionHost=null,productionWorkspace=null,
      loadGeneration=0,productionGeneration=0;
    var canEdit=options.canEdit!==false,onChange=typeof options.onChange==='function'?options.onChange:function(){};
    var confirmHook=typeof options.confirm==='function'?options.confirm:
      (typeof window!=='undefined'&&typeof window.confirm==='function'?window.confirm.bind(window):function(){ return false; });
    var doc=Object.prototype.hasOwnProperty.call(options,'document')?options.document:
      (typeof document!=='undefined'?document:null);
    var state={activeStage:'settings',canEdit:canEdit,busy:false,error:'',stale:false,synopsisSaved:false,
      loadFailed:false,loadStatus:0,destroyed:false,
      scriptVersion:null,planning:{running:false,percent:0,label:'',cost:null},
      scriptDraft:null,scriptDraftBase:null,scriptDraftRevision:null,scriptDraftDirty:false,
      scriptConflicts:[],
      characterDrafts:null,characterDraftBase:null,characterDraftRevision:null,
      characterDraftDirty:false,characterConflicts:[],
      avatarCandidates:{items:[],loaded:false,loading:false,error:'',canCreate:false},
      assetGraph:null,assetGraphLoading:false,assetGraphError:''};

    function snapshotState(){ return cloneValue(state); }
    function resetCharacterDrafts(value){
      var characters=cloneValue(value&&value.characters||[]);
      state.characterDrafts=characters;
      state.characterDraftBase=cloneValue(characters);
      state.characterDraftRevision=value&&value.revision!=null?Number(value.revision):null;
      state.characterDraftDirty=false;
      state.characterConflicts=[];
    }
    function captureCharacterDrafts(){
      if(!host||!project||state.activeStage!=='characters_review') return;
      try{
        state.characterDrafts=readCharacters(true);
        state.characterDraftDirty=true;
      }catch(error){
        state.error=workspaceErrorMessage(error);
      }
    }
    function resetScriptDraft(value){
      var scripts=value&&value.script_versions||[],latest=cloneValue(scripts[scripts.length-1]||{});
      state.scriptDraft=latest;
      state.scriptDraftBase=cloneValue(latest);
      state.scriptDraftRevision=value&&value.revision!=null?Number(value.revision):null;
      state.scriptDraftDirty=false;
      state.scriptConflicts=[];
    }
    function captureScriptDraft(){
      if(!host||!project||state.activeStage!=='script_review') return;
      try{
        var draft=readScript();
        var latest=(project.script_versions||[]).slice(-1)[0]||{};
        draft.version=latest.version;
        state.scriptDraft=draft;
        state.scriptDraftDirty=true;
      }catch(error){
        state.error=workspaceErrorMessage(error);
      }
    }
    function mutateDialogue(action,index){
      captureScriptDraft();
      var draft=state.scriptDraft;
      if(!draft) return;
      var lines=draft.dialogue_lines||[];
      if(action==='add'){
        var firstCharacter=(project.characters||[])[0]||{};
        lines.push({
          id:'',client_token:dialogueClientToken(),
          character_key:firstCharacter.character_key||'narrator',
          speaker_name_snapshot:firstCharacter.name||'旁白',
          text:'',subtitle_enabled:true,voice_overrides:{}
        });
      }else if(action==='copy'&&lines[index]){
        var copy=cloneValue(lines[index]);
        copy.id='';copy.client_token=dialogueClientToken();
        lines.splice(index+1,0,copy);
      }else if(action==='delete'&&lines[index]){
        lines.splice(index,1);
      }
      state.scriptDraftDirty=true;state.error='';render();
    }
    function resolveScriptConflict(index,choice){
      var conflict=(state.scriptConflicts||[])[index];
      if(!conflict||!state.scriptDraft) return;
      if(choice==='remote'){
        if(conflict.scope==='script'){
          state.scriptDraft[conflict.field]=cloneValue(conflict.remote);
        }else{
          var lines=state.scriptDraft.dialogue_lines||[];
          if(conflict.field==='__order__'){
            var byKey=Object.create(null),ordered=[];
            lines.forEach(function(line){ byKey[scriptLineKey(line)]=line; });
            (conflict.remote||[]).forEach(function(key){
              if(byKey[key]){ ordered.push(byKey[key]);delete byKey[key]; }
            });
            lines.forEach(function(line){
              if(byKey[scriptLineKey(line)]) ordered.push(line);
            });
            state.scriptDraft.dialogue_lines=ordered;
            lines=ordered;
          }
          var position=-1;
          lines.some(function(line,lineIndex){
            if(scriptLineKey(line)===conflict.key){ position=lineIndex;return true; }
            return false;
          });
          if(conflict.field==='__order__'){
            position=-1;
          }else if(conflict.field==='__line__'){
            if(conflict.remote==null&&position>=0) lines.splice(position,1);
            else if(conflict.remote!=null&&position<0) lines.push(cloneValue(conflict.remote));
            else if(conflict.remote!=null) lines[position]=cloneValue(conflict.remote);
          }else if(position>=0){
            lines[position][conflict.field]=cloneValue(conflict.remote);
          }
        }
      }
      state.scriptConflicts.splice(index,1);
      state.scriptDraftDirty=true;
      state.error=state.scriptConflicts.length?
        '仍有 '+state.scriptConflicts.length+' 处剧本冲突待处理。':'';
      render();
    }
    function needsAvatarCandidates(){
      return (state.characterDrafts||project&&project.characters||[]).some(function(character){
        return character.source_type==='cinematic_avatar';
      });
    }
    function loadAvatarCandidates(force){
      if(!project||!needsAvatarCandidates()||
          !client||typeof client.getAvatarCandidates!=='function'){
        return Promise.resolve(state.avatarCandidates);
      }
      if(state.avatarCandidates.loading) return Promise.resolve(state.avatarCandidates);
      if(state.avatarCandidates.loaded&&!force) return Promise.resolve(state.avatarCandidates);
      captureCharacterDrafts();
      state.avatarCandidates.loading=true;state.avatarCandidates.error='';render();
      return Promise.resolve(client.getAvatarCandidates(project.id,!!force)).then(function(result){
        state.avatarCandidates={
          items:Array.isArray(result&&result.items)?result.items:[],
          loaded:true,loading:false,error:'',
          canCreate:!!(result&&result.can_create_avatar)
        };
        render();
        return state.avatarCandidates;
      }).catch(function(error){
        state.avatarCandidates.loading=false;
        state.avatarCandidates.loaded=true;
        state.avatarCandidates.error='形象库加载失败，请重试';
        render();
        return state.avatarCandidates;
      });
    }
    function loadAssetGraph(){
      if(!project||!client||typeof client.getAssetGraph!=='function') return Promise.resolve(null);
      state.assetGraphLoading=true;state.assetGraphError='';render();
      return Promise.resolve(client.getAssetGraph(project.id)).then(function(result){
        state.assetGraph=result||null;state.assetGraphLoading=false;render();return result;
      }).catch(function(error){
        state.assetGraphLoading=false;
        state.assetGraphError=workspaceErrorMessage(error);
        render();return null;
      });
    }
    function syncAssetGraph(){
      ensureCanMutate();
      if(!client||typeof client.syncAssetGraph!=='function'){
        return Promise.reject(new Error('资产图谱客户端未加载'));
      }
      var revision=Number(state.assetGraph&&state.assetGraph.graph_revision)||1;
      state.busy=true;state.assetGraphError='';render();
      return Promise.resolve(client.syncAssetGraph(project.id,revision)).then(function(){
        state.busy=false;return loadAssetGraph();
      }).catch(function(error){ showWorkspaceError(error);throw error; });
    }
    function createAsset(){
      ensureCanMutate();
      if(!client||typeof client.createAsset!=='function'){
        return Promise.reject(new Error('资产图谱客户端未加载'));
      }
      var revision=Number(state.assetGraph&&state.assetGraph.graph_revision)||1;
      var typeNode=host&&host.querySelector('[data-asset-field="asset_type"]');
      var nameNode=host&&host.querySelector('[data-asset-field="name"]');
      var descriptionNode=host&&host.querySelector('[data-asset-field="description"]');
      var name=String(nameNode&&nameNode.value||'').trim();
      if(!name) return Promise.reject(new Error('请填写资产名称'));
      var assetType=String(typeNode&&typeNode.value||'prop');
      var assetKey=assetType+':custom:'+Date.now().toString(36);
      state.busy=true;state.assetGraphError='';render();
      return Promise.resolve(client.createAsset(project.id,revision,{
        asset_key:assetKey,asset_type:assetType,name:name,
        description:String(descriptionNode&&descriptionNode.value||'').trim()
      })).then(function(){ state.busy=false;return loadAssetGraph(); }).catch(function(error){
        showWorkspaceError(error);throw error;
      });
    }
    function bindAsset(){
      ensureCanMutate();
      if(!client||typeof client.bindAsset!=='function'){
        return Promise.reject(new Error('资产图谱客户端未加载'));
      }
      var revision=Number(state.assetGraph&&state.assetGraph.graph_revision)||0;
      var shotNode=host&&host.querySelector('[data-asset-bind="shot_id"]');
      var entityNode=host&&host.querySelector('[data-asset-bind="entity_id"]');
      var relationNode=host&&host.querySelector('[data-asset-bind="relation_type"]');
      if(!revision||!shotNode||!entityNode) return Promise.reject(new Error('请先同步基础资产'));
      state.busy=true;state.assetGraphError='';render();
      return Promise.resolve(client.bindAsset(project.id,revision,{
        shot_id:shotNode.value,entity_id:entityNode.value,
        relation_type:String(relationNode&&relationNode.value||'related')
      })).then(function(){ state.busy=false;return loadAssetGraph(); }).catch(function(error){
        showWorkspaceError(error);throw error;
      });
    }
    function lockAssetVersion(versionId){
      ensureCanMutate();
      var revision=Number(state.assetGraph&&state.assetGraph.graph_revision)||0;
      if(!revision) return Promise.reject(new Error('请先同步基础资产'));
      state.busy=true;state.assetGraphError='';render();
      return Promise.resolve(client.lockAssetVersion(project.id,revision,versionId)).then(function(){
        state.busy=false;return loadAssetGraph();
      }).catch(function(error){ showWorkspaceError(error);throw error; });
    }
    function buildAssetSnapshots(){
      ensureCanMutate();
      var revision=Number(state.assetGraph&&state.assetGraph.graph_revision)||0;
      if(!revision) return Promise.reject(new Error('请先同步并锁定基础资产'));
      var shots=project.shots||[],blocked=[];
      state.busy=true;state.assetGraphError='';render();
      var pending=Promise.resolve();
      shots.forEach(function(shot){
        pending=pending.then(function(){
          return client.buildAssetSnapshot(project.id,revision,shot.id).then(function(result){
            if(result&&result.status==='blocked') blocked=blocked.concat(result.blockers||[]);
          });
        });
      });
      return pending.then(function(){
        state.busy=false;
        state.assetGraphError=blocked.length?
          '仍有 '+blocked.length+' 个未锁定或缺失的镜头资产，请处理后重新生成。':'';
        render();return blocked;
      }).catch(function(error){ showWorkspaceError(error);throw error; });
    }
    function render(){
      if(productionWorkspace){
        var productionContent=productionWorkspace.render();
        return renderProductionFrame(project,state,productionContent);
      }
      var html=renderWorkspace(project,state);
      if(host&&!destroyed) host.innerHTML=html;
      return html;
    }
    function destroyProductionWorkspace(){
      productionGeneration+=1;
      if(!productionWorkspace) return;
      var delegated=productionWorkspace;
      productionWorkspace=null;productionHost=null;
      if(typeof delegated.destroy==='function') delegated.destroy();
    }
    function syncProductionFrame(){
      if(!host||destroyed||!productionWorkspace) return;
      var brand=host.querySelector('.nc-short-drama-brand strong');
      var meta=host.querySelector('.nc-short-drama-brand small');
      var nav=host.querySelector('.nc-short-drama-topbar nav');
      if(brand) brand.textContent=project&&project.title||'新短剧';
      if(meta) meta.textContent=(project&&project.id||options.projectId||'')+' · r'+(project&&project.revision||0);
      if(nav) nav.innerHTML=renderStageNavigation(project,state);
    }
    function dedicatedVoiceModule(){
      return Object.prototype.hasOwnProperty.call(options,'voiceModule')?
        options.voiceModule:(root&&root.HQCanvas&&root.HQCanvas.shortDramaVoice);
    }
    function dedicatedVideoModule(){
      return Object.prototype.hasOwnProperty.call(options,'videoModule')?
        options.videoModule:(root&&root.HQCanvas&&root.HQCanvas.shortDramaVideo);
    }
    function dedicatedAssemblyModule(){
      return Object.prototype.hasOwnProperty.call(options,'assemblyModule')?
        options.assemblyModule:(root&&root.HQCanvas&&(
          root.HQCanvas.shortDramaWorkspace||root.HQCanvas.shortDramaAssembly
        ));
    }
    function isAssemblyProjectStage(stage){
      return stage==='assembly_review'||stage==='completed';
    }
    function isVideoProjectStage(stage){
      return stage==='video_review';
    }
    function acceptProductionSummary(summary,generation){
      if(destroyed||!project||!summary||typeof summary!=='object') return;
      var summaryProjectId=summary.project_id||summary.id;
      if(summaryProjectId&&summaryProjectId!==(project.id||project.project_id)) return;
      var wasVoiceStage=project.stage==='voice_review';
      var wasVideoStage=isVideoProjectStage(project.stage);
      var wasAssemblyStage=isAssemblyProjectStage(project.stage);
      var next=Object.assign({},project);
      ['revision','stage','ratio','spent_points','point_budget','reserved_points'].forEach(function(key){
        if(Object.prototype.hasOwnProperty.call(summary,key)) next[key]=summary[key];
      });
      delete next.progress;
      project=next;
      var isVoiceStage=project.stage==='voice_review';
      var isVideoStage=isVideoProjectStage(project.stage);
      var isAssemblyStage=isAssemblyProjectStage(project.stage);
      var voiceModule=dedicatedVoiceModule();
      var videoModule=dedicatedVideoModule();
      var assemblyModule=dedicatedAssemblyModule();
      var shouldSwitch=(
        wasVoiceStage!==isVoiceStage||
        wasVideoStage!==isVideoStage||
        wasAssemblyStage!==isAssemblyStage
      )&&!!(
        (isVoiceStage&&voiceModule&&typeof voiceModule.createWorkspace==='function')||
        (isVideoStage&&(
          (videoModule&&typeof videoModule.createWorkspace==='function')||
          (options.productionModule&&typeof options.productionModule.createWorkspace==='function')
        ))||
        (isAssemblyStage&&assemblyModule&&typeof assemblyModule.createWorkspace==='function')||
        (!isVoiceStage&&!isVideoStage&&!isAssemblyStage)
      );
      state.activeStage=project.stage;
      state.busy=false;state.error='';state.stale=false;state.loadFailed=false;state.loadStatus=0;
      if(!shouldSwitch) syncProductionFrame();
      onChange(summarizeProject(project));
      if(!shouldSwitch) return Promise.resolve(project);
      if(destroyed||generation!==loadGeneration) return Promise.resolve(null);
      return activateProductionWorkspace(generation).catch(function(error){
        if(destroyed||generation!==loadGeneration) return null;
        showWorkspaceError(error,true);
        return null;
      });
    }
    function batchPromptDetails(quote,bodies){
      var requests=Array.isArray(bodies)?bodies:[];
      var quotes=quote&&Array.isArray(quote.quotes)?quote.quotes:[];
      var invalid='批量报价缺少完整提示词，请重新报价后再试';
      if(!requests.length||quotes.length!==requests.length) throw new Error(invalid);
      var byShot=Object.create(null);
      quotes.forEach(function(item){
        var shotId=cleanText(item&&item.shot_id);
        if(!shotId||Object.prototype.hasOwnProperty.call(byShot,shotId)||
            typeof item.base_prompt!=='string'||
            typeof item.user_direction!=='string'||
            typeof item.compiled_prompt!=='string'||!cleanText(item.compiled_prompt)||
            typeof item.source_prompt_hash!=='string'||
            !/^[a-f0-9]{64}$/.test(item.source_prompt_hash)){
          throw new Error(invalid);
        }
        byShot[shotId]=item;
      });
      return requests.map(function(body,index){
        var shotId=cleanText(body&&body.shot_id);
        var item=shotId&&byShot[shotId];
        if(!item) throw new Error(invalid);
        delete byShot[shotId];
        return '\n\n镜头 '+(index+1)+'（'+shotId+'）'+
          '\n分镜画面提示词：\n'+item.base_prompt+
          (item.user_direction?'\n本次补充要求：\n'+item.user_direction:'')+
          '\n最终提交提示词：\n'+item.compiled_prompt;
      }).join('');
    }
    function confirmProduction(cost,quote,body){
      var points=Math.max(0,Number(cost)||0),message;
      if(quote&&quote.kind==='voice'){
        var lineCount=Number(quote.line_count)||(Array.isArray(body)?body.length:1);
        message='生成 '+lineCount+' 条配音将消耗 '+points+
          ' 点'+(typeof quote.points_left==='number'?'（账户余额 '+quote.points_left+' 点）':'')+
          (typeof quote.budget_left==='number'?'，项目可用预算 '+quote.budget_left+' 点':'')+
          '。每条台词独立生成，失败会自动退款；确认提交吗？';
      }else if(quote&&quote.kind==='video'){
        message='生成电影化身视频将消耗 '+points+' 点'+
          (typeof quote.points_left==='number'?'（账户余额 '+quote.points_left+' 点）':'')+
          (typeof quote.budget_left==='number'?'，项目可用预算 '+quote.budget_left+' 点':'')+
          '。失败任务会自动退款，确认提交吗？';
      }else if(Array.isArray(body)||(quote&&quote.kind==='still-batch')){
        var shotCount=Array.isArray(body)?body.length:Number(quote&&quote.shot_count)||0;
        message='批量生成 '+shotCount+
          ' 个镜头的关键帧（每个镜头 2 张候选）将消耗 '+points+' 点。'+
          batchPromptDetails(quote,body)+'\n\n确认提交吗？';
      }else{
        var shotId=body&&body.shot_id||'当前镜头';
        var candidateCount=Number(body&&body.count)||Number(quote&&quote.count)||2;
        message='生成镜头 '+shotId+' 的 '+candidateCount+
          ' 张关键帧候选将消耗 '+points+' 点。'+
          (quote&&quote.base_prompt?'\n\n分镜画面提示词：\n'+quote.base_prompt:'')+
          (quote&&quote.user_direction?'\n\n本次补充要求：\n'+quote.user_direction:'')+
          (quote&&quote.compiled_prompt?'\n\n最终提交提示词：\n'+quote.compiled_prompt:'')+
          '\n\n确认提交吗？';
      }
      return confirmHook(message);
    }
    function activateProductionWorkspace(generation){
      var activationGeneration=generation==null?loadGeneration:generation;
      if(activationGeneration!==loadGeneration) return Promise.resolve(null);
      var isVoiceStage=project&&project.stage==='voice_review';
      var isVideoStage=project&&isVideoProjectStage(project.stage);
      var isAssemblyStage=project&&isAssemblyProjectStage(project.stage);
      var moduleOption=isVoiceStage?'voiceModule':
        (isVideoStage?'videoModule':
          (isAssemblyStage?'assemblyModule':'productionModule'));
      var legacyProductionModule=Object.prototype.hasOwnProperty.call(options,'productionModule')?
        options.productionModule:(root&&root.HQCanvas&&root.HQCanvas.shortDramaProduction);
      var defaultModule=isVoiceStage?
        (dedicatedVoiceModule()||legacyProductionModule):
        (isVideoStage?
          (dedicatedVideoModule()||legacyProductionModule):
          (isAssemblyStage?
            (dedicatedAssemblyModule()||legacyProductionModule):
            legacyProductionModule));
      var productionModule=Object.prototype.hasOwnProperty.call(options,moduleOption)?
        options[moduleOption]:defaultModule;
      if(destroyed) return Promise.reject(new Error('workspace destroyed'));
      if(!productionModule||typeof productionModule.createWorkspace!=='function'){
        return Promise.reject(new Error('短剧生产工作区未加载，请刷新页面重试'));
      }
      var productionClient=options.apiClient&&typeof options.apiClient.json==='function'?
        options.apiClient:(client&&typeof client.json==='function'?client:null);
      if(!productionClient){
        return Promise.reject(new Error('短剧生产工作区缺少已认证 API 客户端，请刷新页面重试'));
      }
      destroyProductionWorkspace();
      var delegateGeneration=productionGeneration;
      state.activeStage=project.stage;
      if(host&&!destroyed){
        host.innerHTML=renderProductionFrame(project,state,'');
        productionHost=host.querySelector('[data-production-host]');
      }
      var delegated,pendingSummaries=[];
      try{
        delegated=productionModule.createWorkspace({
          projectId:options.projectId,
          boardId:options.boardId,
          project:cloneValue(project),
          client:productionClient,
          host:productionHost,
          canEdit:canEdit&&project.stage!=='completed',
          confirm:confirmProduction,
          onError:function(error){
            if(root&&typeof root.alert==='function'){
              root.alert(workspaceErrorMessage(error));
            }
          },
          onChange:function(summary){
            if(destroyed||activationGeneration!==loadGeneration||delegateGeneration!==productionGeneration){
              return Promise.resolve(null);
            }
            if(!delegated){ pendingSummaries.push(summary);return Promise.resolve(null); }
            if(productionWorkspace!==delegated) return Promise.resolve(null);
            return acceptProductionSummary(summary,activationGeneration);
          }
        });
      }catch(error){
        productionHost=null;
        return Promise.reject(error);
      }
      if(!delegated||typeof delegated.render!=='function'||typeof delegated.destroy!=='function'){
        productionHost=null;
        return Promise.reject(new Error('短剧生产工作区接口无效，请刷新页面重试'));
      }
      productionWorkspace=delegated;
      var readyResult=Promise.resolve(delegated.ready).then(function(){
        return {ok:true};
      },function(error){
        return {ok:false,error:error};
      });
      var pending=Promise.resolve(project);
      pendingSummaries.forEach(function(summary){
        pending=pending.then(function(){
          if(destroyed||activationGeneration!==loadGeneration||delegateGeneration!==productionGeneration||
            productionWorkspace!==delegated) return null;
          return acceptProductionSummary(summary,activationGeneration);
        });
      });
      return pending.then(function(){
        if(destroyed||activationGeneration!==loadGeneration||delegateGeneration!==productionGeneration||
          productionWorkspace!==delegated) return null;
        return readyResult.then(function(result){
          if(destroyed||activationGeneration!==loadGeneration||delegateGeneration!==productionGeneration||
            productionWorkspace!==delegated) return null;
          if(!result.ok) throw result.error;
          render();
          return project;
        });
      }).catch(function(error){
        if(destroyed||activationGeneration!==loadGeneration||delegateGeneration!==productionGeneration||
          productionWorkspace!==delegated) return null;
        throw error;
      });
    }
    function showWorkspaceError(error,loadFailed){
      if(destroyed) return;
      state.busy=false;state.error=workspaceErrorMessage(error);
      state.stale=!!(error&&error.code==='revision_conflict');
      state.loadFailed=!!loadFailed;
      state.loadStatus=loadFailed?Number(error&&error.status)||0:0;
      if(state.planning.running){ state.planning.running=false; state.planning.label=state.error; }
      render();
    }
    function latestScriptVersionOf(value){
      var versions=value&&value.script_versions||[],latest=versions[versions.length-1];
      return latest&&latest.version!=null?String(latest.version):null;
    }
    function acceptProject(next,notify){
      if(destroyed) throw new Error('workspace destroyed');
      if(!next||typeof next!=='object') throw new Error('短剧项目返回数据无效');
      var previousProject=project,previousLatest=latestScriptVersionOf(previousProject);
      project=next;state.busy=false;state.error='';state.stale=false;state.loadFailed=false;state.loadStatus=0;
      if(!state.characterDraftDirty||!previousProject||previousProject.id!==project.id){
        resetCharacterDrafts(project);
      }
      if(!previousProject||previousProject.id!==project.id||previousLatest!==latestScriptVersionOf(project)){
        state.scriptVersion=null;
        if(state.scriptDraftDirty&&previousProject&&previousProject.id===project.id&&state.scriptDraft){
          var latestScript=(project.script_versions||[]).slice(-1)[0]||{};
          var mergedScript=mergeScriptDrafts(
            state.scriptDraftBase||(previousProject.script_versions||[]).slice(-1)[0]||{},
            latestScript,state.scriptDraft,project.dialogue_token_receipts||{}
          );
          state.scriptDraft=mergedScript.script;
          state.scriptDraftBase=cloneValue(latestScript);
          state.scriptDraftRevision=Number(project.revision);
          state.scriptConflicts=mergedScript.conflicts;
          if(mergedScript.conflicts.length){
            state.error='项目已更新，草稿存在 '+mergedScript.conflicts.length+
              ' 处冲突。请选择保留本地内容或采用服务器内容。';
          }
        }else{
          resetScriptDraft(project);
        }
      }
      if(isProductionStage(project.stage)) state.activeStage=project.stage;
      else if(!isStageEnabled(project,state.activeStage)) state.activeStage=project.stage==='draft'?'settings':project.stage;
      if(notify) onChange(summarizeProject(project));
      if(!isProductionStage(project.stage)) render();
      return project;
    }
    function acceptAndMaybeDelegate(next,notify,generation){
      var accepted=acceptProject(next,notify);
      if(!isProductionStage(accepted.stage)) return Promise.resolve(accepted);
      return activateProductionWorkspace(generation).catch(function(error){
        if(generation!=null&&generation!==loadGeneration) return null;
        if(!notify) throw error;
        showWorkspaceError(error,true);
        return accepted;
      });
    }
    function loadProject(){
      if(destroyed) return Promise.reject(new Error('workspace destroyed'));
      var generation=++loadGeneration;
      destroyProductionWorkspace();
      state.busy=true;state.error='';state.loadFailed=false;state.loadStatus=0;render();
      return Promise.resolve(client.get(options.projectId)).then(function(next){
        if(destroyed||generation!==loadGeneration) return null;
        state.synopsisSaved=canGeneratePlan(next,true);
        if(next&&next.stage!=='draft') state.activeStage=next.stage;
        return acceptAndMaybeDelegate(next,false,generation).then(function(accepted){
          if(!accepted||destroyed||generation!==loadGeneration) return accepted;
          return Promise.all([loadAvatarCandidates(false),loadAssetGraph()]).then(function(){
            return accepted;
          });
        });
      }).catch(function(error){
        if(destroyed||generation!==loadGeneration) return null;
        showWorkspaceError(error,true);
        return null;
      });
    }
    function ensureCanMutate(stage){
      if(destroyed) throw new Error('workspace destroyed');
      if(state.busy) throw new Error('workspace busy');
      if(!canEdit) throw new Error('read-only workspace');
      if(!project) throw new Error('project is not loaded');
      if(state.stale) throw new Error('请先刷新项目版本');
      if(stage&&!isStageEditable(project,stage,canEdit)) throw new Error('stage is not editable');
    }
    function lockMountedControls(){
      var controls=host&&host.querySelectorAll?
        Array.prototype.slice.call(host.querySelectorAll('input,textarea,select,button')): [];
      var previous=controls.map(function(control){
        return {control:control,disabled:control.disabled===true};
      });
      previous.forEach(function(item){ item.control.disabled=true; });
      if(host&&host.setAttribute) host.setAttribute('aria-busy','true');
      var released=false;
      return function(){
        if(released) return;
        released=true;
        previous.forEach(function(item){ item.control.disabled=item.disabled; });
        if(host&&host.removeAttribute) host.removeAttribute('aria-busy');
      };
    }
    function savePatch(patch,stage){
      try{ ensureCanMutate(stage); }catch(error){ return Promise.reject(error); }
      state.busy=true;state.error='';
      // Keep the live form mounted while the request is in flight. Re-rendering here
      // rebuilds every textarea from the last persisted project and visibly discards
      // the character prompts that are currently being saved. Lock the mounted
      // controls so the request snapshot cannot diverge from editable DOM values.
      var unlock=lockMountedControls();
      return Promise.resolve(client.update(project.id,project.revision,patch)).then(function(next){
        unlock();
        return acceptProject(next,true);
      }).catch(function(error){ unlock();showWorkspaceError(error);throw error; });
    }
    function saveSettings(value){
      var errors=validateSettings(value);
      if(errors.length) return Promise.reject(new Error(errors.join('\n')));
      var patch=makeSettingsPatch(value);
      return savePatch(patch,'settings').then(function(next){
        state.synopsisSaved=canGeneratePlan(next,true);render();return next;
      });
    }
    function deleteProject(){
      try{ ensureCanMutate(); }catch(error){ return Promise.reject(error); }
      var confirmed=true;
      if(typeof options.confirmDelete==='function') confirmed=options.confirmDelete(project)!==false;
      else if(typeof window!=='undefined'&&typeof window.confirm==='function'){
        confirmed=window.confirm('删除后该短剧项目将无法继续打开，确认删除？');
      }
      if(!confirmed) return Promise.resolve(null);
      state.busy=true;state.error='';render();
      return Promise.resolve(client.delete(project.id,project.revision)).then(function(result){
        if(typeof options.onDelete==='function') options.onDelete(result);
        destroy();
        return result;
      }).catch(function(error){ showWorkspaceError(error); throw error; });
    }
    function submitCharacters(value,recoverRevisionConflict){
      var patch=makeCharactersPatch(value);
      state.characterDrafts=cloneValue(patch.characters);
      state.characterDraftDirty=true;
      state.characterConflicts=[];
      try{ ensureCanMutate('characters_review'); }catch(error){ return Promise.reject(error); }
      state.busy=true;state.error='';
      var unlock=lockMountedControls();
      return Promise.resolve(client.update(
        project.id,project.revision,patch
      )).then(function(next){
        unlock();
        var accepted=acceptProject(next,true);
        resetCharacterDrafts(accepted);
        render();
        return accepted;
      }).catch(function(error){
        unlock();
        if(recoverRevisionConflict&&error&&error.code==='revision_conflict'&&
            typeof client.get==='function'){
          return Promise.resolve(client.get(project.id)).then(function(latest){
            var merged=mergeCharacterDrafts(
              state.characterDraftBase||project.characters||[],
              latest&&latest.characters||[],
              state.characterDrafts||[]
            );
            project=latest;
            state.characterDraftBase=cloneValue(latest.characters||[]);
            state.characterDraftRevision=Number(latest.revision);
            state.characterDrafts=cloneValue(merged.characters);
            state.characterDraftDirty=true;
            state.stale=false;state.busy=false;
            state.characterConflicts=merged.conflicts;
            if(merged.conflicts.length){
              var labels=merged.conflicts.map(function(item){
                var character=(state.characterDrafts||[]).find(function(candidate){
                  return candidate.character_key===item.character_key;
                });
                return (character&&character.name||item.character_key)+' / '+item.field;
              });
              state.error='项目已在其他页面更新，已保留你的修改。请核对冲突字段：'+
                labels.join('、');
              render();
              var conflict=new Error(state.error);
              conflict.code='character_merge_conflict';
              throw conflict;
            }
            return submitCharacters(state.characterDrafts,false);
          }).catch(function(recoveryError){
            if(recoveryError&&recoveryError.code==='character_merge_conflict'){
              throw recoveryError;
            }
            showWorkspaceError(recoveryError);
            throw recoveryError;
          });
        }
        showWorkspaceError(error);
        throw error;
      });
    }
    function saveCharacters(value){
      var errors=validateCharacters(value);
      if(errors.length){
        state.characterDrafts=cloneValue(makeCharactersPatch(value).characters);
        state.characterDraftDirty=true;
        return Promise.reject(new Error(errors.join('\n')));
      }
      return submitCharacters(value,true);
    }
    function generateCharacterReference(index,value){
      if(!project||!state.canEdit||state.busy||
          ['characters_review','script_review','storyboard_review','stills_review'].indexOf(project.stage)<0){
        return Promise.reject(new Error('当前阶段不能修改角色标准图'));
      }
      var characters=value||readCharacters(),character=characters[index];
      if(!character||character.source_type!=='ai_character'){
        return Promise.reject(new Error('仅 AI 角色需要生成角色标准图'));
      }
      var required=['name','identity_text','personality','appearance_prompt','wardrobe_prompt'];
      if(required.some(function(field){ return !cleanText(character[field]); })){
        return Promise.reject(new Error('请先完整填写角色名称、身份、性格、外观和服装描述'));
      }
      var confirmed=true;
      if(typeof window!=='undefined'&&typeof window.confirm==='function'){
        confirmed=window.confirm('Nano Banana 2 高清角色标准图将消耗 35 点，确认生成吗？');
      }
      if(!confirmed) return Promise.resolve(null);
      var characterKey=character.character_key;
      // The generation endpoint only accepts a character key and reads prompts from
      // the database. Persist the current form first so it cannot generate from (and
      // then re-render) the original AI defaults.
      return saveSectionIfChanged('characters_review',characters).then(function(savedProject){
        var savedCharacter=(savedProject.characters||[]).find(function(item){
          return item.character_key===characterKey;
        });
        if(!savedCharacter) throw new Error('保存后的角色不存在，请刷新后重试');
        state.busy=true;state.error='';render();
        return client.generateCharacterReference(savedProject,savedCharacter);
      }).then(function(){
        return client.get(project.id);
      }).then(function(next){
        return acceptProject(next,true);
      }).catch(function(error){ showWorkspaceError(error); throw error; });
    }
    function ensureLatestScriptVersion(value){
      if(!project) return;
      var versions=project.script_versions||[],latest=versions[versions.length-1]||{};
      var requestedVersion=value&&value.version!=null?Number(value.version):Number(selectedScript(project,state).version);
      if(requestedVersion!==Number(latest.version)) throw new Error('请切换到最新版本后编辑或确认剧本');
    }
    function submitScript(value,recoverRevisionConflict){
      var latest=(project.script_versions||[]).slice(-1)[0]||{};
      state.scriptDraft=cloneValue(value);
      state.scriptDraft.version=latest.version;
      state.scriptDraftDirty=true;
      try{ ensureCanMutate('script_review'); }catch(error){ return Promise.reject(error); }
      state.busy=true;state.error='';
      var unlock=lockMountedControls();
      return Promise.resolve(client.update(
        project.id,project.revision,makeScriptPatch(state.scriptDraft)
      )).then(function(next){
        unlock();
        var accepted=acceptProject(next,true);
        resetScriptDraft(accepted);
        render();
        return accepted;
      }).catch(function(error){
        unlock();
        if(recoverRevisionConflict&&error&&error.code==='revision_conflict'&&
            typeof client.get==='function'){
          return Promise.resolve(client.get(project.id)).then(function(latestProject){
            var latestScript=(latestProject.script_versions||[]).slice(-1)[0]||{};
            var merged=mergeScriptDrafts(
              state.scriptDraftBase||{},latestScript,state.scriptDraft||{},
              latestProject.dialogue_token_receipts||{}
            );
            project=latestProject;
            state.scriptDraftBase=cloneValue(latestScript);
            state.scriptDraftRevision=Number(latestProject.revision);
            state.scriptDraft=cloneValue(merged.script);
            state.scriptDraftDirty=true;state.stale=false;state.busy=false;
            state.scriptConflicts=merged.conflicts;
            if(merged.conflicts.length){
              state.error='项目已更新，发现 '+merged.conflicts.length+
                ' 处剧本冲突。请逐项处理后再保存。';
              render();
              var conflictError=new Error(state.error);
              conflictError.code='script_merge_conflict';
              throw conflictError;
            }
            return submitScript(state.scriptDraft,false);
          }).catch(function(recoveryError){
            if(recoveryError&&recoveryError.code==='script_merge_conflict'){
              throw recoveryError;
            }
            showWorkspaceError(recoveryError);
            throw recoveryError;
          });
        }
        showWorkspaceError(error);
        throw error;
      });
    }
    function saveScript(value){
      try{ ensureLatestScriptVersion(value); }catch(error){ return Promise.reject(error); }
      if((state.scriptConflicts||[]).length){
        return Promise.reject(new Error('请先处理全部剧本冲突'));
      }
      var errors=validateScript(value,project);
      if(errors.length) return Promise.reject(new Error(errors.join('\n')));
      return submitScript(value,true);
    }
    function saveShots(value){
      var errors=validateShots(value,project);
      if(errors.length) return Promise.reject(new Error(errors.join('\n')));
      return savePatch(makeShotsPatch(value),'storyboard_review');
    }
    function currentSectionValue(stage,value){
      if(value!==undefined) return value;
      if(host){
        if(stage==='characters_review') return readCharacters();
        if(stage==='script_review') return readScript();
        if(stage==='storyboard_review') return readShots();
      }
      if(stage==='characters_review') return project.characters||[];
      if(stage==='script_review') return (project.script_versions||[]).slice(-1)[0]||{};
      return project.shots||[];
    }
    function sectionPatch(stage,value){
      if(stage==='characters_review') return makeCharactersPatch(value);
      if(stage==='script_review') return makeScriptPatch(value);
      return makeShotsPatch(value);
    }
    function currentSectionPatch(stage){
      if(stage==='characters_review') return makeCharactersPatch(project.characters||[]);
      if(stage==='script_review') return makeScriptPatch((project.script_versions||[]).slice(-1)[0]||{});
      return makeShotsPatch(project.shots||[]);
    }
    function saveSectionIfChanged(stage,value){
      var candidate=currentSectionValue(stage,value);
      if(JSON.stringify(sectionPatch(stage,candidate))===JSON.stringify(currentSectionPatch(stage))){
        return Promise.resolve(project);
      }
      if(stage==='characters_review') return saveCharacters(candidate);
      if(stage==='script_review') return saveScript(candidate);
      return saveShots(candidate);
    }
    function confirm(stage,value){
      var candidate,expectedPatch;
      try{
        ensureCanMutate();
        if(project.stage!==stage) throw new Error('confirmation order must match the current stage');
        ensureCanMutate(stage);
        if(stage==='script_review') ensureLatestScriptVersion(value);
        candidate=currentSectionValue(stage,value);
        expectedPatch=sectionPatch(stage,candidate);
      }catch(error){ return Promise.reject(error); }
      return saveSectionIfChanged(stage,candidate).then(function(){
        if(destroyed) throw new Error('workspace destroyed');
        if(project.stage!==stage) throw new Error('confirmation order must match the current stage');
        if(stage==='storyboard_review'&&
          JSON.stringify(currentSectionPatch(stage))!==JSON.stringify(expectedPatch)){
          throw new Error('服务器保存后的分镜与当前编辑内容不一致，请刷新后重试');
        }
        state.busy=true;state.error='';render();
        return Promise.resolve(client.confirm(project.id,project.revision,stage));
      }).then(function(next){
          state.activeStage=next.stage;
          return acceptAndMaybeDelegate(next,true);
        }).catch(function(error){ showWorkspaceError(error); throw error; });
    }
    function generatePlan(){
      try{
        ensureCanMutate('settings');
        if(!canGeneratePlan(project,state.synopsisSaved)) throw new Error('saved synopsis is required; placeholder synopsis cannot be submitted');
      }catch(error){ return Promise.reject(error); }
      state.busy=true;state.error='';state.planning={running:false,percent:0,label:'正在查询实时价格',cost:null};render();
      if(typeof client.getPlanningQuote!=='function'){
        var missingQuote=new Error('short drama client requires planning quote support');
        showWorkspaceError(missingQuote);return Promise.reject(missingQuote);
      }
      return Promise.resolve(client.getPlanningQuote()).then(function(quote){
        if(destroyed) throw new Error('workspace destroyed');
        var cost=quote&&quote.cost;
        if(typeof cost!=='number'||!isFinite(cost)||Math.floor(cost)!==cost||cost<0){
          throw new Error('短剧策划报价无效，请稍后重试');
        }
        state.planning.cost=cost;state.planning.label='实时报价 '+cost+' 点';state.busy=false;render();
        if(!confirmHook('生成短剧策划将消耗 '+cost+' 点，确认提交吗？')) return null;
        state.busy=true;state.planning.running=true;state.planning.percent=15;
        state.planning.label='已按 '+cost+' 点确认，正在提交';render();
        return Promise.resolve(client.generatePlan(project,{
          onCost:function(submittedCost){
            if(destroyed) return;
            if(typeof submittedCost==='number'&&isFinite(submittedCost)&&submittedCost>=0){
              state.planning.cost=submittedCost;
            }
            state.planning.label='任务已受理，正在生成策划';render();
          },
          onProgress:function(progress){
            if(destroyed) return;
            state.planning.percent=Math.max(state.planning.percent,Math.min(80,Number(progress&&progress.percent)||20));
            state.planning.label=progress&&progress.label||'正在轮询策划进度';render();
          }
        })).then(function(applied){
          if(destroyed) throw new Error('workspace destroyed');
          state.planning.percent=85;state.planning.label='策划已生成，正在刷新项目';render();
          return typeof client.get==='function'?client.get(project.id):applied;
        }).then(function(next){
          state.planning.running=false;state.planning.percent=100;state.planning.label='策划已应用';
          state.synopsisSaved=true;return acceptProject(next,true);
        });
      }).catch(function(error){ showWorkspaceError(error); throw error; });
    }

    function valueFrom(container,name){
      var input=container&&container.querySelector('[data-field="'+name+'"]');
      return input?input.value:'';
    }
    function readDialogueLines(form){
      return Array.prototype.map.call(
        form.querySelectorAll('.nc-short-drama-dialogue[data-dialogue-index]'),
        function(row){
          return {
            id:valueFrom(row,'id'),character_key:valueFrom(row,'character_key'),text:valueFrom(row,'text')
          };
        }
      );
    }
    function readSettings(){
      var form=host.querySelector('.nc-short-drama-settings-form');
      return {title:valueFrom(form,'title'),synopsis:valueFrom(form,'synopsis'),ratio:valueFrom(form,'ratio'),
        target_duration:Number(valueFrom(form,'target_duration')),shot_count:Number(valueFrom(form,'shot_count')),
        visual_style:valueFrom(form,'visual_style'),target_platform:valueFrom(form,'target_platform'),
        point_budget:Number(valueFrom(form,'point_budget'))};
    }
    function readCharacters(allowInvalidJson){
      return Array.prototype.map.call(host.querySelectorAll('[data-character-index]'),function(card){
        var voiceSettings,voiceSettingsRaw=valueFrom(card,'voice_settings')||'{}';
        try{ voiceSettings=JSON.parse(voiceSettingsRaw); }
        catch(error){
          if(!allowInvalidJson) throw new Error('语音设置必须是有效 JSON 对象');
          voiceSettings={};
        }
        var character={character_key:valueFrom(card,'character_key'),name:valueFrom(card,'name'),identity_text:valueFrom(card,'identity_text'),
          personality:valueFrom(card,'personality'),source_type:valueFrom(card,'source_type'),avatar_id:valueFrom(card,'avatar_id'),
          appearance_prompt:valueFrom(card,'appearance_prompt'),wardrobe_prompt:valueFrom(card,'wardrobe_prompt'),
          reference_job_id:valueFrom(card,'reference_job_id'),reference_locked:valueFrom(card,'reference_locked')==='true',
          voice_key:valueFrom(card,'voice_key'),voice_settings:voiceSettings};
        if(allowInvalidJson) character._voice_settings_raw=voiceSettingsRaw;
        return character;
      });
    }
    function readScript(){
      var form=host.querySelector('.nc-short-drama-script-form');
      return {title:valueFrom(form,'title'),logline:valueFrom(form,'logline'),hook:valueFrom(form,'hook'),
        conflict_text:valueFrom(form,'conflict_text'),turn_text:valueFrom(form,'turn_text'),ending:valueFrom(form,'ending'),
        dialogue_lines:readDialogueLines(form)};
    }
    function readShots(){
      return Array.prototype.map.call(host.querySelectorAll('[data-shot-index]'),function(card){
        return {shot_key:valueFrom(card,'shot_key'),duration:Number(valueFrom(card,'duration')),
          scene_description:valueFrom(card,'scene_description'),camera_description:valueFrom(card,'camera_description'),
          character_keys:asList(valueFrom(card,'character_keys')),dialogue_line_ids:asList(valueFrom(card,'dialogue_line_ids')),
          image_prompt:valueFrom(card,'image_prompt'),video_prompt:valueFrom(card,'video_prompt')};
      });
    }
    function selectWorkspaceStage(stage){
      if(!project||!isStageEnabled(project,stage)) return false;
      if(isProductionStage(stage)){
        if(stage!==project.stage) return false;
        state.activeStage=stage;
        if(productionWorkspace) render();
        else activateProductionWorkspace().catch(function(error){ showWorkspaceError(error,true); });
        return true;
      }
      destroyProductionWorkspace();
      state.activeStage=stage;render();
      return true;
    }
    function runDomAction(promise){ Promise.resolve(promise).catch(function(error){ if(!state.error){ state.error=workspaceErrorMessage(error); render(); } }); }
    function handleClick(event){
      var jump=findActionTarget(event.target,'data-character-jump',host);
      if(jump){
        var characterIndex=Number(jump.getAttribute('data-character-jump'));
        if(project&&isStageEnabled(project,'characters_review')){
          state.activeStage='characters_review';render();
          var characterCard=host&&host.querySelector('[data-character-index="'+characterIndex+'"]');
          if(characterCard){
            if(characterCard.scrollIntoView) characterCard.scrollIntoView({block:'center',behavior:'smooth'});
            var characterField=characterCard.querySelector&&characterCard.querySelector('input,textarea,select');
            if(characterField&&characterField.focus) characterField.focus();
          }
        }
        return;
      }
      var tab=findActionTarget(event.target,'data-tab',host);
      if(tab){ selectWorkspaceStage(tab.getAttribute('data-tab'));return; }
      var version=findActionTarget(event.target,'data-script-version',host);
      if(version){ state.scriptVersion=Number(version.getAttribute('data-script-version'));render();return; }
      var confirmButton=findActionTarget(event.target,'data-confirm-stage',host);
      if(confirmButton){ runDomAction(confirm(confirmButton.getAttribute('data-confirm-stage')));return; }
      var action=findActionTarget(event.target,'data-action',host);
      if(!action) return;
      var actionTarget=action;
      action=action.getAttribute('data-action');
      try{
        if(action==='close') return destroy();
        if(action==='reload') return runDomAction(loadProject());
        if(action==='delete-project') return runDomAction(deleteProject());
        if(action==='save-settings') return runDomAction(saveSettings(readSettings()));
        if(action==='save-characters') return runDomAction(saveCharacters(readCharacters()));
        if(action==='select-character-avatar'){
          captureCharacterDrafts();
          var avatarIndex=Number(actionTarget.getAttribute('data-avatar-index'));
          var avatarId=cleanText(actionTarget.getAttribute('data-avatar-id'));
          if(state.characterDrafts&&state.characterDrafts[avatarIndex]){
            state.characterDrafts[avatarIndex].source_type='cinematic_avatar';
            state.characterDrafts[avatarIndex].avatar_id=avatarId;
            state.characterDraftDirty=true;state.error='';render();
          }
          return;
        }
        if(action==='refresh-character-avatars'){
          return runDomAction(loadAvatarCandidates(true));
        }
        if(action==='create-character-avatar'){
          if(typeof window!=='undefined'&&typeof window.open==='function'){
            window.open(
              '/workbench/video.html?function=cinematic&action=create-avatar',
              '_blank','noopener'
            );
          }
          return;
        }
        if(action==='generate-character-reference'){
          return runDomAction(generateCharacterReference(Number(actionTarget.getAttribute('data-reference-index'))));
        }
        if(action==='add-dialogue'){
          mutateDialogue('add',-1);return;
        }
        if(action==='copy-dialogue'){
          mutateDialogue('copy',Number(actionTarget.getAttribute('data-dialogue-index')));return;
        }
        if(action==='delete-dialogue'){
          mutateDialogue('delete',Number(actionTarget.getAttribute('data-dialogue-index')));return;
        }
        if(action==='resolve-script-conflict'){
          resolveScriptConflict(
            Number(actionTarget.getAttribute('data-conflict-index')),
            actionTarget.getAttribute('data-conflict-choice')
          );
          return;
        }
        if(action==='save-script') return runDomAction(saveScript(readScript()));
        if(action==='save-shots') return runDomAction(saveShots(readShots()));
        if(action==='sync-asset-graph') return runDomAction(syncAssetGraph());
        if(action==='create-asset') return runDomAction(createAsset());
        if(action==='bind-asset') return runDomAction(bindAsset());
        if(action==='lock-asset-version'){
          return runDomAction(lockAssetVersion(actionTarget.getAttribute('data-version-id')));
        }
        if(action==='build-asset-snapshots') return runDomAction(buildAssetSnapshots());
        if(action==='generate-plan') return runDomAction(generatePlan());
      }catch(error){ state.error=workspaceErrorMessage(error);render(); }
    }
    function handleChange(event){
      var target=event&&event.target;
      if(!target||!host) return;
      if(target.closest&&target.closest('[data-character-index]')){
        captureCharacterDrafts();
        if(target.getAttribute('data-field')==='source_type'){
          var card=target.closest('[data-character-index]');
          var index=Number(card&&card.getAttribute('data-character-index'));
          if(state.characterDrafts&&state.characterDrafts[index]){
            state.characterDrafts[index].source_type=target.value;
            if(target.value!=='cinematic_avatar'){
              state.characterDrafts[index].avatar_id=null;
            }
            state.characterDraftDirty=true;render();
            if(target.value==='cinematic_avatar'){
              loadAvatarCandidates(false);
            }
          }
        }
        return;
      }
      if(target.closest&&target.closest('[data-dialogue-index]')){
        captureScriptDraft();
        return;
      }
      if(target.getAttribute('data-field')!=='target_duration') return;
      var shotSelect=host.querySelector('[data-field="shot_count"]');
      if(!shotSelect) return;
      var counts=validShotCounts(Number(target.value));
      var current=Number(shotSelect.value);
      shotSelect.innerHTML=counts.map(function(count){
        return '<option value="'+count+'">'+count+' 镜</option>';
      }).join('');
      shotSelect.value=String(counts.indexOf(current)>=0?current:counts[0]);
    }
    function handleInput(event){
      var target=event&&event.target;
      if(target&&target.closest&&target.closest('[data-character-index]')){
        captureCharacterDrafts();
      }
      if(target&&target.closest&&target.closest('[data-dialogue-index]')){
        captureScriptDraft();
      }
    }
    function destroy(){
      if(destroyed) return;
      destroyed=true;
      loadGeneration+=1;
      state.destroyed=true;state.busy=false;
      destroyProductionWorkspace();
      if(host&&host.removeEventListener) host.removeEventListener('click',handleClick);
      if(host&&host.removeEventListener) host.removeEventListener('change',handleChange);
      if(host&&host.removeEventListener) host.removeEventListener('input',handleInput);
      if(host&&host.parentNode) host.parentNode.removeChild(host);
      host=null;
    }
    if(doc&&doc.createElement&&doc.body){
      host=doc.createElement('div');host.className='nc-short-drama-host';
      host.addEventListener('click',handleClick);host.addEventListener('change',handleChange);
      host.addEventListener('input',handleInput);doc.body.appendChild(host);render();
    }
    var ready=loadProject();
    function reloadWorkspace(){
      if(productionWorkspace&&typeof productionWorkspace.reload==='function') return productionWorkspace.reload();
      return loadProject();
    }
    return {
      projectId:options.projectId||null,client:client,ready:ready,destroy:destroy,render:render,
      getProject:function(){ return cloneValue(project); },getState:snapshotState,
      selectStage:selectWorkspaceStage,
      reload:reloadWorkspace,saveSettings:saveSettings,saveCharacters:saveCharacters,saveScript:saveScript,
      saveShots:saveShots,deleteProject:deleteProject,confirm:confirm,generatePlan:generatePlan,
      generateCharacterReference:generateCharacterReference,
      canGeneratePlan:function(){ return canGeneratePlan(project,state.synopsisSaved); }
    };
  }

  return {
    validShotCounts:validShotCounts,
    normalizeSettings:normalizeSettings,
    normalizeNodeParams:normalizeNodeParams,
    sanitizeNodeData:sanitizeNodeData,
    creationPayload:creationPayload,
    canOpenNode:canOpenNode,
    createProjectCoordinator:createProjectCoordinator,
    mergeScriptDrafts:mergeScriptDrafts,
    stageIndex:stageIndex,
    summarizeProject:summarizeProject,
    planningPayload:planningPayload,
    createClient:createClient,
    PLACEHOLDER_SYNOPSIS:PLACEHOLDER_SYNOPSIS,
    isStageEnabled:isStageEnabled,
    isStageEditable:isStageEditable,
    isRoleDowngrade:isRoleDowngrade,
    makeSettingsPatch:makeSettingsPatch,
    makeCharactersPatch:makeCharactersPatch,
    mergeCharacterDrafts:mergeCharacterDrafts,
    makeScriptPatch:makeScriptPatch,
    makeShotsPatch:makeShotsPatch,
    validateSettings:validateSettings,
    validateCharacters:validateCharacters,
    validateScript:validateScript,
    validateShots:validateShots,
    canGeneratePlan:canGeneratePlan,
    workspaceErrorMessage:workspaceErrorMessage,
    renderLoadState:renderLoadState,
    renderWorkspace:renderWorkspace,
    createWorkspace:createWorkspace
  };
});
