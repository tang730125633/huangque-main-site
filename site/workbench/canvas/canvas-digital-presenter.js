(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{};root.HQCanvas.digitalPresenter=api; }
})(typeof window!=='undefined'?window:null,function(){
  'use strict';

  function cloneValue(value){
    if(Array.isArray(value)) return value.map(cloneValue);
    if(value&&typeof value==='object'){
      var copy={};Object.keys(value).forEach(function(key){ copy[key]=cloneValue(value[key]); });return copy;
    }
    return value;
  }
  function numberInRange(value,min,max,fallback){
    value=Number(value);return isFinite(value)&&Math.floor(value)===value&&value>=min&&value<=max?value:fallback;
  }
  function normalizeNodeParams(input){
    var value=input||{};
    return {
      project_id:value.project_id||value.id||null,
      title:String(value.title||'数字人口播').slice(0,80),
      ratio:value.ratio==='16:9'?'16:9':'9:16',
      target_duration:numberInRange(value.target_duration,30,180,30),
      stage:String(value.stage||'draft'),
      progress:Math.max(0,Math.min(100,Number(value.progress)||0)),
      spent_points:Math.max(0,Number(value.spent_points)||0),
      estimated_points:Math.max(0,Number(value.estimated_points)||0),
      failed_segment_count:Math.max(0,Math.floor(Number(value.failed_segment_count)||0)),
      avatar_thumbnail:value.avatar_thumbnail==null?null:String(value.avatar_thumbnail).slice(0,500)
    };
  }
  function summarizeProject(project){
    project=project||{};
    return normalizeNodeParams({
      project_id:project.id||project.project_id||null,title:project.title,ratio:project.ratio,
      target_duration:project.target_duration,stage:project.stage,progress:project.progress,
      spent_points:project.spent_points,estimated_points:project.estimated_points,
      failed_segment_count:project.failed_segment_count,avatar_thumbnail:project.avatar_thumbnail
    });
  }
  function sanitizeNodeData(node){
    var copy=cloneValue(node||{});
    if(copy.type==='digitalPresenter'){
      copy.params=normalizeNodeParams(copy.params);
      copy.outputs={};
    }
    return copy;
  }
  function copyNodeData(node){
    var copy=sanitizeNodeData(node);
    if(copy.type==='digitalPresenter') copy.params=normalizeNodeParams(Object.assign({},copy.params,{
      project_id:null,stage:'draft',progress:0,spent_points:0,estimated_points:0,
      failed_segment_count:0,avatar_thumbnail:null
    }));
    return copy;
  }
  function creationPayload(params){
    var value=normalizeNodeParams(params);
    return {title:value.title,ratio:value.ratio,target_duration:value.target_duration};
  }
  function canRegisterEntry(capability){ return !!capability&&capability.enabled===true; }
  function canOpenNode(params,canEdit){ return !!(params&&params.project_id)||!!canEdit; }
  function isRoleDowngrade(previous,next){
    var rank={owner:3,editor:2,viewer:1};
    return !!rank[previous]&&!!rank[next]&&rank[next]<rank[previous];
  }
  function canonicalValue(value){
    if(Array.isArray(value)) return value.map(canonicalValue);
    if(value&&typeof value==='object'){
      var sorted={};Object.keys(value).sort().forEach(function(key){ sorted[key]=canonicalValue(value[key]); });return sorted;
    }
    return value;
  }
  function creationFingerprint(payload){ return JSON.stringify(canonicalValue(payload||{})); }
  function newIdempotencyKey(){
    var random=(typeof crypto!=='undefined'&&crypto.randomUUID)?crypto.randomUUID():Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
    return 'dp-create-'+random;
  }
  function defaultSessionStorage(){
    try{ return typeof sessionStorage!=='undefined'?sessionStorage:null; }catch(error){ return null; }
  }

  function createEntryRegistrar(addEntry){
    if(typeof addEntry!=='function') throw new Error('digital presenter entry registrar requires addEntry');
    var registered=false;
    return {register:function(capability){
      if(registered||!canRegisterEntry(capability)) return false;
      addEntry();registered=true;return true;
    },isRegistered:function(){return registered;}};
  }

  function createNodeCreationPolicy(options){
    options=options||{};
    if(typeof options.context!=='function') throw new Error('node creation policy requires context');
    function canCreate(type){
      var context=options.context()||{};
      if(!context.canEdit) return false;
      if(type==='digitalPresenter'){
        return context.entryEnabled===true&&context.scope==='collab';
      }
      return true;
    }
    function canMaterialize(type,source){
      if(type!=='digitalPresenter') return source!=='create'||canCreate(type);
      if(source==='local-template') return false;
      var context=options.context()||{};
      if(source==='trusted-collab'){
        return context.entryEnabled===true&&context.scope==='collab';
      }
      return canCreate(type);
    }
    return {
      canCreate:canCreate,
      canMaterialize:canMaterialize,
      run:function(type,create){
        if(!canCreate(type)) return false;
        create();return true;
      }
    };
  }

  function createWorkspaceLifecycle(){
    var entries=Object.create(null);
    function key(scope,nodeId){ return JSON.stringify([String(scope||''),String(nodeId||'')]); }
    function destroy(itemKey){
      var workspace=entries[itemKey];
      if(!workspace) return false;
      delete entries[itemKey];
      if(workspace.destroy) workspace.destroy();
      return true;
    }
    function destroyScope(scope){
      scope=String(scope||'');
      Object.keys(entries).forEach(function(itemKey){
        var parsed=JSON.parse(itemKey);
        if(parsed[0]===scope) destroy(itemKey);
      });
    }
    function destroyAll(){ Object.keys(entries).forEach(destroy); }
    return {
      attach:function(scope,nodeId,workspace){
        var itemKey=key(scope,nodeId);
        if(entries[itemKey]&&entries[itemKey]!==workspace) destroy(itemKey);
        entries[itemKey]=workspace;return workspace;
      },
      removeNode:function(scope,nodeId){ return destroy(key(scope,nodeId)); },
      removeWorkspace:function(workspace){
        var found=Object.keys(entries).find(function(itemKey){return entries[itemKey]===workspace;});
        return found?destroy(found):false;
      },
      restoreScope:destroyScope,
      switchScope:destroyScope,
      roleChanged:function(previous,next){ if(isRoleDowngrade(previous,next)) destroyAll(); },
      destroyAll:destroyAll,
      size:function(){return Object.keys(entries).length;}
    };
  }

  function observeWorkspaceReady(workspace,options){
    options=options||{};
    if(!workspace||!workspace.ready||typeof workspace.ready.then!=='function'){
      return Promise.reject(new Error('digital presenter workspace requires ready promise'));
    }
    function active(){ return typeof options.isActive!=='function'||options.isActive(); }
    return Promise.resolve(workspace.ready).then(function(){
      if(!active()) return null;
      if(typeof options.onReady==='function') options.onReady(workspace);
      return workspace;
    },function(error){
      if(active()&&typeof options.onError==='function') options.onError(error);
      return null;
    });
  }

  function createProjectCoordinator(options){
    options=options||{};
    if(typeof options.getNode!=='function'||typeof options.create!=='function'||typeof options.apply!=='function'){
      throw new Error('digital presenter project coordinator requires getNode, create, and apply methods');
    }
    var pending=Object.create(null),completed=Object.create(null),discarded=Object.create(null),identities=Object.create(null);
    var storage=options.storage===undefined?defaultSessionStorage():options.storage;
    var identityPrefix='hq_digital_presenter_create:';
    var coordinatorId=newIdempotencyKey();
    function key(scope,nodeId){ return JSON.stringify([String(scope||''),String(nodeId||'')]); }
    function identityStorageKey(itemKey){ return identityPrefix+itemKey; }
    function readIdentity(itemKey){
      if(identities[itemKey]) return identities[itemKey];
      if(!storage||typeof storage.getItem!=='function') return null;
      try{
        var value=JSON.parse(storage.getItem(identityStorageKey(itemKey))||'null');
        if(value&&typeof value.key==='string'&&typeof value.fingerprint==='string') identities[itemKey]=value;
      }catch(error){}
      return identities[itemKey]||null;
    }
    function writeIdentity(itemKey,identity){
      identities[itemKey]=identity;
      if(storage&&typeof storage.setItem==='function'){
        try{ storage.setItem(identityStorageKey(itemKey),JSON.stringify(identity)); }catch(error){}
      }
      return identity;
    }
    function creationIdentity(itemKey,payload){
      var fingerprint=creationFingerprint(payload),identity=readIdentity(itemKey);
      if(identity&&identity.fingerprint===fingerprint){
        if(identity.coordinator_id!==coordinatorId){
          identity={key:identity.key,fingerprint:identity.fingerprint,coordinator_id:coordinatorId};
          writeIdentity(itemKey,identity);
        }
        return identity;
      }
      return writeIdentity(itemKey,{key:newIdempotencyKey(),fingerprint:fingerprint,coordinator_id:coordinatorId});
    }
    function clearIdentity(itemKey,identity){
      if(identity&&identities[itemKey]&&identities[itemKey].key!==identity.key) return;
      delete identities[itemKey];
      if(storage&&typeof storage.removeItem==='function'){
        try{ storage.removeItem(identityStorageKey(itemKey)); }catch(error){}
      }
    }
    function linked(node){ return node&&node.params&&node.params.project_id||null; }
    function scopePending(scope){
      return Object.keys(pending).some(function(item){ return pending[item].scope===scope; });
    }
    function consume(scope,nodeId,entry){
      var itemKey=key(scope,nodeId),node=options.getNode(scope,nodeId);
      if(!node) return entry.projectId;
      var current=linked(node);
      if(current!==entry.expectedProjectId){ delete completed[itemKey];return current||entry.projectId; }
      options.apply(node,entry.project);delete completed[itemKey];return entry.projectId;
    }
    function ensure(scope,nodeId,payload,canCreate,expectedProjectId){
      scope=String(scope||'');nodeId=String(nodeId||'');expectedProjectId=expectedProjectId||null;
      var itemKey=key(scope,nodeId);
      if(pending[itemKey]) return pending[itemKey].promise;
      if(completed[itemKey]) return Promise.resolve(consume(scope,nodeId,completed[itemKey]));
      var current=options.getNode(scope,nodeId),projectId=linked(current);
      if(projectId){
        var confirmedIdentity=readIdentity(itemKey);
        if(!confirmedIdentity||confirmedIdentity.coordinator_id!==coordinatorId) clearIdentity(itemKey);
        return Promise.resolve(projectId);
      }
      if(!canCreate) return Promise.reject(new Error('当前画布为只读，无法创建数字人口播项目'));
      var identity=creationIdentity(itemKey,payload);
      var request=Promise.resolve().then(function(){ return options.create(payload,identity.key); }).then(function(project){
        var createdId=project&&(project.id||project.project_id);
        if(!createdId) throw new Error('创建数字人口播项目失败');
        if(discarded[scope]) return createdId;
        var entry={scope:scope,nodeId:nodeId,projectId:createdId,expectedProjectId:expectedProjectId,project:project};
        completed[itemKey]=entry;return consume(scope,nodeId,entry);
      });
      pending[itemKey]={scope:scope,promise:request};
      function clear(){
        if(pending[itemKey]&&pending[itemKey].promise===request) delete pending[itemKey];
        if(discarded[scope]&&!scopePending(scope)) delete discarded[scope];
      }
      request.then(clear,clear);return request;
    }
    function cleanupScope(scope){
      scope=String(scope||'');
      Object.keys(completed).forEach(function(item){ if(completed[item].scope===scope) delete completed[item]; });
      if(scopePending(scope)) discarded[scope]=true;
    }
    return {ensure:ensure,cleanupScope:cleanupScope,hasPending:function(scope,nodeId){return !!pending[key(scope,nodeId)];}};
  }

  function projectPath(id){ return '/api/gen/digital-presenter/project?id='+encodeURIComponent(id); }
  function createClient(apiClient,boardId){
    if(!apiClient||typeof apiClient.json!=='function') throw new Error('digital presenter client requires JSON API');
    function headers(){ return boardId?{'X-Canvas-Board-Id':String(boardId)}:{}; }
    return {
      capability:function(){ return apiClient.json('/api/gen/digital-presenter/capability'); },
      create:function(payload,idempotencyKey){
        var requestHeaders=headers();requestHeaders['Idempotency-Key']=idempotencyKey||newIdempotencyKey();
        return apiClient.json('/api/gen/digital-presenter/projects',{method:'POST',body:payload,headers:requestHeaders});
      },
      get:function(id){ return apiClient.json(projectPath(id),{headers:headers()}); },
      update:function(id,revision,patch){
        return apiClient.json('/api/gen/digital-presenter/project',{method:'PUT',headers:headers(),body:Object.assign({project_id:id,revision:revision},patch)});
      },
      delete:function(id,revision){ return apiClient.json(projectPath(id)+'&revision='+encodeURIComponent(revision),{method:'DELETE',headers:headers()}); }
    };
  }
  function escapeHtml(value){
    return String(value==null?'':value).replace(/[&<>"']/g,function(char){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char];
    });
  }
  function normalizeSettings(settings){
    settings=settings||{};
    var out={};
    if(Object.prototype.hasOwnProperty.call(settings,'title')) out.title=String(settings.title||'').trim().slice(0,80);
    if(Object.prototype.hasOwnProperty.call(settings,'script_text')) out.script_text=String(settings.script_text||'').slice(0,20000);
    if(Object.prototype.hasOwnProperty.call(settings,'ratio')) out.ratio=settings.ratio==='16:9'?'16:9':'9:16';
    if(Object.prototype.hasOwnProperty.call(settings,'target_duration')) out.target_duration=numberInRange(settings.target_duration,30,180,30);
    return out;
  }
  function createWorkspace(options){
    options=options||{};
    var client=options.client||createClient(options.apiClient,options.boardId);
    var doc=options.document===undefined?(typeof document!=='undefined'?document:null):options.document;
    var project=null,error='',loading=true,busy=false,destroyed=false,host=null;
    function ensureAlive(){ if(destroyed) throw new Error('workspace destroyed'); }
    function render(){
      if(loading) return '<div class="nc-digital-presenter-workspace"><p>正在加载项目…</p></div>';
      if(error) return '<div class="nc-digital-presenter-workspace"><header><strong>项目加载失败</strong><button type="button" data-action="close">×</button></header><p>'+escapeHtml(error)+'</p></div>';
      var current=project||{};
      return '<div class="nc-digital-presenter-workspace" data-readonly="'+(!options.canEdit)+'">'+
        '<header><div><small>项目设置</small><h2>'+escapeHtml(current.title||'数字人口播')+'</h2></div><button type="button" data-action="close">×</button></header>'+
        '<section class="nc-digital-presenter-settings"><label>项目名称<input data-field="title" value="'+escapeHtml(current.title||'')+'" '+(!options.canEdit?'disabled':'')+'></label>'+
        '<label>画幅<select data-field="ratio" '+(!options.canEdit?'disabled':'')+'><option value="9:16"'+(current.ratio==='9:16'?' selected':'')+'>9:16</option><option value="16:9"'+(current.ratio==='16:9'?' selected':'')+'>16:9</option></select></label>'+
        '<label>目标时长<input data-field="target_duration" type="number" min="30" max="180" value="'+escapeHtml(current.target_duration||30)+'" '+(!options.canEdit?'disabled':'')+'></label>'+
        '<label class="wide">口播脚本<textarea data-field="script_text" '+(!options.canEdit?'disabled':'')+'>'+escapeHtml(current.script_text||'')+'</textarea></label>'+
        (options.canEdit?'<button type="button" data-action="save">保存项目设置</button>':'')+'</section>'+
        '<section class="nc-digital-presenter-coming"><strong>后续阶段尚未开放</strong><p>脚本分段、素材编排、媒体生成和时间线将在后续阶段提供。</p></section></div>';
    }
    function paint(){ if(host) host.innerHTML=render(); }
    function applyProject(next){
      ensureAlive();project=cloneValue(next);loading=false;error='';paint();
      if(typeof options.onChange==='function') options.onChange(summarizeProject(project));
      return cloneValue(project);
    }
    function load(){
      loading=true;error='';paint();
      return Promise.resolve(client.get(options.projectId)).then(applyProject,function(reason){
        ensureAlive();loading=false;error=reason&&reason.message||'项目加载失败';paint();throw reason;
      });
    }
    function saveSettings(settings){
      try{ ensureAlive(); }catch(reason){ return Promise.reject(reason); }
      if(!options.canEdit) return Promise.reject(new Error('read-only workspace'));
      if(busy) return Promise.reject(new Error('workspace busy'));
      busy=true;
      return Promise.resolve(client.update(options.projectId,project.revision,normalizeSettings(settings))).then(applyProject).finally(function(){ busy=false; });
    }
    function destroy(){
      if(destroyed) return;destroyed=true;
      if(host&&host.parentNode) host.parentNode.removeChild(host);host=null;
    }
    function handleClick(event){
      var action=event.target&&event.target.getAttribute&&event.target.getAttribute('data-action');
      if(action==='close') destroy();
      if(action==='save'&&host){
        var value=function(field){ var element=host.querySelector('[data-field="'+field+'"]');return element&&element.value; };
        saveSettings({title:value('title'),ratio:value('ratio'),target_duration:Number(value('target_duration')),script_text:value('script_text')}).catch(function(reason){ error=reason.message||String(reason);paint(); });
      }
    }
    if(doc&&doc.createElement&&doc.body){
      host=doc.createElement('div');host.className='nc-digital-presenter-host';host.addEventListener('click',handleClick);doc.body.appendChild(host);paint();
    }
    var ready=load();
    return {projectId:options.projectId,ready:ready,render:render,destroy:destroy,reload:load,saveSettings:saveSettings,getProject:function(){return cloneValue(project);}};
  }

  return {
    normalizeNodeParams:normalizeNodeParams,summarizeProject:summarizeProject,
    sanitizeNodeData:sanitizeNodeData,copyNodeData:copyNodeData,creationPayload:creationPayload,
    canRegisterEntry:canRegisterEntry,canOpenNode:canOpenNode,isRoleDowngrade:isRoleDowngrade,
    createEntryRegistrar:createEntryRegistrar,createNodeCreationPolicy:createNodeCreationPolicy,
    createWorkspaceLifecycle:createWorkspaceLifecycle,
    observeWorkspaceReady:observeWorkspaceReady,createProjectCoordinator:createProjectCoordinator,
    creationFingerprint:creationFingerprint,newIdempotencyKey:newIdempotencyKey,
    createClient:createClient,createWorkspace:createWorkspace
  };
});
