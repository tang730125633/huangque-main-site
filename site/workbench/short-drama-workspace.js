(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root) root.HQShortDramaWorkspace=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function text(value){return String(value==null?'':value);}
  function shotReferenceSelectionPolicy(currentIdentity,previousIdentity,previousTailReady){
    currentIdentity=currentIdentity||{};previousIdentity=previousIdentity||{};
    var currentKey=text(currentIdentity.scene_key).trim(),currentVersion=text(currentIdentity.reference_identity).trim();
    var previousKey=text(previousIdentity.scene_key).trim(),previousVersion=text(previousIdentity.reference_identity).trim();
    var hasScene=!!currentKey;
    var same=!!(previousTailReady&&hasScene&&currentVersion&&currentKey===previousKey&&currentVersion===previousVersion);
    var selectedLimit=same?4:5;
    return {
      same_scene_reference:same,
      tail_required:same,
      selected_reference_limit:selectedLimit,
      character_limit:Math.max(0,selectedLimit-(hasScene?1:0))
    };
  }
  function effectiveSceneReferenceIdentity(selectedIdentity,defaultIdentity){
    selectedIdentity=selectedIdentity||{};defaultIdentity=defaultIdentity||{};
    var selectedKey=text(selectedIdentity.scene_key).trim();
    var identity=selectedKey?selectedIdentity:defaultIdentity;
    return {
      scene_key:text(identity.scene_key).trim(),
      reference_identity:text(identity.reference_identity).trim()
    };
  }
  function dialogueReadingSeconds(value,speechRate){
    var length=text(value).replace(/[\s，。！？、；：“”‘’…]/g,'').length;
    var rate=Number(speechRate)||1;
    if([1,1.15,1.3,1.5,2].indexOf(rate)<0)rate=1;
    return Math.round(((.45+length/3.5)/rate)*100)/100;
  }
  function shotDialogueValues(values){
    values=values||{};
    if(Array.isArray(values.dialogues))return values.dialogues.map(function(item,index){
      item=item||{};
      return {id:text(item.id).trim(),kind:text(item.kind||'dialogue'),character_key:text(item.character_key).trim(),text:text(item.text),speech_rate:Number(item.speech_rate)||1,timing_mode:index>0&&text(item.timing_mode)==='simultaneous'?'simultaneous':'sequential'};
    });
    var kind=text(values.dialogue_kind||'dialogue');
    if(kind==='silence')return [];
    var legacyDialogue={kind:kind,character_key:text(values.character_key).trim(),text:text(values.dialogue_text),speech_rate:values.speech_rate,timing_mode:'sequential'};
    legacyDialogue.speech_rate=Number(legacyDialogue.speech_rate)||1;
    return [legacyDialogue];
  }
  function editableShotDialogues(lines){
    return (Array.isArray(lines)?lines:[]).filter(function(line){
      return line&&line.kind!=='silence'&&text(line.text).trim();
    }).map(function(line,index){
      return {id:line.id||'',kind:line.kind||'dialogue',character_key:line.character_key||'',text:line.text||'',speech_rate:Number(line.speech_rate)||1,timing_mode:index>0&&text(line.timing_mode)==='simultaneous'?'simultaneous':'sequential'};
    });
  }
  function confirmedShotDialogueHtml(lines){
    var items=(Array.isArray(lines)?lines:[]).filter(function(item){return item&&text(item.kind)!=='silence'&&text(item.text).trim();});
    var content=items.length?'<ol>'+items.map(function(item,index){
      var kind=text(item.kind||'dialogue'),kindLabel=kind==='on_screen_text'?'画面文字':kind==='voiceover'?'旁白':'人物对白';
      var speaker=kind==='on_screen_text'?'':text(item.speaker||item.character_name||item.character_key||(kind==='voiceover'?'旁白':'角色')).trim();
      var simultaneous=index>0&&['simultaneous','simultaneous_with_previous'].indexOf(text(item.timing_mode))>=0;
      var rate=Number(item.speech_rate)||1,meta=[kindLabel];
      if(speaker)meta.push(speaker);
      if(simultaneous)meta.push('与上一条同时说');
      if(kind!=='on_screen_text')meta.push(String(rate)+'×');
      return '<li><div><span>'+meta.map(function(value){return '<em>'+escapeHtml(value)+'</em>';}).join('')+'</div><p>'+escapeHtml(item.text)+'</p></li>';
    }).join('')+'</ol>':'<p class="empty">本镜头无台词，为静默表演。</p>';
    return '<section class="wide sd-confirmed-shot-dialogues"><header><div><b>已确认台词</b><small>来自已锁定剧本，将随本镜头提交给视频模型</small></div><span>只读</span></header>'+content+'</section>';
  }
  function dialogueTimelineSeconds(dialogues,naturalSpeed){
    var total=0,group=0;
    shotDialogueValues({dialogues:dialogues}).forEach(function(item,index){
      var seconds=(item.kind==='dialogue'||item.kind==='voiceover')?dialogueReadingSeconds(item.text,naturalSpeed?1:item.speech_rate):0;
      if(index>0&&item.timing_mode==='simultaneous')group=Math.max(group,seconds);
      else{total+=group;group=seconds;}
    });
    return Math.round((total+group)*100)/100;
  }
  function shotTimingIssue(values){
    values=values||{};
    var duration=Number(values.duration_seconds),dialogues=shotDialogueValues(values);
    if(!Number.isFinite(duration)||duration<4||duration>15)return {field:'duration_seconds',message:'镜头时长必须为 4–15 秒。'};
    if(dialogues.length>6)return {code:'dialogue_count_invalid',field:'dialogue_text',message:'每个镜头最多可设置 6 条台词、旁白或画面文字。'};
    var allowedKinds=['dialogue','voiceover','on_screen_text'],allowedRates=[1,1.15,1.3,1.5,2];
    for(var index=0;index<dialogues.length;index+=1){
      var item=dialogues[index],kind=text(item.kind||'dialogue'),dialogue=text(item.text).trim(),rate=Number(item.speech_rate)||1;
      if(allowedKinds.indexOf(kind)<0)return {field:'dialogue_kind',dialogueIndex:index,message:'第 '+(index+1)+' 条内容类型无效。'};
      if((kind==='dialogue'||kind==='voiceover')&&!text(item.character_key).trim())return {field:'character_key',dialogueIndex:index,message:'第 '+(index+1)+' 条人物对白或旁白必须选择说话角色。'};
      if(!dialogue)return {field:'dialogue_text',dialogueIndex:index,message:'第 '+(index+1)+' 条需要填写台词、旁白或画面文字。'};
      if(dialogue.length>120)return {field:'dialogue_text',dialogueIndex:index,message:'第 '+(index+1)+' 条内容不能超过 120 字。'};
      if(allowedRates.indexOf(rate)<0)return {field:'speech_rate',dialogueIndex:index,message:'第 '+(index+1)+' 条语速无效。'};
    }
    var reading=dialogueTimelineSeconds(dialogues,false);
    if(reading>duration)return {code:'dialogue_too_long',field:'dialogue_text',relatedField:'duration_seconds',message:'全部台词预计需要 '+reading.toFixed(1)+' 秒，本镜头只有 '+duration+' 秒，超出 '+(reading-duration).toFixed(1)+' 秒。请选择更快语速、精简台词、延长镜头或拆分到下一镜头。'};
    return null;
  }
  function shotTimingStatus(values){
    values=values||{};
    var duration=Number(values.duration_seconds)||0,dialogues=shotDialogueValues(values),normal=dialogueTimelineSeconds(dialogues,true),actual=dialogueTimelineSeconds(dialogues,false);
    var first=dialogues[0]||{};
    return {duration:duration,kind:first.kind||'silence',speech_rate:Number(first.speech_rate)||1,dialogue_count:dialogues.length,normal_seconds:normal,reading_seconds:actual,remaining_seconds:Math.round((duration-actual)*100)/100,issue:shotTimingIssue(values)};
  }
  function durationBandLabel(value){value=Number(value)||30;return value===60?'60–90 秒':value===45?'30–60 秒':'15–30 秒';}
  function escapeHtml(value){return text(value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function downloadFilename(value,fallback){
    var filename=text(value).trim()||fallback||'短剧';
    return filename.replace(/[\\/:*?"<>|]+/g,'-').replace(/\s+/g,' ').slice(0,80);
  }
  function key(prefix){
    if(typeof crypto!=='undefined'&&crypto.randomUUID)return prefix+'-'+crypto.randomUUID();
    return prefix+'-'+Date.now()+'-'+Math.random().toString(16).slice(2);
  }
  function avatarCreateUrl(){
    return '/workbench/video.html?function=cinematic&action=create-avatar';
  }
  function hash(value){
    var first=2166136261,second=2246822519,input=text(value);
    for(var i=0;i<input.length;i+=1){first=Math.imul(first^input.charCodeAt(i),16777619);second=Math.imul(second^input.charCodeAt(i),3266489917);}
    return (first>>>0).toString(16).padStart(8,'0')+(second>>>0).toString(16).padStart(8,'0');
  }
  function persistPaidOperation(storageKey,operation){
    var serialized=JSON.stringify(operation);
    try{
      if(typeof localStorage==='undefined')throw new Error('localStorage unavailable');
      localStorage.setItem(storageKey,serialized);
      if(localStorage.getItem(storageKey)!==serialized)throw new Error('localStorage verification failed');
    }catch(cause){
      try{if(typeof localStorage!=='undefined')localStorage.removeItem(storageKey);}catch(ignore){}
      var error=new Error('浏览器安全存储不可用或空间不足，未发送付费请求。请释放本站存储空间、允许网站存储后重试。');
      error.code='paid_operation_persistence_unavailable';error.cause=cause;throw error;
    }
    return operation;
  }
  function operationOwner(value){
    var owner=text(value).trim();
    if(!owner){var error=new Error('无法确认当前登录账号，未发送付费请求。请重新登录后重试。');error.code='paid_operation_owner_required';throw error;}
    return owner;
  }
  function legacyShotDraftStorageKey(projectId,shotKey){
    return 'hq-short-drama-shot-draft:'+text(projectId)+':'+text(shotKey);
  }
  function shotDraftStorageKey(ownerUsername,projectId,shotKey){
    return 'hq-short-drama-shot-draft:'+hash(operationOwner(ownerUsername))+':'+hash(text(projectId))+':'+hash(text(shotKey));
  }
  function discardLegacyShotDraft(storage,projectId,shotKey){
    try{if(storage)storage.removeItem(legacyShotDraftStorageKey(projectId,shotKey));}catch(ignore){}
  }
  function avatarOperation(payload,ownerUsername){
    payload=payload||{};var binding=payload.short_drama_binding||{},owner=operationOwner(ownerUsername);
    var signature=JSON.stringify({project_id:binding.project_id||'',character_key:binding.character_key||'',name:payload.name||'',image_data:payload.image_data||''});
    var storageKey='hq-short-drama-avatar-operation:'+hash(owner)+':'+hash(signature),value='';
    try{if(typeof localStorage!=='undefined')value=localStorage.getItem(storageKey)||'';}catch(ignore){}
    if(value){try{var saved=JSON.parse(value);if(saved&&saved.key&&saved.owner===owner){var restored={key:saved.key,storage_key:storageKey,job_id:saved.job_id||null,payload:saved.payload||payload,owner:owner};return saved.payload?restored:persistPaidOperation(storageKey,restored);}}catch(error){if(error&&error.code==='paid_operation_persistence_unavailable')throw error;}}
    value='character-avatar-create-'+hash(signature)+'-'+key('op').split('-').slice(-1)[0];
    var operation={key:value,storage_key:storageKey,job_id:null,payload:JSON.parse(JSON.stringify(payload)),owner:owner};
    return persistPaidOperation(storageKey,operation);
  }
  function finishAvatarOperation(operation){
    if(!operation||!operation.storage_key)return;
    try{if(typeof localStorage!=='undefined')localStorage.removeItem(operation.storage_key);}catch(ignore){}
  }
  function characterImageOperation(payload,ownerUsername){
    payload=payload||{};var owner=operationOwner(ownerUsername);
    var signature=JSON.stringify({project_id:payload.project_id||'',revision:Number(payload.revision||0),character_key:payload.character_key||''});
    var storageKey='hq-short-drama-character-image-operation:'+hash(owner)+':'+hash(signature),value='';
    try{if(typeof localStorage!=='undefined')value=localStorage.getItem(storageKey)||'';}catch(ignore){}
    if(value){try{var saved=JSON.parse(value);if(saved&&saved.key&&saved.owner===owner){saved.storage_key=storageKey;if(saved.payload)return saved;saved.payload=payload;return persistPaidOperation(storageKey,saved);}}catch(error){if(error&&error.code==='paid_operation_persistence_unavailable')throw error;}}
    var operation={key:'character-image-'+hash(signature)+'-'+key('op').split('-').slice(-1)[0],storage_key:storageKey,job_id:null,payload:JSON.parse(JSON.stringify(payload)),owner:owner};
    return persistPaidOperation(storageKey,operation);
  }
  function finishCharacterImageOperation(operation){
    if(!operation||!operation.storage_key)return;
    try{if(typeof localStorage!=='undefined')localStorage.removeItem(operation.storage_key);}catch(ignore){}
  }
  function sceneImageOperation(payload,ownerUsername){
    payload=payload||{};var owner=operationOwner(ownerUsername);
    var signature=JSON.stringify({project_id:payload.project_id||'',scene_key:payload.scene_key||'',prompt:payload.prompt||'',ratio:payload.ratio||''});
    var storageKey='hq-short-drama-scene-image-operation:'+hash(owner)+':'+hash(signature),value='';
    try{if(typeof localStorage!=='undefined')value=localStorage.getItem(storageKey)||'';}catch(ignore){}
    if(value){try{var saved=JSON.parse(value);if(saved&&saved.key&&saved.owner===owner){saved.storage_key=storageKey;return saved;}}catch(ignore){}}
    return persistPaidOperation(storageKey,{key:'scene-image-'+hash(signature)+'-'+key('op').split('-').slice(-1)[0],storage_key:storageKey,job_id:null,payload:JSON.parse(JSON.stringify(payload)),owner:owner});
  }
  function finishSceneImageOperation(operation){
    if(!operation||!operation.storage_key)return;
    try{if(typeof localStorage!=='undefined')localStorage.removeItem(operation.storage_key);}catch(ignore){}
  }
  function characterImageOperationState(character,currentOperation){
    character=character||{};
    if(character.reference_job_status==='ready'&&!character.reference_image_url){
      return {character_key:character.character_key||'',phase:'stale',message:'角色资料已更新，旧任务结果未自动采用；可按最新资料重新生成。',error:false,active:false};
    }
    if(currentOperation&&currentOperation.character_key===character.character_key)return currentOperation;
    if(character.reference_job_status==='linked'&&character.reference_job_id&&!character.reference_image_url){
      return {character_key:character.character_key||'',phase:'pending',message:'后台已有角色形象生成任务，请检查生成结果；不要重复提交。',error:false,active:false};
    }
    return {character_key:character.character_key||'',phase:'idle',message:'',error:false,active:false};
  }
  function characterImageAction(operation){
    if(operation&&operation.active)return 'blocked';
    if(operation&&operation.phase==='pending')return 'check';
    return 'generate';
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
          if(!response.ok){var error=new Error(data.detail||('HTTP '+response.status));error.status=response.status;error.code=data.code;error.need=data.need;error.operation_terminal=!!data.operation_terminal;throw error;}
          return data;
        });});
    }
    function mutate(path,payload,prefix){return request(path,{method:'POST',headers:{'Idempotency-Key':key(prefix)},body:payload});}
    function createAvatar(payload,ownerUsername){
      var operation;try{operation=avatarOperation(payload,ownerUsername);}catch(error){return Promise.reject(error);}
      return request('/api/gen/avatar',{method:'POST',headers:{'Idempotency-Key':operation.key},body:operation.payload||payload}).then(function(result){
        result=result||{};operation.job_id=result.job_id||operation.job_id||null;
        try{if(typeof localStorage!=='undefined')localStorage.setItem(operation.storage_key,JSON.stringify(operation));}catch(ignore){}
        result._avatar_operation=operation;return result;
      }).catch(function(error){if(error&&error.operation_terminal)finishAvatarOperation(operation);throw error;});
    }
    function recoverAvatarOperations(ownerUsername){
      var operations=[];
      try{
        var owner=operationOwner(ownerUsername),accountPrefix='hq-short-drama-avatar-operation:'+hash(owner)+':';
        if(typeof localStorage==='undefined')return Promise.resolve([]);
        for(var i=0;i<localStorage.length;i+=1){
          var storageKey=localStorage.key(i);
          if(!storageKey||storageKey.indexOf(accountPrefix)!==0)continue;
          var operation=JSON.parse(localStorage.getItem(storageKey)||'{}');
          if(operation&&operation.owner===owner&&operation.key&&(operation.job_id||operation.payload)){operation.storage_key=storageKey;operations.push(operation);}
        }
      }catch(ignore){return Promise.resolve([]);}
      return Promise.all(operations.map(function(operation){
        var submitted=operation.job_id?Promise.resolve(operation):request('/api/gen/avatar',{method:'POST',headers:{'Idempotency-Key':operation.key},body:operation.payload}).then(function(result){
          operation.job_id=result&&result.job_id||null;
          try{localStorage.setItem(operation.storage_key,JSON.stringify(operation));}catch(ignore){}
          return operation;
        });
        return submitted.then(function(){if(!operation.job_id)return null;return request('/api/gen/job/'+encodeURIComponent(operation.job_id));}).then(function(job){
          if(!job)return null;
          var binding=job&&job.result&&job.result.short_drama_binding||{};
          if(['error','failed'].indexOf(job&&job.status)>=0||(job&&job.status==='done'&&binding.status!=='pending'))finishAvatarOperation(operation);
          return job;
        }).catch(function(error){if(error&&error.operation_terminal)finishAvatarOperation(operation);return null;});
      }));
    }
    function generateCharacterImage(payload,ownerUsername){
      var operation;try{operation=characterImageOperation(payload,ownerUsername);}catch(error){return Promise.reject(error);}
      return request('/api/gen/short-drama/generate-character-reference',{method:'POST',headers:{'Idempotency-Key':operation.key},body:operation.payload||payload}).then(function(result){
        result=result||{};operation.job_id=result.job_id||operation.job_id||null;
        try{if(typeof localStorage!=='undefined')localStorage.setItem(operation.storage_key,JSON.stringify(operation));}catch(ignore){}
        result._character_image_operation=operation;return result;
      }).catch(function(error){if(error&&error.operation_terminal)finishCharacterImageOperation(operation);throw error;});
    }
    function recoverCharacterImageOperations(ownerUsername){
      var operations=[];
      try{
        var owner=operationOwner(ownerUsername),accountPrefix='hq-short-drama-character-image-operation:'+hash(owner)+':';
        if(typeof localStorage==='undefined')return Promise.resolve([]);
        for(var i=0;i<localStorage.length;i+=1){
          var storageKey=localStorage.key(i);
          if(!storageKey||storageKey.indexOf(accountPrefix)!==0)continue;
          var operation=JSON.parse(localStorage.getItem(storageKey)||'{}');
          if(operation&&operation.owner===owner&&operation.key&&(operation.job_id||operation.payload)){operation.storage_key=storageKey;operations.push(operation);}
        }
      }catch(ignore){return Promise.resolve([]);}
      return Promise.all(operations.map(function(operation){
        var submitted=operation.job_id?Promise.resolve(operation):request('/api/gen/short-drama/generate-character-reference',{method:'POST',headers:{'Idempotency-Key':operation.key},body:operation.payload}).then(function(result){
          operation.job_id=result&&result.job_id||null;
          try{localStorage.setItem(operation.storage_key,JSON.stringify(operation));}catch(ignore){}
          return operation;
        });
        return submitted.then(function(){if(!operation.job_id)return null;return request('/api/gen/job/'+encodeURIComponent(operation.job_id));}).then(function(job){
          if(job&&['done','error','failed'].indexOf(job.status)>=0)finishCharacterImageOperation(operation);
          return job;
        }).catch(function(error){if(error&&error.operation_terminal)finishCharacterImageOperation(operation);return null;});
      }));
    }
    function generateSceneImage(payload,ownerUsername){
      var operation;try{operation=sceneImageOperation(payload,ownerUsername);}catch(error){return Promise.reject(error);}
      var imagePayload={provider:'banana',model:'nb2',quality:'hd',count:1,ratio:payload.ratio||'16:9',prompt:payload.prompt};
      return request('/api/gen/image',{method:'POST',headers:{'Idempotency-Key':operation.key},body:imagePayload}).then(function(result){
        result=result||{};operation.job_id=result.job_id||operation.job_id||null;
        try{if(typeof localStorage!=='undefined')localStorage.setItem(operation.storage_key,JSON.stringify(operation));}catch(ignore){}
        result._scene_image_operation=operation;return result;
      }).catch(function(error){if(error&&error.operation_terminal)finishSceneImageOperation(operation);throw error;});
    }
    function recoverSceneImageOperations(ownerUsername){
      var operations=[];
      try{
        var owner=operationOwner(ownerUsername),accountPrefix='hq-short-drama-scene-image-operation:'+hash(owner)+':';
        if(typeof localStorage==='undefined')return Promise.resolve([]);
        for(var index=0;index<localStorage.length;index+=1){
          var storageKey=localStorage.key(index);
          if(!storageKey||storageKey.indexOf(accountPrefix)!==0)continue;
          var operation=JSON.parse(localStorage.getItem(storageKey)||'{}');
          if(operation&&operation.owner===owner&&operation.key&&operation.payload){operation.storage_key=storageKey;operations.push(operation);}
        }
      }catch(ignore){return Promise.resolve([]);}
      return Promise.all(operations.map(function(operation){
        var submitted=operation.job_id?Promise.resolve(operation):request('/api/gen/image',{method:'POST',headers:{'Idempotency-Key':operation.key},body:{provider:'banana',model:'nb2',quality:'hd',count:1,ratio:operation.payload.ratio||'16:9',prompt:operation.payload.prompt}}).then(function(result){
          operation.job_id=result&&result.job_id||null;
          try{localStorage.setItem(operation.storage_key,JSON.stringify(operation));}catch(ignore){}
          return operation;
        });
        return submitted.then(function(){return operation.job_id?request('/api/gen/job/'+encodeURIComponent(operation.job_id)):null;})
          .then(function(job){return {operation:operation,job:job};})
          .catch(function(error){return {operation:operation,error:error};});
      }));
    }
    function imageData(url){
      url=text(url).trim();
      if(!url)return Promise.reject(new Error('请先生成角色形象图'));
      if(/^data:image\/(?:jpeg|png|webp);base64,/i.test(url))return Promise.resolve(url);
      var base=(typeof location!=='undefined'&&location.href)||'http://localhost/';
      var relative=false;
      try{relative=new URL(url,base).origin===new URL(base).origin;}catch(ignore){relative=false;}
      var headers={'Accept':'image/jpeg,image/png,image/webp'};
      if(relative)headers.Authorization='Bearer __cookie__';
      return fetchImpl(url,{method:'GET',credentials:relative?'same-origin':'omit',cache:'no-store',headers:headers})
        .then(function(response){
          if(!response.ok)throw new Error('角色形象图读取失败（HTTP '+response.status+'）');
          return response.blob();
        }).then(function(blob){
          if(!/^image\/(?:jpeg|png|webp)$/i.test(blob.type||''))throw new Error('角色形象图格式必须为 JPG、PNG 或 WebP');
          return new Promise(function(resolve,reject){
            var reader=new FileReader();
            reader.onload=function(){resolve(String(reader.result||''));};
            reader.onerror=function(){reject(new Error('角色形象图读取失败'));};
            reader.readAsDataURL(blob);
          });
        });
    }
    return {
      workspace:function(id){return request('/api/gen/short-drama/conversation?project_id='+encodeURIComponent(id));},
      project:function(id){return request('/api/gen/short-drama/project?id='+encodeURIComponent(id));},
      currentUsername:function(){return request('/api/auth/me').then(function(result){return operationOwner(result&&result.user&&result.user.username);});},
      message:function(payload){return mutate('/api/gen/short-drama/conversation/messages',payload,'message');},
      generate:function(payload){return mutate('/api/gen/short-drama/conversation/script/generate',payload,'generate');},
      updateShot:function(payload){return mutate('/api/gen/short-drama/conversation/script/shot/update',payload,'shot-update');},
      regenerateShot:function(payload){return mutate('/api/gen/short-drama/conversation/script/shot/regenerate',payload,'shot-regenerate');},
      setShotLock:function(payload){return mutate('/api/gen/short-drama/conversation/script/shot/lock',payload,'shot-lock');},
      changeShotStructure:function(payload){return mutate('/api/gen/short-drama/conversation/script/shot/structure',payload,'shot-structure');},
      restore:function(payload){return mutate('/api/gen/short-drama/conversation/script/restore',payload,'restore');},
      lock:function(payload){return mutate('/api/gen/short-drama/conversation/script/lock',payload,'lock');},
      characterStudio:function(id){return request('/api/gen/short-drama/character-studio?project_id='+encodeURIComponent(id));},
      saveCharacterProfile:function(payload){return mutate('/api/gen/short-drama/character-studio/profile',payload,'character-profile');},
      bindCharacterAvatar:function(payload){return mutate('/api/gen/short-drama/character-studio/bind-avatar',payload,'character-avatar');},
      generateCharacterImage:generateCharacterImage,
      recoverCharacterImageOperations:recoverCharacterImageOperations,
      finishCharacterImageOperation:finishCharacterImageOperation,
      sceneWorkspace:function(id){return request('/api/gen/short-drama/asset-graph/scenes?project_id='+encodeURIComponent(id));},
      listImageAssets:function(offset,limit){return request('/api/gen/history?limit='+Math.max(1,Math.min(120,Number(limit)||60))+'&offset='+Math.max(0,Number(offset)||0)+'&kind=image');},
      syncSceneGraph:function(payload){return mutate('/api/gen/short-drama/asset-graph/sync',payload,'scene-sync');},
      createScene:function(payload){return mutate('/api/gen/short-drama/asset-graph/scenes',payload,'scene-create');},
      updateScene:function(payload){return mutate('/api/gen/short-drama/asset-graph/scenes/update',payload,'scene-update');},
      bindSceneToShot:function(payload){return mutate('/api/gen/short-drama/asset-graph/scenes/bind-shot',payload,'scene-bind-shot');},
      deleteScene:function(payload){return mutate('/api/gen/short-drama/asset-graph/scenes/delete',payload,'scene-delete');},
      restoreScene:function(payload){return mutate('/api/gen/short-drama/asset-graph/scenes/restore',payload,'scene-restore');},
      setSceneReference:function(payload){return mutate('/api/gen/short-drama/asset-graph/scenes/reference',payload,'scene-reference');},
      lockSceneReference:function(payload){return mutate('/api/gen/short-drama/asset-graph/scenes/lock',payload,'scene-lock');},
      generateSceneImage:generateSceneImage,
      recoverSceneImageOperations:recoverSceneImageOperations,
      finishSceneImageOperation:finishSceneImageOperation,
      createAvatar:createAvatar,
      finishAvatarOperation:finishAvatarOperation,
      recoverAvatarOperations:recoverAvatarOperations,
      job:function(id){return request('/api/gen/job/'+encodeURIComponent(id));},
      imageData:imageData,
      preflight:function(id){return request('/api/gen/short-drama/preflight?project_id='+encodeURIComponent(id));},
      prepare:function(payload){return mutate('/api/gen/short-drama/preflight/generate',payload,'preflight');},
      confirmPlan:function(payload){return mutate('/api/gen/short-drama/preflight/confirm',payload,'confirm-plan');},
      autodraft:function(id){return request('/api/gen/short-drama/autodraft?project_id='+encodeURIComponent(id));},
      providerPreflight:function(payload){return mutate('/api/gen/short-drama/autodraft/provider-preflight',payload,'provider-preflight');},
      providerQuote:function(payload){return mutate('/api/gen/short-drama/autodraft/provider-quote',payload,'provider-quote');},
      recoverLegacyMedia:function(payload){return mutate('/api/gen/short-drama/autodraft/legacy-media/recover',payload,'legacy-media-recovery');},
      selectProviderVersion:function(payload){return mutate('/api/gen/short-drama/autodraft/provider-version/select',payload,'provider-version-select');},
      startProviderJob:function(payload){return mutate('/api/gen/short-drama/autodraft/provider-jobs',payload,'provider-shot');},
      providerJob:function(projectId,jobId){return request('/api/gen/short-drama/autodraft/provider-jobs/'+encodeURIComponent(jobId)+'?project_id='+encodeURIComponent(projectId));},
      startDraft:function(payload){return mutate('/api/gen/short-drama/autodraft/jobs',payload,'autodraft');},
      draftJob:function(projectId,jobId){return request('/api/gen/short-drama/autodraft/jobs/'+encodeURIComponent(jobId)+'?project_id='+encodeURIComponent(projectId));},
      refinement:function(id){return request('/api/gen/short-drama/refinement?project_id='+encodeURIComponent(id));},
      previewRefinement:function(payload){return mutate('/api/gen/short-drama/refinement/changes/preview',payload,'refinement-preview');},
      refineShot:function(payload){return mutate('/api/gen/short-drama/refinement/jobs',payload,'refinement-shot');},
      adoptRefinementCandidate:function(payload){return mutate('/api/gen/short-drama/refinement/candidates/adopt',payload,'refinement-candidate-adopt');},
      reassembleRefinementCandidates:function(payload){return mutate('/api/gen/short-drama/refinement/candidates/reassemble',payload,'refinement-candidate-reassemble');},
      markRefinementIssue:function(payload){return mutate('/api/gen/short-drama/refinement/issues',payload,'refinement-issue');},
      keepOriginalRefinementShot:function(payload){return mutate('/api/gen/short-drama/refinement/issues/keep-original',payload,'refinement-keep-original');},
      setRefinementMediaPreference:function(payload){return mutate('/api/gen/short-drama/refinement/media-preference',payload,'refinement-media');},
      reassembleRefinement:function(payload){return mutate('/api/gen/short-drama/refinement/reassemble',payload,'refinement-reassemble');},
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
  function conversationWorkspaceMode(raw){
    raw=raw||{};
    var conversation=raw.conversation||{},understanding=conversation.understanding||{};
    var locked=conversation.state==='script_locked';
    var hasScript=!!conversation.current_version_id||!!raw.current_script;
    var directionConfirmed=!!understanding.direction_confirmed;
    var canEdit=!!(raw.permissions&&raw.permissions.can_edit);
    var projectReady=locked||hasScript||directionConfirmed;
    return {
      locked:locked,hasScript:hasScript,directionConfirmed:directionConfirmed,
      projectReady:projectReady,readOnly:projectReady||!canEdit,
      canMessage:canEdit&&!projectReady
    };
  }
  function setWorkspaceControlDisabled(node,disabled){
    node.disabled=!!disabled;
    node.setAttribute('data-workspace-disabled-recomputed','true');
  }
  function sceneWorkspaceRequired(raw){
    return !!(raw&&raw.current_script&&raw.current_script.script);
  }
  function applyConversationMode(doc,root,mode,historyExpanded){
    mode=mode||{};var projectReady=!!mode.readOnly;
    var grid=doc.getElementById('sdWorkspaceGrid'),chatToggle=doc.getElementById('sdChatToggle'),historyButton=doc.getElementById('sdHistoryButton'),messageForm=doc.getElementById('sdMessageForm');
    grid.classList.toggle('chat-readonly',projectReady);
    grid.classList.toggle('project-ready',projectReady);
    grid.classList.toggle('history-open',projectReady&&historyExpanded);
    doc.getElementById('sdChatTitle').textContent=projectReady?'历史创作记录（只读）':'和创作助手对话';
    doc.getElementById('sdChatDescription').textContent=projectReady?'项目创建前的讨论记录，仅供追溯。':'说清人物、冲突、情绪和结局。';
    chatToggle.hidden=!projectReady;
    chatToggle.textContent='关闭创作记录';
    chatToggle.setAttribute('aria-expanded',historyExpanded?'true':'false');
    historyButton.hidden=!projectReady;
    historyButton.textContent=historyExpanded?'关闭创作记录':'创作记录';
    historyButton.setAttribute('aria-expanded',historyExpanded?'true':'false');
    messageForm.hidden=projectReady;
    setWorkspaceControlDisabled(root.querySelector('#sdMessageForm textarea'),!mode.canMessage);
    setWorkspaceControlDisabled(root.querySelector('#sdMessageForm button'),!mode.canMessage);
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
  function messageHtml(item,readOnly){
    var metadata=item&&item.metadata||{},recommendations=Array.isArray(metadata.recommendations)?metadata.recommendations:[],quickReplies=Array.isArray(metadata.quick_replies)?metadata.quick_replies:[];
    if(readOnly){recommendations=[];quickReplies=[];}
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
  function compactReviewText(value,limit){
    value=text(value).replace(/\s+/g,' ').replace(/^[\s，,。；;：:]+|[\s，,。；;：:]+$/g,'');
    limit=Number(limit||180);return value.length>limit?value.slice(0,limit).replace(/[，,；;：:\s]+$/,'')+'…':value;
  }
  function reviewStagePoints(contract){
    contract=contract||{};
    var raw=(contract.plot_points||[]).map(function(item){return text(item&&item.excerpt);}).filter(Boolean).join(' '),markers=[];
    [/(?:镜头\s*)(\d+)\s*[（(][^）)]{1,40}[）)]\s*[：:]?/g,/(?:^|[^\d])(\d+)\s*[、.]\s*\d+\s*[-—~至]\s*\d+\s*秒\s*[：:]/g].forEach(function(pattern){
      var matched;while((matched=pattern.exec(raw)))markers.push({number:Number(matched[1]),start:matched.index,end:pattern.lastIndex});
    });
    markers.sort(function(a,b){return a.start-b.start;});
    var shotMap={};markers.forEach(function(marker,index){
      var end=index+1<markers.length?markers[index+1].start:raw.length;
      var excerpt=compactReviewText(raw.slice(marker.end,end).replace(/^(人物|角色|场景|分镜|时长)[^。]{0,160}[。；;]/,'').replace(/\s*(?:人物|角色|场景)\s*[：:][\s\S]*$/,''),150);
      if(excerpt&&(!shotMap[marker.number]||excerpt.length<shotMap[marker.number].excerpt.length))shotMap[marker.number]={number:marker.number,excerpt:excerpt};
    });
    var shots=Object.keys(shotMap).map(Number).sort(function(a,b){return a-b;}).map(function(number){return shotMap[number];});
    if(shots.length>=3){
      var boundaries=[0,Math.max(1,Math.floor(shots.length/3)),Math.max(2,Math.floor(shots.length*2/3)),shots.length],labels=['start','middle','end'];
      return labels.map(function(position,index){var group=shots.slice(boundaries[index],boundaries[index+1]);return {position:position,excerpt:compactReviewText(group.map(function(item){return item.excerpt;}).join('；'),210)};});
    }
    var global=contract.global_structure||{},fallback=[global.setup,global.development||global.turning_point,global.ending].map(function(value,index){return {position:['start','middle','end'][index],excerpt:compactReviewText(value,210)};}).filter(function(item){return item.excerpt;});
    if(fallback.length===3)return fallback;
    return (contract.plot_points||[]).slice(0,3).map(function(item,index){return {position:item.position||['start','middle','end'][index],excerpt:compactReviewText(item.excerpt,210)};}).filter(function(item){return item.excerpt;});
  }
  function importContractHtml(contract){
    contract=contract||{};
    if(!contract.source_hash)return '';
    var mode=contract.import_mode==='optimize'?'AI 协助优化':'尊重原稿';
    var characters=(contract.characters||[]).map(escapeHtml).join('、')||'待确认';
    var points=reviewStagePoints(contract).map(function(item){return '<li><b>'+escapeHtml({start:'开场',middle:'发展',end:'结尾'}[item.position]||item.position||'剧情节点')+'</b><span>'+escapeHtml(item.excerpt||'')+'</span></li>';}).join('');
    var dialogues=(contract.key_dialogues||[]).map(function(item){return '<li><b>'+escapeHtml(item.speaker||'人物')+'</b><span>'+escapeHtml(item.text||'')+'</span></li>';}).join('');
    var changes=(contract.proposed_changes||[]).map(function(item){var status=item.status||'pending';var statusText=status==='confirmed'?'已确认':(status==='denied'?'已排除':'待确认');return '<li class="'+escapeHtml(status)+'"><b>'+escapeHtml(item.label||'优化项')+'</b><span>'+escapeHtml(item.summary||'')+'</span><em>'+statusText+'</em></li>';}).join('');
    var preservations=(contract.required_preservations||[]).map(function(item){return '<li class="confirmed"><b>'+escapeHtml(item.kind==='dialogue'?'必保对白':'必保内容')+'</b><span>'+escapeHtml(item.source||'')+'</span><em>原稿位置 '+Number(item.source_offset||0)+'</em></li>';}).join('');
    var global=contract.global_structure||{},globalItems=[['开场设定',global.setup],['故事发展',global.development],['关键转折',global.turning_point],['高潮选择',global.climax],['结局落点',global.ending],['核心冲突',global.central_conflict]].filter(function(item){return item[1];}).map(function(item){return '<li><b>'+item[0]+'</b><span>'+escapeHtml(item[1])+'</span></li>';}).join('');
    return '<section class="sd-overview-card sd-import-contract"><header><div><span>剧本结构</span><b>原稿理解快照</b></div><em>'+escapeHtml(mode)+'</em></header><p><span>识别人物</span><b>'+characters+'</b></p>'+(globalItems?'<h4>长剧本全局结构</h4><ul>'+globalItems+'</ul>':'')+(points?'<h4>首 / 中 / 尾剧情节点</h4><ul>'+points+'</ul>':'')+(dialogues?'<h4>关键对白</h4><ul>'+dialogues+'</ul>':'')+(preservations?'<h4>用户追加的必须保留内容</h4><ul>'+preservations+'</ul>':'')+(changes?'<h4>重要优化边界</h4><ul>'+changes+'</ul>':'')+'</section>';
  }
  function importContractTechnicalHtml(contract){
    contract=contract||{};
    if(!contract.source_hash)return '<p class="sd-placeholder">当前项目没有原稿导入技术记录。</p>';
    var mode=contract.import_mode==='optimize'?'AI 协助优化':'尊重原稿';
    return '<dl class="sd-tech-list"><dt>处理方式</dt><dd>'+escapeHtml(mode)+'</dd><dt>契约版本</dt><dd>第 '+Number(contract.revision||1)+' 版</dd><dt>契约哈希</dt><dd><code>'+escapeHtml(contract.contract_hash||'待生成')+'</code></dd><dt>原稿哈希</dt><dd><code>'+escapeHtml(contract.source_hash)+'</code></dd></dl>';
  }
  function storyActsHtml(acts){
    acts=Array.isArray(acts)?acts:[];
    if(!acts.length)return '';
    return '<section class="sd-story-acts"><b>三幕结构</b><div>'+acts.map(function(item){return '<article><strong>第'+Number(item.act)+'幕 · '+escapeHtml(item.name||'故事阶段')+'</strong><p>'+escapeHtml(item.summary||'')+'</p></article>';}).join('')+'</div></section>';
  }
  function storyboardQualityHtml(script){
    script=script||{};var quality=script.quality_gate||{};
    if(!quality.status)return '';
    var status=quality.status||'unknown',metrics=quality.metrics||{},shotCount=Number(metrics.shot_count)||(script.shots||[]).length;
    var providerReady=Number(metrics.provider_ready_shots)||(script.shots||[]).filter(function(shot){return !!text(shot.provider_prompt);}).length;
    var issues=(quality.blockers||[]).concat(quality.warnings||[]);
    var summary=status==='pass'?'✓ '+providerReady+'/'+shotCount+' 镜检查通过，可以锁定':status==='warning'?'还有 '+issues.length+' 个镜头建议复核':'还有 '+issues.length+' 个镜头需要处理';
    var details=issues.map(function(item){return '<p>'+escapeHtml(item.shot_key?item.shot_key+' · ':'')+escapeHtml(userFacingVideoMessage(item.message||item.code,'待检查'))+'</p>';}).join('')||'<p>镜头时长、对白、剧情推进和生成提示词检查通过。</p>';
    return '<div class="sd-inspector-quality '+escapeHtml(status)+'"><b>'+escapeHtml(summary)+'</b><details><summary>查看检查详情</summary>'+details+'</details></div>';
  }
  function scriptHeaderState(version){
    version=version||{};var status=text(version.status).toLowerCase(),quality=version.script&&version.script.quality_gate||{};
    if(status==='locked'||status==='confirmed')return {key:'locked',label:'已锁定'};
    if(quality.status==='pass')return {key:'ready',label:'可锁定'};
    return {key:'draft',label:'草稿'};
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
    allProviderJobs(autodraft).forEach(function(job){
      var jobShotKey=text(job&&job.shot_key);
      if(!jobShotKey)return;
      if(!index[jobShotKey])index[jobShotKey]={versions:[],job:null};
      if(!index[jobShotKey].job)index[jobShotKey].job=job;
    });
    return index;
  }
  function allProviderJobs(autodraft){
    autodraft=autodraft||{};
    var jobs=Array.isArray(autodraft.provider_jobs)?autodraft.provider_jobs.slice():[];
    var legacyJob=autodraft.provider_job;
    if(legacyJob&&!jobs.some(function(item){return item&&item.id===legacyJob.id;}))jobs.push(legacyJob);
    return jobs;
  }
  function activeProviderJobs(autodraft){
    return allProviderJobs(autodraft).filter(function(item){return item&&['billing','queued','submitting','running'].indexOf(item.status)>=0;});
  }
  function providerJobsWithResult(autodraft,result){
    if(!result)return allProviderJobs(autodraft);
    var shotKey=text(result.shot_key);
    return [result].concat(allProviderJobs(autodraft).filter(function(item){
      return item&&item.id!==result.id&&(!shotKey||text(item.shot_key)!==shotKey);
    }));
  }
  function providerJobDisplay(job){
    job=job||{};
    var status=text(job.status||''),phase=text(job.phase||'').toLowerCase();
    var active=['billing','queued','submitting','running'].indexOf(status)>=0;
    var minimax=text(job.provider)==='minimax_h3';
    var progress=Math.max(0,Math.min(100,Number(job.progress||0)));
    var label='',shortLabel='生成中';
    if(status==='succeeded'){label='视频生成完成';shortLabel='已完成';}
    else if(['failed','submit_unknown','canceled'].indexOf(status)>=0){label='视频生成失败';shortLabel='失败';}
    else if(status==='billing'){label='正在确认生成费用';shortLabel='确认中';}
    else if(status==='submitting'||phase==='minimax_submitting'){label='正在提交视频任务';shortLabel='提交中';}
    else if(status==='queued'||['minimax_queued','minimax_queueing','minimax_preparing'].indexOf(phase)>=0){label='视频任务排队中';shortLabel='排队中';}
    else if(phase==='minimax_retrying'){label='正在重新连接视频服务';shortLabel='重试中';}
    else if(phase==='minimax_downloading'){label='正在下载并保存视频';shortLabel='保存中';}
    else if(minimax){label='正在生成视频';shortLabel='生成中';}
    else{label='正在生成视频';shortLabel='生成中';}
    var indeterminate=active&&(job.progress_indeterminate===true||minimax);
    return {
      active:active,label:label,shortLabel:shortLabel,progress:progress,
      indeterminate:indeterminate,
      heading:indeterminate?label:(active?label+' · '+progress+'%':label),
      taskLabel:indeterminate?label:status+' · '+progress+'%'
    };
  }
  function currentShotExecutionPrompt(shot,autodraft){
    shot=shot||{};autodraft=autodraft||{};
    var shotKey=text(shot.shot_key),job=(shotMediaIndex(autodraft)[shotKey]||{}).job||{},request=job.request||{};
    var execution=(autodraft.provider_execution_overrides||{})[shotKey]||{};
    return text(request.prompt||execution.provider_prompt||shot.provider_prompt).trim();
  }
  function defaultWorkspaceShotKey(shots,autodraft,requestedKey){
    shots=Array.isArray(shots)?shots:[];
    if(!shots.length)return '';
    requestedKey=text(requestedKey);
    if(shots.some(function(shot){return text(shot.shot_key)===requestedKey;}))return requestedKey;
    var mediaByShot=shotMediaIndex(autodraft),priority=['failed','active','pending'];
    function statusOf(shot){
      var media=mediaByShot[text(shot.shot_key)]||{versions:[],job:null},job=media.job||null;
      if((media.versions||[]).length)return 'completed';
      if(job&&['failed','submit_unknown','canceled'].indexOf(job.status)>=0)return 'failed';
      if(job&&['billing','queued','submitting','running'].indexOf(job.status)>=0)return 'active';
      return 'pending';
    }
    for(var index=0;index<priority.length;index+=1){
      var match=shots.filter(function(shot){return statusOf(shot)===priority[index];})[0];
      if(match)return text(match.shot_key);
    }
    return text(shots[0].shot_key);
  }
  function shotStructureCapabilities(shots,activeIndex,canAdjust){
    shots=Array.isArray(shots)?shots:[];activeIndex=Number(activeIndex);
    var shot=shots[activeIndex]||null,previous=activeIndex>0?shots[activeIndex-1]:null,next=activeIndex+1<shots.length?shots[activeIndex+1]:null;
    var previousOuter=activeIndex>1?shots[activeIndex-2]:null,nextOuter=activeIndex+2<shots.length?shots[activeIndex+2]:null;
    var enabled=!!canAdjust&&!!shot&&!shot.locked,previousLocked=!!(previous&&previous.locked),nextLocked=!!(next&&next.locked);
    var moveUpLocked=previousLocked||nextLocked||!!(previousOuter&&previousOuter.locked),moveDownLocked=previousLocked||nextLocked||!!(nextOuter&&nextOuter.locked);
    return {enabled:enabled,moveUp:enabled&&activeIndex>0&&!moveUpLocked,moveDown:enabled&&activeIndex<shots.length-1&&!moveDownLocked,copy:enabled&&!nextLocked,insertBefore:enabled&&!previousLocked,insertAfter:enabled&&!nextLocked,smartInsert:enabled&&!nextLocked,deleteShot:enabled&&shots.length>1&&!previousLocked&&!nextLocked};
  }
  function shotGenerationOverviewHtml(shots,autodraft,activeShotKey,project,capabilities){
    shots=Array.isArray(shots)?shots:[];
    if(!shots.length)return '';
    var mediaByShot=shotMediaIndex(autodraft),counts={completed:0,active:0,failed:0,pending:0};
    var nodes=shots.map(function(shot,index){
      var shotKey=text(shot&&shot.shot_key),media=mediaByShot[shotKey]||{versions:[],job:null};
      var hasVideo=(media.versions||[]).length>0,job=media.job||null,status='pending',label='未生成';
      if(hasVideo){status='completed';label='已完成';}
      else if(job&&['billing','queued','submitting','running'].indexOf(job.status)>=0){status='active';label=providerJobDisplay(job).shortLabel;}
      else if(job&&['failed','submit_unknown','canceled'].indexOf(job.status)>=0){status='failed';label='失败';}
      counts[status]+=1;
      return '<button type="button" class="sd-shot-progress-node '+status+(text(activeShotKey)===shotKey?' current':'')+'" data-action="show-workspace-shot" data-shot-key="'+escapeHtml(shotKey)+'" aria-pressed="'+(text(activeShotKey)===shotKey?'true':'false')+'" title="查看镜头 '+Number(shot.sort_order||index+1)+' · '+label+'"><i></i><span>#'+Number(shot.sort_order||index+1)+'</span><em>'+label+'</em></button>';
    }).join('');
    var total=shots.length,remaining=total-counts.completed,percent=Math.round(counts.completed*100/total);
    var message=remaining===0?'全部镜头已生成，可以合成预览':'还有 '+remaining+' 个镜头未完成';
    var warnings=(counts.active?'<span class="active">'+counts.active+' 个生成中</span>':'')+(counts.failed?'<span class="failed">'+counts.failed+' 个失败</span>':'')+(counts.pending?'<span>'+counts.pending+' 个未生成</span>':'');
    var seconds=shots.reduce(function(sum,shot){return sum+Number(shot.duration_seconds||0);},0),target=Number(project&&project.target_duration||30),lower=target===60?60:target===45?30:15,upper=target===60?90:target===45?60:30;
    var timingClass=seconds<lower||seconds>upper?' warning':'',timingText=seconds<lower?'比建议区间少 '+(lower-seconds)+' 秒':seconds>upper?'比建议区间多 '+(seconds-upper)+' 秒':'处于建议时长范围';
    capabilities=capabilities&&typeof capabilities==='object'?capabilities:shotStructureCapabilities(shots,shots.map(function(shot){return text(shot.shot_key);}).indexOf(text(activeShotKey)),!!capabilities);
    var adjust=capabilities.enabled?'<div class="sd-shot-structure-toolbar"><button type="button" data-action="add-shot-after" data-shot-key="'+escapeHtml(activeShotKey)+'"'+(capabilities.insertAfter?'':' disabled')+'>＋ 新增镜头</button><button type="button" data-action="smart-insert-shot" data-shot-key="'+escapeHtml(activeShotKey)+'"'+(capabilities.smartInsert?'':' disabled')+'>智能插入过渡镜头</button><span>可在当前镜头前后插入；生成过的旧合成片会保留并标记需重新合成。</span></div>':'';
    return '<section class="sd-shot-progress-overview '+(remaining===0?'complete':'')+'"><header><div><span>镜头生成进度</span><h3>已生成 '+counts.completed+' / '+total+' 个镜头</h3></div><strong>'+escapeHtml(message)+'</strong></header><div class="sd-shot-progress-track" role="progressbar" aria-label="镜头生成进度" aria-valuemin="0" aria-valuemax="'+total+'" aria-valuenow="'+counts.completed+'"><i style="width:'+percent+'%"></i></div><div class="sd-shot-progress-meta"><div>'+warnings+'</div><b>'+percent+'%</b></div><div class="sd-shot-duration-summary'+timingClass+'"><b>当前 '+total+' 个镜头 · 预计 '+seconds+' 秒</b><span>建议 '+lower+'–'+upper+' 秒，'+timingText+'；不会强制截断镜头。</span></div><div class="sd-shot-progress-nodes">'+nodes+'</div>'+adjust+'</section>';
  }
  function sceneLockingHtml(workspace,canEdit,operations,pendingDeleteKey){
    workspace=workspace||{};operations=operations||{};var scenes=Array.isArray(workspace.scenes)?workspace.scenes:[],deletedScenes=Array.isArray(workspace.deleted_scenes)?workspace.deleted_scenes:[];
    var lockedCount=scenes.filter(function(item){return item.locked;}).length,lockedPercent=scenes.length?Math.round(lockedCount*100/scenes.length):0;
    return '<section class="sd-script-block sd-scene-locking"><header class="sd-block-heading"><div><span class="sd-section-kicker">场景连续性</span><h3>场景锁定</h3><p>为同一地点设置统一场景图，关联镜头会自动沿用相同的空间、光线和氛围。</p></div><div class="sd-scene-heading-actions">'+(deletedScenes.length?'<button type="button" class="secondary" data-action="restore-scene" data-scene-key="'+escapeHtml(deletedScenes[0].scene_key||'')+'">↶ 恢复最近删除</button>':'')+'<button type="button" data-action="add-scene" '+(canEdit?'':'disabled')+'>＋ 添加场景</button><div class="sd-scene-progress"><b>已锁定 '+lockedCount+' / '+scenes.length+'</b><span role="progressbar" aria-label="场景锁定进度" aria-valuemin="0" aria-valuemax="'+scenes.length+'" aria-valuenow="'+lockedCount+'"><i style="width:'+lockedPercent+'%"></i></span><small>'+lockedPercent+'%</small></div></div></header><div class="sd-scene-list">'+scenes.map(function(scene){
      var preview=scene.preview||{},image=preview.url||'',shotList=(scene.shots||[]).map(function(shot){return Number(shot.sort_order||0);}).filter(Boolean),prompt=preview.prompt||scene.description||'',generation=operations[scene.scene_key]||{},generating=!!generation.active;
      var stateKey=generating?'generating':scene.locked?'locked':image?'pending':'empty',stateLabel=generating?(generation.label||'生成中'):scene.locked?'已锁定':image?'待确认':'未设置';
      return '<article class="sd-scene-card '+stateKey+'" data-scene-key="'+escapeHtml(scene.scene_key||'')+'"><div class="sd-scene-visual">'+
        (image?'<button type="button" class="sd-scene-image" data-action="preview-character-image" data-image-url="'+escapeHtml(image)+'" data-image-title="'+escapeHtml(scene.name||'场景')+'"><img src="'+escapeHtml(image)+'" alt="'+escapeHtml(scene.name||'场景')+' 场景图"><span>点击预览</span></button>':'<div class="sd-scene-image empty"><i>景</i><b>尚未设置场景图</b><span>上传图片或用描述生成</span></div>')+
        (generating?'<div class="sd-scene-generation-overlay" role="status" aria-live="polite"><i></i><b>'+escapeHtml(generation.label||'背景图生成中')+'</b><span>'+escapeHtml(generation.message||'任务已提交，可以继续处理其他场景或镜头。')+'</span></div>':'')+
        '<em class="sd-scene-status">'+stateLabel+'</em></div><div class="sd-scene-copy"><header><div><b>'+escapeHtml(scene.name||'未命名场景')+'</b><small>'+shotList.length+' 个关联镜头</small></div></header><div class="sd-scene-shot-tags">'+(shotList.length?shotList.map(function(order){return '<span>#'+order+'</span>';}).join(''):'<span>待关联</span>')+'</div><p class="sd-scene-summary">'+escapeHtml(prompt||'还没有场景描述，补充环境、时间和光线后可生成背景图。')+'</p><details class="sd-scene-prompt-editor"><summary>编辑场景描述</summary><label>场景提示词<textarea data-scene-prompt maxlength="1200" placeholder="例如：傍晚的小区长椅，暖色夕阳，树影斑驳，无人物" '+(generating?'disabled':'')+'>'+escapeHtml(prompt)+'</textarea></label></details><div class="sd-scene-actions">'+
          '<button type="button" data-action="choose-scene-asset" '+(canEdit?'':'disabled')+'>我的资产</button>'+
          '<label class="sd-scene-upload '+(canEdit?'':'disabled')+'">'+(image?'替换图片':'上传场景图')+'<input type="file" accept="image/jpeg,image/png,image/webp" data-scene-upload '+(canEdit?'':'disabled')+'></label>'+
          '<button type="button" data-action="generate-scene-image" '+(canEdit&&!generating?'':'disabled')+'>'+(generating?'<i class="sd-inline-spinner"></i> '+escapeHtml(generation.buttonLabel||'背景图生成中…'):'AI 生成背景图')+'</button>'+
          (image&&!scene.locked?'<button type="button" class="primary" data-action="lock-scene-reference" '+(canEdit?'':'disabled')+'>确认并锁定场景</button>':'')+
          (scene.locked?'<button type="button" class="ghost" data-action="replace-scene-reference" '+(canEdit?'':'disabled')+'>修改场景描述</button>':'')+
          (scene.custom?'<button type="button" class="ghost" data-action="edit-scene">编辑与绑定</button><button type="button" class="danger '+(pendingDeleteKey===scene.scene_key?'confirm':'')+'" data-action="delete-scene">'+(pendingDeleteKey===scene.scene_key?'确认移入回收站':'删除')+'</button>':'')+
        '</div>'+(pendingDeleteKey===scene.scene_key?'<p class="sd-scene-delete-warning">退出项目不会删除。只有再次点击“确认移入回收站”，场景才会被删除；删除后仍可恢复。</p>':'')+'<p class="sd-scene-help '+(generation.error?'error':'')+'">'+escapeHtml(generation.error||generation.message||(scene.locked?'生成相关镜头时会自动携带这张场景图。':'选择图片后先预览，确认锁定前不会影响视频生成。'))+'</p></div></article>';
    }).join('')+(scenes.length?'':'<div class="sd-scene-empty-library"><b>还没有场景</b><span>添加场景后，可绑定镜头并统一视频中的空间、光线与氛围。</span></div>')+'</div></section>';
  }
  function shotHistoryPromptHtml(rawPrompt){
    var prompt=text(rawPrompt||'未保存提示词快照'),escaped=escapeHtml(prompt);
    return '<details class="sd-shot-history-prompt"><summary><span class="sd-shot-history-prompt-preview">'+escaped+'</span><b class="sd-shot-history-prompt-expand">展开完整提示词</b><b class="sd-shot-history-prompt-collapse">收起提示词</b></summary><p>'+escaped+'</p></details>';
  }
  function shotMediaHtml(shot,media,projectRatio){
    media=media||{versions:[],job:null};
    var versions=media.versions||[],current=versions.filter(function(item){return item.selected;})[0]||versions[0]||null,job=media.job||null;
    var active=job&&['billing','queued','submitting','running'].indexOf(job.status)>=0;
    var failed=job&&['failed','submit_unknown','canceled'].indexOf(job.status)>=0;
    var statusHtml='';
    if(active){
      var display=providerJobDisplay(job);
      statusHtml='<div class="sd-shot-media-status working" data-provider-media-progress="'+escapeHtml(shot.shot_key||'')+'"><b>'+escapeHtml(display.heading)+'</b><span>后台任务 '+escapeHtml(job.id||'')+'</span><div class="sd-progress'+(display.indeterminate?' indeterminate':'')+'"><i'+(display.indeterminate?'':' style="width:'+display.progress+'%"')+'></i></div></div>';
    }else if(failed&&!current){
      statusHtml='<div class="sd-shot-media-status failed"><b>本镜头生成失败</b><span>'+escapeHtml(userFacingVideoMessage(job.error&&job.error.detail,'请在右侧查看失败原因后重试'))+'</span></div>';
    }else if(!current){
      statusHtml='<div class="sd-shot-media-status empty"><b>尚未生成镜头视频</b><span>在本镜头卡片中完成免费预检、报价和生成。</span></div>';
    }
    if(!current)return '<section class="sd-shot-media">'+statusHtml+'</section>';
    var history=versions.length?'<details class="sd-shot-media-history"'+(versions.length>1?' open':'')+'><summary>视频版本（'+versions.length+'）</summary><div>'+versions.map(function(item){var snapshot=item.request_snapshot||{};return '<article class="'+(item.id===current.id?'current':'')+'"><div><b>v'+Number(item.version||0)+(item.id===current.id?' · 当前采用':'')+'</b><span>'+escapeHtml(snapshot.duration_seconds?String(snapshot.duration_seconds)+' 秒 · '+(snapshot.resolution||''):'生成记录')+'</span>'+shotHistoryPromptHtml(snapshot.prompt)+'</div><div><a href="'+escapeHtml(item.url||'')+'" target="_blank" rel="noopener">预览</a>'+(item.id===current.id?'':'<button type="button" data-action="select-provider-version" data-shot-key="'+escapeHtml(item.shot_key||'')+'" data-version-id="'+escapeHtml(item.id||'')+'">采用此版本</button>')+'</div></article>';}).join('')+'</div></details>':'';
    var ratio=text(projectRatio||current.request_snapshot&&current.request_snapshot.ratio||'16:9'),ratioClass=ratio==='9:16'?'portrait':'landscape';
    return '<section class="sd-shot-media ready sd-shot-media-'+ratioClass+'"><header><div><b>镜头视频 · v'+Number(current.version||0)+'</b><span>生成完成</span></div><a href="'+escapeHtml(current.url||'')+'" target="_blank" rel="noopener">单独打开</a></header><div class="sd-shot-media-frame"><video controls preload="metadata" src="'+escapeHtml(current.url||'')+'"></video></div>'+statusHtml+history+'</section>';
  }
  function providerShotControlsHtml(shot,autodraft,canGenerate,selectedProviderShotKey,generationReason,providerShotErrors){
    autodraft=autodraft||{};
    var shotKey=text(shot&&shot.shot_key),poc=autodraft.provider_poc||{},providerShot=(poc.shots||[]).filter(function(item){return text(item.shot_key)===shotKey;})[0];
    if(!providerShot)return '';
    var media=shotMediaIndex(autodraft)[shotKey]||{versions:[],job:null},hasVideo=(media.versions||[]).length>0;
    var job=media.job||null,active=job&&['billing','queued','submitting','running'].indexOf(job.status)>=0,jobForShot=job;
    var selected=text(selectedProviderShotKey)===shotKey,preview=autodraft.provider_preview||null,quote=autodraft.provider_quote||null;
    var previewForShot=preview&&preview.shot&&text(preview.shot.shot_key)===shotKey,quoteForShot=quote&&quote.shot&&text(quote.shot.shot_key)===shotKey;
    var buttonLabel=jobForShot&&active?'查看生成进度':hasVideo?'管理 / 重新生成视频':'生成镜头视频';
    var toggle='<button type="button" class="sd-shot-provider-toggle" data-action="select-provider-shot" data-shot-key="'+escapeHtml(shotKey)+'" aria-expanded="'+(selected?'true':'false')+'">'+(selected?'收起生成操作':buttonLabel)+'</button>';
    if(!selected)return '<div class="sd-shot-provider-entry">'+toggle+'</div>';
    var requiredKeys=providerShot.character_keys||[],requiredCharacters=requiredKeys.map(function(key){return (poc.characters||[]).filter(function(item){return item.character_key===key;})[0]||{name:key,binding_ready:false};});
    var missing=requiredCharacters.filter(function(item){return !item.binding_ready;});
    var sequenceReady=providerShot.sequence_ready!==false,previousShotKey=text(providerShot.previous_shot_key);
    var continuityStatus=sequenceReady?
      '<div class="sd-check pass"><b>'+(previousShotKey?'已接入上一镜头连续性':'已建立全片视觉基线')+'</b><p>'+(previousShotKey?'将继承 '+escapeHtml(previousShotKey)+' 的结束画面、人物站位、道具和光线，再推进本镜头动作。':'首镜头将使用锁定场景、角色标准图和全片统一风格作为起点。')+'</p></div>':
      '<div class="sd-check warning"><b>请先完成上一个镜头</b><p>当前镜头需要承接 '+escapeHtml(previousShotKey)+' 的结束状态，完成后才能生成，避免背景和动作跳变。</p></div>';
    var binding=providerShot.binding_ready?
      '<div class="sd-check pass"><b>角色标准图已就绪</b><p>'+escapeHtml(requiredCharacters.map(function(item){return item.name;}).join('、')||'本镜头无需角色形象')+' · 将自动随镜头提交</p></div>':
      '<div class="sd-check warning"><b>请先锁定角色标准图</b><p>'+escapeHtml(missing.map(function(item){return item.name;}).join('、')||'镜头角色配置尚未完成')+'</p></div>';
    var trimNotice=previewForShot&&preview.request&&preview.request.assembly_trim_required?
      '<p class="sd-provider-timing-note">剧本镜头为 '+Number(preview.request.timeline_duration_seconds||0)+' 秒；生成服务最低返回 '+Number(preview.request.duration_seconds||0)+' 秒。预览将保留服务实际返回的完整镜头，并按最终实际时长验收。</p>':'';
    var requestMeta=previewForShot&&preview.request?'<small>'+escapeHtml(preview.request.ratio||'')+' · '+escapeHtml(preview.request.resolution||'')+' · '+Number(preview.request.duration_seconds||0)+' 秒</small>':'';
    var result=previewForShot?'<div class="sd-check '+(preview.ready?'pass':'warning')+'"><b>'+escapeHtml(userFacingVideoMessage(preview.message,'预检完成'))+'</b><p>'+escapeHtml(preview.request&&preview.request.prompt||'')+'</p>'+requestMeta+trimNotice+'</div>':'';
    var quoteHtml=quoteForShot?'<div class="sd-estimate"><strong>'+Number(quote.cost||0)+' 点</strong><span>报价 5 分钟内有效，确认后才扣点</span></div>':'';
    var jobDisplay=providerJobDisplay(jobForShot);
    var jobHtml=jobForShot?'<div class="sd-check '+(job.status==='succeeded'?'pass':(['failed','submit_unknown'].indexOf(job.status)>=0?'warning':''))+'" data-provider-job-progress="'+escapeHtml(shotKey)+'"><b>视频任务 · '+escapeHtml(jobDisplay.taskLabel)+'</b><p>'+escapeHtml(userFacingVideoMessage(job.error&&job.error.detail,job.status==='succeeded'?'新视频已生成，可在上方播放器查看。':jobDisplay.label+'，可离开页面。'))+'</p></div>':'';
    var localError=text(providerShotErrors&&providerShotErrors[shotKey]).trim();
    var localErrorHtml=localError?'<div class="sd-check warning sd-shot-provider-error" role="alert"><b>本次生成未提交</b><p>'+escapeHtml(localError)+'</p></div>':'';
    var blockedByOther=active&&!jobForShot;
    var disabledReason=!canGenerate?(generationReason||'请先确认并锁定当前剧本，再生成镜头视频。'):!providerShot.binding_ready?'请先确认并锁定当前镜头所需角色的标准图。':!sequenceReady?'请先生成上一个镜头，系统会用其结束画面承接当前镜头。':active?'已有视频任务正在处理，请等待任务结束。':'';
    var actions='<button data-action="edit-shot-execution" data-shot-key="'+escapeHtml(shotKey)+'" type="button"'+(canGenerate&&!active?'':' disabled')+'>'+(hasVideo?'调整要求并重新生成':jobForShot&&jobForShot.status==='failed'?'修改要求并重新生成':'编辑镜头生成要求')+'</button><button data-action="provider-preflight" data-shot-key="'+escapeHtml(shotKey)+'" type="button"'+(canGenerate&&providerShot.binding_ready&&sequenceReady&&!active?'':' disabled')+'>'+(hasVideo?'按当前要求免费预检':'免费检查生成参数')+'</button>';
    if(previewForShot&&preview.ready&&!quoteForShot)actions+='<button data-action="provider-quote" data-shot-key="'+escapeHtml(shotKey)+'" type="button"'+(canGenerate&&!active?'':' disabled')+'>获取付费报价</button>';
    if(quoteForShot)actions+='<button data-action="provider-start" data-shot-key="'+escapeHtml(shotKey)+'" type="button"'+(canGenerate&&!active?'':' disabled')+'>确认扣 '+Number(quote.cost||0)+' 点并生成</button>';
    return '<div class="sd-shot-provider-entry expanded">'+toggle+'<section class="sd-shot-provider-panel"><header><div><span>视频生成</span><b>生成服务</b></div><em>预检、报价不扣点</em></header>'+binding+continuityStatus+(blockedByOther?'<div class="sd-check warning"><b>另一个镜头正在生成</b><p>请等待当前任务结束后再提交本镜头，避免重复建单。</p></div>':'')+result+quoteHtml+jobHtml+localErrorHtml+providerFailureRecoveryHtml(jobForShot,{shot:shot,providerShot:providerShot,providerCharacters:poc.characters||[],execution:(autodraft.provider_execution_overrides||{})[shotKey]||{}})+'<div class="sd-shot-provider-actions">'+actions+'</div>'+(disabledReason?'<p class="sd-shot-provider-disabled-reason">'+escapeHtml(disabledReason)+'</p>':'')+'</section></div>';
  }
  function projectReviewHtml(understanding,project){
    understanding=understanding||{};project=project||{};
    var contract=understanding.import_contract||{},premise=understanding.premise||project.synopsis||'尚未填写核心故事';
    var characters=(contract.characters||[]).filter(Boolean),plotPoints=reviewStagePoints(contract);
    var shotCount=Number(understanding.shot_count||project.shot_count||plotPoints.length||0),duration=Number(understanding.duration_seconds||project.target_duration||0);
    var characterSummary=characters.length?characters.join('、'):'系统将在剧本生成时识别人物';
    var shotSummary=shotCount?shotCount+' 个镜头 · 预计 '+duration+' 秒':'系统将根据故事长度安排镜头';
    var plotHtml=plotPoints.length?'<ul>'+plotPoints.slice(0,6).map(function(item){return '<li><b>'+escapeHtml({start:'开场',middle:'发展',end:'结尾'}[item.position]||'剧情节点')+'</b><span>'+escapeHtml(item.excerpt)+'</span></li>';}).join('')+'</ul>':'<p>当前没有单独填写分镜要求，系统会依据核心故事自动补充。</p>';
    return '<section class="sd-project-review"><header><span>项目内容待确认</span><h1>生成前，请检查这三项内容</h1><p>确认无误后，可在右侧补充生成要求并生成第一版剧本。</p></header><nav class="sd-review-steps" aria-label="短剧创建进度"><span class="done"><i>✓</i>基本信息</span><span class="current"><i>2</i>内容确认</span><span><i>3</i>角色形象</span><span><i>4</i>生成剧本</span></nav><div class="sd-review-cards"><details open><summary><span class="sd-review-icon">故</span><span><small>01 · 核心故事</small><b>'+escapeHtml(premise)+'</b></span><em>已读取</em></summary><div class="sd-review-detail"><p>'+escapeHtml(premise)+'</p></div></details><details><summary><span class="sd-review-icon">角</span><span><small>02 · 主要角色</small><b>'+escapeHtml(characterSummary)+'</b></span><em>'+characters.length+' 个角色</em></summary><div class="sd-review-detail">'+(characters.length?'<div class="sd-review-tags">'+characters.map(function(name){return '<span>'+escapeHtml(name)+'</span>';}).join('')+'</div>':'<p>'+escapeHtml(characterSummary)+'</p>')+'</div></details><details><summary><span class="sd-review-icon">镜</span><span><small>03 · 分镜概要</small><b>'+escapeHtml(shotSummary)+'</b></span><em>'+plotPoints.length+' 个关键节点</em></summary><div class="sd-review-detail">'+plotHtml+'</div></details></div><footer><span>✓</span><p><b>确认后仍可继续修改</b><small>生成第一版剧本不会锁定项目，也不会扣点。</small></p></footer></section>';
  }
  function scriptHtml(version,canEdit,autodraft,selectedProviderShotKey,canGenerate,understanding,confirmationMessage,generationReason,sceneWorkspace,project,activeWorkspaceShotKey,sceneImageOperations,pendingSceneDeleteKey,providerShotErrors){
    if(!version||!version.script){
      return projectReviewHtml(understanding,project);
    }
    var script=version.script,overview=script.overview||{},mediaByShot=shotMediaIndex(autodraft),shots=script.shots||[];
    var structureJob=autodraft&&autodraft.provider_job||null,structureBusy=structureJob&&['billing','queued','submitting','running'].indexOf(structureJob.status)>=0;
    activeWorkspaceShotKey=defaultWorkspaceShotKey(shots,autodraft,activeWorkspaceShotKey);
    var activeShotIndex=Math.max(0,shots.map(function(shot){return text(shot.shot_key);}).indexOf(activeWorkspaceShotKey));
    var structureCapabilities=shotStructureCapabilities(shots,activeShotIndex,canEdit&&!structureBusy);
    if(shots[activeShotIndex]&&shots[activeShotIndex].locked)structureBusy=true;
    var visibleShots=shots.length?[shots[activeShotIndex]]:[];
    var legacy=version.model_version==='conversation-script-v2'?'<div class="sd-preflight-stale">该版本由旧通用模板生成，镜头可能与故事摘要不一致。请基于当前项目创建新版本后重新生成剧本。</div>':'';
    var dialogueById={};(script.dialogue_lines||[]).forEach(function(line){dialogueById[text(line.id)]=line;});
    var headerState=scriptHeaderState(version);
    return '<header class="sd-script-head '+headerState.key+'"><div class="sd-script-head-copy"><div class="sd-script-head-meta"><span>结构化剧本</span><span>版本 v'+Number(version.version||0)+'</span></div><h2>'+escapeHtml(overview.title||'未命名剧本')+'</h2><p>'+escapeHtml(overview.logline||'')+'</p></div><em>'+escapeHtml(headerState.label)+'</em></header>'+legacy+
      '<section class="sd-script-block"><h3>角色</h3><div class="sd-character-list">'+(script.characters||[]).map(function(item){return '<article><b>'+escapeHtml(item.name)+'</b><span>'+escapeHtml(item.identity)+'</span><p>'+escapeHtml(item.personality)+'</p></article>';}).join('')+'</div></section>'+sceneLockingHtml(sceneWorkspace,(canEdit||canGenerate),sceneImageOperations,pendingSceneDeleteKey)+shotGenerationOverviewHtml(shots,autodraft,activeWorkspaceShotKey,project,structureCapabilities)+
      '<section class="sd-script-block sd-single-shot-workspace"><header class="sd-block-heading"><div><span class="sd-section-kicker">当前镜头 '+(activeShotIndex+1)+' / '+shots.length+'</span><h3>镜头与台词</h3><p>点击上方进度中的镜头即可切换；页面只展示当前镜头。</p></div><nav><button type="button" data-action="step-workspace-shot" data-direction="-1"'+(activeShotIndex<=0?' disabled':'')+'>← 上一个镜头</button><button type="button" data-action="step-workspace-shot" data-direction="1"'+(activeShotIndex>=shots.length-1?' disabled':'')+'>下一个镜头 →</button></nav></header>'+visibleShots.map(function(shot){var index=activeShotIndex,lineIds=shot.dialogue_line_ids||[],lines=(lineIds.length?lineIds.map(function(lineId){return dialogueById[text(lineId)];}):[(script.dialogue_lines||[])[index]]).filter(function(line){return line&&line.kind!=='silence'&&text(line.text).trim();}),lineLabels=lines.map(function(line){return line.kind==='on_screen_text'?'画面文字：'+text(line.text):(line.speaker||'旁白')+'：'+text(line.text);}),dialogueHtml=lineLabels.length?'<ol class="sd-shot-dialogue-display">'+lineLabels.map(function(label){return '<li>'+escapeHtml(label)+'</li>';}).join('')+'</ol>':'<p>静默表演</p>',userAuthored=shot.source_type==='user_storyboard',sourceLabel=userAuthored?'用户原稿':'系统补充',sourceDetail=shot.source_text?'<dt>原稿依据</dt><dd>'+escapeHtml(shot.source_text)+'</dd>':'';var adjustable=structureCapabilities.enabled;return '<article class="sd-shot '+(shot.locked?'locked':'')+' '+(text(selectedProviderShotKey)===text(shot.shot_key)?'provider-selected':'')+'" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"><header><span>#'+Number(shot.sort_order||index+1)+' · '+Number(shot.duration_seconds||0)+'s · '+escapeHtml(shot.beat||'')+' <em class="sd-shot-origin '+(userAuthored?'user':'system')+'">'+sourceLabel+'</em></span><div>'+(shot.locked?'<em>已锁定</em>':'')+(canEdit?'<button type="button" data-action="edit-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'">编辑</button><button type="button" data-action="regenerate-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"'+(shot.locked?' disabled':'')+'>重生成分镜</button><button type="button" data-action="toggle-shot-lock" data-shot-key="'+escapeHtml(shot.shot_key||'')+'" data-locked="'+(shot.locked?'1':'0')+'">'+(shot.locked?'解锁':'锁定')+'</button>':'')+'</div></header>'+(adjustable?'<div class="sd-shot-structure-actions"><button data-action="move-shot-up" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"'+(structureCapabilities.moveUp?'':' disabled')+'>上移</button><button data-action="move-shot-down" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"'+(structureCapabilities.moveDown?'':' disabled')+'>下移</button><button data-action="copy-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"'+(structureCapabilities.copy?'':' disabled')+'>复制</button><button data-action="add-shot-before" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"'+(structureCapabilities.insertBefore?'':' disabled')+'>前面插入</button><button data-action="add-shot-after" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"'+(structureCapabilities.insertAfter?'':' disabled')+'>后面插入</button><button class="danger" data-action="delete-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"'+(structureCapabilities.deleteShot?'':' disabled')+'>删除</button></div>':'')+'<small>'+escapeHtml(shot.purpose||'剧情推进')+'</small><b>'+escapeHtml(shot.visual)+'</b>'+dialogueHtml+shotMediaHtml(shot,mediaByShot[text(shot.shot_key)],project&&project.ratio)+providerShotControlsHtml(shot,autodraft,canGenerate===undefined?canEdit:canGenerate,selectedProviderShotKey,generationReason,providerShotErrors)+'<details><summary>查看镜头执行信息</summary><dl><dt>内容来源</dt><dd>'+sourceLabel+'</dd>'+sourceDetail+'<dt>场景</dt><dd>'+escapeHtml(shot.scene||'')+'</dd><dt>机位</dt><dd>'+escapeHtml(shot.camera||'')+'</dd><dt>连续性</dt><dd>'+escapeHtml(shot.continuity||'')+'</dd><dt>实际提交提示词</dt><dd>'+escapeHtml(currentShotExecutionPrompt(shot,autodraft))+'</dd></dl></details></article>';}).join('')+'</section>';
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
        '<div class="sd-direction-gate ready"><b>'+(hasScript?'当前剧本可继续修改':'项目内容已确认')+'</b><p>'+(hasScript?'本次要求将生成新的剧本版本。':'现在可以生成首版结构化剧本。')+'</p></div>':
        '<div class="sd-direction-gate pending"><b>'+(understanding.confirmation_invalidated?'内容修改后需要重新确认':'确认项目内容')+'</b><p>请核对核心故事、角色和分镜要求，确认后生成第一版剧本。</p>'+(questions?'<ul>'+questions+'</ul>':'')+'</div>';
      if(hasScript)return '<section class="sd-current-action">'+gate+'<button data-action="lock" class="secondary" type="button">确认并锁定当前剧本</button><details><summary>需要修改剧本</summary><textarea id="sdInstruction" maxlength="2000" placeholder="填写需要修改的内容"></textarea><button data-action="generate" type="button">按要求生成新版本</button></details><p class="sd-free">确认和修改剧本均不扣点</p></section>';
      var firstAction=!confirmed&&understanding.phase==='import_review'?'<button data-action="confirm-and-generate" data-message="确认尊重原稿并生成" type="button">确认项目内容并生成第一版剧本</button>':'<button data-action="generate" type="button">生成第一版完整剧本</button>';
      return '<section class="sd-current-action">'+gate+'<details><summary>补充生成要求（可选）</summary><textarea id="sdInstruction" maxlength="2000" placeholder="例如：保留指定台词、加强冲突、避免出现某类内容"></textarea></details>'+firstAction+'<p class="sd-free">本阶段不扣点</p></section>';
    }
    preflight=preflight||{};
    var current=preflight.current_plan,plan=current&&current.plan;
    if(!plan)return '<section class="sd-preflight"><span class="sd-stage-label">PR-3 · 制作准备</span><h2>制作前自动体检</h2><p>检查时长、素材、复杂度和预算，并把锁定剧本转换为可执行制作计划。</p><label>制作路线<select id="sdQualityRoute"><option value="quick_draft">单镜头生成 · 768p</option><option value="formal">全片草稿 · 1080p</option></select></label><button data-action="prepare" type="button"'+(canEdit?'':' disabled')+'>生成制作方案</button><p class="sd-free">只估算，不扣点</p></section>';
    var confirmed=current.status==='confirmed',stale=!!preflight.stale;
    var checks=(plan.checks||[]).map(function(item){return '<article class="sd-check '+escapeHtml(item.status)+'"><span>'+escapeHtml(item.status==='pass'?'通过':item.status==='blocker'?'阻塞':'需确认')+'</span><b>'+escapeHtml(item.label)+'</b><p>'+escapeHtml(item.summary)+'</p>'+(item.suggestion?'<small>'+escapeHtml(item.suggestion)+'</small>':'')+'</article>';}).join('');
    var routeOptions=(plan.route_options||[]).map(function(item){return '<option value="'+escapeHtml(item.key)+'"'+(item.key===plan.quality_route?' selected':'')+'>'+escapeHtml(item.name)+' · '+Number(item.estimated_points)+' 点估算</option>';}).join('');
    var acceptance=(plan.required_acceptance||[]).length?'<label class="sd-accept"><input id="sdAcceptAdjustments" type="checkbox"> 我已了解并接受 '+Number(plan.required_acceptance.length)+' 项系统建议</label>':'';
    return '<section class="sd-preflight"><span class="sd-stage-label">PR-3 · 制作准备</span><h2>'+(confirmed?'制作方案已确认':'制作方案 v'+Number(current.version)+' 待确认')+'</h2>'+(stale?'<div class="sd-preflight-stale">剧本或项目规格已变化，请重新体检后再确认。</div>':'')+'<div class="sd-estimate"><strong>'+Number(plan.estimate&&plan.estimate.points||0)+' 点</strong><span>'+escapeHtml(plan.estimate&&plan.estimate.resolution||'')+' · 约 '+Number(plan.estimate&&plan.estimate.minutes||0)+' 分钟</span></div><label>制作路线<select id="sdQualityRoute"'+(confirmed||!canEdit?' disabled':'')+'>'+routeOptions+'</select></label><div class="sd-checks">'+checks+'</div><p class="sd-plan-meta">'+Number(plan.duration&&plan.duration.shots&&plan.duration.shots.length||0)+' 镜 · '+Number(plan.duration&&plan.duration.target_ms||0)/1000+' 秒 · '+Number((plan.assets||[]).length)+' 项推荐素材</p>'+(confirmed?'<div class="sd-confirmed">已锁定制作方案 v'+Number(current.version)+'，下一阶段可据此生成自动草稿。</div>':acceptance+'<button data-action="confirm-plan" class="secondary" type="button"'+(plan.ready&&!stale&&canEdit?'':' disabled')+'>确认制作方案</button><button data-action="prepare" type="button"'+(canEdit?'':' disabled')+'>按当前路线重新体检</button>')+'<p class="sd-free">当前仅为估算，本阶段不扣点</p></section>';
  }
  function legacyMediaRecoveryEvidence(result,timing){
    result=result||{};timing=timing||{};
    return {
      operation_version:'legacy-media-recovery-evidence-v1',
      project_id:text(result.project_id).trim(),
      started_at:text(timing.started_at).trim(),
      completed_at:text(timing.completed_at).trim(),
      recovered_shot_keys:(result.recovered_shot_keys||[]).map(function(value){return text(value).trim();}),
      failed_shots:(result.failed_shots||[]).map(function(item){item=item||{};return {shot_key:text(item.shot_key).trim(),code:text(item.code).trim(),detail:text(item.detail)};}),
      skipped_shot_keys:(result.skipped_shot_keys||[]).map(function(value){return text(value).trim();})
    };
  }
  function legacyMediaRecoveryResultJson(evidence){
    return JSON.stringify(evidence||{},null,2);
  }
  function legacyMediaRecoveryResultHtml(evidence){
    if(!evidence)return '';
    var recovered=evidence.recovered_shot_keys||[],failed=evidence.failed_shots||[],skipped=evidence.skipped_shot_keys||[];
    var recoveredHtml=recovered.map(function(shotKey){return '<li><code>'+escapeHtml(shotKey)+'</code></li>';}).join('')||'<li>无</li>';
    var failedHtml=failed.map(function(item){return '<li><code>'+escapeHtml(item.shot_key)+'</code><b>'+escapeHtml(item.code)+'</b><small>'+escapeHtml(item.detail)+'</small></li>';}).join('')||'<li>无</li>';
    var skippedHtml=skipped.map(function(shotKey){return '<li><code>'+escapeHtml(shotKey)+'</code></li>';}).join('')||'<li>无</li>';
    return '<section class="sd-recovery-evidence" aria-live="polite"><header><div><span class="sd-stage-label">历史原片恢复记录</span><h3>本次校验结果</h3></div><div><button type="button" class="secondary" data-action="copy-legacy-media-recovery">复制 JSON</button><button type="button" class="secondary" data-action="download-legacy-media-recovery">下载 JSON</button></div></header><p class="sd-recovery-time"><span>开始：<time>'+escapeHtml(evidence.started_at)+'</time></span><span>完成：<time>'+escapeHtml(evidence.completed_at)+'</time></span></p><div class="sd-recovery-groups"><article class="pass"><b>已恢复 '+recovered.length+'</b><ul>'+recoveredHtml+'</ul></article><article class="warning"><b>失败 '+failed.length+'</b><ul>'+failedHtml+'</ul></article><article><b>已跳过 '+skipped.length+'</b><ul>'+skippedHtml+'</ul></article></div><details><summary>查看完整 JSON</summary><pre>'+escapeHtml(legacyMediaRecoveryResultJson(evidence))+'</pre></details></section>';
  }
  function handleLegacyMediaRecoveryEvidenceAction(action,evidence,dependencies){
    dependencies=dependencies||{};
    var payload=legacyMediaRecoveryResultJson(evidence);
    if(action==='copy-legacy-media-recovery'){
      var clipboard=dependencies.clipboard;
      if(clipboard&&typeof clipboard.writeText==='function'){
        try{return Promise.resolve(clipboard.writeText(payload)).then(function(){return {action:'copied',payload:payload};});}
        catch(error){return Promise.reject(error);}
      }
      var copyDocument=dependencies.document;
      if(copyDocument&&copyDocument.body&&typeof copyDocument.createElement==='function'&&typeof copyDocument.execCommand==='function'){
        var textarea=copyDocument.createElement('textarea');
        textarea.value=payload;textarea.setAttribute('readonly','');textarea.style.position='fixed';textarea.style.opacity='0';
        copyDocument.body.appendChild(textarea);textarea.select();
        var copied=copyDocument.execCommand('copy');textarea.remove();
        if(copied)return Promise.resolve({action:'copied',payload:payload});
      }
      return Promise.reject(new Error('浏览器未开放剪贴板权限；请展开并手动复制完整 JSON。'));
    }
    if(action==='download-legacy-media-recovery'){
      var downloadDocument=dependencies.document,BlobConstructor=dependencies.Blob,URLApi=dependencies.URL;
      if(!downloadDocument||!downloadDocument.body||typeof downloadDocument.createElement!=='function'||typeof BlobConstructor!=='function'||!URLApi||typeof URLApi.createObjectURL!=='function')return Promise.reject(new Error('浏览器暂不支持下载；请展开并手动保存完整 JSON。'));
      var blob=new BlobConstructor([payload],{type:'application/json;charset=utf-8'}),url=URLApi.createObjectURL(blob),anchor=downloadDocument.createElement('a');
      var projectPart=text(evidence&&evidence.project_id||'project').replace(/[^0-9A-Za-z_-]+/g,'-')||'project';
      var timePart=text(evidence&&evidence.completed_at||'result').replace(/[^0-9A-Za-z_-]+/g,'-').replace(/-+$/,'')||'result';
      anchor.href=url;anchor.download='legacy-media-recovery-'+projectPart+'-'+timePart+'.json';
      downloadDocument.body.appendChild(anchor);anchor.click();anchor.remove();
      (dependencies.setTimeout||setTimeout)(function(){if(typeof URLApi.revokeObjectURL==='function')URLApi.revokeObjectURL(url);},1000);
      return Promise.resolve({action:'downloaded',payload:payload,filename:anchor.download});
    }
    return Promise.resolve({action:'ignored',payload:payload});
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
      return '<section class="sd-autodraft-actions" data-background-job-progress="autodraft"><span class="sd-stage-label">PR-4 · 自动草稿</span><h2>后台正在制作</h2><div class="sd-progress"><i style="width:'+Math.max(0,Math.min(100,Number(job.progress||0)))+'%"></i></div><strong>'+Number(job.progress||0)+'%</strong><p>'+escapeHtml(job.phase||'queued')+' · 可离开页面，任务会继续执行。</p></section>';
    }
    var plan=autodraft.confirmed_plan;
    if(!plan)return '';
    if(production.ready===false){
      var poc=autodraft.provider_poc||{},preview=autodraft.provider_preview||null,quote=autodraft.provider_quote||null,shotJobs=allProviderJobs(autodraft),shots=poc.shots||[],characters=poc.characters||[],provider=production.provider||{},providerState=provider.configured?'配置已就绪':'尚未配置';
      var boundCharacters=characters.filter(function(item){return item.binding_ready;});
      var missingCharacters=characters.filter(function(item){return !item.binding_ready;});
      var allRolesBound=characters.length>0&&boundCharacters.length===characters.length;
      var firstShot=shots[0]||null;
      var shotOptions=shots.map(function(item){return '<option value="'+escapeHtml(item.shot_key)+'">#'+Number(item.sort_order||0)+' · '+escapeHtml(item.scene||item.shot_key)+' · '+Math.ceil(Number(item.duration_ms||0)/1000)+'s</option>';}).join('');
      var result=preview?'<div class="sd-check '+(preview.ready?'pass':'warning')+'"><b>'+escapeHtml(userFacingVideoMessage(preview.message,'预检完成'))+'</b><p>'+escapeHtml(preview.request&&preview.request.prompt||'')+'</p><small>'+escapeHtml(preview.request&&preview.request.ratio||'')+' · '+escapeHtml(preview.request&&preview.request.resolution||'')+' · '+Number(preview.request&&preview.request.duration_seconds||0)+' 秒<br>'+escapeHtml(userFacingVideoMessage(preview.next_action,''))+'</small></div>':'';
      var quoteHtml=quote?'<div class="sd-estimate"><strong>'+Number(quote.cost||0)+' 点</strong><span>报价 '+escapeHtml(quote.shot&&quote.shot.shot_key||'')+' · 5 分钟内有效</span></div>':'';
      var assemblyProgress=production.assembly||{},readyShots=Number(assemblyProgress.ready_count||0),requiredShots=Number(assemblyProgress.required_count||shots.length||0);
      var providerJobHtml=shotJobs.map(function(shotJob){
        var jobError=shotJob.error&&shotJob.error.detail||'';
        var jobMessage=userFacingVideoMessage(jobError,shotJob.status==='succeeded'?
          '镜头 '+(shotJob.shot_key||'')+' 已生成完成':
          '镜头 '+(shotJob.shot_key||'')+' 正在后台处理');
        var shotJobDisplay=providerJobDisplay(shotJob);
        return '<div class="sd-check '+(shotJob.status==='succeeded'?'pass':(['failed','submit_unknown'].indexOf(shotJob.status)>=0?'warning':''))+'"><b>整体进度 · 已完成 '+readyShots+'/'+requiredShots+' 个镜头</b><p>最近任务：'+escapeHtml(shotJob.shot_key||'')+' · '+escapeHtml(shotJobDisplay.taskLabel)+'</p><small>'+escapeHtml(jobMessage)+'</small><button type="button" class="sd-shot-jump" data-action="jump-to-shot" data-shot-key="'+escapeHtml(shotJob.shot_key||'')+'">在镜头与台词中查看</button></div>';
      }).join('');
      var lowResolutionShots=(production.assembly&&production.assembly.low_resolution_shot_keys)||[];
      var missingVerificationShots=(production.assembly&&production.assembly.media_verification_missing_shot_keys)||[];
      var canRecoverLegacy=!!(autodraft.permissions&&autodraft.permissions.can_recover_legacy_media);
      var recoveryBusy=allProviderJobs(autodraft).some(function(item){
        return ['billing','queued','submitting','running','submit_unknown'].indexOf(item.status)>=0;
      })||!!(job&&['queued','running'].indexOf(job.status)>=0);
      var qualityWarning=lowResolutionShots.length?'<div class="sd-check warning"><b>历史 768p 版本需要重新生成</b><p>原生 2K 草稿要求下，请重新生成：'+escapeHtml(lowResolutionShots.join('、'))+'。旧版本会继续保留。</p></div>':missingVerificationShots.length?'<div class="sd-check warning"><b>历史 2K 镜头缺少媒体校验记录</b><p>请先完成本地验证恢复：'+escapeHtml(missingVerificationShots.join('、'))+'。验证通过后无需重新生成，也不会扣点。</p>'+(canRecoverLegacy?'<button type="button" data-action="recover-legacy-media"'+(recoveryBusy?' disabled':'')+'>验证并恢复历史原片</button>'+(recoveryBusy?'<small>请等待当前任务结束后再恢复。</small>':''):'<small>仅项目所有者可以执行历史原片恢复。</small>')+'</div>':'';
      var bindingSummary=allRolesBound?
        '<div class="sd-check pass" id="sdProviderBindingStatus"><b>'+boundCharacters.length+'/'+characters.length+' 个角色已锁定，可开始检查镜头</b><p>人物形象统一由左侧角色卡管理，当前镜头会自动使用对应角色的已锁定形象。</p></div>':
        '<div class="sd-check warning" id="sdProviderBindingStatus"><b>角色形象尚未准备完整</b><p>'+(missingCharacters.length?'未绑定：'+escapeHtml(missingCharacters.map(function(item){return item.name||item.character_key;}).join('、'))+'。':'角色资料仍在加载。')+' 请点击左侧角色卡完成形象生成、选择与锁定。</p></div>';
      var activeCount=activeProviderJobs(autodraft).length;
      return '<section class="sd-autodraft-actions sd-provider-summary"><span class="sd-stage-label">PR-4 · 视频生成</span><h2>视频生成总览</h2><div class="sd-preflight-stale">'+escapeHtml(userFacingVideoMessage(production.message,'当前不能生成与剧本一致的短剧。'))+'</div>'+qualityWarning+'<div class="sd-estimate"><strong>生成服务</strong><span>'+escapeHtml(providerState)+'</span></div>'+bindingSummary+'<div class="sd-provider-counts"><span><b>'+shots.length+'</b> 个镜头</span><span><b>'+boundCharacters.length+'/'+characters.length+'</b> 角色就绪</span><span><b>'+activeCount+'</b> 个任务处理中</span></div><p>请在左侧“镜头与台词”中点击对应镜头的“生成镜头视频”。预检和报价不扣点，确认生成后才扣点。</p>'+providerJobHtml+'</section>';
    }
    if(production.mode==='demo')return '<section class="sd-autodraft-actions"><span class="sd-stage-label">PR-4 · 演示模式</span><h2>生成界面联调示例</h2><p>该模式只验证任务、轮询和播放器，不会根据剧本生成真实画面。</p><div class="sd-estimate"><strong>0 点</strong><span>固定示例 · 不可交付</span></div><button data-action="start-draft" type="button"'+(canEdit?'':' disabled')+'>生成演示草稿</button></section>';
    var assembling=production.mode==='provider_poc'&&production.assembly&&production.assembly.all_ready;
    var continuity=production.assembly||{},continuityRequired=Number(continuity.continuity_required_count||0),continuityReady=Number(continuity.continuity_ready_count||0),continuityMissing=continuity.continuity_missing_shot_keys||[];
    var continuityHtml=assembling?(continuity.continuity_ready?
      '<div class="sd-check pass"><b>镜头连续性已建立</b><p>'+continuityReady+'/'+continuityRequired+' 个镜头衔接已承接上一镜头的画面状态。</p></div>':
      '<div class="sd-check warning"><b>有 '+continuityMissing.length+' 个镜头未建立画面承接</b><p>可先合成预览；若背景或动作跳变，建议重新生成 '+escapeHtml(continuityMissing.join('、'))+'。</p></div>') : '';
    if(job&&['failed','canceled'].indexOf(job.status)>=0){
      var failureDetail=userFacingVideoMessage(job.error&&job.error.detail,'合成任务未完成，请重新尝试。');
      return '<section class="sd-autodraft-actions sd-autodraft-failed"><span class="sd-stage-label">PR-4 · 合成预览</span><h2>上次合成失败</h2><div class="sd-preflight-stale">'+escapeHtml(failureDetail)+'</div><p>已经生成的镜头均已保留，重新合成不会再次生成镜头，也不会重复扣点。</p><div class="sd-estimate"><strong>'+Number(billing.cost||0)+' 点</strong><span>1080p · 可安全重试</span></div><button data-action="start-draft" type="button"'+(canEdit&&assembling?'':' disabled')+'>重新合成 1080p 草稿</button></section>';
    }
    return '<section class="sd-autodraft-actions"><span class="sd-stage-label">PR-4 · 合成预览</span><h2>'+(assembling?'全部镜头已完成':'一键生成可播放草稿')+'</h2><p>'+(assembling?'将 '+Number(production.assembly.ready_count||0)+' 个已生成镜头按剧本顺序合成为 1080p 全片草稿；不会重复扣除镜头生成费用。':'自动准备素材、画面、配音、字幕与基础口型；个别镜头失败时会安全降级，优先交付完整草稿。')+'</p>'+continuityHtml+'<div class="sd-estimate"><strong>'+Number(billing.cost||0)+' 点</strong><span>1080p · 后台任务</span></div><button data-action="start-draft" type="button"'+(canEdit?'':' disabled')+'>'+(assembling?'合成 1080p 草稿':'开始自动制作')+'</button><p class="sd-free">'+(billing.mode==='provider_assets_already_charged'?'镜头费用已结算，本次合成不重复扣点':billing.mode==='development_free'?'本地开发模式：不扣点':'提交后扣点；建单失败自动退款')+'</p></section>';
  }
  function draftHtml(autodraft){
    var version=autodraft&&autodraft.current_version;
    if(!version)return '';
    var manifest=version.manifest||{},shots=manifest.shots||[],issues=manifest.issues||[];
    if(version.is_demo)return '<section class="sd-draft"><header><div><span>PR-4 · 演示模式</span><h2>固定界面联调视频</h2><p>该视频与当前剧本无关，仅用于验证播放器，不能作为项目成果。</p></div><em>demo</em></header><video controls preload="metadata" src="'+escapeHtml(version.url||'')+'"></video></section>';
    var previewResolution=text(manifest.resolution||'720p').toLowerCase(),previewLabel=previewResolution==='1080p'?'1080p 全片草稿':previewResolution+' 自动草稿';
    return '<section class="sd-draft"><header><div><span>PR-4 · '+escapeHtml(previewLabel)+'</span><h2>可播放草稿 v'+Number(version.version||0)+'</h2><p>'+(version.status==='degraded'?'已安全降级交付，可继续优化问题镜头。':'全部镜头已完成。')+'</p></div><em>'+escapeHtml(version.status||'ready')+'</em></header><video controls preload="metadata" src="'+escapeHtml(version.url||'')+'"></video><div class="sd-draft-media-actions"><a href="'+escapeHtml(version.url||'')+'" target="_blank" rel="noopener">单独打开</a><a href="'+escapeHtml(version.url||'')+'" download>下载预览</a></div><div class="sd-draft-summary"><strong>'+shots.length+' 个镜头</strong><strong>'+issues.length+' 个待优化</strong><strong>'+Math.round(Number(manifest.duration_ms||0)/1000)+' 秒</strong></div><h3>镜头状态</h3><div class="sd-draft-shots">'+shots.map(function(shot){return '<article class="'+escapeHtml(shot.status||'ready')+'"><b>#'+Number(shot.sort_order||0)+' · '+escapeHtml(shot.shot_key||'')+'</b><span>'+escapeHtml(shot.status==='degraded'?'安全替代':'已就绪')+'</span><p>'+escapeHtml(shot.issue&&shot.issue.message||'画面与视频原生声音已装配。')+'</p></article>';}).join('')+'</div></section>';
  }
  function refinementIssueGroups(current){
    var issues=current&&Array.isArray(current.issues)?current.issues:[];
    return {
      shots:issues.filter(function(item){return !!text(item&&item.shot_key).trim();}),
      preparation:issues.filter(function(item){return !text(item&&item.shot_key).trim();})
    };
  }
  function refinementTimelineTime(milliseconds){
    var seconds=Math.max(0,Math.round(Number(milliseconds||0)/1000)),minutes=Math.floor(seconds/60);
    return minutes+':'+String(seconds%60).padStart(2,'0');
  }
  function refinementShotTimeline(shots,assembly){
    shots=Array.isArray(shots)?shots.slice():[];assembly=assembly||{};
    shots.sort(function(left,right){return Number(left&&left.sort_order||0)-Number(right&&right.sort_order||0);});
    var assemblyDurations={};
    (Array.isArray(assembly.shot_durations)?assembly.shot_durations:[]).forEach(function(item){
      var shotKey=text(item&&item.shot_key).trim(),duration=Number(item&&item.duration_ms||0);
      if(shotKey&&duration>0)assemblyDurations[shotKey]=duration;
    });
    var durations=shots.map(function(shot){
      shot=shot||{};var start=Number(shot.start_ms),end=Number(shot.end_ms),duration=Number(assemblyDurations[text(shot.shot_key).trim()]||0);
      if(!(duration>0))duration=Number(shot.media_validation&&shot.media_validation.duration_ms||0);
      if(!(duration>0))duration=end>start?end-start:0;
      return duration>0?duration:0;
    });
    var knownDurations=durations.filter(function(value){return value>0;}),knownTotal=knownDurations.reduce(function(total,value){return total+value;},0);
    var declaredTotal=Number(assembly.source_duration_ms||assembly.preview_duration_ms||0),missingCount=durations.filter(function(value){return !(value>0);}).length;
    var fallback=knownDurations.length?knownTotal/knownDurations.length:declaredTotal>0&&shots.length?declaredTotal/shots.length:5000;
    if(missingCount&&declaredTotal>knownTotal)fallback=(declaredTotal-knownTotal)/missingCount;
    var cursor=0;
    var entries=shots.map(function(shot,index){
      var duration=durations[index]>0?durations[index]:fallback,start=cursor,end=start+duration;cursor=end;
      return {shot:shot,shot_key:text(shot&&shot.shot_key).trim(),sort_order:Number(shot&&shot.sort_order||index+1),start_ms:Math.round(start),end_ms:Math.round(end),duration_ms:Math.round(duration)};
    });
    return {entries:entries,total_ms:Math.round(cursor)};
  }
  function refinementShotLocatorHtml(shots,assembly,issues){
    var timeline=refinementShotTimeline(shots,assembly),entries=timeline.entries;
    if(!entries.length)return '';
    if(assembly.available===false||assembly.reassembly_required===true){
      return '<section class="sd-refinement-locator paused" data-refinement-shot-locator-paused aria-label="镜头定位暂不可用">'+
        '<header><div><span>镜头定位</span><b>定位已暂停</b></div></header>'+
        '<p>当前完整预览与逐镜素材暂不一致。重新合成完整预览后，镜头跳转和当前镜头标记会自动恢复。</p></section>';
    }
    var issueKeys={};
    (Array.isArray(issues)?issues:[]).forEach(function(item){var value=text(item&&item.shot_key).trim();if(value)issueKeys[value]=true;});
    var first=entries[0],firstLabel='#'+first.sort_order+' · '+refinementTimelineTime(first.start_ms)+'–'+refinementTimelineTime(first.end_ms);
    return '<section class="sd-refinement-locator" data-refinement-shot-locator data-total-ms="'+timeline.total_ms+'" aria-label="合成视频镜头定位">'+
      '<header><div><span>镜头定位</span><b>当前播放：<output data-refinement-current-shot>'+escapeHtml(firstLabel)+'</output></b></div><button type="button" data-action="mark-current-refinement-shot" data-shot-key="'+escapeHtml(first.shot_key)+'">标记当前镜头有问题</button></header>'+
      '<div class="sd-refinement-locator-scroll"><div class="sd-refinement-locator-track">'+entries.map(function(entry,index){
        var shot=entry.shot||{},share=timeline.total_ms?entry.duration_ms/timeline.total_ms*100:100/entries.length;
        var flagged=issueKeys[entry.shot_key]||shot.status==='degraded'||!!shot.issue;
        var summary=text(shot.scene||shot.purpose||shot.action||shot.visual||entry.shot_key).trim();
        var range=refinementTimelineTime(entry.start_ms)+'–'+refinementTimelineTime(entry.end_ms);
        return '<button type="button" class="sd-refinement-locator-shot '+(flagged?'flagged':'ready')+(index===0?' current':'')+'" data-action="seek-refinement-shot" data-shot-key="'+escapeHtml(entry.shot_key)+'" data-shot-order="'+entry.sort_order+'" data-start-ms="'+entry.start_ms+'" data-end-ms="'+entry.end_ms+'" data-shot-range="'+escapeHtml(range)+'" style="--shot-share:'+share.toFixed(4)+'%" aria-label="跳转到镜头 '+entry.sort_order+(flagged?'，已标记问题':'')+'，'+escapeHtml(range)+'" title="镜头 #'+entry.sort_order+' · '+escapeHtml(range)+(summary?' · '+escapeHtml(summary):'')+'"'+(index===0?' aria-current="true"':'')+'><b>#'+entry.sort_order+'</b><small>'+Math.max(1,Math.round(entry.duration_ms/1000))+' 秒'+(flagged?' · 有问题':'')+'</small></button>';
      }).join('')+'</div></div><p><span class="current">当前播放</span><span class="ready">已就绪</span><span class="flagged">已标记问题</span>点击镜头可跳转到该镜头开头</p></section>';
  }
  function refinementShotCandidateHtml(shot,autodraft){
    var media=shotMediaIndex(autodraft||{})[text(shot&&shot.shot_key)]||{versions:[]};
    var issue=shot&&shot.issue||{},floor=Number(issue.provider_version_floor);
    if(!Number.isFinite(floor))floor=Number(shot&&shot.provider_version||0);
    var candidates=(media.versions||[]).filter(function(item){return Number(item.version||0)>floor;});
    if(!candidates.length)return '<section class="sd-refinement-candidates empty"><header><div><span>候选版本</span><h4>还没有候选镜头</h4></div></header><p>完成上方四步后，新版本会出现在这里。原镜头和整片预览都会保留。</p><button type="button" data-action="edit-shot-execution" data-shot-key="'+escapeHtml(shot.shot_key||'')+'">修改提示词与生成要求</button><footer class="sd-refinement-redo-sticky"><div><b>暂无可采用的候选版本</b><small>重新生成完成后，请先选择满意版本再采用。</small></div><button type="button" disabled>采用当前候选版本</button></footer></section>';
    var selectedCandidate=candidates.filter(function(item){return item.selected;})[0]||null;
    return '<section class="sd-refinement-candidates"><header><div><span>候选版本</span><h4>对比并选择满意版本</h4><p>每个版本都可独立播放；选择后不会立即替换整片。</p></div><em>'+candidates.length+' 个候选</em></header><div class="sd-refinement-candidate-rail">'+candidates.map(function(item){
      var candidateVideo=item.url?'<video controls preload="metadata" src="'+escapeHtml(item.url||'')+'"></video>':'<div class="sd-refinement-candidate-empty">候选视频暂不可预览</div>';
      return '<article class="'+(item.selected?'selected':'')+'"><header><b>候选 v'+Number(item.version||0)+'</b><span>'+(item.selected?'当前选择':'待选择')+'</span></header>'+candidateVideo+'<footer>'+(item.selected?'<b>已选中，确认后才会采用</b>':'<button type="button" data-action="select-provider-version" data-shot-key="'+escapeHtml(item.shot_key||shot.shot_key||'')+'" data-version-id="'+escapeHtml(item.id||'')+'">选择此版本</button>')+'</footer></article>';
    }).join('')+'</div><footer class="sd-refinement-redo-sticky"><div><b>'+(selectedCandidate?'候选 v'+Number(selectedCandidate.version||0)+' 已选中':'请先选择候选版本')+'</b><small>采用后仅更新当前镜头，整片需要最后统一重新合成。</small></div><button type="button" data-action="refine-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'" data-version-id="'+escapeHtml(selectedCandidate&&selectedCandidate.id||'')+'"'+(selectedCandidate?'':' disabled')+'>采用当前候选版本</button></footer></section>';
  }
  function refinementCandidateRequest(projectId,shotKey,replacementVersionId,preview){
    projectId=text(projectId).trim();shotKey=text(shotKey).trim();replacementVersionId=text(replacementVersionId).trim();
    if(!projectId||!shotKey||!replacementVersionId)throw new Error('请先选择要采用的候选版本');
    var previewPayload={project_id:projectId,shot_key:shotKey,replacement_provider_version_id:replacementVersionId};
    if(!preview)return {preview:previewPayload,adoption:null};
    if(text(preview.replacement_provider_version_id).trim()!==replacementVersionId)throw new Error('候选版本已变化，请刷新后重新选择');
    return {
      preview:previewPayload,
      adoption:{project_id:projectId,shot_key:shotKey,source_version_id:preview.source_version_id,replacement_provider_version_id:replacementVersionId}
    };
  }
  function refinementRedoGenerationHtml(shot,autodraft,canEdit){
    autodraft=autodraft||{};shot=shot||{};
    var shotKey=text(shot.shot_key),poc=autodraft.provider_poc||{};
    var providerShot=(poc.shots||[]).filter(function(item){return text(item.shot_key)===shotKey;})[0]||null;
    if(!providerShot)return '<section class="sd-refinement-redo-generation"><header><div><span>重新生成当前镜头</span><h4>生成资料尚未就绪</h4></div></header><div class="sd-check warning"><b>请刷新后重试</b><p>系统暂未读取到这个镜头的生成配置，原镜头和整片预览不会受到影响。</p></div></section>';
    var preview=autodraft.provider_preview||null,quote=autodraft.provider_quote||null,job=autodraft.provider_job||null;
    var previewForShot=preview&&(!preview.shot||text(preview.shot.shot_key)===shotKey),quoteForShot=quote&&(!quote.shot||text(quote.shot.shot_key)===shotKey);
    var jobForShot=job&&text(job.shot_key)===shotKey?job:null;
    var active=job&&['billing','queued','submitting','running'].indexOf(job.status)>=0,blockedByOther=active&&!jobForShot;
    var requiredKeys=providerShot.character_keys||[],characters=poc.characters||[];
    var requiredCharacters=requiredKeys.map(function(key){return characters.filter(function(item){return item.character_key===key;})[0]||{name:key,binding_ready:false};});
    var missing=requiredCharacters.filter(function(item){return !item.binding_ready;});
    var sequenceReady=providerShot.sequence_ready!==false;
    var readyForPreflight=canEdit&&providerShot.binding_ready&&sequenceReady&&!active;
    var readyForQuote=canEdit&&previewForShot&&preview.ready&&!quoteForShot&&!active;
    var readyForStart=canEdit&&quoteForShot&&!active;
    var binding=providerShot.binding_ready?
      '<div class="sd-check pass"><b>本镜头角色已就绪</b><p>'+escapeHtml(requiredCharacters.map(function(item){return item.name;}).join('、')||'本镜头无需绑定角色')+'</p></div>':
      '<div class="sd-check warning"><b>请先锁定角色标准图</b><p>'+escapeHtml(missing.map(function(item){return item.name;}).join('、')||'镜头角色配置尚未完成')+'</p></div>';
    var continuity=sequenceReady?
      '<div class="sd-check pass"><b>镜头连续性已就绪</b><p>'+(providerShot.previous_shot_key?'将承接 '+escapeHtml(providerShot.previous_shot_key)+' 的结束状态。':'首镜头将使用全片统一视觉基线。')+'</p></div>':
      '<div class="sd-check warning"><b>请先完成上一镜头</b><p>当前镜头需要承接 '+escapeHtml(providerShot.previous_shot_key||'上一镜头')+' 的结束状态。</p></div>';
    var stepOneStatus='<div class="sd-refinement-step-status ready"><span>修改后保存，系统会直接完成免费预检。</span></div>';
    var stepTwoStatus=previewForShot?'<div class="sd-refinement-step-status '+(preview.ready?'ready':'warning')+'"><details class="sd-refinement-step-details"><summary>'+escapeHtml(userFacingVideoMessage(preview.message,preview.ready?'参数检查通过':'参数仍需调整'))+'</summary><p>'+escapeHtml(preview.request&&preview.request.prompt||'')+'</p></details></div>':'<div class="sd-refinement-step-status"><span>等待免费检查</span></div>';
    var stepThreeStatus=quoteForShot?'<div class="sd-refinement-step-status ready"><strong>'+Number(quote.cost||0)+' 点</strong><span>5 分钟内有效，确认前不扣点</span></div>':'<div class="sd-refinement-step-status"><span>'+(previewForShot&&preview.ready?'检查已通过，可以获取报价':'预检通过后可获取报价')+'</span></div>';
    var stepFourStatus=jobForShot?'<div class="sd-refinement-step-status '+(job.status==='succeeded'?'ready':(['failed','submit_unknown','canceled'].indexOf(job.status)>=0?'warning':''))+'" data-provider-job-progress="'+escapeHtml(shotKey)+'"><b>镜头任务 · '+escapeHtml(job.status||'')+' · '+Number(job.progress||0)+'%</b><p>'+escapeHtml(userFacingVideoMessage(job.error&&job.error.detail,job.status==='succeeded'?'候选镜头已生成，请在下方预览并选择。':'任务正在后台处理，可继续查看其他问题镜头。'))+'</p></div>':'<div class="sd-refinement-step-status"><span>'+(quoteForShot?'报价已就绪，确认后才扣点':'等待报价')+'</span></div>';
    var blocking=blockedByOther?'<div class="sd-check warning"><b>另一个镜头正在生成</b><p>请等待当前任务结束后再提交本镜头，避免重复建单。</p></div>':'';
    return '<section class="sd-refinement-redo-generation" data-refinement-redo-generation data-shot-key="'+escapeHtml(shotKey)+'"><header><div><span>重新生成当前镜头</span><h4>按顺序完成下面四步</h4><p>所有操作只影响镜头 #'+Number(shot.sort_order||0)+'，原镜头与整片预览会一直保留。</p></div><em>预检、报价不扣点</em></header>'+binding+continuity+blocking+
      '<div class="sd-refinement-redo-steps">'+
        '<article data-provider-step="1"><span>1</span><div><b>修改提示词</b><small>调整画面、动作、运镜、角色和连续性要求。</small></div><button type="button" data-action="edit-shot-execution" data-shot-key="'+escapeHtml(shotKey)+'"'+(canEdit&&!active?'':' disabled')+'>修改提示词与生成要求</button>'+stepOneStatus+'</article>'+
        '<article data-provider-step="2"><span>2</span><div><b>免费检查参数</b><small>检查角色绑定、场景、时长和生成请求，不扣点。</small></div><button type="button" data-action="provider-preflight" data-shot-key="'+escapeHtml(shotKey)+'"'+(readyForPreflight?'':' disabled')+'>免费检查当前镜头</button>'+stepTwoStatus+'</article>'+
        '<article data-provider-step="3"><span>3</span><div><b>获取报价</b><small>检查通过后获取本次生成费用，报价阶段不扣点。</small></div><button type="button" data-action="provider-quote" data-shot-key="'+escapeHtml(shotKey)+'"'+(readyForQuote?'':' disabled')+'>获取付费报价</button>'+stepThreeStatus+'</article>'+
        '<article data-provider-step="4"><span>4</span><div><b>确认重新生成</b><small>确认费用后才会扣点，并只生成当前问题镜头。</small></div><button type="button" data-action="provider-start" data-shot-key="'+escapeHtml(shotKey)+'"'+(readyForStart?'':' disabled')+'>'+(quoteForShot?'确认扣 '+Number(quote.cost||0)+' 点并重新生成':'确认并重新生成')+'</button>'+stepFourStatus+'</article>'+
      '</div>'+providerFailureRecoveryHtml(jobForShot,{shot:shot,providerShot:providerShot,providerCharacters:characters,execution:(autodraft.provider_execution_overrides||{})[shotKey]||{}})+'</section>';
  }
  function refinementRedoSummaryHtml(refinement,autodraft,selectedShotKey){
    var current=refinement&&refinement.current_refinement,issues=refinementIssueGroups(current).shots;
    if(!issues.length)return '<section class="sd-refinement-redo-summary"><span class="sd-stage-label">PR-5 · 镜头重做</span><h2>问题镜头已处理完成</h2><p>返回合成预览后，可以统一重新合成完整视频。</p></section>';
    var projectShots=current&&Array.isArray(current.shots)?current.shots:[];
    var selectedIssue=issues.filter(function(item){return text(item.shot_key)===text(selectedShotKey);})[0]||issues[0];
    var projectShot=projectShots.filter(function(item){return text(item.shot_key)===text(selectedIssue.shot_key);})[0]||{};
    var selected=Object.assign({},projectShot,selectedIssue,{issue:Object.assign({},projectShot.issue||{},selectedIssue.issue||{})});
    var media=shotMediaIndex(autodraft||{})[text(selected.shot_key)]||{versions:[]},job=autodraft&&autodraft.provider_job;
    var issue=selected.issue&&selected.issue.message||'等待重新生成',jobForShot=job&&text(job.shot_key)===text(selected.shot_key)?job:null;
    return '<section class="sd-refinement-redo-summary"><span class="sd-stage-label">PR-5 · 镜头重做</span><h2>处理进度</h2><div class="sd-refinement-redo-summary-count"><strong>'+issues.length+'</strong><span>个问题镜头待处理</span></div><dl><dt>当前镜头</dt><dd>#'+Number(selected.sort_order||0)+' · '+escapeHtml(selected.shot_key||'')+'</dd><dt>问题</dt><dd>'+escapeHtml(issue)+'</dd><dt>候选版本</dt><dd>'+Number((media.versions||[]).length)+' 个</dd><dt>生成状态</dt><dd>'+escapeHtml(jobForShot?(jobForShot.status||'处理中'):'尚未提交')+'</dd></dl><p>提示词修改、预检、报价、重新生成和候选版本采用均在左侧完成。</p></section>';
  }
  function refinementRedoHtml(refinement,autodraft,selectedShotKey,canEdit){
    refinement=refinement||{};var current=refinement.current_refinement;
    if(!current)return '';
    var issues=refinementIssueGroups(current).shots,issueKeys=issues.map(function(item){return text(item.shot_key);});
    var shots=(current.shots||[]).filter(function(item){return issueKeys.indexOf(text(item.shot_key))>=0;});
    if(!shots.length)return '<section class="sd-refinement-redo complete"><header><div><span>PR-5 · 镜头重做</span><h2>问题镜头已全部处理</h2><p>候选镜头已采用。返回合成预览后，可一次性重新合成完整视频。</p></div></header><button type="button" data-action="exit-refinement-redo">返回合成预览</button></section>';
    var selected=shots.filter(function(item){return text(item.shot_key)===text(selectedShotKey);})[0]||shots[0];
    var media=shotMediaIndex(autodraft||{})[text(selected.shot_key)]||{},versions=media.versions||[];
    var currentMedia=versions.filter(function(item){return Number(item.version||0)===Number(selected.provider_version||0);})[0]||versions.filter(function(item){return item.selected;})[0]||null;
    var refinementJob=refinement.current_refinement_job;
    var providerActive=allProviderJobs(autodraft).some(function(providerJob){
      return providerJob&&text(providerJob.shot_key)===text(selected.shot_key)&&['billing','queued','submitting','running','submit_unknown'].indexOf(providerJob.status)>=0;
    });
    var refinementActive=refinementJob&&text(refinementJob.shot_key)===text(selected.shot_key)&&['queued','running'].indexOf(refinementJob.status)>=0;
    var keepOriginalBlocked=providerActive||refinementActive;
    var keepOriginalHint=keepOriginalBlocked?'当前重做任务执行中，不能取消':'接受当前已知问题，继续采用原视频；不会生成新视频或扣点。';
    var originalVideo=currentMedia&&currentMedia.url?'<video controls preload="metadata" src="'+escapeHtml(currentMedia.url)+'"></video>':'<div class="sd-refinement-redo-empty">当前镜头视频暂不可预览</div>';
    return '<section class="sd-refinement-redo" data-refinement-redo-workspace data-selected-shot-key="'+escapeHtml(selected.shot_key||'')+'"><header><div><span>PR-5 · 镜头重做</span><h2>镜头重做</h2><p><b>'+shots.length+' 个问题镜头待处理</b> · 只处理已标记的问题镜头，原片始终保留。</p></div><div class="sd-refinement-redo-header-actions"><button type="button" class="keep-original" data-action="keep-original-refinement-shot" data-shot-key="'+escapeHtml(selected.shot_key||'')+'" title="'+escapeHtml(keepOriginalHint)+'"'+(canEdit&&!keepOriginalBlocked?'':' disabled')+'>保留原视频并取消重做</button><small>'+escapeHtml(keepOriginalHint)+'</small><button type="button" data-action="exit-refinement-redo">返回合成预览</button></div></header><div class="sd-refinement-redo-layout"><nav aria-label="问题镜头队列"><b>问题镜头</b>'+shots.map(function(shot){var duration=Math.max(0,Math.round(Number(shot.duration_ms||shot.media_validation&&shot.media_validation.duration_ms||0)/1000));var status=shot.shot_key===selected.shot_key?'正在处理':((shot.issue&&shot.issue.status)||'待修改');return '<button type="button" class="'+(shot.shot_key===selected.shot_key?'current':'')+'" data-action="select-refinement-redo-shot" data-shot-key="'+escapeHtml(shot.shot_key||'')+'"><span>#'+Number(shot.sort_order||0)+'</span><b>'+escapeHtml(shot.issue&&shot.issue.message||'待重新生成')+'</b><small>'+(duration?duration+' 秒 · ':'')+escapeHtml(status)+'</small></button>';}).join('')+'</nav><div class="sd-refinement-redo-main"><section class="sd-refinement-redo-preview"><header><div><span>当前问题镜头</span><h3>#'+Number(selected.sort_order||0)+' · '+escapeHtml(selected.shot_key||'')+'</h3><p>'+escapeHtml(selected.issue&&selected.issue.message||'需要重新生成此镜头')+'</p></div><div class="sd-refinement-current-version"><b>当前采用版本 v'+Number(selected.provider_version||currentMedia&&currentMedia.version||0)+'</b><small>原版本保留，采用候选前不会替换</small></div></header>'+originalVideo+'</section>'+refinementRedoGenerationHtml(selected,autodraft,canEdit)+refinementShotCandidateHtml(selected,autodraft)+'</div></div></section>';
  }
  function refinementHtml(refinement,autodraft){
    refinement=refinement||{};
    var delivery=refinement.current_delivery,current=refinement.current_refinement;
    if(delivery){
      var snapshot=delivery.snapshot||{},deliverable=snapshot.deliverable===true,url=text(delivery.url).trim();
      var version=Number(delivery.version||0),projectTitle=downloadFilename(refinement.project&&refinement.project.title,'短剧');
      var deliveryResolution=text(snapshot.resolution||'1080p').toLowerCase()==='2k'?'2K':'1080p';
      var filename=projectTitle+'-v'+version+(deliverable?'-'+deliveryResolution.toLowerCase()+'.mp4':'-preview.mp4');
      var mediaActions=url?'<div class="sd-draft-media-actions"><a href="'+escapeHtml(url)+'" download="'+escapeHtml(filename)+'">'+(deliverable?'下载 '+deliveryResolution+' 成片':'下载演示预览')+'</a></div>':'<div class="sd-preflight-stale">成片文件地址缺失，请刷新后重试。</div>';
      return '<section class="sd-draft sd-delivery"><header><div><span>PR-5 · '+(deliverable?'正式交付':'开发演示')+'</span><h2>'+(deliverable?deliveryResolution+' 正式成片':'本地演示预览')+' v'+version+'</h2><p>'+(deliverable?'交付快照已固化，素材计划、精修版本和输出哈希均可追溯。':'该视频复用现有素材，仅用于本地流程验收，不是 2K 正式交付文件。')+'</p></div><em>'+(deliverable?'ready':'demo')+'</em></header><video controls preload="metadata" src="'+escapeHtml(url)+'"></video>'+mediaActions+'<div class="sd-delivery-proof"><b>'+(deliverable?'不可变交付快照':'不可交付的演示快照')+'</b><span>精修 v'+Number(snapshot.refinement_version||0)+'</span><code>'+escapeHtml(delivery.input_hash||'')+'</code></div></section>';
    }
    if(!current)return '';
    var shots=current.shots||[],groups=refinementIssueGroups(current),shotIssues=groups.shots,preparation=groups.preparation,assembly=current.assembly_status||{};
    var stagedCount=Number(assembly.staged_count||0),sourceSeconds=Math.round(Number(assembly.source_duration_ms||0)/1000);
    var reassemblyDetail=shotIssues.length?'请继续处理剩余 '+shotIssues.length+' 个问题镜头；全部满意后再统一重新合成完整视频。':'所有问题镜头都已处理，可以复用现有镜头免费重新合成完整视频。不会调用视频模型，也不会扣点。';
    if(!stagedCount&&!shotIssues.length&&sourceSeconds)reassemblyDetail='当前完整视频约 '+sourceSeconds+' 秒。'+reassemblyDetail;
    var assemblyWarning=assembly.reassembly_required?'<div class="sd-reassembly-warning"><b>'+(stagedCount?stagedCount+' 个候选镜头已采用':'预览需要重新装配')+'</b><p>'+reassemblyDetail+'</p>'+(shotIssues.length?'':'<button type="button" data-action="reassemble-refinement">重新合成完整视频</button>')+'</div>':'';
    var redoEntry=shotIssues.length?'<div class="sd-refinement-redo-entry"><div><b>'+shotIssues.length+' 个问题镜头待重做</b><p>进入独立工作区修改提示词、生成候选并逐个采用。</p></div><button type="button" data-action="enter-refinement-redo" data-shot-key="'+escapeHtml(shotIssues[0].shot_key||'')+'">进入镜头重做</button></div>':'';
    return '<section class="sd-draft sd-refinement"><header><div><span>PR-5 · 智能精修</span><h2>精修工作副本 v'+Number(current.version||0)+'</h2><p>'+(shotIssues.length?'已标记的问题镜头将在独立“镜头重做”工作区处理。':assembly.reassembly_required?'候选镜头均已采用，请重新合成完整视频。':preparation.length?'镜头均已就绪，请完成右侧验收准备。':'问题镜头已处理完，可确认精修版本。')+'</p></div><em>'+escapeHtml(current.status||'draft')+'</em></header>'+assemblyWarning+'<video controls data-refinement-player preload="metadata" src="'+escapeHtml(current.url||'')+'"></video>'+refinementShotLocatorHtml(shots,assembly,shotIssues)+redoEntry+'<div class="sd-draft-media-actions"><a href="'+escapeHtml(current.url||'')+'" download>下载预览</a></div><div class="sd-draft-summary"><strong>'+shots.length+' 个镜头</strong><strong>'+shotIssues.length+' 个待处理镜头</strong><strong>'+stagedCount+' 个已采用待合成</strong></div><h3>镜头精修</h3><div class="sd-draft-shots">'+shots.map(function(shot){var degraded=shot.status==='degraded',resolution=shot.refinement_resolution||{},keptOriginal=resolution.decision==='keep_original';var statusLabel=degraded?'待精修':keptOriginal?'已保留原片':'已就绪';var detail=shot.issue&&shot.issue.message||(keptOriginal?'已人工接受原片的已知问题：'+text(resolution.issue_message||resolution.issue_code||'未填写说明'):'该镜头已通过精修检查。');return '<article class="'+escapeHtml(shot.status||'ready')+(keptOriginal?' kept-original':'')+'"><b>#'+Number(shot.sort_order||0)+' · '+escapeHtml(shot.shot_key||'')+'</b><span>'+escapeHtml(statusLabel)+'</span><p>'+escapeHtml(detail)+'</p>'+(degraded?'<button type="button" data-action="enter-refinement-redo" data-shot-key="'+escapeHtml(shot.shot_key||'')+'">进入镜头重做</button>':'<button type="button" data-action="mark-refinement-issue" data-shot-key="'+escapeHtml(shot.shot_key||'')+'">标记这个镜头有问题</button>')+'</article>';}).join('')+'</div></section>';
  }
  function refinementActionsHtml(refinement,canEdit){
    refinement=refinement||{};
    var current=refinement.current_refinement,refineJob=refinement.current_refinement_job,deliveryJob=refinement.current_delivery_job,billing=refinement.billing||{};
    if(refinement.current_delivery){
      var delivered=refinement.current_delivery.snapshot&&refinement.current_delivery.snapshot.deliverable===true;
      var deliveredMode=refinement.media_preference&&refinement.media_preference.mode||'voice_timeline';
      var deliveredModeLabel={voice_timeline:'配音与字幕',provider_audio:'视频原生声音',silent:'完全静音'}[deliveredMode]||'配音与字幕';
      return '<section><span class="sd-stage-label">PR-5 · '+(delivered?'已交付':'开发演示')+'</span><h2>'+(delivered?'正式成片已生成':'演示预览已生成')+'</h2><p class="sd-free">'+(delivered?'当前交付快照保持不变；声音方式按该快照保留。':'仅供本地验收，不扣点、不可作为正式交付文件。')+'</p><div class="sd-media-mode-picker"><b>当前声音：'+escapeHtml(deliveredModeLabel)+'</b><p>该声音状态属于当前不可变交付快照。</p></div></section>';
    }
    if(deliveryJob&&['queued','running'].indexOf(deliveryJob.status)>=0){
      var demoJob=billing.mode==='development_free';
      return '<section data-background-job-progress="delivery"><span class="sd-stage-label">PR-5 · '+(demoJob?'开发演示':'正式导出')+'</span><h2>'+(demoJob?'正在准备免费演示预览':'正在用原生 2K 镜头合成正式成片')+'</h2><div class="sd-progress"><i style="width:'+Number(deliveryJob.progress||0)+'%"></i></div><strong>'+Number(deliveryJob.progress||0)+'%</strong><p>'+escapeHtml(deliveryJob.phase||'queued')+' · 任务可恢复，可离开页面。</p></section>';
    }
    if(deliveryJob&&deliveryJob.status==='failed'){
      var deliveryFailure=userFacingVideoMessage(deliveryJob.error&&deliveryJob.error.detail,'2K 正式导出没有完成。');
      return '<section><span class="sd-stage-label">PR-5 · 正式导出</span><h2>上次导出未完成</h2><div class="sd-preflight-stale">'+escapeHtml(deliveryFailure)+'</div><p>精修版本和镜头均已保留，可以直接重新导出，不会重新生成镜头或重复扣点。</p><button type="button" data-action="start-delivery"'+(canEdit?'':' disabled')+'>重新导出 2K 正式成片</button></section>';
    }
    if(refineJob&&['queued','running'].indexOf(refineJob.status)>=0)return '<section data-background-job-progress="refinement"><span class="sd-stage-label">PR-5 · 镜头精修</span><h2>正在重做 '+escapeHtml(refineJob.shot_key||'镜头')+'</h2><div class="sd-progress"><i style="width:'+Number(refineJob.progress||0)+'%"></i></div><strong>'+Number(refineJob.progress||0)+'%</strong></section>';
    if(refineJob&&refineJob.status==='failed'){
      var refineFailure=userFacingVideoMessage(refineJob.error&&refineJob.error.detail,'候选镜头尚未采用，生成结果和历史版本仍然保留。');
      var failedTitle=refineJob.defer_reassembly?(escapeHtml(refineJob.shot_key||'问题镜头')+' 尚未采用'):(escapeHtml(refineJob.shot_key||'问题镜头')+' 尚未替换到全片');
      var failedRetention=refineJob.defer_reassembly?'候选镜头不会丢失，也不会重复扣生成费用。请回到对应镜头重新选择并采用。':'新镜头不会丢失，也不会再次扣镜头生成费用。请回到对应镜头重试装配。';
      var failedAction=refineJob.defer_reassembly?'回到这个镜头':'回到这个镜头重试';
      return '<section><span class="sd-stage-label">PR-5 · 单镜头精修</span><h2>'+failedTitle+'</h2><div class="sd-preflight-stale">'+escapeHtml(refineFailure)+'</div><p>'+failedRetention+'</p><button type="button" data-action="jump-to-shot" data-shot-key="'+escapeHtml(refineJob.shot_key||'')+'">'+failedAction+'</button></section>';
    }
    if(!current)return '';
    var groups=refinementIssueGroups(current),issues=groups.shots,confirmed=current.status==='confirmed';
    if(confirmed){
      var confirmedAssembly=current.assembly_status||{},confirmedAssemblyBlocked=confirmedAssembly.available===false||confirmedAssembly.reassembly_required===true;
      if(confirmedAssemblyBlocked)return '<section><span class="sd-stage-label">PR-5 · 正式交付</span><h2>'+(confirmedAssembly.available===false?'完整镜头时长暂时无法核对':'请先重新装配完整预览')+'</h2><p>'+(confirmedAssembly.available===false?'当前不能安全报价或创建 2K 任务，请稍后重试。':'当前已确认版本的预览未包含全部镜头，重新装配后需再次验收。')+'</p><button type="button" disabled>正式交付不可用</button></section>';
      if(billing.delivery_enabled!==true)return '<section><span class="sd-stage-label">PR-5 · 正式交付</span><h2>真实 2K 交付暂未启用</h2><p>真实渲染执行器尚未接入，系统不会询价、建单或扣点。</p><button type="button" disabled>正式交付不可用</button><p class="sd-free">精修版本已安全保留，执行器启用后可继续。</p></section>';
      var demo=billing.mode==='development_free',localRender=billing.mode==='local_ffmpeg';
      return '<section><span class="sd-stage-label">PR-5 · '+(demo?'开发演示':'正式交付')+'</span><h2>精修版本已确认</h2><p>'+(demo?'生成一个复用现有素材的本地流程预览。':localRender?'按已锁定顺序，使用原生 2K 镜头重新合成正式成片；1080p 只是验收草稿。完成后会固化不可变交付快照。':'正式导出前会重新报价并校验确认版本。')+'</p><div class="sd-estimate"><strong>'+Number(billing.formal_cost||0)+' 点</strong><span>'+(demo?'源规格 · 不可交付':'2K · 不可变快照')+'</span></div><button type="button" data-action="start-delivery"'+(canEdit?'':' disabled')+'>'+(demo?'生成免费演示预览':localRender?'导出 2K 正式成片':'询价并生成正式成片')+'</button><p class="sd-free">'+(demo?'本地开发模式：不扣点、不可交付':localRender?'原生 2K 合成：确认后扣点，建单失败自动退款':'按报价扣点，建单失败自动退款')+'</p></section>';
    }
    var requirements=refinement.acceptance_requirements||{},hasMediaRequirement=Object.prototype.hasOwnProperty.call(requirements,'media'),media=requirements.media||{},mediaReady=media.ready===true||(!hasMediaRequirement&&!groups.preparation.length),assembly=current.assembly_status||{},assemblyBlocked=assembly.available===false||assembly.reassembly_required===true,blocked=issues.length||!mediaReady||assemblyBlocked;
    var silentMode=media.mode==='silent',providerAudioMode=media.mode==='provider_audio';
    var acceptanceChecks=[['story_continuity','剧情与镜头顺序连贯'],['character_consistency','人物形象一致且无串脸'],['audio_video_sync',silentMode?'静音模式符合预期':providerAudioMode?'镜头原声连续且音量正常':'音画同步且无异常静音'],['subtitle_timing',(silentMode||providerAudioMode)?'已确认本片无需字幕':'字幕未越界且时间正确'],['visual_integrity','无黑帧、花屏或明显生成瑕疵'],['transition_quality','转场自然并符合节奏']];
    var invalidAudioShots=(media.invalid_shot_keys||[]).map(function(value){return text(value).trim();}).filter(Boolean),invalidAudioLabel=invalidAudioShots.length?'未通过镜头：'+invalidAudioShots.join('、')+'。':'';
    var mediaPreparation=providerAudioMode&&mediaReady?'<div class="sd-media-mode-picker"><b>当前声音：视频原生声音</b><p>台词、环境声、动作音效和音乐由视频生成服务随画面生成。</p></div>':silentMode&&mediaReady?'<div class="sd-media-mode-picker"><b>当前声音：完全静音</b><p>这是历史项目保留的声音方式。</p></div>':mediaReady?'<div class="sd-media-mode-picker"><b>当前声音：历史配音与字幕</b><p>这是历史项目保留的声音方式。</p></div>':providerAudioMode?'<div class="sd-acceptance-preparation"><b>视频原生声音尚未就绪</b><p>'+escapeHtml(invalidAudioLabel)+'请检查未通过声音校验的镜头，调整“声音设计”后重新生成该镜头。</p></div>':'<div class="sd-acceptance-preparation"><b>请选择成片声音方式</b><p>三个选项均复用现有镜头；重新合成不会重新生成镜头或重复扣点。</p><div class="sd-acceptance-preparation-actions"><button type="button" data-action="go-to-voice-settings"'+(canEdit?'':' disabled')+'>配音与字幕</button><button type="button" data-action="confirm-provider-audio"'+(canEdit?'':' disabled')+'>保留镜头原声</button><button type="button" data-action="confirm-silent-media"'+(canEdit?'':' disabled')+'>完全静音</button></div></div>';
    var title=issues.length?'还有 '+issues.length+' 个问题镜头':(!mediaReady?'还有 1 项验收准备未完成':assembly.available===false?'完整镜头时长暂时无法核对':assembly.reassembly_required?'请先重新装配完整预览':'全片可以验收并锁定');
    return '<section class="sd-full-acceptance"><span class="sd-stage-label">PR-5 · 全片验收</span><h2>'+title+'</h2>'+mediaPreparation+'<p>请完整播放 1080p 草稿并检查以下项目。验收通过后将锁定镜头、音轨、字幕和素材版本，用于 2K 导出。</p><div class="sd-checks">'+acceptanceChecks.map(function(item){return '<label><input type="checkbox" data-acceptance-check="'+item[0]+'"'+(blocked?' disabled':'')+'> '+escapeHtml(item[1])+'</label>';}).join('')+'</div><button type="button" data-action="confirm-refinement" disabled>全片验收通过并锁定</button><small>'+(issues.length?'请先单独处理全部问题镜头。':!mediaReady?(providerAudioMode?'请先让全部镜头通过视频原生声音校验，再开始全片验收。':'请先完成配音/字幕准备，再开始全片验收。'):assembly.available===false?'完整镜头时长暂时无法核对，请稍后重试。':assembly.reassembly_required?'候选镜头已采用，请先重新装配并完整播放最新预览。':'勾选全部验收项后可锁定当前精修版本。')+'</small></section>';
  }
  function refinementProviderHtml(autodraft,refinement,canEdit,selectedShotKey){
    var current=refinement&&refinement.current_refinement,issues=refinementIssueGroups(current).shots;
    if(!issues.length)return '';
    var issueKeys=issues.map(function(item){return item.shot_key;});
    var poc=autodraft&&autodraft.provider_poc||{},shots=(poc.shots||[]).filter(function(item){return issueKeys.indexOf(item.shot_key)>=0;});
    var preview=autodraft&&autodraft.provider_preview,quote=autodraft&&autodraft.provider_quote,job=autodraft&&autodraft.provider_job;
    var options=shots.map(function(item){return '<option value="'+escapeHtml(item.shot_key)+'"'+(text(item.shot_key)===text(selectedShotKey)?' selected':'')+'>#'+Number(item.sort_order||0)+' · '+escapeHtml(item.scene||item.shot_key)+'</option>';}).join('');
    var active=job&&['billing','queued','submitting','running'].indexOf(job.status)>=0;
    var status=job?'<div class="sd-check '+(job.status==='succeeded'?'pass':'')+'"><b>镜头生成任务 · '+escapeHtml(job.status||'')+' · '+Number(job.progress||0)+'%</b><p>'+escapeHtml(userFacingVideoMessage(job.error&&job.error.detail,'镜头生成完成后，可点击上方“预览并重做这个镜头”重新装配全片。'))+'</p></div>':'';
    var quoteHtml=quote?'<div class="sd-estimate"><strong>'+Number(quote.cost||0)+' 点</strong><span>确认后才会扣点并提交生成任务</span></div><button data-action="provider-start" type="button"'+(canEdit&&!active?'':' disabled')+'>确认扣点并生成新镜头</button>':'';
    var previewHtml=preview&&preview.ready?'<div class="sd-check pass"><b>视频生成请求预检通过</b><p>'+escapeHtml(preview.request&&preview.request.prompt||'')+'</p></div>'+(quote?'':'<button data-action="provider-quote" type="button"'+(canEdit&&!active?'':' disabled')+'>获取付费报价</button>'):'';
    return '<section class="sd-autodraft-actions sd-refinement-provider"><span class="sd-stage-label">PR-5 · 问题镜头重新生成</span><h2>只重新生成当前问题镜头</h2><p>可以反复生成并切换多个候选版本。满意后在左侧问题镜头中点击“采用当前候选镜头”；全部问题处理完后，再统一重新合成完整视频。</p><label>问题镜头<select id="sdProviderShot"'+(shots.length&&!active?'':' disabled')+'>'+options+'</select></label><div class="sd-check" id="sdProviderShotCharacter"><b>正在读取镜头角色</b></div><button data-action="provider-preflight" type="button"'+(canEdit&&shots.length&&!active?'':' disabled')+'>免费检查当前镜头</button>'+previewHtml+quoteHtml+status+'</section>';
  }
  function shellHtml(){
    return '<div class="sd-workspace-top"><a href="short-drama.html">← 返回项目</a><div><span id="sdWorkspaceState"></span><b id="sdWorkspaceTitle"></b></div><div class="sd-workspace-top-actions"><button type="button" class="sd-inspector-button" data-action="toggle-inspector" id="sdInspectorButton" aria-expanded="true">收起摘要</button></div></div>'+
      '<div class="sd-workspace-grid" id="sdWorkspaceGrid">'+
      '<main class="sd-script" id="sdScript"></main>'+
      '<aside class="sd-inspector"><section class="sd-overview-shell"><header class="sd-overview-header"><div><span class="sd-stage-label">项目概况</span><h2 id="sdOverviewTitle">项目概要</h2></div><span class="sd-overview-phase" id="sdOverviewPhase"></span></header><div id="sdUnderstanding" class="sd-overview-content"></div></section><section class="sd-preflight-shell"><header><span class="sd-stage-label">当前步骤</span></header><div id="sdActions"></div></section><section class="sd-story-shell"><header><div><span class="sd-stage-label">故事摘要</span><h2>核心故事</h2></div></header><div id="sdStorySummary"></div></section><details class="sd-tech-details"><summary>版本与技术信息</summary><div id="sdTechnicalContract"></div><section><h3>版本历史</h3><div id="sdVersions"></div></section></details><div class="sd-workspace-notice" id="sdWorkspaceNotice" hidden></div></aside>'+
      '</div>';
  }
  function authoritativeCharacterList(scriptCharacters,projectCharacters,studioCharacters,contract){
    scriptCharacters=Array.isArray(scriptCharacters)?scriptCharacters:[];
    projectCharacters=Array.isArray(projectCharacters)?projectCharacters:[];
    studioCharacters=Array.isArray(studioCharacters)?studioCharacters:[];
    contract=Array.isArray(contract)?contract:[];
    function byKey(items,key){return items.filter(function(item){return item&&item.character_key===key;})[0]||{};}
    if(contract.length){
      return contract.filter(function(item){return item&&text(item.character_key).trim();}).map(function(item){
        var key=text(item.character_key).trim();
        return Object.assign({},byKey(scriptCharacters,key),byKey(projectCharacters,key),byKey(studioCharacters,key),item,{character_key:key});
      });
    }
    if(studioCharacters.length)return studioCharacters.slice();
    if(projectCharacters.length)return projectCharacters.slice();
    return scriptCharacters.slice();
  }
  function movieAvatarRequired(provider){
    provider=text(provider).trim().toLowerCase();
    return provider==='heygen_cinematic';
  }
  function userFacingVideoMessage(value,fallback){
    var message=text(value).trim()||text(fallback).trim();
    if(/input new_sensitive|input text sensitive|input sensitive/i.test(message)){
      return '输入内容未通过审核，请调整镜头文字或参考图后重新预检。';
    }
    return message
      .replace(/MiniMax\s+Hailuo\s*2\.3|MiniMax-Hailuo-2\.3|MiniMax[-\s]?H3|MiniMax|麦克视频|minimax(?:[_-]h3)?/gi,'视频生成服务')
      .replace(/\b(?:Grok|Seedance)\b/gi,'视频生成服务')
      .replace(/真实画面\s+Provider/g,'视频生成服务')
      .replace(/\bProvider\b/g,'生成服务');
  }
  function providerStartFailureMessage(error,quotedCost){
    if(Number(error&&error.status)===402){
      var need=Number(error&&error.need||quotedCost||0);
      return need>0?'点数不足，本次需要 '+need+' 点，请充值后再试':'点数不足，请充值后再试';
    }
    return userFacingVideoMessage(error&&error.message,'单镜头任务提交失败');
  }
  function sensitiveProviderFailure(job){
    var error=job&&job.error||{},message=(text(error.provider_message)+' '+text(error.detail)).toLowerCase();
    return text(error.provider_code)==='1026'||/new_sensitive|text sensitive|input sensitive|\u8f93\u5165\u5185\u5bb9\u672a\u901a\u8fc7\u5ba1\u6838/.test(message);
  }
  function escapeRegExp(value){return String(value||'').replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}
  function saferProviderPrompt(value){
    var prompt=text(value).trim();
    var replacements=[
      [/中国初中教室/g,'校园教室'],[/初中教室/g,'校园教室'],[/初中生/g,'学生'],
      [/瘦弱男生/g,'清瘦人物'],[/瘦弱女生/g,'清瘦人物'],[/清瘦学生/g,'清瘦人物'],
      [/未成年/g,'年轻人物'],[/小学生/g,'年轻人物'],[/中学生/g,'年轻人物'],[/学生/g,'年轻人物'],
      [/校园教室/g,'普通室内教室'],[/校园/g,'公共空间'],[/霸凌/g,'人物冲突'],[/压迫/g,'紧张氛围']
    ];
    replacements.forEach(function(item){prompt=prompt.replace(item[0],item[1]);});
    return prompt.replace(/\s{2,}/g,' ').trim();
  }
  function providerInputReview(shot,providerShot,providerCharacters,job,execution){
    shot=shot||{};providerShot=providerShot||{};providerCharacters=Array.isArray(providerCharacters)?providerCharacters:[];execution=execution||{};
    var request=job&&job.request||{},prompt=text(request.prompt||execution.provider_prompt||shot.provider_prompt).trim();
    var required=Array.isArray(execution.character_keys)&&execution.character_keys.length?execution.character_keys:(providerShot.character_keys||[]),expected=providerCharacters.filter(function(item){return required.indexOf(item.character_key)>=0;}).map(function(item){return text(item.name).trim();}).filter(Boolean);
    var unexpected=providerCharacters.filter(function(item){return required.indexOf(item.character_key)<0&&text(item.name).trim()&&prompt.indexOf(text(item.name).trim())>=0;}).map(function(item){return text(item.name).trim();});
    var candidates=['小学','初中','未成年','儿童','小男孩','小女孩','少年','少女'].filter(function(item){return prompt.indexOf(item)>=0;});
    var references=(request.reference_images||[]).map(function(item){
      var key=text(item.character_key),type=key==='__continuity_tail__'?'continuity':key==='__scene_reference__'?'scene':'character';
      return {type:type,name:text(item.name).trim()||'参考图',required:type==='character'};
    });
    return {sensitive:sensitiveProviderFailure(job),prompt:prompt,candidates:candidates,expected:expected,unexpected:unexpected,references:references};
  }
  function optimizedSensitiveExecution(shot,providerShot,saved,review){
    shot=shot||{};providerShot=providerShot||{};saved=saved||{};review=review||{};
    var execution=Object.assign({
      visual:text(shot.visual),camera:text(shot.camera),performance:'',scene:text(shot.scene),lighting:'',composition_style:'',continuity:text(shot.continuity),sound_design:text(shot.sound_design),negative_prompt:text(shot.negative_prompt),provider_prompt:'',
      character_keys:(providerShot.character_keys||[]).slice(),include_continuity_reference:true,include_scene_reference:true,prompt_semantics:'structured-supplement-v1'
    },saved);
    execution.character_keys=Array.isArray(saved.character_keys)&&saved.character_keys.length?saved.character_keys.slice():(providerShot.character_keys||[]).slice();
    ['visual','camera','performance','scene','lighting','composition_style','continuity','sound_design','provider_prompt'].forEach(function(fieldName){
      execution[fieldName]=saferProviderPrompt(syncProviderCharacterNames(execution[fieldName],review));
    });
    execution.prompt_semantics='structured-supplement-v1';
    return execution;
  }
  function syncProviderCharacterNames(value,review){
    var result=text(value),replacement=review&&review.expected&&review.expected.length===1?review.expected[0]:'';
    if(!replacement)return result;
    (review.unexpected||[]).forEach(function(name){result=result.replace(new RegExp(escapeRegExp(name),'g'),replacement);});
    return result;
  }
  function syncShotBindingPrompt(value,previousNames,nextNames){
    var result=text(value),replacement=(nextNames||[])[0]||'';
    if(!replacement)return result;
    (previousNames||[]).forEach(function(name){
      name=text(name).trim();
      if(name&&(nextNames||[]).indexOf(name)<0)result=result.replace(new RegExp(escapeRegExp(name),'g'),replacement);
    });
    return result;
  }
  function providerFailureRecoveryHtml(job,context){
    if(!job||job.status!=='failed')return '';
    context=context||{};
    var recovery=job.billing_recovery||{},billing='';
    if(recovery.refunded)billing='<p><b>本次扣点已自动退回。</b></p>';
    else if(recovery.refund_pending)billing='<p><b>本次退点正在自动处理中，刷新页面会继续恢复。</b></p>';
    var review=providerInputReview(context.shot,context.providerShot,context.providerCharacters,job,context.execution);
    if(!review.sensitive)return '<div class="sd-check warning sd-provider-recovery">'+billing+'<p>你可以修改镜头动作、参考图或生成要求，免费预检通过后再重新生成。</p></div>';
    var candidateHtml=review.candidates.length?'<div class="sd-sensitive-tags"><span>建议重点检查</span>'+review.candidates.map(function(item){return '<b>'+escapeHtml(item)+'</b>';}).join('')+'</div>':'';
    var mismatchHtml=review.unexpected.length&&review.expected.length===1?'<p class="sd-sensitive-mismatch"><b>角色名称不一致：</b>当前镜头绑定“'+escapeHtml(review.expected[0])+'”，提示词中出现“'+escapeHtml(review.unexpected.join('、'))+'”。</p>':'';
    var bindingHtml=review.expected.length&&!review.unexpected.length?'<p class="sd-sensitive-binding-ok"><b>角色绑定正常：</b>本次实际提交的是“'+escapeHtml(review.expected.join('、'))+'”及对应角色标准图；失败原因是内容审核，不需要更换绑定角色。</p>':'';
    var refs=review.references.length?'<div class="sd-sensitive-refs">'+review.references.map(function(item){return '<span class="'+item.type+'"><b>'+escapeHtml(item.name)+'</b><small>'+(item.required?'角色标准图 · 必须保留':item.type==='continuity'?'上一镜头尾帧 · 同场景同版本时必须保留':'场景参考图 · 已绑定时必须保留')+'</small></span>';}).join('')+'</div>':'';
    return '<div class="sd-sensitive-recovery">'+billing+'<header><span>输入内容未通过审核</span><b>修改后再重新生成</b><p>不是 API Key 或余额问题。本次文字或参考素材被生成服务拦截，重复提交原内容仍会失败。</p></header>'+candidateHtml+mismatchHtml+bindingHtml+refs+'<ol><li>检查并优化最终提示词</li><li>确认角色名称与绑定角色一致</li><li>场景图与同场景同版本的上一镜头尾帧属于连续性约束，不能停用</li><li>免费预检通过后，再获取报价并生成</li></ol><button type="button" data-action="optimize-and-preflight-sensitive" data-shot-key="'+escapeHtml(job.shot_key||'')+'">优化文字并免费重新预检</button><button type="button" class="secondary" data-action="edit-shot-execution" data-shot-key="'+escapeHtml(job.shot_key||'')+'">手动调整生成要求</button></div>';
  }
  function providerLabel(){
    return '视频生成服务';
  }
  function setWorkspaceBusyState(root,flag,canEdit){
    root.classList.toggle('busy',!!flag);
    root.querySelectorAll('button,textarea,input,select').forEach(function(node){
      var action=node.getAttribute('data-action');
      var readOnlyAction=action==='toggle-history'||action==='seek-refinement-shot'||action==='copy-legacy-media-recovery'||action==='download-legacy-media-recovery';
      var busyState='data-workspace-disabled-before-busy',permissionState='data-workspace-disabled-before-readonly',recomputedState='data-workspace-disabled-recomputed';
      if(flag){
        if(!node.hasAttribute(busyState))node.setAttribute(busyState,node.disabled?'true':'false');
        node.disabled=true;
        return;
      }
      if(canEdit&&node.hasAttribute(permissionState)){
        if(node.hasAttribute(recomputedState)&&node.hasAttribute(busyState))node.disabled=node.getAttribute(busyState)==='true';
        else if(node.disabled&&node.getAttribute(permissionState)==='false'&&!node.hasAttribute(recomputedState))node.disabled=false;
        node.removeAttribute(permissionState);
        node.removeAttribute(busyState);
        node.removeAttribute(recomputedState);
        return;
      }
      if(node.hasAttribute(busyState)){
        node.disabled=node.getAttribute(busyState)==='true';
        node.removeAttribute(busyState);
      }
      if(node.hasAttribute(permissionState)){
        node.disabled=true;
        node.removeAttribute(recomputedState);
        return;
      }
      if(!readOnlyAction&&!canEdit&&!!node.closest('form,section')){
        node.setAttribute(permissionState,node.disabled?'true':'false');
        node.disabled=true;
      }
      node.removeAttribute(recomputedState);
    });
  }
  function mount(doc,options){
    options=options||{};
    var projectId=text(options.projectId).trim(),client=options.client||createClient(options.fetchImpl),accountUsername='',state=normalize({}),projectDetail={characters:[]},preflight={state:'script_required',current_plan:null,versions:[]},autodraft={state:'plan_required',versions:[]},refinement=null,legacyMediaRecoveryResult=null,characterStudio=null,sceneWorkspace={graph_revision:1,scenes:[],deleted_scenes:[]},sceneImageOperations={},recoveredSceneOperations=[],pendingSceneDeleteKey='',selectedCharacterKey='',selectedShotKey='',shotEditorMode='script',selectedProviderShotKey='',providerShotErrors={},activeWorkspaceShotKey='',pollTimer=null,historyExpanded=false,inspectorExpanded=!(doc.defaultView&&doc.defaultView.innerWidth<=1050),characterNameEditing=false,characterProfileDirty=false,shotEditErrors={},characterImageOperation={character_key:'',phase:'idle',message:'',error:false,active:false};
    var inspectorStorageKey='hq-short-drama-inspector-state';
    try{
      var savedInspectorState=doc.defaultView&&doc.defaultView.localStorage&&doc.defaultView.localStorage.getItem(inspectorStorageKey);
      if(savedInspectorState==='expanded'||savedInspectorState==='collapsed')inspectorExpanded=savedInspectorState==='expanded';
    }catch(ignoreInspectorStorage){}
    var refinementIssueDraft=null,refinementIssueTrigger=null,refinementRedoMode=false,refinementRedoShotKey='';
    var root=doc.getElementById('shortDramaWorkspace');
    if(!root||!projectId)throw new Error('workspace target unavailable');
    root.innerHTML=shellHtml();root.insertAdjacentHTML('beforeend','<div class="sd-character-modal sd-shot-modal" id="sdShotModal" hidden><div class="sd-character-modal-backdrop" data-action="close-shot-editor"></div><section role="dialog" aria-modal="true" aria-labelledby="sdShotModalTitle"><header><div><span>单镜头编辑器</span><h2 id="sdShotModalTitle">编辑镜头</h2></div><button type="button" data-action="close-shot-editor" aria-label="关闭">×</button></header><div id="sdShotModalBody"></div></section></div><div class="sd-character-image-lightbox" id="sdCharacterImageLightbox" hidden><button type="button" class="sd-character-image-lightbox-backdrop" data-action="close-character-image-preview" aria-label="关闭图片预览"></button><section role="dialog" aria-modal="true" aria-labelledby="sdCharacterImagePreviewTitle"><header><div><span>图片预览</span><h2 id="sdCharacterImagePreviewTitle">大图预览</h2></div><div class="sd-character-image-lightbox-tools" aria-label="图片缩放控制"><button type="button" data-action="zoom-out-character-image" aria-label="缩小图片">−</button><output id="sdCharacterImageZoom">适应窗口</output><button type="button" data-action="zoom-in-character-image" aria-label="放大图片">＋</button><button type="button" data-action="reset-character-image" aria-label="恢复完整预览">复位</button><button type="button" data-action="close-character-image-preview" aria-label="关闭">×</button></div></header><div class="sd-character-image-lightbox-stage" id="sdCharacterImagePreviewStage"><img id="sdCharacterImagePreview" alt="" draggable="false"></div><p class="sd-character-image-lightbox-hint">滚轮缩放 · 放大后拖动查看 · 双击恢复完整图片</p></section></div>');root.hidden=false;
    root.insertAdjacentHTML('beforeend','<div class="sd-character-modal sd-refinement-issue-modal" id="sdRefinementIssueModal" hidden><div class="sd-character-modal-backdrop" data-action="close-refinement-issue"></div><section role="dialog" aria-modal="true" aria-labelledby="sdRefinementIssueTitle"><header><div><span>SHOT REVIEW</span><h2 id="sdRefinementIssueTitle">标记问题镜头</h2></div><button type="button" data-action="close-refinement-issue" aria-label="关闭">×</button></header><div id="sdRefinementIssueBody"></div></section></div>');
    var notice=doc.getElementById('sdWorkspaceNotice');
    var characterImagePreviewTrigger=null;
    var workspaceBusy=false;
    function busy(flag){workspaceBusy=!!flag;setWorkspaceBusyState(root,workspaceBusy,state.permissions.can_edit);}
    var characterImageZoom=1,characterImageOffsetX=0,characterImageOffsetY=0,characterImageDragging=false,characterImageDragX=0,characterImageDragY=0;
    function updateCharacterImagePreview(){
      var image=doc.getElementById('sdCharacterImagePreview'),label=doc.getElementById('sdCharacterImageZoom');
      if(image){image.style.transform='translate3d('+characterImageOffsetX+'px,'+characterImageOffsetY+'px,0) scale('+characterImageZoom+')';image.classList.toggle('zoomed',characterImageZoom>1);}
      if(label)label.textContent=characterImageZoom===1?'适应窗口':Math.round(characterImageZoom*100)+'%';
    }
    function resetCharacterImagePreview(){characterImageZoom=1;characterImageOffsetX=0;characterImageOffsetY=0;characterImageDragging=false;updateCharacterImagePreview();}
    function zoomCharacterImage(delta){
      characterImageZoom=Math.max(1,Math.min(4,Math.round((characterImageZoom+delta)*10)/10));
      if(characterImageZoom===1){characterImageOffsetX=0;characterImageOffsetY=0;}
      updateCharacterImagePreview();
    }
    function show(message,error){message=userFacingVideoMessage(message,'');notice.textContent=message;notice.classList.toggle('error',!!error);notice.hidden=!message;}
    function setProviderShotError(shotKey,message){
      shotKey=text(shotKey).trim();
      if(!shotKey)return;
      message=text(message).trim();
      if(message)providerShotErrors[shotKey]=message;else delete providerShotErrors[shotKey];
      renderPreservingViewport();
    }
    function setSceneImageStatus(sceneKey,patch){
      sceneImageOperations[sceneKey]=Object.assign({},sceneImageOperations[sceneKey]||{},patch||{});
      renderPreservingViewport();
    }
    function sceneImageResultUrl(result){var urls=Array.isArray(result&&result.urls)?result.urls:[];return result&&result.url||urls[0]||'';}
    function saveGeneratedSceneImage(sceneKey,promptValue,operation,jobId,result){
      var url=sceneImageResultUrl(result);
      if(!url)return Promise.reject(new Error('场景图已生成，但没有返回可用图片'));
      setSceneImageStatus(sceneKey,{active:true,phase:'saving',label:'正在保存',buttonLabel:'正在保存背景图…',message:'图片已生成，正在保存到当前场景。'});
      return client.setSceneReference({project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1),scene_key:sceneKey,source:'asset',reference_source:'ai_generation',asset_job_id:Number(jobId),asset_url:url,prompt:promptValue,filename:'AI 生成场景图'}).then(function(resultWorkspace){
        client.finishSceneImageOperation(operation);delete sceneImageOperations[sceneKey];sceneWorkspace=resultWorkspace||sceneWorkspace;renderPreservingViewport();show('场景图已生成，请预览后确认锁定',false);
      });
    }
    function watchSceneImage(sceneKey,promptValue,operation,jobId,attempts){
      attempts=Number(attempts||0)+1;
      return client.job(jobId).then(function(job){
        if(job.status==='done')return saveGeneratedSceneImage(sceneKey,promptValue,operation,jobId,job.result||{});
        if(job.status==='error'||job.status==='failed'){client.finishSceneImageOperation(operation);throw new Error(job.error||'场景图生成失败，点数将自动退回');}
        setSceneImageStatus(sceneKey,{active:true,phase:'generating',label:'生成中',buttonLabel:'背景图生成中…',message:'任务正在后台生成，可以继续处理其他场景或镜头。'});
        if(attempts>=180)throw new Error('场景图仍在后台生成，刷新页面后会继续检查任务结果');
        return new Promise(function(resolve){setTimeout(resolve,1000);}).then(function(){return watchSceneImage(sceneKey,promptValue,operation,jobId,attempts);});
      }).catch(function(error){
        sceneImageOperations[sceneKey]={active:false,phase:'failed',label:'生成失败',buttonLabel:'重新生成背景图',message:'',error:error.message||'场景图生成失败，请重试'};
        renderPreservingViewport();show(sceneImageOperations[sceneKey].error,true);
        if(Number(error&&error.status)===409)return loadSceneWorkspace();
      });
    }
    function closeRefinementIssueModal(){
      var modal=doc.getElementById('sdRefinementIssueModal');
      if(modal)modal.hidden=true;
      refinementIssueDraft=null;
      if(refinementIssueTrigger)refinementIssueTrigger.focus();
      refinementIssueTrigger=null;
    }
    function renderRefinementIssueModal(){
      var modal=doc.getElementById('sdRefinementIssueModal'),body=doc.getElementById('sdRefinementIssueBody');
      if(!modal||!body)return;
      if(!refinementIssueDraft){modal.hidden=true;return;}
      var shotKey=refinementIssueDraft.shot_key||'';
      body.innerHTML='<form id="sdRefinementIssueForm" class="sd-refinement-issue-form"><p>选择最接近的问题，可继续补充希望重新生成时怎样调整。</p><fieldset><legend>问题类型</legend>'+
        '<label><input type="radio" name="issue_type" value="background_continuity" checked><span><b>背景不连续</b><small>场景、光线或空间结构与前后镜头对不上</small></span></label>'+
        '<label><input type="radio" name="issue_type" value="character_consistency"><span><b>人物不一致</b><small>人物长相、服装、年龄或体型发生变化</small></span></label>'+
        '<label><input type="radio" name="issue_type" value="action_continuity"><span><b>动作不连贯</b><small>站位、朝向、动作或道具状态无法衔接</small></span></label>'+
        '<label><input type="radio" name="issue_type" value="visual_artifact"><span><b>画面瑕疵</b><small>变形、闪烁、黑帧、穿模或其他生成错误</small></span></label>'+
        '<label><input type="radio" name="issue_type" value="other"><span><b>其他问题</b><small>不属于以上类型的修改要求</small></span></label></fieldset>'+
        '<label class="sd-refinement-issue-notes">具体修改要求<textarea name="details" maxlength="500" placeholder="例如：保持上一镜头的黄昏街道和暖色光线，人物站位不要改变"></textarea><small>可选，填写得越具体，重新生成越接近预期。</small></label>'+
        '<footer><span>当前镜头：'+escapeHtml(shotKey)+'</span><button type="button" class="secondary" data-action="close-refinement-issue">取消</button><button type="submit">确认标记</button></footer></form>';
      modal.hidden=false;
      var first=body.querySelector('input[name="issue_type"]');if(first)first.focus();
    }
    function characterImageOperationFor(characterKey){
      var character=studioCharacter(characterKey);
      return characterImageOperationState(character||{character_key:characterKey},characterImageOperation);
    }
    function characterImageButtonLabel(operation){
      if(operation.phase==='saving')return '正在保存视觉设定…';
      if(operation.phase==='submitting')return '正在提交生成任务…';
      if(operation.phase==='generating')return '角色形象生成中…';
      if(operation.phase==='pending')return '检查生成结果';
      if(operation.phase==='stale')return '按最新资料重新生成';
      if(operation.phase==='success')return '重新生成角色形象图';
      return '生成角色形象图（按规则扣点）';
    }
    function setCharacterImageOperation(characterKey,phase,message,error,active){
      characterImageOperation={character_key:characterKey,phase:phase||'idle',message:message||'',error:!!error,active:!!active};
      var status=doc.getElementById('sdCharacterImageStatus');
      if(status){
        status.className='sd-character-image-status '+(characterImageOperation.error?'error':phase==='success'?'success':characterImageOperation.active?'active':phase==='pending'?'pending':'');
        status.innerHTML=(characterImageOperation.active?'<i aria-hidden="true"></i>':'')+'<span>'+escapeHtml(characterImageOperation.message)+'</span>';
        status.hidden=!characterImageOperation.message;
      }
      var button=root.querySelector('[data-action="generate-character-image"]');
      if(button){button.disabled=characterImageOperation.active;button.textContent=characterImageButtonLabel(characterImageOperation);}
    }
    function characterImageFailureMessage(error,stage){
      var detail=text(error&&error.message).trim()||'请稍后重试';
      if(Number(error&&error.status)===402)return '点数不足，无法生成角色形象：'+detail;
      if(Number(error&&error.status)===409)return '角色资料或项目版本已经变化，请刷新后再试：'+detail;
      if(Number(error&&error.status)===429)return '当前生成任务较多，请稍后再试：'+detail;
      if(Number(error&&error.status)>=500)return '角色生图服务暂时不可用：'+detail;
      return stage+'失败：'+detail;
    }
    function studioCharacter(characterKey){
      return characterStudio&&characterStudio.characters&&characterStudio.characters.filter(function(item){return item.character_key===characterKey;})[0];
    }
    function persistedCharacter(characterKey){
      return projectDetail&&projectDetail.characters&&projectDetail.characters.filter(function(item){return item.character_key===characterKey;})[0];
    }
    function confirmedCharacters(){
      var scriptCharacters=state.current_script&&state.current_script.script&&state.current_script.script.characters||[];
      var projectCharacters=projectDetail&&projectDetail.characters||[];
      var studioCharacters=characterStudio&&characterStudio.characters||[];
      var contract=state.conversation&&state.conversation.understanding&&state.conversation.understanding.import_contract&&state.conversation.understanding.import_contract.character_contract||projectDetail&&projectDetail.script_import&&projectDetail.script_import.character_contract||[];
      return authoritativeCharacterList(scriptCharacters,projectCharacters,studioCharacters,contract);
    }
    function activeVideoProvider(){
      return text(autodraft&&autodraft.production&&autodraft.production.provider&&autodraft.production.provider.selected||autodraft&&autodraft.provider_poc&&autodraft.provider_poc.provider).trim().toLowerCase();
    }
    function providerRequiresMovieAvatar(){
      return movieAvatarRequired(activeVideoProvider());
    }
    function characterNameStorageKey(characterKey){
      return 'hq-short-drama-character-name:'+projectId+':'+text(characterKey).trim();
    }
    function storedCharacterName(characterKey){
      try{return text(doc.defaultView.localStorage.getItem(characterNameStorageKey(characterKey))).trim();}catch(ignore){return '';}
    }
    function rememberCharacterName(characterKey,name){
      try{doc.defaultView.localStorage.setItem(characterNameStorageKey(characterKey),text(name).trim());}catch(ignore){}
    }
    function forgetStoredCharacterName(characterKey){
      try{doc.defaultView.localStorage.removeItem(characterNameStorageKey(characterKey));}catch(ignore){}
    }
    function applyStoredCharacterNames(studio){
      if(!studio||!Array.isArray(studio.characters))return studio;
      studio.characters.forEach(function(character){
        var localName=storedCharacterName(character.character_key);
        if(localName)character.name=localName;
      });
      return studio;
    }
    function currentShot(shotKey){
      var script=state.current_script&&state.current_script.script;
      return script&&(script.shots||[]).filter(function(item){return item.shot_key===shotKey;})[0];
    }
    function currentShotLines(shot){
      var script=state.current_script&&state.current_script.script;
      var lineById={};
      (script&&script.dialogue_lines||[]).forEach(function(item){lineById[text(item.id)]=item;});
      return (shot&&shot.dialogue_line_ids||[]).map(function(lineId){return lineById[text(lineId)];}).filter(Boolean);
    }
    function sceneIdentityByKey(sceneKey){
      var scene=((sceneWorkspace&&sceneWorkspace.scenes)||[]).filter(function(item){return text(item&&item.scene_key)===text(sceneKey);})[0]||{};
      return {scene_key:text(scene.scene_key).trim(),reference_identity:text(scene.preview&&scene.preview.reference_identity).trim()};
    }
    function previousShotReferenceContext(providerShot){
      var previousShotKey=text(providerShot&&providerShot.previous_shot_key).trim();
      if(!previousShotKey)return {identity:{},tail_ready:false};
      var versions=(autodraft.provider_versions||[]).filter(function(item){return text(item&&item.shot_key)===previousShotKey;}).slice();
      versions.sort(function(left,right){
        if(!!left.selected!==!!right.selected)return left.selected?-1:1;
        return Number(right.version||0)-Number(left.version||0);
      });
      var version=versions[0]||null,snapshot=version&&version.request_snapshot||{},identity=snapshot.scene_reference||null;
      return {identity:identity||{},tail_ready:!!version};
    }
    function shotExecutionReferenceState(form){
      var selectedCharacters=Array.prototype.slice.call(form.querySelectorAll('input[name="character_keys"]:checked'));
      var selectedScene=form.querySelector('input[name="scene_key"]:checked');
      var currentIdentity=effectiveSceneReferenceIdentity(
        {scene_key:text(selectedScene&&selectedScene.value).trim(),reference_identity:text(selectedScene&&selectedScene.getAttribute('data-scene-reference-identity')).trim()},
        {scene_key:text(form.getAttribute('data-default-scene-key')).trim(),reference_identity:text(form.getAttribute('data-default-scene-reference-identity')).trim()}
      );
      var previousIdentity={scene_key:text(form.getAttribute('data-previous-scene-key')).trim(),reference_identity:text(form.getAttribute('data-previous-scene-reference-identity')).trim()};
      var policy=shotReferenceSelectionPolicy(currentIdentity,previousIdentity,form.getAttribute('data-previous-tail-ready')==='1');
      return {characters:selectedCharacters,current_identity:currentIdentity,policy:policy,selected_count:selectedCharacters.length+(currentIdentity.scene_key?1:0)};
    }
    function refreshShotReferenceSelection(form){
      if(!form)return null;
      var state=shotExecutionReferenceState(form),overLimit=state.selected_count>state.policy.selected_reference_limit;
      Array.prototype.slice.call(form.querySelectorAll('input[name="character_keys"]')).forEach(function(field){
        var ready=field.getAttribute('data-reference-ready')==='1';
        field.disabled=!ready||(!field.checked&&state.characters.length>=state.policy.character_limit);
        var card=field.closest('.sd-shot-character-option');if(card)card.classList.toggle('selected',field.checked);
      });
      Array.prototype.slice.call(form.querySelectorAll('input[name="scene_key"]')).forEach(function(field){var card=field.closest('.sd-shot-scene-option');if(card)card.classList.toggle('selected',field.checked);});
      var status=form.querySelector('#sdShotCharacterBindingStatus');
      if(status){
        status.textContent=!state.characters.length?'请至少选择一个已设置标准图的角色。':state.policy.tail_required?'人物与场景已选择 '+state.selected_count+'/4 张；系统会强制追加上一镜头尾帧（总计最多 5 张）。':'人物与场景已选择 '+state.selected_count+'/5 张；场景参考图不同，本镜头不会提交上一镜头尾帧。';
        status.classList.toggle('error',!state.characters.length||overLimit);
      }
      return state;
    }
    function readShotDraft(shotKey){
      discardLegacyShotDraft(doc.defaultView.localStorage,projectId,shotKey);
      try{return JSON.parse(doc.defaultView.localStorage.getItem(shotDraftStorageKey(accountUsername,projectId,shotKey))||'null')||null;}catch(ignore){return null;}
    }
    function saveShotDraft(shotKey,values){
      discardLegacyShotDraft(doc.defaultView.localStorage,projectId,shotKey);
      try{doc.defaultView.localStorage.setItem(shotDraftStorageKey(accountUsername,projectId,shotKey),JSON.stringify(values||{}));}catch(ignore){}
    }
    function clearShotDraft(shotKey){
      discardLegacyShotDraft(doc.defaultView.localStorage,projectId,shotKey);
      try{doc.defaultView.localStorage.removeItem(shotDraftStorageKey(accountUsername,projectId,shotKey));}catch(ignore){}
      delete shotEditErrors[shotKey];
    }
    function shotFormValues(form){
      var fields=form&&form.elements||{};
      var dialogues=Array.prototype.map.call(form&&form.querySelectorAll('[data-dialogue-row]')||[],function(row){
        var field=function(name){return row.querySelector('[data-dialogue-field="'+name+'"]');};
        return {id:text(row.getAttribute('data-dialogue-id')).trim(),kind:text(field('dialogue_kind')&&field('dialogue_kind').value||'dialogue'),character_key:text(field('character_key')&&field('character_key').value).trim(),text:text(field('dialogue_text')&&field('dialogue_text').value),speech_rate:Number(field('speech_rate')&&field('speech_rate').value)||1,timing_mode:text(field('timing_mode')&&field('timing_mode').value||'sequential')};
      });
      return {purpose:text(fields.purpose&&fields.purpose.value).trim(),duration_seconds:Number(fields.duration_seconds&&fields.duration_seconds.value),scene_key:text(fields.scene_key&&fields.scene_key.value).trim(),visual:text(fields.visual&&fields.visual.value).trim(),camera:text(fields.camera&&fields.camera.value).trim(),continuity:text(fields.continuity&&fields.continuity.value).trim(),dialogues:dialogues,sound_design:text(fields.sound_design&&fields.sound_design.value).trim(),provider_prompt:text(fields.provider_prompt&&fields.provider_prompt.value).trim(),negative_prompt:text(fields.negative_prompt&&fields.negative_prompt.value).trim()};
    }
    function shotDialogueRowsHtml(dialogues){
      dialogues=Array.isArray(dialogues)?dialogues:[];
      if(!dialogues.length)return '<div class="sd-shot-dialogues-empty">当前为静默表演。需要角色说话、旁白或画面文字时，请添加内容。</div>';
      return dialogues.map(function(item,index){
        item=item||{};var kind=text(item.kind||'dialogue'),speechRate=Number(item.speech_rate)||1,timingMode=index>0&&text(item.timing_mode)==='simultaneous'?'simultaneous':'sequential';
        var characterOptions='<option value="">不指定</option>'+confirmedCharacters().map(function(character){return '<option value="'+escapeHtml(character.character_key||'')+'"'+(text(character.character_key)===text(item.character_key)?' selected':'')+'>'+escapeHtml(character.name||'角色')+'</option>';}).join('');
        return '<article class="sd-shot-dialogue-row" data-dialogue-row data-dialogue-index="'+index+'" data-dialogue-id="'+escapeHtml(item.id||'')+'"><header><b>第 '+(index+1)+' 条</b><div><button type="button" data-action="move-shot-dialogue-up"'+(index===0?' disabled':'')+'>上移</button><button type="button" data-action="move-shot-dialogue-down"'+(index===dialogues.length-1?' disabled':'')+'>下移</button><button type="button" class="danger" data-action="remove-shot-dialogue">删除</button></div></header><div class="sd-shot-dialogue-fields">'+
          '<label>内容类型<select name="dialogue_kind" data-dialogue-field="dialogue_kind"><option value="dialogue"'+(kind==='dialogue'?' selected':'')+'>人物对白</option><option value="voiceover"'+(kind==='voiceover'?' selected':'')+'>旁白</option><option value="on_screen_text"'+(kind==='on_screen_text'?' selected':'')+'>画面文字</option></select></label>'+
          '<label>说话角色<select name="character_key" data-dialogue-field="character_key">'+characterOptions+'</select></label>'+
          '<label>语速<select name="speech_rate" data-dialogue-field="speech_rate"><option value="1"'+(speechRate===1?' selected':'')+'>自然 · 1.0×</option><option value="1.15"'+(speechRate===1.15?' selected':'')+'>稍快 · 1.15×</option><option value="1.3"'+(speechRate===1.3?' selected':'')+'>快速 · 1.3×</option><option value="1.5"'+(speechRate===1.5?' selected':'')+'>很快 · 1.5×</option><option value="2"'+(speechRate===2?' selected':'')+'>极快 · 2.0×</option></select></label>'+
          '<label>说话顺序<select name="timing_mode" data-dialogue-field="timing_mode"><option value="sequential"'+(timingMode==='sequential'?' selected':'')+'>'+(index===0?'第一条开始说话':'接着上一条说')+'</option>'+(index>0?'<option value="simultaneous"'+(timingMode==='simultaneous'?' selected':'')+'>与上一条同时说</option>':'')+'</select></label>'+
          '<label class="wide">台词 / 旁白 / 画面文字<textarea name="dialogue_text" data-dialogue-field="dialogue_text" maxlength="120">'+escapeHtml(item.text||'')+'</textarea></label></div></article>';
      }).join('');
    }
    function updateShotTimingHint(form){
      if(!form)return;
      var values=shotFormValues(form),hint=form.querySelector('[data-shot-timing-hint]'),status=shotTimingStatus(values);
      if(!hint)return;
      var remaining=status.remaining_seconds;
      hint.textContent=!status.dialogue_count?'当前镜头为静默表演；添加台词后系统会合计朗读时长。':remaining>=0?'共 '+status.dialogue_count+' 条内容，预计朗读 '+status.reading_seconds.toFixed(1)+' 秒，可用 '+status.duration+' 秒，还剩 '+remaining.toFixed(1)+' 秒。':'共 '+status.dialogue_count+' 条内容，预计朗读 '+status.reading_seconds.toFixed(1)+' 秒，可用 '+status.duration+' 秒，超出 '+Math.abs(remaining).toFixed(1)+' 秒。请调整语速、台词或镜头时长。';
      hint.classList.toggle('warning',status.dialogue_count>0&&remaining<0);
      hint.classList.toggle('ready',!status.dialogue_count||remaining>=0);
    }
    function clearShotIssuePresentation(form,shotKey){
      delete shotEditErrors[shotKey];
      if(!form)return;
      var summary=form.querySelector('.sd-shot-save-summary');if(summary)summary.remove();
      Array.prototype.forEach.call(form.querySelectorAll('.sd-shot-field-error'),function(item){item.remove();});
      Array.prototype.forEach.call(form.querySelectorAll('.has-error'),function(item){item.classList.remove('has-error');});
    }
    function refreshShotTimingValidation(form,shotKey){
      updateShotTimingHint(form);
      var previous=shotEditErrors[shotKey];
      if(!previous||previous.code!=='dialogue_too_long')return;
      var issue=shotTimingIssue(shotFormValues(form));
      if(!issue||issue.code!=='dialogue_too_long'){
        clearShotIssuePresentation(form,shotKey);
        return;
      }
      shotEditErrors[shotKey]=issue;
      var summary=form.querySelector('.sd-shot-save-summary p');if(summary)summary.textContent=issue.message;
      var fieldMessage=form.querySelector('[data-dialogue-field="dialogue_text"] + .sd-shot-field-error');if(fieldMessage)fieldMessage.textContent=issue.message;
    }
    function shotFieldValue(draft,name,fallback){return draft&&Object.prototype.hasOwnProperty.call(draft,name)?draft[name]:fallback;}
    function shotEditErrorHtml(shotKey){
      var issue=shotEditErrors[shotKey];
      var title=issue&&issue.partial?'镜头内容已保存，场景绑定需要重试':'其他可用内容已保存，还有 1 处需要修改';
      return issue?'<section class="sd-shot-save-summary" role="alert"><b>'+title+'</b><p>'+escapeHtml(issue.message)+'</p></section>':'';
    }
    function backendShotIssue(error,values){
      var message=text(error&&error.message).trim()||'镜头信息未通过检查，请修改标红内容后重试。';
      if(/script_version_limit|版本数量已达上限/i.test(text(error&&error.code)+' '+message))return {message:'当前项目的历史版本已达到上限；本次编辑草稿未保存，请刷新后重试。',system:true};
      if(/台词超过镜头可用时长|dialogue_too_long/i.test(message)){
        var localIssue=shotTimingIssue(values)||{};
        return {code:'dialogue_too_long',field:'dialogue_text',dialogueIndex:localIssue.dialogueIndex,relatedField:'duration_seconds',message:localIssue.message||message};
      }
      if(/镜头时长|shot_duration|duration_seconds/i.test(message))return {field:'duration_seconds',message:message};
      if(/说话角色|speaker|character_key/i.test(message))return {field:'character_key',message:message};
      return {message:message,system:true};
    }
    function focusShotIssue(form,issue){
      if(!form||!issue)return;
      Array.prototype.forEach.call(form.querySelectorAll('.sd-shot-field-error'),function(item){item.remove();});
      Array.prototype.forEach.call(form.querySelectorAll('.has-error'),function(item){item.classList.remove('has-error');});
      [issue.field,issue.relatedField].filter(Boolean).forEach(function(name){
        var row=Number.isInteger(issue.dialogueIndex)?form.querySelectorAll('[data-dialogue-row]')[issue.dialogueIndex]:null;
        var field=row&&name!=='duration_seconds'?row.querySelector('[data-dialogue-field="'+name+'"]'):form.elements[name];if(!field)return;
        var label=field.closest('label');if(label)label.classList.add('has-error');
        var message=doc.createElement('small');message.className='sd-shot-field-error';message.textContent=name===issue.field?issue.message:'此项与上面的错误有关，请一并调整。';field.insertAdjacentElement('afterend',message);
      });
      var targetRow=Number.isInteger(issue.dialogueIndex)?form.querySelectorAll('[data-dialogue-row]')[issue.dialogueIndex]:null;
      var target=targetRow?targetRow.querySelector('[data-dialogue-field="'+issue.field+'"]'):form.elements[issue.field];if(target){target.scrollIntoView({block:'center',behavior:'smooth'});target.focus();}
    }
    function presentShotIssue(form,shotKey,issue){
      shotEditErrors[shotKey]=issue;
      if(!form)return;
      var summary=form.querySelector('.sd-shot-save-summary');
      if(!summary){summary=doc.createElement('section');summary.className='sd-shot-save-summary';summary.setAttribute('role','alert');form.insertAdjacentElement('afterbegin',summary);}
      summary.innerHTML='<b>'+(issue.partial?'镜头内容已保存，场景绑定需要重试':issue.field?'保存未完成，请修改标红内容':'保存未完成，请稍后重试')+'</b><p>'+escapeHtml(issue.message)+'</p>';
      if(issue.field)focusShotIssue(form,issue);
    }
    function renderShotModal(){
      var modal=doc.getElementById('sdShotModal'),body=doc.getElementById('sdShotModalBody'),shot=currentShot(selectedShotKey);
      if(!selectedShotKey||!shot||!state.current_script){modal.hidden=true;return;}
      var script=state.current_script.script,lines=currentShotLines(shot),locked=!!shot.locked;
      modal.hidden=false;
      doc.getElementById('sdShotModalTitle').textContent='镜头 #'+Number(shot.sort_order||0)+(locked?' · 已锁定':'');
      if(shotEditorMode==='execution'){
        var saved=(autodraft.provider_execution_overrides||{})[shot.shot_key]||{};
        var value=function(key,fallback){return escapeHtml(Object.prototype.hasOwnProperty.call(saved,key)?saved[key]:(fallback||''));};
        var providerShot=((autodraft.provider_poc&&autodraft.provider_poc.shots)||[]).filter(function(item){return item.shot_key===shot.shot_key;})[0]||{};
        var executionJob=(shotMediaIndex(autodraft)[shot.shot_key]||{}).job||null;
        var inputReview=providerInputReview(shot,providerShot,(autodraft.provider_poc&&autodraft.provider_poc.characters)||[],executionJob,saved);
        var selectedCharacterKeys=Array.isArray(saved.character_keys)?saved.character_keys.slice():(providerShot.character_keys||shot.character_keys||[]).slice();
        var availableCharacters=confirmedCharacters();
        var characterBindingOptions=availableCharacters.map(function(item){
          var providerCharacter=((autodraft.provider_poc&&autodraft.provider_poc.characters)||[]).filter(function(candidate){return candidate.character_key===item.character_key;})[0]||{};
          var image=providerCharacter.image_url||item.image_url||item.reference_url||'';
          var ready=providerCharacter.binding_ready!==false&&!!(image||providerCharacter.generation_identity_id);
          return '<label class="sd-shot-character-option '+(selectedCharacterKeys.indexOf(item.character_key)>=0?'selected ':'')+(ready?'':'unready')+'"><input type="checkbox" name="character_keys" data-reference-ready="'+(ready?'1':'0')+'" value="'+escapeHtml(item.character_key||'')+'" '+(selectedCharacterKeys.indexOf(item.character_key)>=0?'checked ':'')+(ready?'':'disabled ')+'><span>'+(image?'<img src="'+escapeHtml(image)+'" alt="">':'<i>'+escapeHtml((item.name||'?').slice(0,1))+'</i>')+'<b>'+escapeHtml(item.name||'角色')+'</b><small>'+(ready?'标准图已就绪':'请先设置标准图')+'</small></span></label>';
        }).join('');
        var characterBinding='';
        var lockedScenes=((sceneWorkspace&&sceneWorkspace.scenes)||[]).filter(function(item){return item&&item.locked&&item.preview&&(item.preview.url||item.preview.file);});
        var defaultScene=lockedScenes.filter(function(item){return (item.shots||[]).some(function(link){return link.shot_key===shot.shot_key;});})[0]||null;
        var selectedSceneKey=Object.prototype.hasOwnProperty.call(saved,'scene_key')?text(saved.scene_key):text(defaultScene&&defaultScene.scene_key);
        var defaultSceneIdentity=sceneIdentityByKey(defaultScene&&defaultScene.scene_key);
        var selectedSceneIdentity=effectiveSceneReferenceIdentity(sceneIdentityByKey(selectedSceneKey),defaultSceneIdentity),previousReferenceContext=previousShotReferenceContext(providerShot);
        var initialReferencePolicy=shotReferenceSelectionPolicy(selectedSceneIdentity,previousReferenceContext.identity,previousReferenceContext.tail_ready);
        var initialReferenceCount=selectedCharacterKeys.length+(selectedSceneIdentity.scene_key?1:0);
        var initialReferenceStatus=initialReferencePolicy.tail_required?'人物与场景已选择 '+initialReferenceCount+'/4 张；系统会强制追加上一镜头尾帧（总计最多 5 张）。':'人物与场景已选择 '+initialReferenceCount+'/5 张；场景参考图不同，本镜头不会提交上一镜头尾帧。';
        characterBinding='<fieldset class="wide sd-shot-character-binding"><legend>本镜头角色绑定</legend><p>选择下一次生成真正使用的人物及标准图。只影响当前镜头，不会修改其他镜头或覆盖旧视频。</p><div>'+characterBindingOptions+'</div><small id="sdShotCharacterBindingStatus" class="'+(initialReferenceCount>initialReferencePolicy.selected_reference_limit?'error':'')+'">'+escapeHtml(initialReferenceStatus)+'</small></fieldset>';
        var detailChips=function(field,items,current){return '<div class="sd-shot-detail-chips">'+items.map(function(item){return '<button type="button" data-action="set-shot-detail" data-detail-field="'+escapeHtml(field)+'" data-detail-value="'+escapeHtml(item.value)+'" class="'+(text(current)===text(item.value)?'active':'')+'">'+escapeHtml(item.label)+'</button>';}).join('')+'</div>';};
        var lightingValue=text(saved.lighting||'');
        var compositionValue=text(saved.composition_style||'');
        var lightingChips=detailChips('lighting',[{label:'跟随场景',value:''},{label:'清晨',value:'清晨柔光'},{label:'白天',value:'自然日光'},{label:'黄昏',value:'黄昏暖光'},{label:'夜晚',value:'夜景灯光'},{label:'阴雨',value:'阴雨氛围'}],lightingValue);
        var compositionChips=detailChips('composition_style',[{label:'系统判断',value:''},{label:'近景',value:'近景构图'},{label:'中景',value:'中景构图'},{label:'全景',value:'全景构图'},{label:'固定镜头',value:'固定机位'},{label:'缓慢推进',value:'镜头缓慢推进'},{label:'跟拍',value:'跟随人物运动拍摄'},{label:'手持感',value:'轻微手持纪实感'}],compositionValue);
        var sceneBindingOptions=lockedScenes.map(function(item){
          var preview=item.preview||{},image=preview.url||(preview.file?'/api/gen/file/'+String(preview.file).replace(/^\/+/, ''):''),selected=selectedSceneKey===text(item.scene_key);
          return '<label class="sd-shot-scene-option '+(selected?'selected':'')+'"><input type="radio" name="scene_key" data-scene-reference-identity="'+escapeHtml(preview.reference_identity||'')+'" value="'+escapeHtml(item.scene_key||'')+'" '+(selected?'checked':'')+'><span>'+(image?'<button type="button" data-action="preview-character-image" data-image-url="'+escapeHtml(image)+'" data-image-title="'+escapeHtml((item.name||'场景')+' · 场景预览')+'"><img src="'+escapeHtml(image)+'" alt=""></button>':'<i>景</i>')+'<b>'+escapeHtml(item.name||'锁定场景')+'</b><small>已锁定 · '+(item.shots||[]).length+' 个关联镜头</small></span></label>';
        }).join('');
        var sceneBinding='<fieldset class="wide sd-shot-scene-binding"><legend>本镜头场景绑定</legend><p>选择上方已锁定的场景图。生成时会同时带入场景图、场景描述和角色标准图；修改只影响下一次生成。</p><div>'+(sceneBindingOptions||'<p class="sd-shot-scene-empty">暂无已锁定场景，请先在“场景锁定”中上传或生成并锁定场景图。</p>')+'</div>'+(sceneBindingOptions?'<label class="sd-shot-scene-none"><input type="radio" name="scene_key" value="" '+(!selectedSceneKey?'checked':'')+'>沿用故事镜头当前绑定场景</label>':'')+'</fieldset>';
        var reviewBanner=inputReview.sensitive?'<section class="sd-sensitive-editor-alert"><span>上次生成未通过内容审核</span><h4>请检查文字和参考素材后重新预检</h4><p>以下修改只影响下一次生成，不会覆盖旧视频，也不会修改已锁定剧本。</p>'+(inputReview.candidates.length?'<div>'+inputReview.candidates.map(function(item){return '<b>'+escapeHtml(item)+'</b>';}).join('')+'</div>':'')+(inputReview.unexpected.length&&inputReview.expected.length===1?'<p class="mismatch">当前绑定角色为“'+escapeHtml(inputReview.expected[0])+'”，提示词中出现“'+escapeHtml(inputReview.unexpected.join('、'))+'”。</p>':'')+(inputReview.expected.length&&!inputReview.unexpected.length?'<p class="sd-sensitive-binding-ok"><b>角色绑定正常：</b>本次使用“'+escapeHtml(inputReview.expected.join('、'))+'”及对应标准图，失败来自内容审核。</p>':'')+'</section>':'';
        var optionalTypes=inputReview.references.map(function(item){return item.type;});
        var referenceControls=inputReview.sensitive&&optionalTypes.some(function(type){return type==='continuity'||type==='scene';})?'<fieldset class="wide sd-reference-controls"><legend>本次参考素材</legend><p>角色标准图和锁定场景图必须保留；只有当前镜头与上一镜头使用同一场景、同一版本时，系统才会强制追加上一镜头尾帧。</p></fieldset>':'';
        var confirmedDialogue=confirmedShotDialogueHtml(lines);
        var promptTools='<div class="sd-final-prompt-tools"><button type="button" data-action="optimize-sensitive-prompt">优化敏感表达</button>'+(inputReview.unexpected.length&&inputReview.expected.length===1?'<button type="button" data-action="sync-shot-character-names">统一为绑定角色“'+escapeHtml(inputReview.expected[0])+'”</button>':'')+'<span id="sdPromptAssistStatus"></span></div>';
        body.innerHTML='<form id="sdShotExecutionEditor" class="sd-shot-editor sd-shot-execution-editor" data-default-scene-key="'+escapeHtml(defaultSceneIdentity.scene_key||'')+'" data-default-scene-reference-identity="'+escapeHtml(defaultSceneIdentity.reference_identity||'')+'" data-previous-scene-key="'+escapeHtml(previousReferenceContext.identity.scene_key||'')+'" data-previous-scene-reference-identity="'+escapeHtml(previousReferenceContext.identity.reference_identity||'')+'" data-previous-tail-ready="'+(previousReferenceContext.tail_ready?'1':'0')+'">'+reviewBanner+'<header class="sd-execution-intro"><span>仅影响下一次视频生成</span><h3>调整镜头执行要求</h3><p>这里的修改不会改动已确认剧本。角色标准图和已锁定场景仍会自动带入。</p></header><div class="sd-shot-editor-grid">'+characterBinding+sceneBinding+
          '<label class="wide">画面与人物动作<textarea name="visual" maxlength="600" placeholder="人物做什么、物体如何变化、画面最终呈现什么">'+value('visual',shot.visual)+'</textarea></label>'+
          '<label>景别与运镜<textarea name="camera" maxlength="300" placeholder="例如：中近景，缓慢推近，稳定镜头">'+value('camera',shot.camera)+'</textarea></label>'+
          '<label>表演与情绪<textarea name="performance" maxlength="300" placeholder="例如：先得意，发现同伴后略显犹豫">'+value('performance','')+'</textarea></label>'+
          '<details class="wide sd-execution-advanced"><summary><span>镜头细节（可选）</span><small>不设置时自动跟随锁定场景和上一镜头</small></summary><div class="sd-shot-editor-grid">'+
          '<details class="wide sd-scene-supplement" '+((!selectedSceneKey||text(saved.scene||shot.scene))?'open':'')+'><summary>补充当前镜头的场景细节</summary><label>仅填写当前镜头独有的区域、道具或空间变化<textarea name="scene" maxlength="160" placeholder="例如：人物移动到靠窗位置，桌上增加一本打开的书">'+value('scene',shot.scene)+'</textarea><small>锁定场景图负责整体空间一致，这里不会替换已锁定场景。</small></label></details>'+
          '<label>光线 / 时间 / 天气'+lightingChips+'<textarea name="lighting" maxlength="240" placeholder="可直接选择，也可以自行补充">'+value('lighting','')+'</textarea></label>'+
          '<label>景别 / 运镜补充'+compositionChips+'<textarea name="composition_style" maxlength="240" placeholder="可直接选择，也可以自行补充">'+value('composition_style','')+'</textarea></label>'+
          '<label class="wide sd-continuity-control">连续性（系统自动继承）<textarea name="continuity" maxlength="360" readonly>'+value('continuity',shot.continuity)+'</textarea><span><small>自动承接上一镜头的时间、服装、人物位置和关键道具。</small><button type="button" data-action="edit-shot-continuity">调整</button></span></label>'+
          '<details class="wide sd-system-guardrails"><summary>系统保护规则</summary><p>用于防止字幕、水印、人物变脸、服装突变和不符合物理规律的动作，默认不可修改。</p><textarea name="negative_prompt" maxlength="600" readonly>'+value('negative_prompt',shot.negative_prompt)+'</textarea></details></div></details>'+
          referenceControls+confirmedDialogue+'<label class="wide">声音设计<textarea name="sound_design" maxlength="600" placeholder="例如：0–2秒风声渐强；脚步踏过碎石；远处金属碰撞；结尾环境声骤停">'+value('sound_design',shot.sound_design)+'</textarea><small>由视频生成服务随画面生成台词、环境声、动作音效、音乐和声音转场。</small></label><label class="wide sd-final-prompt">补充生成要求（可选）<textarea name="provider_prompt" maxlength="1600">'+value('provider_prompt','')+'</textarea>'+promptTools+'<small>画面动作、景别运镜、表演、场景和连续性等结构化设置会始终加入最终提示词；这里的内容只作补充，不会覆盖上面的设置。</small></label>'+
          '</div><footer><button type="submit">保存并免费预检</button><button type="button" class="secondary" data-action="close-shot-editor">取消</button></footer></form>';
        refreshShotReferenceSelection(body.querySelector('#sdShotExecutionEditor'));
        return;
      }
      var draft=readShotDraft(shot.shot_key),persistedDialogues=editableShotDialogues(lines);
      var draftDialogues=draft&&Array.isArray(draft.dialogues)?draft.dialogues:persistedDialogues;
      if(draft&&!Array.isArray(draft.dialogues)&&draft.dialogue_kind&&draft.dialogue_kind!=='silence')draftDialogues=[{kind:draft.dialogue_kind,character_key:draft.character_key||'',text:draft.dialogue_text||'',speech_rate:Number(draft.speech_rate)||1}];
      var shotSceneBinding=((sceneWorkspace&&sceneWorkspace.scenes)||[]).filter(Boolean);
      var boundShotScene=shotSceneBinding.filter(function(item){return (item.shots||[]).some(function(link){return text(link.shot_key)===text(shot.shot_key);});})[0]||null;
      var draftSceneKey=text(shotFieldValue(draft,'scene_key',boundShotScene&&boundShotScene.scene_key));
      var shotSceneOptions='<option value=""'+(!draftSceneKey?' selected':'')+'>不绑定故事场景</option>'+shotSceneBinding.map(function(item){return '<option value="'+escapeHtml(item.scene_key||'')+'"'+(draftSceneKey===text(item.scene_key)?' selected':'')+'>'+escapeHtml(item.name||'未命名场景')+'（'+(item.locked?'已锁定':'未设置场景图')+'）</option>';}).join('');
      body.innerHTML='<form id="sdShotEditor" class="sd-shot-editor">'+shotEditErrorHtml(shot.shot_key)+'<div class="sd-shot-editor-grid">'+
        '<label>剧情任务<textarea name="purpose" required maxlength="160">'+escapeHtml(shotFieldValue(draft,'purpose',shot.purpose||''))+'</textarea></label>'+
        '<label>镜头时长（秒）<input type="number" name="duration_seconds" min="4" max="15" value="'+Number(shotFieldValue(draft,'duration_seconds',shot.duration_seconds||4))+'" required><small>视频生成支持 4–15 秒；台词与时长不匹配时会保留草稿，其他有效内容仍会保存。</small></label>'+
        '<label class="sd-shot-scene-select">绑定故事场景<select name="scene_key">'+shotSceneOptions+'</select><small>'+(shotSceneBinding.length?'已锁定场景会在下次生成时携带场景图；未设置场景图的选项先保存场景关联；选择“不绑定”则只使用镜头文字。':'暂无故事场景，可先保存镜头，再到“场景锁定”中添加。')+'</small></label>'+
        '<label class="wide">具体画面与动作<textarea name="visual" required maxlength="360">'+escapeHtml(shotFieldValue(draft,'visual',shot.visual||''))+'</textarea></label>'+
        '<label class="wide">机位与运镜<textarea name="camera" maxlength="300">'+escapeHtml(shotFieldValue(draft,'camera',shot.camera||''))+'</textarea></label>'+
        '<label class="wide">连续性要求<textarea name="continuity" maxlength="360">'+escapeHtml(shotFieldValue(draft,'continuity',shot.continuity||''))+'</textarea></label>'+
        '<fieldset class="wide sd-shot-dialogues"><legend><span>台词、旁白与画面文字</span><button type="button" data-action="add-shot-dialogue"'+(draftDialogues.length>=6?' disabled':'')+'>＋ 添加一条</button></legend><p>最多 6 条；默认按列表顺序说话，也可将后续台词设为“与上一条同时说”。重复台词允许保存，同时组按最长一条计算时长。</p><div data-dialogue-list>'+shotDialogueRowsHtml(draftDialogues)+'</div><small class="sd-shot-timing-hint" data-shot-timing-hint>系统会按顺序组与同时组计算全部台词时长。</small></fieldset>'+
        '<label class="wide">声音设计<textarea name="sound_design" maxlength="600" placeholder="例如：0–2秒客厅安静环境声；拿起物品时加入摩擦声；争抢时音乐加速；结尾骤停并淡出">'+escapeHtml(shotFieldValue(draft,'sound_design',shot.sound_design||''))+'</textarea><small>可直接填写环境声、动作音效、音乐、声音转场及出现时间；不计入台词朗读时长。</small></label>'+
        '<label class="wide">生成提示词<textarea name="provider_prompt" required maxlength="1200">'+escapeHtml(shotFieldValue(draft,'provider_prompt',shot.provider_prompt||''))+'</textarea></label>'+
        '<label class="wide">禁止项<textarea name="negative_prompt" maxlength="500">'+escapeHtml(shotFieldValue(draft,'negative_prompt',shot.negative_prompt||''))+'</textarea></label>'+
        '</div><footer>'+(locked?'<p>当前镜头已锁定；解锁后才能编辑或重新生成。</p>':'<button type="submit">保存为新剧本版本</button>')+'<button type="button" class="secondary" data-action="close-shot-editor">取消</button></footer></form>';
      updateShotTimingHint(body.querySelector('#sdShotEditor'));
    }
    function renderCharacterCards(){
      var list=doc.querySelector('.sd-character-list');
      if(!list||!state.current_script)return;
      var scriptLocked=state.conversation.state==='script_locked';
      var items=confirmedCharacters();
      var needsAvatar=providerRequiresMovieAvatar();
      var avatars=characterStudio&&characterStudio.avatars||[];
      var canCreate=!!(characterStudio&&characterStudio.permissions&&characterStudio.permissions.can_create_avatar);
      list.innerHTML=items.map(function(item){
        var prepared=studioCharacter(item.character_key),persisted=persistedCharacter(item.character_key);
        var image=prepared&&prepared.image_url||persisted&&(persisted.reference_url||persisted.image_url)||'';
        var status=needsAvatar&&prepared&&prepared.binding_ready?'已绑定电影化身':persisted&&persisted.reference_locked&&image?'标准图已锁定':image?'已有角色形象':scriptLocked?'正在加载':'尚未设置标准图';
        var identity=(prepared&&prepared.identity_text)||(persisted&&persisted.identity_text)||item.identity||'';
        var identityParts=text(identity).split(/[；;,]/).map(function(part){return part.trim();}).filter(Boolean);
        var roleCode=text(identityParts[0]).toLowerCase(),roleNames={main:'主要角色',lead:'主要角色',support:'次要角色',secondary:'次要角色',extra:'群演'};
        var roleLabel=roleNames[roleCode]||'故事角色';
        if(roleNames[roleCode])identityParts.shift();
        var identityDetail=identityParts.join(' · ')||'角色资料已确认';
        var personality=(prepared&&prepared.personality)||(persisted&&persisted.personality)||item.personality||'以已确认剧本中的动作、表情和台词为准';
        var avatarOptions=avatars.map(function(avatar){return '<option value="'+escapeHtml(avatar.id||'')+'">'+escapeHtml(avatar.name||'未命名电影化身')+'</option>';}).join('');
        var avatarControls='';
        if(needsAvatar&&prepared&&!prepared.binding_ready){
          avatarControls='<div class="sd-character-inline-actions" data-character-key="'+escapeHtml(item.character_key||'')+'"><small>当前视频服务需要电影化身</small>'+
            (avatarOptions?'<select aria-label="选择电影化身"><option value="">选择已有电影化身</option>'+avatarOptions+'</select><button type="button" data-action="bind-card-avatar" data-character-key="'+escapeHtml(item.character_key||'')+'">绑定</button>':'')+
            (canCreate&&image?'<button type="button" class="secondary" data-action="create-character-avatar" data-character-key="'+escapeHtml(item.character_key||'')+'">根据标准图创建并绑定</button>':'')+'</div>';
        }
        return '<article class="sd-character-card" data-character-key="'+escapeHtml(item.character_key||'')+'">'+
          (image?'<button type="button" class="sd-character-card-image" data-action="preview-character-image" data-image-url="'+escapeHtml(image)+'" data-image-title="'+escapeHtml(item.name||'角色')+'"><img src="'+escapeHtml(image)+'" alt="'+escapeHtml(item.name||'角色')+' 标准图"><span>点击预览</span></button>':'<div class="sd-character-card-image empty"><i>'+escapeHtml((item.name||'?').slice(0,1))+'</i></div>')+
          '<div class="sd-character-card-copy"><header><div><b>'+escapeHtml(item.name||'未命名角色')+'</b><span>'+escapeHtml(roleLabel)+'</span></div><em class="'+(status.indexOf('锁定')>=0?'locked':'')+'">'+escapeHtml(status)+'</em></header><p>'+escapeHtml(identityDetail)+'</p><small>'+escapeHtml(personality)+'</small></div>'+avatarControls+'</article>';
      }).join('');
    }
    function renderCharacterModal(){
      var modal=doc.getElementById('sdCharacterModal'),body=doc.getElementById('sdCharacterModalBody'),character=studioCharacter(selectedCharacterKey);
      if(!modal||!body)return;
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
      modal.hidden=false;
      var title=doc.getElementById('sdCharacterModalTitle'),keyLabel=doc.getElementById('sdCharacterKeyLabel');
      title.textContent=character.name||'准备角色';
      keyLabel.textContent='已继承创建前确认的角色资料与标准图 · 角色标识：'+character.character_key;
      var avatars=(characterStudio&&characterStudio.avatars)||[];
      var selectedAvatar=avatars.filter(function(avatar){return String(character.avatar_id||'')===String(avatar.id);})[0];
      var affected=(character.affected_shots||[]).map(function(item){return '<span>#'+Number(item.sort_order||0)+'</span>';}).join('')||'<em>暂无关联镜头</em>';
      var referenceImage=character.reference_image_url||(!character.avatar_id?character.image_url:'');
      var canCreateAvatar=!!(characterStudio&&characterStudio.permissions&&characterStudio.permissions.can_create_avatar);
      var portrait=character.image_url?'<img src="'+escapeHtml(character.image_url)+'" alt="'+escapeHtml(character.name)+'">':'<i>'+escapeHtml((character.name||'?').slice(0,1))+'</i>';
      var selectedPreview=selectedAvatar&&selectedAvatar.image_url?'<img src="'+escapeHtml(selectedAvatar.image_url)+'" alt="'+escapeHtml(selectedAvatar.name)+'">':portrait;
      body.innerHTML='<div class="sd-character-workspace"><section class="sd-character-profile-panel">'+
        '<div class="sd-character-overview"><div class="sd-character-preview">'+portrait+'</div><div><span class="sd-section-kicker">已确认角色</span><h3>'+escapeHtml(character.name)+'</h3><p>角色资料和标准图已从创建阶段继承，本页只读，不会覆盖原有设定。</p><div class="sd-affected-shots"><b>影响镜头</b>'+affected+'</div></div></div>'+
        '<section class="sd-character-readonly"><div><span>角色身份</span><p>'+escapeHtml(character.identity_text||'未填写')+'</p></div><div><span>人物性格</span><p>'+escapeHtml(character.personality||'未填写')+'</p></div><div><span>外貌特征</span><p>'+escapeHtml(character.appearance_prompt||'以已确认标准图为准')+'</p></div><div><span>服装穿着</span><p>'+escapeHtml(character.wardrobe_prompt||'以已确认标准图为准')+'</p></div></section></section>'+
        '<section class="sd-character-library"><header><div><span class="sd-section-kicker">视频制作资源</span><h3>电影化身（可选）</h3><small>仅当视频生成服务要求电影化身时才需要创建或绑定，不会修改角色资料和标准图。</small></div><button type="button" data-action="refresh-character-library">刷新</button></header><div class="sd-character-selected"><div>'+selectedPreview+'</div><span><small>当前状态</small><b>'+escapeHtml(selectedAvatar&&selectedAvatar.name|| (character.binding_ready?'已绑定电影化身':'尚未绑定电影化身'))+'</b></span></div><div class="sd-character-avatar-grid">'+avatars.map(function(avatar){return '<button type="button" class="'+(String(character.avatar_id||'')===String(avatar.id)?'selected':'')+'" data-action="bind-character-avatar" data-avatar-id="'+escapeHtml(avatar.id)+'">'+(avatar.image_url?'<img src="'+escapeHtml(avatar.image_url)+'" alt="'+escapeHtml(avatar.name)+'">':'<i>像</i>')+'<span><b>'+escapeHtml(avatar.name)+'</b><small>'+(String(character.avatar_id||'')===String(avatar.id)?'当前绑定':'绑定此化身')+'</small></span></button>';}).join('')+'</div>'+(avatars.length?'':'<p class="sd-character-empty">暂无可用电影化身。如当前视频服务需要，可使用下方按钮根据已确认标准图创建。</p>')+'</section></div>'+
        '<footer class="sd-character-actions"><div class="sd-character-action-status"><p>角色资料与标准图已继承；此处只处理可选的电影化身。</p></div><div class="sd-character-action-buttons">'+(character.avatar_id?'<button type="button" class="ghost" data-action="unbind-character-avatar">解除电影化身绑定</button>':'')+'<button type="button" class="secondary" data-action="create-character-avatar" '+(!referenceImage||!canCreateAvatar?'disabled ':'')+'title="'+escapeHtml(!canCreateAvatar?'仅项目所有者可以创建电影化身':!referenceImage?'当前角色没有已确认标准图':'使用已确认标准图创建并自动绑定')+'">'+(referenceImage?'创建电影化身并自动绑定':'缺少已确认标准图')+'</button></div></footer>';
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
          '<b>本镜头暂不能生成</b><p>'+(requiredCharacters.length?'请先确认 '+escapeHtml(requiredCharacters.filter(function(item){return !item.binding_ready;}).map(function(item){return item.name;}).join('、'))+' 的角色标准图已经锁定。':'该镜头尚未关联角色，请先检查剧本镜头配置。')+'</p>';
      }
    }
    function workspaceViewportState(){
      var scriptPane=doc.getElementById('sdScript'),inspectorPane=root.querySelector('.sd-inspector');
      var openDetails=Array.prototype.map.call(root.querySelectorAll('details[open]'),function(item){
        var all=Array.prototype.slice.call(root.querySelectorAll('details')),summary=item.querySelector('summary');
        return {index:all.indexOf(item),summary:text(summary&&summary.textContent).trim()};
      });
      return {scriptTop:scriptPane&&scriptPane.scrollTop||0,inspectorTop:inspectorPane&&inspectorPane.scrollTop||0,openDetails:openDetails};
    }
    function restoreWorkspaceViewport(snapshot){
      snapshot=snapshot||{};
      var scriptPane=doc.getElementById('sdScript'),inspectorPane=root.querySelector('.sd-inspector');
      if(scriptPane)scriptPane.scrollTop=Number(snapshot.scriptTop||0);
      if(inspectorPane)inspectorPane.scrollTop=Number(snapshot.inspectorTop||0);
      var allDetails=Array.prototype.slice.call(root.querySelectorAll('details'));
      (snapshot.openDetails||[]).forEach(function(saved){
        var target=allDetails[saved.index],summary=target&&target.querySelector('summary');
        if(target&&text(summary&&summary.textContent).trim()===saved.summary)target.open=true;
      });
    }
    function renderPreservingViewport(){
      var viewport=workspaceViewportState();
      render();
      restoreWorkspaceViewport(viewport);
    }
    function updateProviderProgressDom(job){
      job=job||{};
      var shotKey=text(job.shot_key),display=providerJobDisplay(job),progress=display.progress;
      Array.prototype.forEach.call(root.querySelectorAll('[data-provider-media-progress="'+shotKey+'"]'),function(node){
        var heading=node.querySelector('b'),track=node.querySelector('.sd-progress'),bar=track&&track.querySelector('i');
        if(heading)heading.textContent=display.heading;
        if(track)track.classList.toggle('indeterminate',display.indeterminate);
        if(bar)bar.style.width=display.indeterminate?'':progress+'%';
      });
      Array.prototype.forEach.call(root.querySelectorAll('[data-provider-job-progress="'+shotKey+'"]'),function(node){
        var heading=node.querySelector('b'),copy=node.querySelector('p');
        if(heading)heading.textContent='视频任务 · '+display.taskLabel;
        if(copy)copy.textContent=display.label+'，可继续查看或编辑其他镜头。';
      });
      var progressNode=root.querySelector('.sd-shot-progress-node[data-shot-key="'+shotKey+'"]');
      if(progressNode){
        progressNode.classList.remove('pending','failed');progressNode.classList.add('active');
        var progressLabel=progressNode.querySelector('em');if(progressLabel)progressLabel.textContent=display.shortLabel;
      }
    }
    function updateBackgroundProgressDom(kind,job){
      job=job||{};
      var node=root.querySelector('[data-background-job-progress="'+kind+'"]');
      if(!node)return;
      var progress=Math.max(0,Math.min(100,Number(job.progress||0))),bar=node.querySelector('.sd-progress i'),value=node.querySelector('strong'),copy=node.querySelector('p');
      if(bar)bar.style.width=progress+'%';
      if(value)value.textContent=progress+'%';
      if(copy)copy.textContent=text(job.phase||job.status||'处理中')+' · 可离开页面，任务会继续执行。';
    }
    function updateRefinementShotLocator(video,preferredShot){
      var locator=root.querySelector('[data-refinement-shot-locator]');
      if(!locator||!video)return;
      var shotNodes=Array.prototype.slice.call(locator.querySelectorAll('[data-action="seek-refinement-shot"]'));
      if(!shotNodes.length)return;
      var timelineTotal=Math.max(0,Number(locator.getAttribute('data-total-ms')||0));
      var videoDurationMs=Math.max(0,Number(video.duration||0)*1000);
      var currentMs=Math.max(0,Number(video.currentTime||0)*1000),currentNode=preferredShot||shotNodes[0];
      if(timelineTotal>0&&videoDurationMs>0)currentMs=currentMs*timelineTotal/videoDurationMs;
      if(!preferredShot){
        for(var index=0;index<shotNodes.length;index+=1){
          var candidate=shotNodes[index],start=Number(candidate.getAttribute('data-start-ms')||0),end=Number(candidate.getAttribute('data-end-ms')||0);
          if(currentMs>=start&&(currentMs<end||index===shotNodes.length-1)){currentNode=candidate;break;}
        }
      }
      shotNodes.forEach(function(node){
        var selected=node===currentNode;node.classList.toggle('current',selected);
        if(selected)node.setAttribute('aria-current','true');else node.removeAttribute('aria-current');
      });
      var order=currentNode.getAttribute('data-shot-order')||'',range=currentNode.getAttribute('data-shot-range')||'';
      var output=locator.querySelector('[data-refinement-current-shot]');if(output)output.textContent='#'+order+' · '+range;
      var markButton=locator.querySelector('[data-action="mark-current-refinement-shot"]');
      if(markButton)markButton.setAttribute('data-shot-key',currentNode.getAttribute('data-shot-key')||'');
      var scroll=locator.querySelector('.sd-refinement-locator-scroll');
      if(scroll&&currentNode.offsetWidth){
        var left=currentNode.offsetLeft,right=left+currentNode.offsetWidth,viewportLeft=scroll.scrollLeft,viewportRight=viewportLeft+scroll.clientWidth;
        if(left<viewportLeft||right>viewportRight)scroll.scrollLeft=Math.max(0,left-(scroll.clientWidth-currentNode.offsetWidth)/2);
      }
    }
    function bindRefinementShotLocator(){
      var video=root.querySelector('video[data-refinement-player]'),locator=root.querySelector('[data-refinement-shot-locator]');
      if(!video||!locator)return;
      var sync=function(){updateRefinementShotLocator(video,null);};
      ['loadedmetadata','durationchange','timeupdate','seeking','seeked'].forEach(function(eventName){video.addEventListener(eventName,sync);});
      sync();
    }
    function render(){
      state=normalize(state);
      var understanding=state.conversation.understanding||{};
      var conversationMode=conversationWorkspaceMode(state),locked=conversationMode.locked;
      var canEditScript=state.permissions.can_edit&&!locked;
      var canGenerateVideo=state.permissions.can_edit&&locked;
      var generationReason=!state.permissions.can_edit?'当前账号没有生成该项目视频的权限。':!locked?'请先确认并锁定当前剧本，再生成镜头视频。':'';
      doc.getElementById('sdWorkspaceTitle').textContent=state.project.title||'短剧项目';
      doc.getElementById('sdWorkspaceState').textContent={direction_review:'项目内容确认',script_review:'剧本确认',script_locked:'剧本已锁定'}[state.conversation.state]||'短剧创作';
      var confirmationMessage='';
      for(var messageIndex=state.messages.length-1;messageIndex>=0&&!confirmationMessage;messageIndex-=1){
        var quickReplies=state.messages[messageIndex]&&state.messages[messageIndex].metadata&&state.messages[messageIndex].metadata.quick_replies||[];
        confirmationMessage=quickReplies.filter(function(value){return /^确认/.test(text(value).trim());})[0]||'';
      }
      var currentShots=state.current_script&&state.current_script.script&&state.current_script.script.shots||[];
      activeWorkspaceShotKey=defaultWorkspaceShotKey(currentShots,autodraft,activeWorkspaceShotKey);
      var refinementIssues=refinement&&refinement.current_refinement?refinementIssueGroups(refinement.current_refinement).shots:[];
      if(refinementRedoMode&&refinementIssues.length){
        if(!refinementIssues.some(function(item){return text(item.shot_key)===text(refinementRedoShotKey);}))refinementRedoShotKey=text(refinementIssues[0].shot_key);
        selectedProviderShotKey=refinementRedoShotKey;
      }else if(refinementRedoMode&&!refinementIssues.length){
        refinementRedoShotKey='';
      }
      doc.getElementById('sdScript').innerHTML=(refinementRedoMode&&refinement?refinementRedoHtml(refinement,autodraft,refinementRedoShotKey,state.permissions.can_edit):refinementHtml(refinement,autodraft))||draftHtml(autodraft)||scriptHtml(state.current_script,canEditScript,autodraft,selectedProviderShotKey,canGenerateVideo,understanding,confirmationMessage,generationReason,sceneWorkspace,state.project,activeWorkspaceShotKey,sceneImageOperations,pendingSceneDeleteKey,providerShotErrors);
      bindRefinementShotLocator();
      if(!refinement&&!autodraft.current_version)renderCharacterCards();
      var phaseLabel={discovering:'项目内容待补充',recommending:'项目内容待确认',refining:'修改后待确认',import_review:'原稿内容待确认',direction_ready:'项目内容已确认'}[understanding.phase]||'项目内容待确认';
      var selected=(understanding.recommendations||[]).filter(function(item){return item.id===understanding.selected_recommendation_id;})[0];
      var selectedDirection=understanding.selected_direction||selected||{};
      var missing=(understanding.missing_fields||[]).join('、');
      doc.getElementById('sdOverviewTitle').textContent=state.project.title||'未命名短剧';
      doc.getElementById('sdOverviewPhase').innerHTML='<span class="sd-advisor-state '+escapeHtml(understanding.phase||'discovering')+'">'+escapeHtml(phaseLabel)+'</span>';
      var actualDuration=Number(understanding.duration_seconds||0),durationText=actualDuration?'预计 '+actualDuration+' 秒':durationBandLabel(state.project.target_duration);
      var overviewMeta='<div class="sd-overview-meta"><span>'+durationText+'</span><span>'+escapeHtml(understanding.ratio||state.project.ratio||'待设置')+'</span><span>'+escapeHtml(understanding.tone||state.project.visual_style||'风格待补充')+'</span></div>';
      var storyPremise=understanding.premise||state.project.synopsis||'待补充';
      var currentScriptBody=state.current_script&&state.current_script.script||{};
      var storyDetails='<div class="sd-story-full"><b>完整核心故事</b><p>'+escapeHtml(storyPremise)+'</p></div>'+storyActsHtml(currentScriptBody.acts)+(selectedDirection.title?'<p><span>故事处理方式</span><b>'+escapeHtml(selectedDirection.title)+'</b><small>'+escapeHtml(selectedDirection.summary||'')+'</small></p>':'')+((understanding.story_notes||[]).length?'<p><span>用户补充</span><b>'+escapeHtml((understanding.story_notes||[]).join('；'))+'</b></p>':'')+importContractHtml(understanding.import_contract);
      var storySummary='<p class="sd-story-copy">'+escapeHtml(storyPremise)+'</p><details class="sd-story-details"><summary>展开故事详情</summary>'+storyDetails+'</details>';
      doc.getElementById('sdUnderstanding').innerHTML=overviewMeta+(missing?'<div class="sd-overview-warning"><b>还需补充</b><span>'+escapeHtml(missing)+'</span></div>':'');
      doc.getElementById('sdStorySummary').innerHTML=storySummary;
      var storyShell=root.querySelector('.sd-story-shell');if(storyShell)storyShell.hidden=!state.current_script;
      doc.getElementById('sdTechnicalContract').innerHTML=importContractTechnicalHtml(understanding.import_contract);
      doc.getElementById('sdVersions').innerHTML=state.versions.map(function(item){return versionHtml(item,state.conversation.current_version_id);}).join('')||'<p class="sd-placeholder">暂无版本</p>';
      var grid=doc.getElementById('sdWorkspaceGrid'),inspectorButton=doc.getElementById('sdInspectorButton');
      grid.classList.toggle('refinement-redo-active',refinementRedoMode);
      grid.classList.toggle('inspector-collapsed',!inspectorExpanded);
      inspectorButton.textContent=inspectorExpanded?'收起摘要':'查看摘要';
      inspectorButton.setAttribute('aria-expanded',inspectorExpanded?'true':'false');
      var qualitySummary=storyboardQualityHtml(state.current_script&&state.current_script.script);
      doc.getElementById('sdActions').innerHTML=refinementRedoMode?'':qualitySummary+(refinement?(refinementActionsHtml(refinement,state.permissions.can_edit)+refinementProviderHtml(autodraft,refinement,state.permissions.can_edit,selectedProviderShotKey)):(autodraft.confirmed_plan?autodraftActionsHtml(autodraft,state.permissions.can_edit):preflightHtml(state.conversation,preflight,state.permissions.can_edit)))+legacyMediaRecoveryResultHtml(legacyMediaRecoveryResult);
      enhanceProviderPreflight();
      renderCharacterModal();
      renderShotModal();
      var generateButtons=root.querySelectorAll('[data-action="generate"]'),lock=root.querySelector('[data-action="lock"]');
      Array.prototype.forEach.call(generateButtons,function(generate){
        generate.disabled=locked||!state.permissions.can_edit||(!state.conversation.current_version_id&&!understanding.direction_confirmed);
      });
      if(lock)lock.disabled=locked||!state.current_script||!state.permissions.can_edit||!!(state.current_script&&state.current_script.script&&state.current_script.script.quality_gate&&state.current_script.script.quality_gate.status==='blocked');
      setWorkspaceBusyState(root,workspaceBusy,state.permissions.can_edit);
    }
    function payload(extra){return Object.assign({project_id:projectId,conversation_revision:Number(state.conversation.revision)},extra||{});}
    function apply(promise,success){
      busy(true);show('',false);
      return promise.then(function(result){state=normalize(result);render();show(success,false);return state;})
        .catch(function(error){show(error.message||'操作失败',true);if(error.status===409)return client.workspace(projectId).then(function(result){state=normalize(result);render();});throw error;})
        .finally(function(){busy(false);render();});
    }
    function sendConversationMessage(value){
      if(!conversationWorkspaceMode(state).canMessage)return Promise.resolve(state);
      value=text(value).trim();
      if(!value)return Promise.resolve(state);
      return apply(client.message(payload({message:value})),'项目内容已确认');
    }
    function confirmDirectionAndGenerate(message,instruction){
      return sendConversationMessage(message).then(function(){
        return apply(client.generate(payload({instruction:text(instruction).trim()})),'第一版完整剧本已生成');
      });
    }
    function loadPreflight(){
      return client.preflight(projectId).then(function(result){preflight=result||{};render();return preflight;});
    }
    function loadSceneWorkspace(){
      return client.sceneWorkspace(projectId).then(function(current){
        current=current||{graph_revision:1,scenes:[]};
        return client.syncSceneGraph({project_id:projectId,graph_revision:Number(current.graph_revision||1)}).then(function(){
          return client.sceneWorkspace(projectId);
        });
      }).then(function(result){sceneWorkspace=result||{graph_revision:1,scenes:[]};render();return sceneWorkspace;})
        .catch(function(error){
          if(Number(error&&error.status)===409)return client.sceneWorkspace(projectId).then(function(result){sceneWorkspace=result||{graph_revision:1,scenes:[]};render();return sceneWorkspace;});
          throw error;
        });
    }
    function bindShotScene(shotKey,sceneKey){
      return client.sceneWorkspace(projectId).then(function(current){
        sceneWorkspace=current||{graph_revision:1,scenes:[]};
        return client.syncSceneGraph({project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1)}).catch(function(error){
          if(Number(error&&error.status)!==409)throw error;
        });
      }).then(function(){return client.sceneWorkspace(projectId);}).then(function(latest){
        sceneWorkspace=latest||sceneWorkspace;
        return client.bindSceneToShot({project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1),shot_key:shotKey,scene_key:sceneKey});
      }).then(function(result){sceneWorkspace=result||sceneWorkspace;return sceneWorkspace;});
    }
    function closeSceneDialog(){
      var host=doc.getElementById('sdSceneManager');if(host&&host.parentNode)host.parentNode.removeChild(host);
    }
    function openSceneManager(scene){
      scene=scene||null;closeSceneDialog();
      var shots=state.current_script&&state.current_script.script&&state.current_script.script.shots||[];
      var selected=(scene&&scene.shots||[]).map(function(item){return text(item.shot_key);});
      var host=doc.createElement('div');host.id='sdSceneManager';host.className='sd-scene-manager-host';
      host.innerHTML='<section class="sd-scene-manager" role="dialog" aria-modal="true" aria-label="'+(scene?'编辑场景':'添加场景')+'"><header><div><small>SCENE LIBRARY</small><h3>'+(scene?'编辑场景':'添加场景')+'</h3><p>设置场景资料并选择关联镜头；每个镜头只使用一个锁定场景。</p></div><button type="button" data-close-scene-manager aria-label="关闭">×</button></header><form id="sdSceneManagerForm"><label>场景名称<input name="name" required maxlength="120" value="'+escapeHtml(scene&&scene.name||'')+'" placeholder="例如：学校天台"></label><label>场景描述<textarea name="description" required maxlength="1200" placeholder="描述空间、陈设、时间、光线与氛围，不要写人物动作">'+escapeHtml(scene&&scene.description||'')+'</textarea></label><fieldset><legend>关联镜头 <span>可多选</span></legend><div class="sd-scene-shot-selector">'+shots.map(function(shot,index){var key=text(shot.shot_key);return '<label><input type="checkbox" name="shot_key" value="'+escapeHtml(key)+'" '+(selected.indexOf(key)>=0?'checked':'')+'><span>#'+Number(shot.sort_order||index+1)+'</span><small>'+escapeHtml(text(shot.scene||shot.scene_description||shot.visual).slice(0,50)||'未填写场景')+'</small></label>';}).join('')+'</div></fieldset><footer><button type="button" data-close-scene-manager>取消</button><button type="submit" class="primary">'+(scene?'保存场景与绑定':'创建场景')+'</button></footer></form></section>';
      root.appendChild(host);
      host.addEventListener('click',function(event){if(event.target===host||event.target.closest('[data-close-scene-manager]'))closeSceneDialog();});
      host.querySelector('form').addEventListener('submit',function(event){event.preventDefault();var form=event.currentTarget,shotKeys=Array.prototype.slice.call(form.querySelectorAll('input[name="shot_key"]:checked')).map(function(input){return input.value;});var data={project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1),name:text(form.elements.name.value).trim(),description:text(form.elements.description.value).trim(),shot_keys:shotKeys};if(scene)data.scene_key=scene.scene_key;busy(true);(scene?client.updateScene(data):client.createScene(data)).then(function(result){sceneWorkspace=result||sceneWorkspace;closeSceneDialog();render();show(scene?'场景资料与镜头绑定已更新':'场景已添加，请选择图片或使用 AI 生成背景图',false);}).catch(function(error){show(error.message||'场景保存失败',true);if(Number(error&&error.status)===409)return loadSceneWorkspace();}).finally(function(){busy(false);render();});});
      var first=host.querySelector('input[name="name"]');if(first)first.focus();
    }
    function openSceneAssetLibrary(scene){
      closeSceneDialog();var host=doc.createElement('div');host.id='sdSceneManager';host.className='sd-scene-manager-host';host.innerHTML='<section class="sd-scene-manager sd-scene-assets" role="dialog" aria-modal="true"><header><div><small>MY ASSETS</small><h3>选择场景图片</h3><p>从图片资产中选择一张场景图，确认前不会改变已锁定内容。</p></div><button type="button" data-close-scene-manager>×</button></header><div class="sd-scene-asset-grid"><p>正在加载图片资产…</p></div><footer><button type="button" data-close-scene-manager>取消</button><button type="button" class="primary" data-confirm-scene-asset disabled>确认使用所选图片</button></footer></section>';root.appendChild(host);var selected=null,grid=host.querySelector('.sd-scene-asset-grid'),confirm=host.querySelector('[data-confirm-scene-asset]');host.addEventListener('click',function(event){if(event.target===host||event.target.closest('[data-close-scene-manager]')){closeSceneDialog();return;}var item=event.target.closest('[data-scene-asset-index]');if(item){selected=item._asset;Array.prototype.forEach.call(grid.querySelectorAll('button'),function(button){button.classList.toggle('selected',button===item);});confirm.disabled=false;}});confirm.addEventListener('click',function(){if(!selected)return;busy(true);client.setSceneReference({project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1),scene_key:scene.scene_key,source:'asset',reference_source:'asset_library',asset_job_id:Number(selected.job_id),asset_url:selected.url,prompt:scene.description||'',filename:text(selected.prompt).slice(0,120)||('图片资产 #'+selected.job_id)}).then(function(result){sceneWorkspace=result||sceneWorkspace;closeSceneDialog();render();show('场景图已选择，请预览后确认锁定',false);}).catch(function(error){show(error.message||'场景图片设置失败',true);}).finally(function(){busy(false);render();});});client.listImageAssets(0,60).then(function(result){var items=(result&&result.items||[]).filter(function(item){return item&&item.url;});grid.innerHTML=items.length?'':'<p>资产库中暂无可用图片。</p>';items.forEach(function(asset){var button=doc.createElement('button');button.type='button';button.setAttribute('data-scene-asset-index','1');button.innerHTML='<img src="'+escapeHtml(asset.url)+'" alt="图片资产"><span>'+escapeHtml(text(asset.prompt).slice(0,36)||('图片资产 #'+asset.job_id))+'</span>';button._asset=asset;grid.appendChild(button);});}).catch(function(error){grid.innerHTML='<p>图片资产加载失败：'+escapeHtml(error.message||'请稍后重试')+'</p>';});
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
          }).then(function(){if(['queued','running'].indexOf(refinement.current_refinement_job&&refinement.current_refinement_job.status)>=0)updateBackgroundProgressDom('refinement',refinement.current_refinement_job);else renderPreservingViewport();schedulePoll();}).catch(function(error){show(error.message||'精修任务状态更新失败',true);});
        },900);
        return;
      }
      if(deliveryJob&&['queued','running'].indexOf(deliveryJob.status)>=0){
        pollTimer=setTimeout(function(){
          client.deliveryJob(projectId,deliveryJob.id).then(function(result){
            refinement.current_delivery_job=result;
            if(result.result)return client.refinement(projectId).then(function(workspace){refinement=workspace||null;show(refinement.billing&&refinement.billing.mode==='development_free'?'免费演示预览已生成，不可作为正式交付':'2K 正式成片已生成并固化交付快照',false);});
          }).then(function(){if(['queued','running'].indexOf(refinement.current_delivery_job&&refinement.current_delivery_job.status)>=0)updateBackgroundProgressDom('delivery',refinement.current_delivery_job);else renderPreservingViewport();schedulePoll();}).catch(function(error){show(error.message||'正式导出状态更新失败',true);});
        },900);
        return;
      }
      var providerJobs=activeProviderJobs(autodraft);
      if(providerJobs.length){
        pollTimer=setTimeout(function(){
          Promise.all(providerJobs.map(function(providerJob){return client.providerJob(projectId,providerJob.id);})).then(function(results){
            var updated={};results.forEach(function(result){updated[result.id]=result;});
            autodraft.provider_jobs=allProviderJobs(autodraft).map(function(item){return updated[item.id]||item;});
            if(autodraft.provider_job&&updated[autodraft.provider_job.id])autodraft.provider_job=updated[autodraft.provider_job.id];
            if(results.some(function(result){return result.status==='succeeded';}))return client.autodraft(projectId).then(function(workspace){
              autodraft=workspace||{};
              show('镜头已生成并保存为可复用版本',false);
            });
            var failed=results.filter(function(result){return ['failed','submit_unknown'].indexOf(result.status)>=0;})[0];
            if(failed){
              show(failed.error&&failed.error.detail||'单镜头生成失败；请查看退款或人工对账状态',true);
            }
          }).then(function(){
            var remaining=activeProviderJobs(autodraft);
            if(remaining.length===1)updateProviderProgressDom(remaining[0]);else renderPreservingViewport();
            schedulePoll();
          }).catch(function(error){
            show(error.message||'单镜头任务状态批量更新失败',true);
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
            if(autodraft.current_version)return client.refinement(projectId).then(function(next){
              refinement=next||null;
              show('1080p 全片草稿已生成，可以播放或下载。',false);
              setTimeout(function(){
                var completedDraft=root.querySelector('.sd-draft');
                if(completedDraft)completedDraft.scrollIntoView({behavior:'smooth',block:'start'});
              },60);
            });
          });
          if(['failed','canceled'].indexOf(result.status)>=0){
            show(userFacingVideoMessage(result.error&&result.error.detail,'合成任务未完成，请重新尝试。'),true);
            return client.autodraft(projectId).then(function(workspace){autodraft=workspace||{};});
          }
        }).then(function(){if(['queued','running'].indexOf(autodraft.current_job&&autodraft.current_job.status)>=0)updateBackgroundProgressDom('autodraft',autodraft.current_job);else renderPreservingViewport();schedulePoll();}).catch(function(error){show(error.message||'自动草稿状态更新失败',true);});
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
        characterStudio=applyStoredCharacterNames(result||null);
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
    function characterNameError(character,name){
      name=text(name).trim();
      if(!name||name.length>20)return '角色名称需为 1 至 20 个字符';
      var duplicate=(characterStudio&&characterStudio.characters||[]).some(function(item){
        return item.character_key!==character.character_key&&text(item.name).trim().toLocaleLowerCase()===name.toLocaleLowerCase();
      });
      return duplicate?'角色名称已被其他角色使用':'';
    }
    function saveCharacterProfile(character,form,nameOverride){
      var fields=form.elements;
      var requestedName=text(nameOverride==null?character.name:nameOverride).trim();
      var request={
        project_id:projectId,
        project_revision:Number(characterStudio.project_revision),
        character_key:character.character_key,
        name:requestedName,
        identity_text:text(fields.identity_text.value).trim(),
        personality:text(fields.personality.value).trim(),
        appearance_prompt:text(fields.appearance_prompt.value).trim(),
        wardrobe_prompt:text(fields.wardrobe_prompt.value).trim()
      };
      return client.saveCharacterProfile(request).then(function(result){
        forgetStoredCharacterName(character.character_key);
        return result;
      }).catch(function(error){
        var oldProfileContract=error&&error.code==='character_profile_invalid'&&(error.status===400||error.status===422);
        if(!oldProfileContract)throw error;
        var legacyRequest=Object.assign({},request);
        delete legacyRequest.name;
        return client.saveCharacterProfile(legacyRequest).then(function(result){
          rememberCharacterName(character.character_key,requestedName);
          result=result||{};
          result.compatibility_name_local_only=true;
          return result;
        });
      });
    }
    root.addEventListener('submit',function(event){
      if(event.target.id!=='sdRefinementIssueForm')return;
      event.preventDefault();
      var issueVersion=refinement&&refinement.current_refinement,fields=event.target.elements;
      if(!issueVersion||!refinementIssueDraft)return;
      var issueShot=refinementIssueDraft.shot_key;
      var issueType=text(fields.issue_type&&fields.issue_type.value).trim()||'other';
      var issueLabels={background_continuity:'背景不连续',character_consistency:'人物不一致',action_continuity:'动作不连贯',visual_artifact:'画面瑕疵',other:'其他问题'};
      var details=text(fields.details&&fields.details.value).trim();
      var issueMessage=issueLabels[issueType]+(details?'：'+details:'，需要重新检查并生成该镜头');
      busy(true);show('',false);
      client.markRefinementIssue({project_id:projectId,version_id:issueVersion.id,shot_key:issueShot,issue_code:'user_'+issueType,message:issueMessage})
        .then(function(){closeRefinementIssueModal();return loadRefinement();})
        .then(function(){refinementRedoMode=true;refinementRedoShotKey=issueShot;selectedProviderShotKey=issueShot;show('问题已记录，已进入镜头重做工作区',false);})
        .catch(function(error){show(error.message||'标记问题镜头失败',true);})
        .finally(function(){busy(false);render();});
    });
    root.addEventListener('change',function(event){
      if(event.target&&event.target.id==='sdProviderShot'){
        selectedProviderShotKey=event.target.value||'';
        if(refinementRedoMode)refinementRedoShotKey=selectedProviderShotKey;
        autodraft.provider_preview=null;autodraft.provider_quote=null;
        render();
        return;
      }
      if(!event.target.matches('#sdShotExecutionEditor input[name="character_keys"], #sdShotExecutionEditor input[name="scene_key"]'))return;
      refreshShotReferenceSelection(event.target.closest('#sdShotExecutionEditor'));
    });
    root.addEventListener('submit',function(event){
      if(event.target.id!=='sdCharacterProfile')return;
      event.preventDefault();
      var character=studioCharacter(selectedCharacterKey);
      if(!character)return;
      busy(true);show('',false);
      var proposedName=doc.getElementById('sdCharacterNameInput').value||character.name;
      var nameError=characterNameError(character,proposedName);
      if(nameError){busy(false);show(nameError,true);return;}
      saveCharacterProfile(character,event.target,proposedName)
        .then(function(result){
          characterNameEditing=false;characterProfileDirty=false;
          var message=result&&result.compatibility_name_local_only?'角色档案已保存；名称已在本地预览，待服务端升级后会永久保存':'角色档案已保存，相关制作计划已标记为需要重新确认';
          return refreshCharacterContext(message);
        })
        .catch(function(error){show('角色档案保存失败：'+(error.message||'服务端未接受本次修改'),true);})
        .finally(function(){busy(false);render();});
    });
    root.addEventListener('submit',function(event){
      if(event.target.id==='sdShotExecutionEditor'){
        event.preventDefault();
        var executionShot=currentShot(selectedShotKey),executionFields=event.target.elements,confirmedPlan=autodraft.confirmed_plan;
        if(!executionShot||!confirmedPlan)return;
        var providerShot=((autodraft.provider_poc&&autodraft.provider_poc.shots)||[]).filter(function(item){return item.shot_key===executionShot.shot_key;})[0]||{};
        var execution={};['visual','camera','performance','scene','lighting','composition_style','continuity','sound_design','negative_prompt','provider_prompt'].forEach(function(key){execution[key]=text(executionFields[key]&&executionFields[key].value).trim();});
        execution.character_keys=Array.prototype.slice.call(executionFields.character_keys||[]).filter(function(field){return field.checked;}).map(function(field){return field.value;});
        execution.scene_key=text(executionFields.scene_key&&executionFields.scene_key.value).trim();
        if(!execution.character_keys.length){show('请至少选择一个已设置标准图的角色',true);return;}
        var referenceState=shotExecutionReferenceState(event.target);
        if(referenceState.selected_count>referenceState.policy.selected_reference_limit){
          show(referenceState.policy.tail_required?'同一场景、同一版本必须为上一镜头尾帧保留第 5 个位置；人物标准图与场景图合计最多选择 4 张。':'人物标准图与场景图合计最多选择 5 张。',true);return;
        }
        var providerCharacters=(autodraft.provider_poc&&autodraft.provider_poc.characters)||[];
        var previousExecution=(autodraft.provider_execution_overrides||{})[executionShot.shot_key]||{};
        var previousKeys=Array.isArray(previousExecution.character_keys)&&previousExecution.character_keys.length?previousExecution.character_keys:(providerShot.character_keys||[]);
        var previousNames=previousKeys.map(function(characterKey){var item=providerCharacters.filter(function(character){return character.character_key===characterKey;})[0];return item&&item.name||'';}).filter(Boolean);
        var nextNames=execution.character_keys.map(function(characterKey){var item=providerCharacters.filter(function(character){return character.character_key===characterKey;})[0];return item&&item.name||'';}).filter(Boolean);
        ['visual','camera','performance','scene','lighting','composition_style','continuity','sound_design','negative_prompt','provider_prompt'].forEach(function(fieldName){
          execution[fieldName]=syncShotBindingPrompt(execution[fieldName],previousNames,nextNames);
        });
        execution.include_continuity_reference=true;
        execution.include_scene_reference=true;
        execution.prompt_semantics='structured-supplement-v1';
        busy(true);show('',false);
        client.providerPreflight({project_id:projectId,plan_id:confirmedPlan.id,shot_key:executionShot.shot_key,avatar_id:'',character_key:execution.character_keys[0],execution:execution}).then(function(result){
          autodraft.provider_preview=result;autodraft.provider_quote=null;
          autodraft.provider_execution_overrides=autodraft.provider_execution_overrides||{};
          autodraft.provider_execution_overrides[executionShot.shot_key]=result.execution||execution;
          selectedProviderShotKey=executionShot.shot_key;selectedShotKey='';render();show('镜头生成要求已保存并通过免费预检，请确认提示词后获取报价',false);
        }).catch(function(error){show(userFacingVideoMessage(error.message,'镜头生成要求保存失败'),true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(event.target.id!=='sdShotEditor')return;
      event.preventDefault();
      var shot=currentShot(selectedShotKey),fields=event.target.elements;
      if(!shot||!state.current_script)return;
      var form=event.target,values=shotFormValues(form);
      saveShotDraft(shot.shot_key,values);
      var issue=shotTimingIssue(values),requestedSceneKey=values.scene_key;
      var requestedScene=((sceneWorkspace&&sceneWorkspace.scenes)||[]).filter(function(item){return text(item.scene_key)===requestedSceneKey;})[0]||null;
      var changes={
        purpose:values.purpose,
        scene:text(requestedScene&&requestedScene.name||shot.scene).trim(),
        visual:values.visual,
        camera:values.camera,
        continuity:values.continuity,
        sound_design:values.sound_design,
        provider_prompt:values.provider_prompt,
        negative_prompt:values.negative_prompt
      };
      if(!issue){
        changes.duration_seconds=values.duration_seconds;
        changes.dialogues=values.dialogues;
      }
      busy(true);show('',false);
      var shotContentSaved=false;
      client.updateShot(payload({
        version_id:state.current_script.id,
        shot_key:shot.shot_key,
        changes:changes
      })).then(function(result){
        state=normalize(result);
        shotContentSaved=true;
        return bindShotScene(shot.shot_key,requestedSceneKey);
      }).then(function(){
        if(issue){
          presentShotIssue(form,shot.shot_key,issue);
          return;
        }
        clearShotDraft(shot.shot_key);selectedShotKey='';render();show('镜头与故事场景绑定已保存',false);
      }).catch(function(error){
        if(shotContentSaved){
          presentShotIssue(form,shot.shot_key,{partial:true,system:true,message:'镜头文字和其他有效信息已经保存；故事场景暂未绑定：'+(error.message||'请稍后重试')});
          return;
        }
        presentShotIssue(form,shot.shot_key,backendShotIssue(error,values));
      }).finally(function(){busy(false);});
    });
    root.addEventListener('input',function(event){
      var shotForm=event.target.closest&&event.target.closest('#sdShotEditor');
      if(shotForm&&selectedShotKey){saveShotDraft(selectedShotKey,shotFormValues(shotForm));refreshShotTimingValidation(shotForm,selectedShotKey);}
      if(event.target.closest('#sdCharacterProfile')||event.target.id==='sdCharacterNameInput'){
        characterProfileDirty=true;
        var saveState=doc.getElementById('sdCharacterSaveState');
        if(saveState){saveState.classList.add('dirty');saveState.textContent='有未保存的修改';}
      }
    });
    root.addEventListener('wheel',function(event){
      if(!event.target.closest('#sdCharacterImagePreviewStage'))return;
      event.preventDefault();zoomCharacterImage(event.deltaY<0?.2:-.2);
    },{passive:false});
    root.addEventListener('pointerdown',function(event){
      var preview=event.target.closest('#sdCharacterImagePreview');
      if(!preview||characterImageZoom<=1)return;
      characterImageDragging=true;characterImageDragX=event.clientX-characterImageOffsetX;characterImageDragY=event.clientY-characterImageOffsetY;
      preview.setPointerCapture(event.pointerId);preview.classList.add('dragging');
    });
    root.addEventListener('pointermove',function(event){
      if(!characterImageDragging)return;
      characterImageOffsetX=event.clientX-characterImageDragX;characterImageOffsetY=event.clientY-characterImageDragY;updateCharacterImagePreview();
    });
    root.addEventListener('pointerup',function(event){
      if(!characterImageDragging)return;
      characterImageDragging=false;var preview=doc.getElementById('sdCharacterImagePreview');if(preview)preview.classList.remove('dragging');
    });
    root.addEventListener('dblclick',function(event){if(event.target.closest('#sdCharacterImagePreview'))resetCharacterImagePreview();});
    root.addEventListener('keydown',function(event){
      var imageLightbox=doc.getElementById('sdCharacterImageLightbox');
      if(event.key==='Escape'&&imageLightbox&&!imageLightbox.hidden){
        event.preventDefault();
        imageLightbox.hidden=true;
        var previewImage=doc.getElementById('sdCharacterImagePreview');
        if(previewImage)previewImage.removeAttribute('src');
        resetCharacterImagePreview();
        if(characterImagePreviewTrigger)characterImagePreviewTrigger.focus();
        characterImagePreviewTrigger=null;
        return;
      }
      if(event.target.id!=='sdCharacterNameInput')return;
      if(event.key==='Escape'){
        event.preventDefault();characterNameEditing=false;renderCharacterModal();
      }else if(event.key==='Enter'){
        event.preventDefault();
        var saveButton=root.querySelector('[data-action="save-character-name"]');
        if(saveButton)saveButton.click();
      }
    });
    root.addEventListener('click',function(event){
      var action=event.target.closest('[data-action]');
      var dialogueAction=action&&action.getAttribute('data-action');
      if(['add-shot-dialogue','remove-shot-dialogue','move-shot-dialogue-up','move-shot-dialogue-down'].indexOf(dialogueAction)>=0){
        var dialogueForm=action.closest('#sdShotEditor');if(!dialogueForm||!selectedShotKey)return;
        var dialogueValues=shotFormValues(dialogueForm),dialogues=dialogueValues.dialogues.slice(),dialogueRow=action.closest('[data-dialogue-row]');
        var dialogueIndex=dialogueRow?Array.prototype.indexOf.call(dialogueForm.querySelectorAll('[data-dialogue-row]'),dialogueRow):-1;
        if(dialogueAction==='add-shot-dialogue'){
          if(dialogues.length>=6){show('每个镜头最多设置 6 条台词、旁白或画面文字。',true);return;}
          var firstCharacter=confirmedCharacters()[0]||{};
          dialogues.push({kind:'dialogue',character_key:firstCharacter.character_key||'',text:'',speech_rate:1,timing_mode:'sequential'});
        }else if(dialogueAction==='remove-shot-dialogue'&&dialogueIndex>=0){dialogues.splice(dialogueIndex,1);
        }else if(dialogueAction==='move-shot-dialogue-up'&&dialogueIndex>0){var previous=dialogues[dialogueIndex-1];dialogues[dialogueIndex-1]=dialogues[dialogueIndex];dialogues[dialogueIndex]=previous;
        }else if(dialogueAction==='move-shot-dialogue-down'&&dialogueIndex>=0&&dialogueIndex<dialogues.length-1){var next=dialogues[dialogueIndex+1];dialogues[dialogueIndex+1]=dialogues[dialogueIndex];dialogues[dialogueIndex]=next;}
        dialogueValues.dialogues=dialogues;saveShotDraft(selectedShotKey,dialogueValues);delete shotEditErrors[selectedShotKey];renderShotModal();
        var refreshedForm=doc.getElementById('sdShotEditor');if(refreshedForm)updateShotTimingHint(refreshedForm);
        return;
      }
      if(action&&action.getAttribute('data-action')==='preview-character-image'){
        var imageLightbox=doc.getElementById('sdCharacterImageLightbox'),previewImage=doc.getElementById('sdCharacterImagePreview'),previewTitle=doc.getElementById('sdCharacterImagePreviewTitle');
        if(!imageLightbox||!previewImage)return;
        characterImagePreviewTrigger=action;
        previewImage.src=action.getAttribute('data-image-url')||'';
        previewImage.alt=(action.getAttribute('data-image-title')||'角色')+' 标准图大图预览';
        resetCharacterImagePreview();
        if(previewTitle)previewTitle.textContent=(action.getAttribute('data-image-title')||'角色')+' · 标准图预览';
        imageLightbox.hidden=false;
        var closePreview=imageLightbox.querySelector('header [data-action="close-character-image-preview"]');
        if(closePreview)closePreview.focus();
        return;
      }
      if(action&&action.getAttribute('data-action')==='zoom-in-character-image'){zoomCharacterImage(.2);return;}
      if(action&&action.getAttribute('data-action')==='zoom-out-character-image'){zoomCharacterImage(-.2);return;}
      if(action&&action.getAttribute('data-action')==='reset-character-image'){resetCharacterImagePreview();return;}
      if(action&&action.getAttribute('data-action')==='close-character-image-preview'){
        var imageLightboxToClose=doc.getElementById('sdCharacterImageLightbox'),imageToClear=doc.getElementById('sdCharacterImagePreview');
        if(imageLightboxToClose)imageLightboxToClose.hidden=true;
        if(imageToClear)imageToClear.removeAttribute('src');
        resetCharacterImagePreview();
        if(characterImagePreviewTrigger)characterImagePreviewTrigger.focus();
        characterImagePreviewTrigger=null;
        return;
      }
      if(action&&action.getAttribute('data-action')==='set-shot-detail'){
        var detailForm=action.closest('#sdShotExecutionEditor'),detailField=detailForm&&detailForm.elements[action.getAttribute('data-detail-field')||''];
        if(!detailField)return;
        detailField.value=action.getAttribute('data-detail-value')||'';
        var detailGroup=action.closest('.sd-shot-detail-chips');
        if(detailGroup)Array.prototype.forEach.call(detailGroup.querySelectorAll('button'),function(button){button.classList.toggle('active',button===action);});
        detailField.focus();
        return;
      }
      if(action&&action.getAttribute('data-action')==='edit-shot-continuity'){
        var continuityForm=action.closest('#sdShotExecutionEditor'),continuityField=continuityForm&&continuityForm.elements.continuity;
        if(!continuityField)return;
        continuityField.readOnly=false;
        action.textContent='正在调整';
        action.disabled=true;
        continuityField.focus();
        continuityField.select();
        return;
      }
      if(action&&action.getAttribute('data-action')==='replace-scene-reference'){
        var replaceCard=action.closest('.sd-scene-card'),replacePrompt=replaceCard&&replaceCard.querySelector('[data-scene-prompt]');
        if(replacePrompt){var replaceEditor=replacePrompt.closest('details');if(replaceEditor)replaceEditor.open=true;replacePrompt.focus();replacePrompt.select();}
        return;
      }
      if(action&&action.getAttribute('data-action')==='add-scene'){
        openSceneManager(null);return;
      }
      if(action&&action.getAttribute('data-action')==='edit-scene'){
        var editSceneCard=action.closest('.sd-scene-card'),editSceneKey=editSceneCard&&editSceneCard.getAttribute('data-scene-key');
        openSceneManager((sceneWorkspace.scenes||[]).filter(function(item){return item.scene_key===editSceneKey;})[0]);return;
      }
      if(action&&action.getAttribute('data-action')==='choose-scene-asset'){
        var assetSceneCard=action.closest('.sd-scene-card'),assetSceneKey=assetSceneCard&&assetSceneCard.getAttribute('data-scene-key'),assetScene=(sceneWorkspace.scenes||[]).filter(function(item){return item.scene_key===assetSceneKey;})[0];
        if(assetScene)openSceneAssetLibrary(assetScene);return;
      }
      if(action&&action.getAttribute('data-action')==='delete-scene'){
        var deleteSceneCard=action.closest('.sd-scene-card'),deleteSceneKey=deleteSceneCard&&deleteSceneCard.getAttribute('data-scene-key'),deleteScene=(sceneWorkspace.scenes||[]).filter(function(item){return item.scene_key===deleteSceneKey;})[0];
        if(!deleteScene)return;
        if((deleteScene.shots||[]).length){show('该场景仍绑定镜头，请先在“编辑与绑定”中取消全部关联镜头',true);return;}
        if(pendingSceneDeleteKey!==deleteSceneKey){pendingSceneDeleteKey=deleteSceneKey;render();show('已进入删除确认。退出项目不会删除；再次确认后场景才会移入回收站。',false);return;}
        pendingSceneDeleteKey='';busy(true);client.deleteScene({project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1),scene_key:deleteSceneKey}).then(function(result){sceneWorkspace=result||sceneWorkspace;render();show('场景已移入回收站，可通过“恢复最近删除”找回',false);}).catch(function(error){show(error.message||'场景删除失败',true);if(Number(error&&error.status)===409)return loadSceneWorkspace();}).finally(function(){busy(false);render();});return;
      }
      if(action&&action.getAttribute('data-action')==='restore-scene'){
        var restoreSceneKey=action.getAttribute('data-scene-key');if(!restoreSceneKey)return;
        pendingSceneDeleteKey='';busy(true);client.restoreScene({project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1),scene_key:restoreSceneKey}).then(function(result){sceneWorkspace=result||sceneWorkspace;render();show('最近删除的场景已恢复',false);}).catch(function(error){show(error.message||'场景恢复失败',true);if(Number(error&&error.status)===409)return loadSceneWorkspace();}).finally(function(){busy(false);render();});return;
      }
      if(action&&action.getAttribute('data-action')==='lock-scene-reference'){
        var lockSceneCard=action.closest('.sd-scene-card'),lockSceneKey=lockSceneCard&&lockSceneCard.getAttribute('data-scene-key');
        if(!lockSceneKey)return;
        busy(true);show('',false);
        client.lockSceneReference({project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1),scene_key:lockSceneKey})
          .then(function(result){sceneWorkspace=result||sceneWorkspace;render();show('场景已锁定，相关镜头生成时会自动引用该场景图',false);})
          .catch(function(error){show(error.message||'场景锁定失败',true);if(Number(error&&error.status)===409)return loadSceneWorkspace();})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='generate-scene-image'){
        var sceneCard=action.closest('.sd-scene-card'),sceneKey=sceneCard&&sceneCard.getAttribute('data-scene-key'),scenePrompt=sceneCard&&sceneCard.querySelector('[data-scene-prompt]'),promptValue=text(scenePrompt&&scenePrompt.value).trim();
        if(!sceneKey)return;
        if(sceneImageOperations[sceneKey]&&sceneImageOperations[sceneKey].active){show('该场景的背景图正在生成，请勿重复提交',false);return;}
        if(promptValue.length<6){show('请先填写更具体的场景提示词',true);if(scenePrompt)scenePrompt.focus();return;}
        if(doc.defaultView&&typeof doc.defaultView.confirm==='function'&&!doc.defaultView.confirm('生成场景图将按现有图片生成规则扣点，确认继续吗？'))return;
        setSceneImageStatus(sceneKey,{active:true,phase:'submitting',label:'正在提交',buttonLabel:'正在提交任务…',message:'正在提交背景图生成任务，请勿重复点击。',error:''});show('背景图任务正在提交，可以继续处理其他场景或镜头',false);
        var sceneOperation=null,sceneJobId=null;
        client.generateSceneImage({project_id:projectId,scene_key:sceneKey,prompt:'真人短剧空场背景图，无人物。'+promptValue+'。保持真实空间结构、自然光影、电影感写实；禁止人物、文字、Logo、水印。',ratio:state.project.ratio||'16:9'},accountUsername)
          .then(function(created){sceneJobId=created&&created.job_id;sceneOperation=created&&created._scene_image_operation;if(!sceneJobId)throw new Error('场景图任务未返回任务编号');return watchSceneImage(sceneKey,promptValue,sceneOperation,sceneJobId,0);})
          .catch(function(error){sceneImageOperations[sceneKey]={active:false,phase:'failed',label:'生成失败',buttonLabel:'重新生成背景图',message:'',error:error.message||'场景图生成失败，请重试'};renderPreservingViewport();show(sceneImageOperations[sceneKey].error,true);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='edit-shot'){
        shotEditorMode='script';
        selectedShotKey=action.getAttribute('data-shot-key')||'';
        renderShotModal();
        return;
      }
      if(action&&/^(?:move-shot-up|move-shot-down|copy-shot|add-shot-before|add-shot-after|smart-insert-shot|delete-shot)$/.test(action.getAttribute('data-action')||'')){
        if(state.conversation&&state.conversation.state==='script_locked'){
          show('剧本已经锁定，不能调整镜头结构。',true);
          return;
        }
        var structureAction=action.getAttribute('data-action'),structureKey=action.getAttribute('data-shot-key')||activeWorkspaceShotKey;
        var operation={
          'move-shot-up':'move_up','move-shot-down':'move_down','copy-shot':'copy',
          'add-shot-before':'insert_before','add-shot-after':'insert_after',
          'smart-insert-shot':'smart_insert','delete-shot':'delete'
        }[structureAction];
        if(!structureKey||!state.current_script)return;
        var structureWindow=doc.defaultView,instruction='';
        if(operation==='delete'){
          if(!structureWindow||!structureWindow.confirm('删除后该镜头将不再参与后续合成；已产生的生成费用不会退回。确认删除吗？'))return;
        }else if(operation==='insert_before'||operation==='insert_after'||operation==='smart_insert'){
          instruction=structureWindow&&typeof structureWindow.prompt==='function'?structureWindow.prompt(operation==='smart_insert'?'可选：说明希望补充怎样的过渡内容':'可选：填写新镜头的画面或剧情要求',''):'';
          if(instruction===null)return;
        }
        apply(client.changeShotStructure(payload({
          version_id:state.current_script.id,
          shot_key:structureKey,
          action:operation,
          instruction:text(instruction).trim()
        })),'镜头结构已更新；旧合成版本已保留，后续需要重新合成').then(function(result){
          var currentShots=result&&result.current_script&&result.current_script.script&&result.current_script.script.shots||[];
          activeWorkspaceShotKey=defaultWorkspaceShotKey(currentShots,autodraft,structureKey);
          selectedProviderShotKey='';
          return Promise.all([loadPreflight(),loadAutodraft(),loadSceneWorkspace()]).then(function(){
            if(preflight&&preflight.stale&&autodraft){
              autodraft.confirmed_plan=null;
              autodraft.provider_preview=null;
              autodraft.provider_quote=null;
              render();
              show('镜头结构已更新，请重新生成并确认制作方案；已生成的单镜头版本仍会保留。',false);
            }
          }).catch(function(){return null;});
        });
        return;
      }
      if(action&&action.getAttribute('data-action')==='optimize-sensitive-prompt'){
        var promptField=doc.querySelector('#sdShotExecutionEditor textarea[name="provider_prompt"]'),assist=doc.getElementById('sdPromptAssistStatus');
        if(!promptField)return;
        var optimizedAny=false;
        ['visual','camera','performance','scene','lighting','composition_style','continuity','sound_design','provider_prompt'].forEach(function(fieldName){
          var field=doc.querySelector('#sdShotExecutionEditor textarea[name="'+fieldName+'"]');
          if(!field)return;
          var optimized=saferProviderPrompt(field.value);
          if(optimized!==text(field.value).trim()){field.value=optimized;optimizedAny=true;}
        });
        if(!optimizedAny){
          if(assist)assist.textContent='没有发现可自动调整的常见表达，请继续人工检查。';
        }else{
          if(assist)assist.textContent='已优化常见误判表达，请确认内容没有偏离原剧情。';
        }
        promptField.focus();
        return;
      }
      if(action&&action.getAttribute('data-action')==='optimize-and-preflight-sensitive'){
        var sensitiveShotKey=action.getAttribute('data-shot-key')||'',sensitiveShot=currentShot(sensitiveShotKey),sensitivePlan=autodraft.confirmed_plan;
        if(!sensitiveShot||!sensitivePlan){show('当前镜头或制作方案尚未准备好，请刷新后重试',true);return;}
        var sensitiveProviderShot=((autodraft.provider_poc&&autodraft.provider_poc.shots)||[]).filter(function(item){return item.shot_key===sensitiveShotKey;})[0]||{};
        var sensitiveCharacters=(autodraft.provider_poc&&autodraft.provider_poc.characters)||[],sensitiveJob=(shotMediaIndex(autodraft)[sensitiveShotKey]||{}).job||null;
        var savedSensitive=(autodraft.provider_execution_overrides||{})[sensitiveShotKey]||{},sensitiveReview=providerInputReview(sensitiveShot,sensitiveProviderShot,sensitiveCharacters,sensitiveJob,savedSensitive);
        var optimizedExecution=optimizedSensitiveExecution(sensitiveShot,sensitiveProviderShot,savedSensitive,sensitiveReview);
        if(!optimizedExecution.character_keys.length){show('当前镜头没有可用的角色绑定，请先手动调整生成要求',true);return;}
        busy(true);show('正在优化文字并执行免费预检，不会报价或扣点…',false);
        client.providerPreflight({project_id:projectId,plan_id:sensitivePlan.id,shot_key:sensitiveShotKey,avatar_id:'',character_key:optimizedExecution.character_keys[0],execution:optimizedExecution}).then(function(result){
          autodraft.provider_preview=result;autodraft.provider_quote=null;
          autodraft.provider_execution_overrides=autodraft.provider_execution_overrides||{};
          autodraft.provider_execution_overrides[sensitiveShotKey]=result.execution||optimizedExecution;
          selectedProviderShotKey=sensitiveShotKey;render();
          show('已优化文字并完成免费预检。请核对实际提交提示词，再获取报价；本次未扣点。',false);
        }).catch(function(error){show(userFacingVideoMessage(error.message,'免费预检失败，请手动调整生成要求'),true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='sync-shot-character-names'){
        var nameShot=currentShot(selectedShotKey),nameProviderShot=((autodraft.provider_poc&&autodraft.provider_poc.shots)||[]).filter(function(item){return item.shot_key===selectedShotKey;})[0]||{};
        var nameJob=(shotMediaIndex(autodraft)[selectedShotKey]||{}).job||null;
        var nameReview=providerInputReview(nameShot,nameProviderShot,(autodraft.provider_poc&&autodraft.provider_poc.characters)||[],nameJob,(autodraft.provider_execution_overrides||{})[selectedShotKey]||{});
        var namePrompt=doc.querySelector('#sdShotExecutionEditor textarea[name="provider_prompt"]'),nameAssist=doc.getElementById('sdPromptAssistStatus');
        if(!namePrompt)return;
        namePrompt.value=syncProviderCharacterNames(namePrompt.value,nameReview);
        if(nameAssist)nameAssist.textContent='人物名称已按当前镜头绑定关系统一，请确认后保存。';
        namePrompt.focus();
        return;
      }
      if(action&&action.getAttribute('data-action')==='edit-shot-execution'){
        shotEditorMode='execution';
        selectedShotKey=action.getAttribute('data-shot-key')||'';
        renderShotModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='enter-refinement-redo'){
        refinementRedoMode=true;
        refinementRedoShotKey=action.getAttribute('data-shot-key')||'';
        selectedProviderShotKey=refinementRedoShotKey;
        render();
        return;
      }
      if(action&&action.getAttribute('data-action')==='exit-refinement-redo'){
        var redoJob=autodraft&&autodraft.provider_job;
        if(redoJob&&['billing','queued','submitting','running'].indexOf(redoJob.status)>=0&&!window.confirm('当前镜头仍在生成。离开后任务会继续，确认返回合成预览吗？'))return;
        refinementRedoMode=false;
        render();
        return;
      }
      if(action&&['copy-legacy-media-recovery','download-legacy-media-recovery'].indexOf(action.getAttribute('data-action'))>=0){
        var recoveryEvidenceAction=action.getAttribute('data-action'),recoveryWindow=doc.defaultView||{};
        handleLegacyMediaRecoveryEvidenceAction(recoveryEvidenceAction,legacyMediaRecoveryResult,{
          document:doc,
          clipboard:recoveryWindow.navigator&&recoveryWindow.navigator.clipboard,
          Blob:recoveryWindow.Blob,
          URL:recoveryWindow.URL,
          setTimeout:recoveryWindow.setTimeout?function(callback,delay){return recoveryWindow.setTimeout(callback,delay);}:setTimeout
        }).then(function(){show(recoveryEvidenceAction==='copy-legacy-media-recovery'?'恢复结果 JSON 已复制':'恢复结果 JSON 已下载',false);})
          .catch(function(error){show(error.message||'恢复结果留证失败',true);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='recover-legacy-media'){
        if(action.disabled||!(autodraft.permissions&&autodraft.permissions.can_recover_legacy_media))return;
        if(!window.confirm('确认在本机校验并恢复历史 2K 原片吗？\n\n系统只处理缺少现代媒体证据的镜头，不会调用生成服务或扣点。'))return;
        busy(true);show('正在校验历史原片，请勿关闭页面…',false);
        var recoveryStartedAt=new Date().toISOString();
        client.recoverLegacyMedia({project_id:projectId})
          .then(function(result){
            legacyMediaRecoveryResult=legacyMediaRecoveryEvidence(result,{started_at:recoveryStartedAt,completed_at:new Date().toISOString()});
            return loadAutodraft().then(function(){
              var recovered=(result&&result.recovered_shot_keys)||[],failed=(result&&result.failed_shots)||[],skipped=(result&&result.skipped_shot_keys)||[];
              var message='历史原片校验完成：恢复 '+recovered.length+' 个，失败 '+failed.length+' 个，跳过 '+skipped.length+' 个。';
              show(message,failed.length>0);
            });
          })
          .catch(function(error){show(error.message||'历史原片恢复失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='keep-original-refinement-shot'){
        var keepSource=refinement&&refinement.current_refinement,keepShotKey=action.getAttribute('data-shot-key')||'';
        if(!keepSource||!keepShotKey||action.disabled)return;
        if(!window.confirm('确认保留当前原视频并取消这个镜头的重做吗？\n\n这表示你接受当前已知问题；不会生成新视频，也不会扣点。'))return;
        busy(true);
        client.keepOriginalRefinementShot({project_id:projectId,version_id:keepSource.id,shot_key:keepShotKey})
          .then(loadRefinement)
          .then(function(){show('已保留原视频并取消该镜头重做',false);})
          .catch(function(error){show(error.message||'取消镜头重做失败',true);})
          .finally(function(){busy(false);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='select-refinement-redo-shot'){
        refinementRedoShotKey=action.getAttribute('data-shot-key')||'';
        selectedProviderShotKey=refinementRedoShotKey;
        autodraft.provider_preview=null;autodraft.provider_quote=null;
        render();
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
        characterNameEditing=false;
        characterProfileDirty=false;
        renderCharacterModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='edit-character-name'){
        characterNameEditing=true;
        renderCharacterModal();
        var editName=doc.getElementById('sdCharacterNameInput');
        if(editName){editName.focus();editName.select();}
        return;
      }
      if(action&&action.getAttribute('data-action')==='cancel-character-name'){
        characterNameEditing=false;
        renderCharacterModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='save-character-name'){
        var characterForName=studioCharacter(selectedCharacterKey),profileForm=doc.getElementById('sdCharacterProfile'),proposed=doc.getElementById('sdCharacterNameInput').value;
        if(!characterForName||!profileForm)return;
        var validationError=characterNameError(characterForName,proposed);
        if(validationError){show(validationError,true);return;}
        busy(true);show('',false);
        saveCharacterProfile(characterForName,profileForm,proposed)
          .then(function(result){
            characterNameEditing=false;characterProfileDirty=false;
            var message=result&&result.compatibility_name_local_only?'名称已用于本地预览；测试服务端升级后会永久保存，内部角色标识不变':'角色名称已更新，内部角色标识保持不变';
            return refreshCharacterContext(message);
          })
          .catch(function(error){show('角色名称保存失败：'+(error.message||'服务端未接受角色名称'),true);})
          .finally(function(){busy(false);render();});
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
        if((characterProfileDirty||characterNameEditing)&&doc.defaultView&&typeof doc.defaultView.confirm==='function'&&!doc.defaultView.confirm('还有未保存的角色修改，确定关闭吗？'))return;
        selectedCharacterKey='';
        characterNameEditing=false;
        characterProfileDirty=false;
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
        selectedCharacterKey=action.getAttribute('data-character-key')||selectedCharacterKey;
        var characterForCreate=studioCharacter(selectedCharacterKey);
        if(!characterForCreate)return;
        if(!(characterStudio&&characterStudio.permissions&&characterStudio.permissions.can_create_avatar)){
          show('只有项目所有者可以创建电影化身',true);return;
        }
        var createName=characterForCreate.name;
        if(!doc.defaultView.confirm('将使用当前角色形象图创建电影化身，并在成功后自动绑定到“'+createName+'”。创建任务会按现有规则扣点，失败自动退款。确认继续吗？'))return;
        busy(true);show('正在读取已确认标准图…',false);
        var avatarStage='读取已确认标准图';
        var avatarCharacter=characterForCreate;
        var avatarOperationState=null;
        Promise.resolve().then(function(){
          var referenceUrl=avatarCharacter&&(avatarCharacter.reference_image_url||(!avatarCharacter.avatar_id?avatarCharacter.image_url:''));
          if(!referenceUrl)throw new Error('当前角色没有可用的已确认标准图。');
          show('正在读取已确认标准图…',false);
          return client.imageData(referenceUrl);
        }).then(function(dataUrl){
          avatarStage='提交电影化身任务';
          show('正在提交电影化身任务…',false);
          return client.createAvatar({
            image_data:dataUrl,name:text(createName).trim(),
            short_drama_binding:{
              project_id:projectId,
              project_revision:Number(characterStudio.project_revision),
              character_key:characterForCreate.character_key
            }
          },accountUsername);
        }).then(function(created){
          var jobId=created&&created.job_id;
          if(!jobId)throw new Error('电影化身任务未返回任务编号');
          avatarOperationState=created._avatar_operation||null;
          avatarStage='等待电影化身生成';
          var attempts=0;
          function pollAvatar(){
            attempts+=1;
            return client.job(jobId).then(function(job){
              if(job.status==='done'){
                var completed=job.result||{},completedBinding=completed.short_drama_binding||{};
                if(completedBinding.status==='pending'&&attempts<180){return new Promise(function(resolve){setTimeout(resolve,250);}).then(pollAvatar);}
                client.finishAvatarOperation(avatarOperationState);return completed;
              }
              if(job.status==='error'||job.status==='failed'){client.finishAvatarOperation(avatarOperationState);throw new Error(job.error||'电影化身生成失败，点数将自动退回');}
              if(attempts>=180)throw new Error('电影化身仍在生成，可稍后刷新形象库查看');
              show('电影化身正在生成（任务 #'+jobId+'），请勿重复提交…',false);
              return new Promise(function(resolve){setTimeout(resolve,1000);}).then(pollAvatar);
            });
          }
          return pollAvatar();
        }).then(function(result){
          if(!result.avatar_id)throw new Error('电影化身已生成，但缺少可绑定的形象编号');
          var binding=result.short_drama_binding||{};
          if(binding.status!=='bound')throw new Error(binding.message||'电影化身已生成，但自动绑定尚未完成，请刷新角色工作室');
          avatarStage='刷新角色工作室';
          return loadCharacterStudio(true);
        }).then(function(){return refreshCharacterContext('电影化身创建成功，并已自动绑定到 '+createName);})
          .catch(function(error){show(avatarStage+'失败：'+(error.message||'请稍后重试'),true);})
          .finally(function(){busy(false);render();renderCharacterModal();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='bind-card-avatar'){
        selectedCharacterKey=action.getAttribute('data-character-key')||'';
        var characterForCardBind=studioCharacter(selectedCharacterKey),inlineActions=action.closest('.sd-character-inline-actions'),avatarSelect=inlineActions&&inlineActions.querySelector('select'),avatarId=avatarSelect&&avatarSelect.value||'';
        if(!characterForCardBind||!avatarId){show('请先选择一个电影化身',true);return;}
        busy(true);show('',false);
        client.bindCharacterAvatar({
          project_id:projectId,
          project_revision:Number(characterStudio.project_revision),
          character_key:characterForCardBind.character_key,
          avatar_id:avatarId
        }).then(function(){return refreshCharacterContext('电影化身已绑定到 '+characterForCardBind.name);})
          .catch(function(error){show(error.message||'电影化身绑定失败',true);})
          .finally(function(){busy(false);render();});
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
        var currentImageOperation=characterImageOperationFor(characterForImage.character_key);
        var currentImageAction=characterImageAction(currentImageOperation);
        if(currentImageAction==='blocked')return;
        if(currentImageAction==='check'){
          setCharacterImageOperation(characterForImage.character_key,'generating','正在检查后台生成结果…',false,true);
          loadCharacterStudio(true).then(function(){
            var pendingCharacter=studioCharacter(characterForImage.character_key);
            if(pendingCharacter&&pendingCharacter.reference_image_url){
              setCharacterImageOperation(characterForImage.character_key,'success','角色形象已生成，可继续创建或绑定电影化身。',false,false);
              render();renderCharacterModal();
            }else setCharacterImageOperation(characterForImage.character_key,'pending','任务仍在后台生成，请稍后再次检查；请勿重复提交扣点任务。',false,false);
          }).catch(function(error){setCharacterImageOperation(characterForImage.character_key,'pending','暂时无法查询生成结果：'+(error.message||'请稍后重试'),true,false);})
            .finally(function(){busy(false);});
          return;
        }
        var imageName=doc.getElementById('sdCharacterNameInput').value||characterForImage.name;
        var imageNameError=characterNameError(characterForImage,imageName);
        if(imageNameError){setCharacterImageOperation(characterForImage.character_key,'failed',imageNameError,true,false);return;}
        if(!doc.defaultView.confirm('生成角色形象将按现有计费规则执行，确认继续吗？'))return;
        busy(true);show('',false);setCharacterImageOperation(characterForImage.character_key,'saving','正在保存角色视觉设定…',false,true);
        var generationStage='保存角色视觉设定';
        saveCharacterProfile(characterForImage,profileForm,imageName).then(function(result){
          characterNameEditing=false;characterProfileDirty=false;
          if(Number(result&&result.project_revision)>0)characterStudio.project_revision=Number(result.project_revision);
          generationStage='提交角色形象生成任务';
          setCharacterImageOperation(characterForImage.character_key,'submitting','视觉设定已保存，正在提交角色形象生成任务…',false,true);
          return client.generateCharacterImage({
            project_id:projectId,
            revision:Number(characterStudio.project_revision),
            character_key:characterForImage.character_key
          },accountUsername);
        }).then(function(){
          setCharacterImageOperation(characterForImage.character_key,'generating','生成任务已提交，正在等待角色形象图…',false,true);
          var attempts=0;
          function poll(){
            attempts+=1;
            return loadCharacterStudio(true).then(function(){
              var current=studioCharacter(characterForImage.character_key);
              if(current&&current.reference_image_url)return current;
              if(attempts>=30)return null;
              return new Promise(function(resolve){setTimeout(resolve,1000);}).then(poll);
            });
          }
          return poll();
        }).then(function(result){
          if(result)setCharacterImageOperation(characterForImage.character_key,'success','角色形象已生成，可继续创建或绑定电影化身。',false,false);
          else setCharacterImageOperation(characterForImage.character_key,'pending','任务已提交并仍在后台生成。请稍后点击“检查生成结果”，不要重复提交。',false,false);
        }).catch(function(error){setCharacterImageOperation(characterForImage.character_key,'failed',characterImageFailureMessage(error,generationStage),true,false);})
          .finally(function(){busy(false);render();renderCharacterModal();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='quick-reply'){
        if(!conversationWorkspaceMode(state).canMessage)return;
        var actionGroup=action.closest('.sd-advisor-actions');
        if(actionGroup){
          action.classList.add('selected');
          actionGroup.querySelectorAll('button').forEach(function(button){button.disabled=true;});
        }
        sendConversationMessage(action.getAttribute('data-message'));
        return;
      }
      if(action&&action.getAttribute('data-action')==='confirm-direction'){
        if(!conversationWorkspaceMode(state).canMessage)return;
        sendConversationMessage(action.getAttribute('data-message'));
        return;
      }
      if(action&&action.getAttribute('data-action')==='confirm-and-generate'){
        if(!conversationWorkspaceMode(state).canMessage)return;
        var directInstruction=doc.getElementById('sdInstruction');
        confirmDirectionAndGenerate(
          action.getAttribute('data-message')||'确认尊重原稿并生成',
          directInstruction?directInstruction.value:''
        );
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
      if(action&&action.getAttribute('data-action')==='select-provider-shot'){
        var selectedKey=action.getAttribute('data-shot-key')||'';
        if(selectedProviderShotKey!==selectedKey){
          selectedProviderShotKey=selectedKey;
          autodraft.provider_preview=null;
          autodraft.provider_quote=null;
        }else{
          selectedProviderShotKey='';
        }
        render();
        return;
      }
      if(action&&action.getAttribute('data-action')==='show-workspace-shot'){
        activeWorkspaceShotKey=action.getAttribute('data-shot-key')||'';
        selectedProviderShotKey='';
        render();
        return;
      }
      if(action&&action.getAttribute('data-action')==='step-workspace-shot'){
        var workspaceShots=state.current_script&&state.current_script.script&&state.current_script.script.shots||[];
        var currentWorkspaceIndex=workspaceShots.map(function(shot){return text(shot.shot_key);}).indexOf(activeWorkspaceShotKey);
        var nextWorkspaceIndex=Math.max(0,Math.min(workspaceShots.length-1,currentWorkspaceIndex+Number(action.getAttribute('data-direction')||0)));
        if(workspaceShots[nextWorkspaceIndex])activeWorkspaceShotKey=text(workspaceShots[nextWorkspaceIndex].shot_key);
        selectedProviderShotKey='';
        render();
        return;
      }
      if(action&&action.getAttribute('data-action')==='select-provider-version'){
        var versionShotKey=action.getAttribute('data-shot-key')||'',versionId=action.getAttribute('data-version-id')||'';
        if(!versionShotKey||!versionId)return;
        busy(true);show('',false);
        client.selectProviderVersion({project_id:projectId,shot_key:versionShotKey,version_id:versionId}).then(function(){return loadAutodraft();}).then(function(){show('已采用所选视频版本，后续合成将使用该版本',false);}).catch(function(error){show(error.message||'视频版本切换失败',true);}).finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='provider-preflight'){
        var confirmedPlan=autodraft.confirmed_plan,requestedShotKey=action.getAttribute('data-shot-key')||selectedProviderShotKey;
        if(!confirmedPlan||!requestedShotKey)return;
        selectedProviderShotKey=requestedShotKey;
        var providerShot=((autodraft.provider_poc&&autodraft.provider_poc.shots)||[]).filter(function(item){return item.shot_key===requestedShotKey;})[0];
        if(!providerShot||!providerShot.binding_ready){setProviderShotError(requestedShotKey,'请先确认当前镜头全部角色的标准图已经锁定');return;}
        delete providerShotErrors[requestedShotKey];busy(true);show('',false);
        client.providerPreflight({project_id:projectId,plan_id:confirmedPlan.id,shot_key:requestedShotKey,avatar_id:providerShot.primary_avatar_id||'',character_key:providerShot.primary_character_key||''}).then(function(result){
          delete providerShotErrors[requestedShotKey];autodraft.provider_preview=result;autodraft.provider_quote=null;renderPreservingViewport();show('单镜头请求预检完成：没有扣点，也没有提交生成任务',false);
        }).catch(function(error){setProviderShotError(requestedShotKey,userFacingVideoMessage(error.message,'单镜头请求预检失败'));})
          .finally(function(){busy(false);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='provider-quote'){
        var preview=autodraft.provider_preview,planForQuote=autodraft.confirmed_plan;
        if(!preview||!planForQuote)return;
        var quoteShotKey=text(preview.shot&&preview.shot.shot_key);
        delete providerShotErrors[quoteShotKey];busy(true);show('',false);
        client.providerQuote({
          project_id:projectId,
          plan_id:planForQuote.id,
          shot_key:quoteShotKey,
          avatar_id:preview.avatar.id,
          character_key:preview.character_key
        }).then(function(result){
          delete providerShotErrors[quoteShotKey];autodraft.provider_quote=result;renderPreservingViewport();
          show('报价已生成；确认后才会扣点并提交外部任务',false);
        }).catch(function(error){setProviderShotError(quoteShotKey,userFacingVideoMessage(error.message,'单镜头报价失败'));})
          .finally(function(){busy(false);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='provider-start'){
        var quote=autodraft.provider_quote;
        if(!quote)return;
        var startShotKey=text(quote.shot&&quote.shot.shot_key);
        var confirmWindow=doc.defaultView;
        if(confirmWindow&&typeof confirmWindow.confirm==='function'&&!confirmWindow.confirm('确认扣除 '+Number(quote.cost||0)+' 点，生成镜头 '+startShotKey+'？'))return;
        delete providerShotErrors[startShotKey];busy(true);show('',false);
        client.startProviderJob({project_id:projectId,quote_token:quote.quote_token}).then(function(result){
          var nextJobs=providerJobsWithResult(autodraft,result);
          delete providerShotErrors[startShotKey];
          autodraft.provider_job=result;
          autodraft.provider_jobs=nextJobs;
          autodraft.provider_quote=null;renderPreservingViewport();
          show('已完成扣点并创建单镜头任务；页面会自动更新进度',false);
          schedulePoll();
        }).catch(function(error){setProviderShotError(startShotKey,providerStartFailureMessage(error,quote.cost));})
          .finally(function(){busy(false);});
        return;
      }
      if(action&&action.getAttribute('data-action')==='jump-to-shot'){
        var jumpKey=action.getAttribute('data-shot-key')||'';
        activeWorkspaceShotKey=jumpKey;
        selectedProviderShotKey=jumpKey;
        render();
        var targetShot=Array.prototype.filter.call(root.querySelectorAll('.sd-shot[data-shot-key]'),function(item){return item.getAttribute('data-shot-key')===jumpKey;})[0];
        if(targetShot){
          targetShot.scrollIntoView({behavior:'smooth',block:'center'});
          targetShot.classList.remove('focused');
          void targetShot.offsetWidth;
          targetShot.classList.add('focused');
        }
        return;
      }
      if(action&&action.getAttribute('data-action')==='reassemble-refinement'){
        var reassemblySource=refinement&&refinement.current_refinement;
        if(!reassemblySource)return;
        busy(true);show('正在按每个镜头的实际时长重新装配完整预览…',false);
        client.reassembleRefinementCandidates({project_id:projectId,version_id:reassemblySource.id})
          .then(loadRefinement)
          .then(function(){show('完整预览已重新装配，不调用视频模型，也不扣点。',false);})
          .catch(function(error){show(error.message||'完整预览重新装配失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='refine-shot'){
        var shotKey=action.getAttribute('data-shot-key'),replacementVersionId=action.getAttribute('data-version-id')||'';
        var candidateRequest;
        try{candidateRequest=refinementCandidateRequest(projectId,shotKey,replacementVersionId);}catch(error){show(error.message,true);return;}
        busy(true);show('',false);
        client.previewRefinement(candidateRequest.preview).then(function(preview){
          if(preview.replacement_ready!==true){throw new Error(preview.replacement_error&&preview.replacement_error.message||'请先在镜头生成区生成该镜头的新版本');}
          candidateRequest=refinementCandidateRequest(projectId,shotKey,replacementVersionId,preview);
          show('正在采用 '+preview.affected_shots.join('、')+' 的当前候选版本；不会立即重新合成整片',false);
          return client.adoptRefinementCandidate(candidateRequest.adoption);
        }).then(function(result){refinement.current_refinement_job=result;render();schedulePoll();})
          .catch(function(error){show(error.message||'镜头精修提交失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='seek-refinement-shot'){
        var refinementVideo=root.querySelector('video[data-refinement-player]');
        if(!refinementVideo)return;
        var refinementLocator=root.querySelector('[data-refinement-shot-locator]');
        var refinementTimelineTotal=Math.max(0,Number(refinementLocator&&refinementLocator.getAttribute('data-total-ms')||0));
        var refinementVideoDurationMs=Math.max(0,Number(refinementVideo.duration||0)*1000);
        var refinementTargetMs=Math.max(0,Number(action.getAttribute('data-start-ms')||0));
        if(refinementTimelineTotal>0&&refinementVideoDurationMs>0)refinementTargetMs=refinementTargetMs*refinementVideoDurationMs/refinementTimelineTotal;
        refinementVideo.currentTime=refinementTargetMs/1000;
        updateRefinementShotLocator(refinementVideo,action);
        return;
      }
      if(action&&['mark-refinement-issue','mark-current-refinement-shot'].indexOf(action.getAttribute('data-action'))>=0){
        if(!(refinement&&refinement.current_refinement))return;
        refinementIssueTrigger=action;
        refinementIssueDraft={shot_key:action.getAttribute('data-shot-key')||''};
        renderRefinementIssueModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='close-refinement-issue'){
        closeRefinementIssueModal();
        return;
      }
      if(action&&action.getAttribute('data-action')==='confirm-refinement'){
        var currentRefinement=refinement&&refinement.current_refinement;if(!currentRefinement)return;
        var requirements=refinement&&refinement.acceptance_requirements||{};
        var checklist={};Array.prototype.slice.call(root.querySelectorAll('[data-acceptance-check]')).forEach(function(item){checklist[item.getAttribute('data-acceptance-check')]=item.checked===true;});
        busy(true);show('',false);
        client.confirmRefinement({project_id:projectId,version_id:currentRefinement.id,checklist:checklist,source_hashes:requirements.source_hashes||{}}).then(loadRefinement)
          .then(function(){show('全片验收通过，精修版本已锁定，可以导出 2K 正式成片',false);})
          .catch(function(error){show(error.message||'精修确认失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      if(action&&action.getAttribute('data-action')==='start-delivery'){
        var deliverySource=refinement&&refinement.current_refinement;if(!deliverySource)return;
        if(!refinement.billing||refinement.billing.delivery_enabled!==true){show('真实 2K 正式交付执行器尚未启用，本次不会扣点',true);return;}
        busy(true);show('',false);
        client.deliveryQuote({project_id:projectId,version_id:deliverySource.id}).then(function(quote){
          var confirmWindow=doc.defaultView;
          if(confirmWindow&&typeof confirmWindow.confirm==='function'&&!confirmWindow.confirm('确认扣除 '+Number(quote.cost||0)+' 点，导出 2K 正式成片？'))return null;
          show('报价 '+Number(quote.cost||0)+' 点，有效期 5 分钟，正在提交正式导出',false);
          return client.startDelivery({project_id:projectId,quote_token:quote.quote_token});
        }).then(function(result){if(!result)return;refinement.current_delivery_job=result;render();schedulePoll();})
          .catch(function(error){show(error.message||'正式导出提交失败',true);})
          .finally(function(){busy(false);render();});
        return;
      }
      var node=event.target.closest('[data-version-id]');if(!node||node.classList.contains('current'))return;
      apply(client.restore(payload({version_id:node.getAttribute('data-version-id')})),'历史版本已恢复为新版本');
    });
    root.addEventListener('change',function(event){
      var shotForm=event.target.closest&&event.target.closest('#sdShotEditor');
      if(shotForm&&selectedShotKey){saveShotDraft(selectedShotKey,shotFormValues(shotForm));refreshShotTimingValidation(shotForm,selectedShotKey);}
      if(event.target&&event.target.hasAttribute('data-scene-upload')){
        var input=event.target,file=input.files&&input.files[0],uploadCard=input.closest('.sd-scene-card'),uploadKey=uploadCard&&uploadCard.getAttribute('data-scene-key'),uploadPrompt=uploadCard&&uploadCard.querySelector('[data-scene-prompt]');
        if(!file||!uploadKey)return;
        if(!/^image\/(?:jpeg|png|webp)$/i.test(file.type||'')){show('请上传 JPG、PNG 或 WebP 场景图',true);input.value='';return;}
        if(file.size>10*1024*1024){show('场景图大小必须在 10MB 以内',true);input.value='';return;}
        busy(true);show('正在读取场景图…',false);
        var reader=new doc.defaultView.FileReader();
        reader.onload=function(){
          client.setSceneReference({project_id:projectId,graph_revision:Number(sceneWorkspace.graph_revision||1),scene_key:uploadKey,source:'upload',image_data:String(reader.result||''),filename:file.name,prompt:text(uploadPrompt&&uploadPrompt.value).trim()})
            .then(function(result){sceneWorkspace=result||sceneWorkspace;render();show('场景图已上传，请预览后确认锁定',false);})
            .catch(function(error){show(error.message||'场景图上传失败',true);if(Number(error&&error.status)===409)return loadSceneWorkspace();})
            .finally(function(){busy(false);render();});
        };
        reader.onerror=function(){busy(false);show('场景图读取失败，请重新选择',true);render();};
        reader.readAsDataURL(file);return;
      }
      if(event.target&&event.target.hasAttribute('data-acceptance-check')){
        var checks=Array.prototype.slice.call(root.querySelectorAll('[data-acceptance-check]'));
        var confirmButton=root.querySelector('[data-action="confirm-refinement"]');
        if(confirmButton)confirmButton.disabled=!checks.length||checks.some(function(item){return !item.checked;});
        return;
      }
    });
    busy(true);
    client.currentUsername().then(function(username){accountUsername=username;return Promise.all([client.workspace(projectId),client.preflight(projectId),client.autodraft(projectId),client.recoverAvatarOperations(accountUsername),client.recoverCharacterImageOperations(accountUsername),client.project(projectId),client.recoverSceneImageOperations(accountUsername)]);}).then(function(results){
      state=normalize(results[0]);preflight=results[1]||{};autodraft=results[2]||{};projectDetail=results[5]||{characters:[]};recoveredSceneOperations=results[6]||[];
      var tasks=[];
      if(state.conversation.state==='script_locked'){
        tasks.push(client.characterStudio(projectId).then(function(value){characterStudio=value||null;}));
      }
      if(sceneWorkspaceRequired(state)){
        tasks.push(loadSceneWorkspace());
      }
      if(autodraft.current_version)tasks.push(client.refinement(projectId).then(function(value){refinement=value||null;}));
      return Promise.all(tasks);
    }).then(function(){
      recoveredSceneOperations.forEach(function(item){
        var operation=item&&item.operation||{},payload=operation.payload||{},job=item&&item.job,sceneKey=text(payload.scene_key);
        if(text(payload.project_id)!==projectId||!sceneKey)return;
        if(item.error){sceneImageOperations[sceneKey]={active:false,phase:'failed',label:'检查失败',buttonLabel:'重新生成背景图',error:item.error.message||'背景图任务检查失败，请重试'};return;}
        if(job&&job.status==='done'){saveGeneratedSceneImage(sceneKey,text(payload.prompt).replace(/^真人短剧空场背景图，无人物。|。保持真实空间结构、自然光影、电影感写实；禁止人物、文字、Logo、水印。$/g,''),operation,operation.job_id,job.result||{});return;}
        if(job&&['error','failed'].indexOf(job.status)>=0){client.finishSceneImageOperation(operation);sceneImageOperations[sceneKey]={active:false,phase:'failed',label:'生成失败',buttonLabel:'重新生成背景图',error:job.error||'场景图生成失败，请重试'};return;}
        sceneImageOperations[sceneKey]={active:true,phase:'generating',label:'生成中',buttonLabel:'背景图生成中…',message:'已恢复后台任务，正在继续检查生成结果。'};
        if(operation.job_id)watchSceneImage(sceneKey,text(payload.prompt).replace(/^真人短剧空场背景图，无人物。|。保持真实空间结构、自然光影、电影感写实；禁止人物、文字、Logo、水印。$/g,''),operation,operation.job_id,0);
      });
      render();schedulePoll();
    }).catch(function(error){show(error.message||'工作区加载失败',true);}).finally(function(){busy(false);render();});
    return {render:render,getState:function(){return state;},getPreflight:function(){return preflight;},getAutodraft:function(){return autodraft;},getRefinement:function(){return refinement;}};
  }
  return {createClient:createClient,shotReferenceSelectionPolicy:shotReferenceSelectionPolicy,effectiveSceneReferenceIdentity:effectiveSceneReferenceIdentity,confirmedShotDialogueHtml:confirmedShotDialogueHtml,characterImageOperationState:characterImageOperationState,characterImageAction:characterImageAction,avatarCreateUrl:avatarCreateUrl,shotDraftStorageKey:shotDraftStorageKey,discardLegacyShotDraft:discardLegacyShotDraft,cloneProjectPayload:cloneProjectPayload,normalize:normalize,conversationWorkspaceMode:conversationWorkspaceMode,sceneWorkspaceRequired:sceneWorkspaceRequired,dialogueReadingSeconds:dialogueReadingSeconds,shotTimingIssue:shotTimingIssue,shotTimingStatus:shotTimingStatus,editableShotDialogues:editableShotDialogues,setWorkspaceControlDisabled:setWorkspaceControlDisabled,applyConversationMode:applyConversationMode,quickReplyPresentation:quickReplyPresentation,messageHtml:messageHtml,importContractHtml:importContractHtml,importContractTechnicalHtml:importContractTechnicalHtml,storyActsHtml:storyActsHtml,storyboardQualityHtml:storyboardQualityHtml,scriptHeaderState:scriptHeaderState,shotMediaIndex:shotMediaIndex,activeProviderJobs:activeProviderJobs,providerJobsWithResult:providerJobsWithResult,providerJobDisplay:providerJobDisplay,currentShotExecutionPrompt:currentShotExecutionPrompt,defaultWorkspaceShotKey:defaultWorkspaceShotKey,shotStructureCapabilities:shotStructureCapabilities,shotGenerationOverviewHtml:shotGenerationOverviewHtml,sceneLockingHtml:sceneLockingHtml,shotMediaHtml:shotMediaHtml,providerShotControlsHtml:providerShotControlsHtml,scriptHtml:scriptHtml,versionHtml:versionHtml,preflightHtml:preflightHtml,legacyMediaRecoveryEvidence:legacyMediaRecoveryEvidence,legacyMediaRecoveryResultJson:legacyMediaRecoveryResultJson,legacyMediaRecoveryResultHtml:legacyMediaRecoveryResultHtml,handleLegacyMediaRecoveryEvidenceAction:handleLegacyMediaRecoveryEvidenceAction,autodraftActionsHtml:autodraftActionsHtml,draftHtml:draftHtml,refinementIssueGroups:refinementIssueGroups,refinementTimelineTime:refinementTimelineTime,refinementShotTimeline:refinementShotTimeline,refinementShotLocatorHtml:refinementShotLocatorHtml,refinementShotCandidateHtml:refinementShotCandidateHtml,refinementCandidateRequest:refinementCandidateRequest,refinementRedoGenerationHtml:refinementRedoGenerationHtml,refinementRedoSummaryHtml:refinementRedoSummaryHtml,refinementRedoHtml:refinementRedoHtml,refinementHtml:refinementHtml,refinementActionsHtml:refinementActionsHtml,refinementProviderHtml:refinementProviderHtml,shellHtml:shellHtml,authoritativeCharacterList:authoritativeCharacterList,movieAvatarRequired:movieAvatarRequired,userFacingVideoMessage:userFacingVideoMessage,providerStartFailureMessage:providerStartFailureMessage,sensitiveProviderFailure:sensitiveProviderFailure,saferProviderPrompt:saferProviderPrompt,providerInputReview:providerInputReview,optimizedSensitiveExecution:optimizedSensitiveExecution,syncProviderCharacterNames:syncProviderCharacterNames,syncShotBindingPrompt:syncShotBindingPrompt,providerFailureRecoveryHtml:providerFailureRecoveryHtml,providerLabel:providerLabel,setWorkspaceBusyState:setWorkspaceBusyState,mount:mount};
});
