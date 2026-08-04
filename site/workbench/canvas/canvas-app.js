/* 节点生产画布：文本→反推→作图 节点图，拖动/连线/调参/并行运行。复用 /api/gen/reverse|banana|image + 轮询。 */
(function(){
  var graphApi=window.HQCanvas&&window.HQCanvas.graph;
  var stateApi=window.HQCanvas&&window.HQCanvas.state;
  var storageApi=window.HQCanvas&&window.HQCanvas.storage;
  var apiModule=window.HQCanvas&&window.HQCanvas.api;
  var agentModule=window.HQCanvas&&window.HQCanvas.agent;
  var shortDramaModule=window.HQCanvas&&window.HQCanvas.shortDrama;
  var digitalPresenterModule=window.HQCanvas&&window.HQCanvas.digitalPresenter;
  var canvasExporter=window.HQCanvas&&window.HQCanvas.exporter;
  var canvasStorage=storageApi.createStorage({storage:function(){return window.localStorage;}});
  var apiClient=apiModule.createClient({fetchImpl:window.fetch.bind(window),tokenProvider:tok,AbortControllerImpl:window.AbortController,setTimeoutImpl:setTimeout,clearTimeoutImpl:clearTimeout});
  var wrap=document.querySelector('.nc-wrap'), canvasShell=document.querySelector('.nc-canvas-shell'), inner=document.getElementById('ncInner'), svg=document.getElementById('ncEdges'), canvas=document.getElementById('ncCanvas'), empty=document.getElementById('ncEmpty'), storyboard=document.getElementById('ncStoryboard'), storyGrid=document.getElementById('ncStoryGrid'), storyMeta=document.getElementById('ncStoryMeta'), selectionBox=document.getElementById('ncSelectionBox'), selectedRegion=document.getElementById('ncSelectedRegion'), guideX=document.getElementById('ncGuideX'), guideY=document.getElementById('ncGuideY');
  var boardHome=document.getElementById('ncBoardHome'), editorView=document.getElementById('ncEditorView'), boardGrid=document.getElementById('ncBoardGrid'), boardSearch=document.getElementById('ncBoardSearch'), boardSort=document.getElementById('ncBoardSort'), backHomeBtn=document.getElementById('ncBackHome');
  var nodeCountEl=document.getElementById('ncNodeCount'), edgeCountEl=document.getElementById('ncEdgeCount'), runStateEl=document.getElementById('ncRunState');
  var undoBtn=document.getElementById('ncUndo'), redoBtn=document.getElementById('ncRedo'), fullscreenBtn=document.getElementById('ncFullscreen'), zoomLabel=document.getElementById('ncZoomLabel'), map=document.getElementById('ncMap'), mapSvg=document.getElementById('ncMapSvg');
  var runAllBtn=document.getElementById('ncRunAll');
  var fsAgent=document.getElementById('ncFsAgent'), fsAdd=document.getElementById('ncFsAdd'), fsUndo=document.getElementById('ncFsUndo'), fsRedo=document.getElementById('ncFsRedo'), fsZoomOut=document.getElementById('ncFsZoomOut'), fsZoomIn=document.getElementById('ncFsZoomIn'), fsZoomLabel=document.getElementById('ncFsZoomLabel'), fsFit=document.getElementById('ncFsFit'), fsMore=document.getElementById('ncFsMore'), fsRun=document.getElementById('ncFsRun'), fsExit=document.getElementById('ncFsExit'), saveStateEl=document.getElementById('ncSaveState'), onlineStateEl=document.getElementById('ncOnlineState');
  var fsTplMenu=document.getElementById('ncFsTplMenu');
  var tplSelect=document.getElementById('ncTemplateSelect'), tplName=document.getElementById('ncTemplateName'), tplImportFile=document.getElementById('ncTplImportFile'), menu=document.getElementById('ncMenu'), cleanupStorageBtn=document.getElementById('ncCleanupStorage');
  var sidePanel=document.getElementById('ncSidePanel'), sideTitle=document.getElementById('ncSideTitle'), sideBody=document.getElementById('ncSideBody'), sideClose=document.getElementById('ncSideClose');
  var sideTplMenu=document.getElementById('ncSideTplMenu'), sideMore=document.getElementById('ncSideMore');
  if(empty&&empty.parentNode!==inner) inner.appendChild(empty);
  var nodes={}, edges=[], nid=0, pendingPort=null, runLabel='就绪', history=stateApi.createHistory({limit:60}), restoring=false, loading=false, dragPort=null, suppressPortClick=false, suppressCanvasClick=false, mapDirty=false, saveTimer=null;
  var localFullscreen=false, canvasView='workflow';
  var selectedNode=null, selectedNodes={}, selectedEdge=-1, clipNode=null, zoom=1, imageToolbar=null;
  var RUN_ALL_REMOTE_LIMIT=2, RUN_ALL_RETRY_MS=4000, runAllBatch=null, runAllRetryTimer=null;
  var activeSidePanel='', accountAssetsLoaded=false, accountAssets=[], accountAssetsPromise=null, dragAsset=null;
  var agentSessions={};
  var agentIP12Context=null, agentIP12Loaded=false, agentIP12Loading=false, agentIP12Error='';
  var VIDEO_POINTS_PER_SECOND=30;
  var videoAvatars=[], videoAvatarsLoaded=false, videoAvatarsPromise=null, videoAvatarsError='';
  var digitalPresenterEntryEnabled=false;
  var digitalPresenterWorkspaceLifecycle=digitalPresenterModule.createWorkspaceLifecycle();
  var currentBoardId=null, boardMode='mine', boardLastSeenUpdatedAt=0, boardConflict=false;
  var currentBoardScope='local', currentCollabVersion=0, currentCollabRole='', currentCollabName='', currentCollabMembers=[];
  var nodeCreationPolicy=digitalPresenterModule.createNodeCreationPolicy({context:function(){
    return {canEdit:canEditCanvas(),scope:currentBoardScope,entryEnabled:digitalPresenterEntryEnabled};
  }});
  var collabBoards=[], collabLoaded=false, collabLoading=false, collabError='', collabErrorHint='', collabCreating=false, collabSaving=false, collabQueuedSnap=null;
  var collabSync=window.HQCanvasCollabSync||null, collabController=null, collabBaseSnap=null, collabPollTimer=null, collabPresenceTimer=null, collabRetryCount=0;
  var collabSyncGeneration=0, collabPendingBatch=null;
  var collabClientId=(function(){
    var key='hq_canvas_collab_client_id', value='';
    try{ value=sessionStorage.getItem(key)||''; }catch(e){}
    if(!value) value=(window.crypto&&crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2));
    try{ sessionStorage.setItem(key,value); }catch(e){}
    return value;
  })();
  var collabNodeSeed='node'+(window.crypto&&window.crypto.randomUUID?window.crypto.randomUUID().replace(/-/g,''):Date.now().toString(36)+Math.random().toString(36).slice(2,14));
  function normalizeShortDramaNodeParams(input){
    return shortDramaModule.normalizeNodeParams(input);
  }
  function shortDramaNodeOutputs(node){
    return node&&node.type==='shortDrama'?{}:node&&node.type==='digitalPresenter'?{}:stateApi.cloneSnapshot(node&&node.outputs||{});
  }
  function destroyShortDramaWorkspace(node){
    if(!node||!node.shortDramaWorkspace) return;
    var workspace=node.shortDramaWorkspace;
    node.shortDramaWorkspace=null;
    if(workspace.destroy) workspace.destroy();
  }
  function destroyAllShortDramaWorkspaces(){
    Object.keys(nodes).forEach(function(id){ destroyShortDramaWorkspace(nodes[id]); });
  }
  function setCurrentCollabRole(role){
    var previousRole=currentCollabRole;
    currentCollabRole=role||currentCollabRole;
    if(shortDramaModule.isRoleDowngrade(previousRole,currentCollabRole)) destroyAllShortDramaWorkspaces();
    if(digitalPresenterModule.isRoleDowngrade(previousRole,currentCollabRole)){
      digitalPresenterWorkspaceLifecycle.roleChanged(previousRole,currentCollabRole);
      clearDigitalPresenterWorkspaceRefs();
    }
    return currentCollabRole;
  }
  function refreshShortDramaNode(node){
    if(!node||node.type!=='shortDrama'||!node.el) return;
    if(node.shortDramaWorkspace&&node.shortDramaWorkspace.projectId!==node.params.project_id){
      destroyShortDramaWorkspace(node);
    }
    refreshNodeMeta(node);
    var ratio=node.el.querySelector('[data-f="shortDramaRatio"]');
    var stage=node.el.querySelector('[data-f="shortDramaStage"]');
    var progress=node.el.querySelector('[data-f="shortDramaProgress"]');
    var points=node.el.querySelector('[data-f="shortDramaPoints"]');
    if(ratio) ratio.textContent=node.params.ratio+' · '+node.params.target_duration+'秒';
    if(stage) stage.textContent=node.params.stage;
    if(progress) progress.textContent=node.params.progress+'%';
    if(points) points.textContent=node.params.spent_points+' / '+node.params.estimated_points+' 点';
  }
  function applyShortDramaSummary(node,summary){
    if(!node||!summary||typeof summary!=='object') return;
    node.params=normalizeShortDramaNodeParams(Object.assign({},node.params,summary));
    node.outputs={};
    refreshShortDramaNode(node);
    scheduleSave();
  }
  function shortDramaScopeKey(scope,boardId){
    return String(scope||'local')+':'+String(boardId||'draft');
  }
  function currentShortDramaScopeKey(){
    return shortDramaScopeKey(currentBoardScope,currentBoardId);
  }
  function shortDramaNodeForScope(scopeKey,nodeId){
    if(!wrap||!wrap.classList.contains('editing')) return null;
    if(scopeKey!==currentShortDramaScopeKey()) return null;
    var node=nodes[nodeId];
    return node&&node.type==='shortDrama'?node:null;
  }
  function applyShortDramaOpenPolicy(scopeKey,nodeId){
    var node=shortDramaNodeForScope(scopeKey,nodeId);
    var openShortDrama=node&&node.el&&node.el.querySelector('[data-f="openShortDrama"]');
    if(openShortDrama) openShortDrama.disabled=!shortDramaModule.canOpenNode(node.params,canEditCanvas());
  }
  var shortDramaProjectCoordinator=shortDramaModule.createProjectCoordinator({
    getNode:function(scopeKey,nodeId){ return shortDramaNodeForScope(scopeKey,nodeId); },
    create:function(payload){
      var headers=currentBoardScope==='collab'?{'X-Canvas-Board-Id':String(currentBoardId)}:{};
      return apiClient.json('/api/gen/short-drama/projects',{method:'POST',body:payload,headers:headers});
    },
    apply:function(node,project){ applyShortDramaSummary(node,project); }
  });
  function ensureShortDramaProject(node,scopeKey){
    if(!node.params.project_id&&canEditCanvas()) setNodeState(node,'running','正在创建短剧项目…','#2dd4bf');
    var payload=shortDramaModule.creationPayload(node.params);
    if(currentBoardScope==='collab') payload.board_id=currentBoardId;
    return shortDramaProjectCoordinator.ensure(scopeKey,node.id,payload,canEditCanvas(),node.params.project_id||null);
  }
  function openShortDramaWorkspace(node){
    if(!shortDramaModule||typeof shortDramaModule.createWorkspace!=='function'){
      setNodeState(node,'error','短剧工作区未加载','#f4708a');
      return Promise.reject(new Error('短剧工作区未加载'));
    }
    var scopeKey=currentShortDramaScopeKey();
    var nodeId=node.id;
    var button=node.el&&node.el.querySelector('[data-f="openShortDrama"]');
    if(button) button.disabled=true;
    return ensureShortDramaProject(node,scopeKey).then(function(projectId){
      node=shortDramaNodeForScope(scopeKey,nodeId);
      if(!node) return null;
      button=node.el&&node.el.querySelector('[data-f="openShortDrama"]');
      var canEdit=canEditCanvas();
      var onChange=function(summary){
        var current=shortDramaNodeForScope(scopeKey,nodeId);
        if(!current||current.params.project_id!==projectId) return;
        applyShortDramaSummary(current,summary);
      };
      destroyShortDramaWorkspace(node);
      node.shortDramaWorkspace=shortDramaModule.createWorkspace({
        projectId:projectId,
        apiClient:apiClient,
        poll:apiModule.poll,
        boardId:currentBoardScope==='collab'?currentBoardId:null,
        canEdit:canEdit,
        onChange:onChange,
        onDelete:function(){
          var current=shortDramaNodeForScope(scopeKey,nodeId);
          if(!current||current.params.project_id!==projectId) return;
          destroyShortDramaWorkspace(current);
          if(current.el) current.el.remove();
          delete nodes[nodeId];
          edges=edges.filter(function(edge){ return edge.from.node!==nodeId&&edge.to.node!==nodeId; });
          if(selectedNode===nodeId) selectedNode=null;
          delete selectedNodes[nodeId];
          redraw();refreshAllGenRefs();updateSelectedRegion();scheduleSave();
          updateState('短剧项目已删除');
        }
      });
      setNodeState(node,'done','短剧工作区已打开','#2bd576');
      return node.shortDramaWorkspace;
    }).catch(function(error){
      var current=shortDramaNodeForScope(scopeKey,nodeId);
      if(current) setNodeState(current,'error',error&&error.message||'打开短剧工作区失败','#f4708a');
      throw error;
    }).finally(function(){ applyShortDramaOpenPolicy(scopeKey,nodeId); });
  }
  function normalizeDigitalPresenterNodeParams(input){ return digitalPresenterModule.normalizeNodeParams(input); }
  function destroyDigitalPresenterWorkspace(node){
    if(!node||!node.digitalPresenterWorkspace) return;
    var workspace=node.digitalPresenterWorkspace;
    node.digitalPresenterWorkspace=null;
    if(!digitalPresenterWorkspaceLifecycle.removeWorkspace(workspace)&&workspace.destroy) workspace.destroy();
  }
  function clearDigitalPresenterWorkspaceRefs(){
    Object.keys(nodes).forEach(function(id){ if(nodes[id]) nodes[id].digitalPresenterWorkspace=null; });
  }
  function destroyAllDigitalPresenterWorkspaces(){
    digitalPresenterWorkspaceLifecycle.destroyAll();clearDigitalPresenterWorkspaceRefs();
  }
  function digitalPresenterNodeForScope(scopeKey,nodeId){
    if(!wrap||!wrap.classList.contains('editing')) return null;
    if(scopeKey!==currentShortDramaScopeKey()) return null;
    var node=nodes[nodeId];
    return node&&node.type==='digitalPresenter'?node:null;
  }
  function refreshDigitalPresenterNode(node){
    if(!node||node.type!=='digitalPresenter'||!node.el) return;
    if(node.digitalPresenterWorkspace&&node.digitalPresenterWorkspace.projectId!==node.params.project_id) destroyDigitalPresenterWorkspace(node);
    refreshNodeMeta(node);
    var ratio=node.el.querySelector('[data-f="digitalPresenterRatio"]');
    var stage=node.el.querySelector('[data-f="digitalPresenterStage"]');
    var progress=node.el.querySelector('[data-f="digitalPresenterProgress"]');
    var points=node.el.querySelector('[data-f="digitalPresenterPoints"]');
    if(ratio) ratio.textContent=node.params.ratio+' · '+node.params.target_duration+'秒';
    if(stage) stage.textContent=node.params.stage;
    if(progress) progress.textContent=node.params.progress+'%';
    if(points) points.textContent=node.params.spent_points+' / '+node.params.estimated_points+' 点';
  }
  function applyDigitalPresenterSummary(node,summary){
    if(!node||!summary||typeof summary!=='object') return;
    node.params=normalizeDigitalPresenterNodeParams(Object.assign({},node.params,summary));
    node.outputs={};refreshDigitalPresenterNode(node);scheduleSave();
  }
  var digitalPresenterProjectCoordinator=digitalPresenterModule.createProjectCoordinator({
    getNode:function(scopeKey,nodeId){ return digitalPresenterNodeForScope(scopeKey,nodeId); },
    create:function(payload,idempotencyKey){
      var headers=currentBoardId?{'X-Canvas-Board-Id':String(currentBoardId)}:{};
      headers['Idempotency-Key']=idempotencyKey;
      return apiClient.json('/api/gen/digital-presenter/projects',{method:'POST',body:payload,headers:headers});
    },
    apply:function(node,project){ applyDigitalPresenterSummary(node,project); }
  });
  function applyDigitalPresenterOpenPolicy(scopeKey,nodeId){
    var node=digitalPresenterNodeForScope(scopeKey,nodeId);
    var button=node&&node.el&&node.el.querySelector('[data-f="openDigitalPresenter"]');
    if(button) button.disabled=!digitalPresenterModule.canOpenNode(node.params,canEditCanvas()&&currentBoardScope==='collab');
  }
  function ensureDigitalPresenterProject(node,scopeKey){
    var canCreate=canEditCanvas()&&currentBoardScope==='collab'&&!!currentBoardId;
    if(!node.params.project_id&&canCreate) setNodeState(node,'running','正在创建数字人口播项目…','#2dd4bf');
    return digitalPresenterProjectCoordinator.ensure(scopeKey,node.id,digitalPresenterModule.creationPayload(node.params),canCreate,node.params.project_id||null);
  }
  function openDigitalPresenterWorkspace(node){
    var scopeKey=currentShortDramaScopeKey(),nodeId=node.id;
    if(!digitalPresenterModule||typeof digitalPresenterModule.createWorkspace!=='function') return Promise.reject(new Error('数字人口播工作区未加载'));
    return ensureDigitalPresenterProject(node,scopeKey).then(function(projectId){
      node=digitalPresenterNodeForScope(scopeKey,nodeId);if(!node) return null;
      destroyDigitalPresenterWorkspace(node);
      var workspace=digitalPresenterModule.createWorkspace({
        projectId:projectId,apiClient:apiClient,boardId:currentBoardId,
        canEdit:canEditCanvas(),onChange:function(summary){
          var current=digitalPresenterNodeForScope(scopeKey,nodeId);
          if(current&&current.params.project_id===projectId) applyDigitalPresenterSummary(current,summary);
        }
      });
      node.digitalPresenterWorkspace=workspace;
      digitalPresenterWorkspaceLifecycle.attach(scopeKey,nodeId,workspace);
      return digitalPresenterModule.observeWorkspaceReady(workspace,{
        isActive:function(){
          var current=digitalPresenterNodeForScope(scopeKey,nodeId);
          return !!current&&current.digitalPresenterWorkspace===workspace;
        },
        onReady:function(){
          var current=digitalPresenterNodeForScope(scopeKey,nodeId);
          if(current) setNodeState(current,'done','数字人口播工作区已打开','#2bd576');
        },
        onError:function(error){
          var current=digitalPresenterNodeForScope(scopeKey,nodeId);
          if(current) setNodeState(current,'error',error&&error.message||'打开数字人口播工作区失败','#f4708a');
        }
      });
    }).catch(function(error){
      var current=digitalPresenterNodeForScope(scopeKey,nodeId);
      if(current) setNodeState(current,'error',error&&error.message||'打开数字人口播工作区失败','#f4708a');
      return null;
    }).finally(function(){ applyDigitalPresenterOpenPolicy(scopeKey,nodeId); });
  }
  function tok(){ return '__cookie__'; }
  function authJson(path, opts){
    return apiClient.json(path,opts).catch(function(error){
      if(error&&error.code==='timeout') error.message='协作服务响应超时';
      throw error;
    });
  }
  function playableAssetUrl(u){
    u=String(u||'');
    if(!u||u.indexOf('/api/gen/file/')!==0) return Promise.resolve(u);
    return apiClient.asset(u+(u.indexOf('?')>=0?'&':'?')+'_='+Date.now())
      .then(function(blob){ return URL.createObjectURL(blob); });
  }
  function renderVideoResult(node,url){
    if(!node||!node.el||!url) return;
    var box=node.el.querySelector('[data-f="videoResult"]');
    if(!box) return;
    box.style.display='block';
    box.innerHTML='<div class="nc-note" style="padding:12px;">视频加载中...</div>';
    playableAssetUrl(url).then(function(src){
      if(!nodes[node.id]||!node.el) return;
      box.innerHTML='<video controls playsinline preload="metadata" src="'+escapeHtml(src)+'"></video>';
    }).catch(function(){
      box.innerHTML='<a class="nc-mini" href="'+escapeHtml(url)+'" target="_blank" rel="noopener">打开视频</a>';
    });
  }
  var CANVAS_BASE_W=8000, CANVAS_BASE_H=5000, CANVAS_GROW_PAD=1200, CANVAS_VIEW_PAD=1200;
  var IMAGE_SAVE_MAX=1280, IMAGE_SAVE_QUALITY=.82;
  var TYPE={
    text:  {name:'文本 · 提示词', color:'#e7b24c', outs:['prompt']},
    image: {name:'图片 · 素材',   color:'#46b4ff', outs:['image']},
    reverse:{name:'提示词反推',   color:'#8a5cf6', ins:['image'], outs:['prompt']},
    gen:   {name:'作图',          color:'#2bd576', ins:['prompt','image'], outs:['image']},
    video: {name:'生视频',        color:'#f472b6', ins:['prompt','image'], outs:['video']},
    videoAsset:{name:'视频 · 素材',color:'#f472b6', outs:['video']},
    shortDrama:{name:'短剧项目', color:'#f59e0b'},
    digitalPresenter:{name:'数字人口播', color:'#e7b24c'}
  };
  function syncRunAllDisabled(){
    var disabled=!canEditCanvas()||runAllExecutableNodes().length===0||!!runAllBatch;
    if(runAllBtn) runAllBtn.disabled=disabled;
    if(fsRun) fsRun.disabled=disabled;
  }
  function updateState(label){
    if(label) runLabel=label;
    var count=Object.keys(nodes).length;
    if(nodeCountEl) nodeCountEl.textContent=count;
    if(edgeCountEl) edgeCountEl.textContent=edges.length;
    if(runStateEl) runStateEl.textContent=runLabel;
    if(empty) empty.classList.toggle('on', count===0);
    if(undoBtn) undoBtn.disabled=!canEditCanvas()||!history.canUndo();
    if(redoBtn) redoBtn.disabled=!canEditCanvas()||!history.canRedo();
    if(fsUndo) fsUndo.disabled=!canEditCanvas()||!history.canUndo();
    if(fsRedo) fsRedo.disabled=!canEditCanvas()||!history.canRedo();
    syncRunAllDisabled();
    syncZoomInputs();
    updateImageToolbar();
    if(canvasView==='story') renderStoryboard();
    if(activeSidePanel&&activeSidePanel!=='agent') renderSidePanel();
    scheduleSave();
  }
  function setNodeState(node,state,msg,color){
    if(!node) return;
    if(state) node.el.setAttribute('data-state',state); else node.el.removeAttribute('data-state');
    if(msg!=null) noteOf(node,msg,color);
  }
  function snapshot(){
    return {
      nid:nid,
      runLabel:runLabel,
      zoom:zoom,
      scroll:{left:canvas?canvas.scrollLeft:0,top:canvas?canvas.scrollTop:0},
      edges:stateApi.cloneSnapshot(edges),
      nodes:Object.keys(nodes).map(function(k){
        var n=nodes[k];
        return {id:n.id,type:n.type,x:n.x,y:n.y,width:n.width||null,height:n.height||null,collapsed:n.el?n.el.classList.contains('collapsed'):!!n.collapsed,params:stateApi.cloneSnapshot(n.type==='shortDrama'?normalizeShortDramaNodeParams(n.params):n.type==='digitalPresenter'?normalizeDigitalPresenterNodeParams(n.params):n.params||{}),outputs:shortDramaNodeOutputs(n),image:n.image||null,state:n.el?n.el.getAttribute('data-state')||'':n.state||'',note:n.el?(n.el.querySelector('[data-f="note"]')||{}).textContent||'':n.note||''};
      })
    };
  }
  function templateSnapshot(){
    var snap=snapshot();
    snap.nodes=(snap.nodes||[]).map(function(n){
      var x=stateApi.cloneSnapshot(n);
      x.image=null;
      if(x.outputs&&x.outputs.image) delete x.outputs.image;
      if(x.outputs&&x.outputs.video) delete x.outputs.video;
      if(x.outputs&&x.outputs.video_url) delete x.outputs.video_url;
      if(x.type==='image'&&x.outputs) delete x.outputs.image;
      return x;
    });
    snap.scroll={left:0,top:0};
    snap.zoom=1;
    snap.runLabel='模板';
    return snap;
  }
  function scheduleSave(){
    if(loading) return;
    if(!canEditCanvas()){ if(currentBoardScope==='collab') setSaveState('readonly'); return; }
    if(boardConflict){ setSaveState('conflict'); return; }
    if(currentBoardId) setSaveState('saving');
    clearTimeout(saveTimer);
    saveTimer=setTimeout(saveDraft,220);
  }
  function saveDraft(){
    if(loading) return;
    var snap=snapshot();
    if(currentBoardScope==='collab'){
      saveCollabDraft(snap);
      return;
    }
    var ok=true;
    if(currentBoardId){
      canvasStorage.removeDraft();
    }else{
      if(!canvasStorage.saveDraft(snap).ok) ok=false;
    }
    if(!saveCurrentBoard(snap)) ok=false;
    if(boardConflict) setSaveState('conflict');
    else setSaveState(ok?'saved':'error');
  }
  function loadDraft(){
    var loaded=canvasStorage.loadDraft(), snap=loaded.ok?loaded.value:null;
    return snap&&snap.nodes?snap:null;
  }
  function getBoards(){
    var loaded=canvasStorage.loadBoards();
    return loaded.ok?(loaded.value||[]):[];
  }
  function setBoards(list){
    return canvasStorage.saveBoards(list).ok;
  }
  function cleanupSavedBoardOutputs(){
    var list=getBoards(), changed=false;
    list.forEach(function(board){
      if(board&&board.data){
        board.data=storageApi.stripHeavyOutputs(board.data);
        changed=true;
      }
    });
    canvasStorage.removeDraft();
    return changed?setBoards(list):true;
  }
  function compressImageSource(src){
    return new Promise(function(resolve,reject){
      if(!src||String(src).indexOf('data:image/')!==0){ resolve(src); return; }
      var im=new Image();
      im.onload=function(){
        var maxSide=Math.max(im.width,im.height), scale=Math.min(1,IMAGE_SAVE_MAX/maxSide);
        var cv=document.createElement('canvas');
        cv.width=Math.max(1,Math.round(im.width*scale));
        cv.height=Math.max(1,Math.round(im.height*scale));
        var ctx=cv.getContext('2d');
        ctx.fillStyle='#fff';
        ctx.fillRect(0,0,cv.width,cv.height);
        ctx.drawImage(im,0,0,cv.width,cv.height);
        var jpg=cv.toDataURL('image/jpeg',IMAGE_SAVE_QUALITY);
        resolve(jpg.length<String(src).length?jpg:src);
      };
      im.onerror=function(){ reject(new Error('图片压缩失败')); };
      im.src=src;
    });
  }
  function setRunAllBusy(on){
    [[runAllBtn,'运行中…'],[fsRun,'运行中…']].forEach(function(pair){
      var btn=pair[0]; if(!btn) return;
      if(!btn.dataset.idleLabel) btn.dataset.idleLabel=btn.textContent;
      btn.disabled=!!on;
      btn.style.opacity=on?'.72':'1';
      btn.style.cursor=on?'wait':'pointer';
      btn.textContent=on?pair[1]:btn.dataset.idleLabel;
    });
  }
  function clearRunAllRetry(){
    if(runAllRetryTimer){ clearTimeout(runAllRetryTimer); runAllRetryTimer=null; }
  }
  function scheduleRunAllRetry(delay){
    if(!runAllBatch) return;
    clearRunAllRetry();
    runAllRetryTimer=setTimeout(function(){
      runAllRetryTimer=null;
      if(runAllBatch&&runAllBatch.tick) runAllBatch.tick();
    }, Math.max(800, delay||RUN_ALL_RETRY_MS));
  }
  function clearNodeGeneratedOutput(node){
    if(!node||!node.outputs) return;
    if(node.type==='reverse'){
      delete node.outputs.prompt;
      var out=node.el&&node.el.querySelector('[data-f="out"]');
      if(out) out.value='';
    }
    if(node.type==='gen'){
      delete node.outputs.image;
      var box=node.el&&node.el.querySelector('[data-f="result"]');
      if(box){ box.style.display='none'; box.style.backgroundImage=''; box.innerHTML=''; }
    }
    if(node.type==='video'){
      delete node.outputs.video;
      delete node.outputs.video_url;
      var videoBox=node.el&&node.el.querySelector('[data-f="videoResult"]');
      if(videoBox){ videoBox.style.display='none'; videoBox.innerHTML=''; }
    }
  }
  function resetNodeForRunAll(node){
    if(!node || (node.type!=='reverse' && node.type!=='gen' && node.type!=='video')) return;
    clearNodeGeneratedOutput(node);
    setNodeState(node, null, '待运行', '#5c6b82');
  }
  function isRunAllRemoteNode(node){
    return !!(node && (node.type==='gen' || node.type==='video'));
  }
  function makeRunNodeError(message, opts){
    var err=new Error(message||'执行失败'), meta=opts||{};
    err.code=meta.code||'';
    err.retryable=!!meta.retryable;
    err.retryAfterMs=meta.retryAfterMs||0;
    return err;
  }
  function refreshVideoNodeHint(node){
    if(!node || node.type!=='video' || !node.el) return;
    var warn=node.el.querySelector('[data-f="videoWarn"]');
    if(warn) warn.style.display=((node.params.channel||'grok')==='grok')?'block':'none';
  }
  function normalizeVideoNodeError(node, err){
    var msg=String((err&&err.message)||err||'生成失败');
    var channel=node&&node.params&&node.params.channel||'grok';
    if(channel==='grok' && /(HTTP\s*404|当前模型暂无|暂无支持该视频参数的可用渠道|渠道不支持当前视频尺寸)/i.test(msg)){
      return '果肉视频当前仅 16:9（横屏）生成更稳定，请切到 16:9 后重试';
    }
    return msg;
  }
  function cleanupLocalSpace(){
    if(!canEditCanvas()) return;
    if(loading) return;
    var before=snapshot(), jobs=[];
    setSaveState('saving');
    Object.keys(nodes).forEach(function(id){
      var node=nodes[id];
      clearNodeGeneratedOutput(node);
      if(node&&node.type==='image'&&node.image&&String(node.image).indexOf('data:image/')===0){
        jobs.push(compressImageSource(node.image).then(function(next){
          node.image=next;
          node.outputs=node.outputs||{};
          node.outputs.image=next;
          var d=node.el&&node.el.querySelector('[data-f="drop"]');
          if(d){ d.style.backgroundImage='url('+next+')'; d.innerHTML=''; }
        }));
      }
    });
    Promise.all(jobs).then(function(){
      pushUndo(before);
      refreshAllGenRefs();
      cleanupSavedBoardOutputs();
      saveDraft();
      updateState('本地空间已清理');
    }).catch(function(){
      saveDraft();
      updateState('清理空间失败');
    });
  }
  function makeBoardId(){
    return 'b'+Date.now().toString(36)+Math.random().toString(36).slice(2,7);
  }
  function boardUrl(id){
    var url=new URL(window.location.href);
    url.searchParams.set('board',id);
    url.searchParams.delete('collab');
    return url.pathname+url.search+url.hash;
  }
  function collabBoardUrl(id){
    var url=new URL(window.location.href);
    url.searchParams.delete('board');
    url.searchParams.set('collab',id);
    return url.pathname+url.search+url.hash;
  }
  function openBoardInNewTab(id){
    var a=document.createElement('a');
    a.href=boardUrl(id);
    a.target='_blank';
    a.rel='noopener';
    a.style.display='none';
    document.body.appendChild(a);
    a.click();
    setTimeout(function(){ a.remove(); },0);
  }
  function openCollabBoardInNewTab(id){
    if(!id) return;
    var a=document.createElement('a');
    a.href=collabBoardUrl(id);
    a.target='_blank';
    a.rel='noopener';
    a.style.display='none';
    document.body.appendChild(a);
    a.click();
    setTimeout(function(){ a.remove(); },0);
  }
  function boardIdFromUrl(){
    try{ return new URLSearchParams(window.location.search).get('board')||''; }catch(e){ return ''; }
  }
  function collabIdFromUrl(){
    try{ return new URLSearchParams(window.location.search).get('collab')||''; }catch(e){ return ''; }
  }
  function clearBoardParam(){
    if(!window.history||!window.history.replaceState) return;
    var url=new URL(window.location.href);
    if(!url.searchParams.has('board')&&!url.searchParams.has('collab')) return;
    url.searchParams.delete('board');
    url.searchParams.delete('collab');
    window.history.replaceState(null,'',url.pathname+url.search+url.hash);
  }
  function setBoardParam(id){
    if(!window.history||!window.history.replaceState||!id) return;
    var url=new URL(window.location.href);
    url.searchParams.delete('collab');
    url.searchParams.set('board',id);
    window.history.replaceState(null,'',url.pathname+url.search+url.hash);
  }
  function setCollabParam(id){
    if(!window.history||!window.history.replaceState||!id) return;
    var url=new URL(window.location.href);
    url.searchParams.delete('board');
    url.searchParams.set('collab',id);
    window.history.replaceState(null,'',url.pathname+url.search+url.hash);
  }
  function emptySnapshot(){
    return {nid:0,runLabel:'就绪',zoom:1,scroll:{left:0,top:0},edges:[],nodes:[]};
  }
  function formatBoardTime(ts){
    if(!ts) return '更新时间：--';
    try{ return '更新时间：'+new Date(ts).toLocaleString('zh-CN',{hour12:false}); }catch(e){ return '更新时间：--'; }
  }
  function isUntitledBoardName(name){
    return !name||String(name).trim()==='未命名画布';
  }
  function cleanBoardName(text){
    return String(text||'').replace(/\s+/g,' ').trim().slice(0,28);
  }
  function inferBoardName(snap){
    var list=(snap&&snap.nodes)||[];
    for(var i=0;i<list.length;i++){
      var n=list[i]||{}, p=n.params||{}, o=n.outputs||{};
      var text=cleanBoardName(p.title||p.text||o.prompt);
      if(text) return text;
    }
    return '';
  }
  function boardPreview(board){
    var snap=board&&board.data, list=(snap&&snap.nodes)||[];
    for(var i=0;i<list.length;i++){
      var n=list[i]||{};
      var img=n.image||(n.outputs&&n.outputs.image);
      if(img) return img;
    }
    return '';
  }
  function saveCurrentBoard(snap){
    if(loading) return true;
    if(currentBoardScope==='collab') return true;
    if(!currentBoardId) return true;
    var list=getBoards(), idx=list.findIndex(function(b){ return b.id===currentBoardId; });
    if(idx<0) return false;
    if(boardConflict) return false;
    var latestAt=list[idx].updatedAt||0;
    if(boardLastSeenUpdatedAt && latestAt && latestAt!==boardLastSeenUpdatedAt){
      boardConflict=true;
      setSaveState('conflict');
      updateState('另一个标签页已更新，请刷新后继续编辑');
      return false;
    }
    list[idx].data=snap||snapshot();
    list[idx].updatedAt=Date.now();
    if(isUntitledBoardName(list[idx].name)){
      var nextName=inferBoardName(list[idx].data);
      if(nextName) list[idx].name=nextName;
    }
    if(!setBoards(list)) return false;
    boardLastSeenUpdatedAt=list[idx].updatedAt||0;
    return true;
  }
  function setSaveState(state){
    if(!saveStateEl) return;
    saveStateEl.classList.remove('saving','saved','error','conflict','warning');
    if(state==='saving'){ saveStateEl.textContent='保存中'; saveStateEl.classList.add('saving'); return; }
    if(state==='syncing'){ saveStateEl.textContent='正在同步'; saveStateEl.classList.add('saving'); return; }
    if(state==='reconnecting'){ saveStateEl.textContent='离线重连'; saveStateEl.classList.add('error'); return; }
    if(state==='conflict'){ saveStateEl.textContent='有冲突'; saveStateEl.classList.add('error','conflict'); return; }
    if(state==='error'){ saveStateEl.textContent=currentBoardScope==='collab'?'同步失败':'保存失败'; saveStateEl.classList.add('error'); return; }
    if(state==='readonly'){ saveStateEl.textContent='只读'; saveStateEl.classList.add('warning'); return; }
    saveStateEl.textContent=currentBoardScope==='collab'?'已同步':'已保存';
    saveStateEl.classList.add('saved');
  }
  function canEditCanvas(){
    return collabSync?collabSync.canEditCanvas(currentBoardScope,currentCollabRole):(currentBoardScope!=='collab'||currentCollabRole==='owner'||currentCollabRole==='editor');
  }
  function collabCanEdit(){
    return currentBoardScope==='collab'&&canEditCanvas();
  }
  function setCollabOnlineCount(count){
    if(!onlineStateEl) return;
    var visible=currentBoardScope==='collab'&&currentBoardId;
    onlineStateEl.hidden=!visible;
    if(visible) onlineStateEl.textContent=Math.max(1,Number(count)||1)+' 人在线';
  }
  function stopCollabSync(){
    clearTimeout(collabPollTimer);
    clearInterval(collabPresenceTimer);
    collabPollTimer=collabPresenceTimer=null;
    if(currentBoardScope==='collab'){
      clearTimeout(saveTimer);
      saveTimer=null;
      loading=false;
    }
    if(collabController) collabController.stop();
    else collabSyncGeneration++;
    collabSaving=false;
    collabQueuedSnap=null;
    collabPendingBatch=null;
    collabBaseSnap=null;
    collabRetryCount=0;
    if(onlineStateEl) onlineStateEl.hidden=true;
  }
  function scheduleCollabPoll(delay){
    clearTimeout(collabPollTimer);
    if(currentBoardScope!=='collab'||!currentBoardId) return;
    collabPollTimer=setTimeout(pollCollabOps,delay==null?(collabSync?collabSync.pollDelay(document.hidden):800):delay);
  }
  function flushActiveCollabTitle(){
    var active=document.activeElement;
    if(!active||!active.closest||active.getAttribute('data-f')!=='headTitle'||active.getAttribute('contenteditable')!=='true') return false;
    var nodeEl=active.closest('.nc-node');
    var nodeId=nodeEl&&nodeEl.getAttribute('data-node-id'), node=nodes[nodeId];
    if(!node) return false;
    var defaultName=(TYPE[node.type]&&TYPE[node.type].name)||nodeTypeLabel(node.type);
    node.params.title=collabSync?collabSync.normalizeNodeTitle(active.textContent,defaultName):String(active.textContent||'').replace(/\s+/g,' ').trim().slice(0,40);
    return true;
  }
  function snapshotForCollab(){
    flushActiveCollabTitle();
    return snapshot();
  }
  function captureCollabFocus(){
    var active=document.activeElement, nodeEl=active&&active.closest?active.closest('.nc-node'):null;
    if(!nodeEl||!active.getAttribute) return null;
    var nodeId=Object.keys(nodes).find(function(id){ return nodes[id]&&nodes[id].el===nodeEl; });
    var field=active.getAttribute('data-f');
    if(!nodeId||!field) return null;
    var state={nodeId:nodeId,field:field,start:typeof active.selectionStart==='number'?active.selectionStart:null,end:typeof active.selectionEnd==='number'?active.selectionEnd:null,editable:active.getAttribute('contenteditable')==='true'};
    if(state.editable&&window.getSelection){
      var selection=window.getSelection();
      if(selection&&selection.rangeCount){
        var range=selection.getRangeAt(0).cloneRange();
        if(active.contains(range.commonAncestorContainer)){
          var startRange=range.cloneRange();
          startRange.selectNodeContents(active);
          startRange.setEnd(range.startContainer,range.startOffset);
          state.start=startRange.toString().length;
          range.selectNodeContents(active);
          range.setEnd(selection.getRangeAt(0).endContainer,selection.getRangeAt(0).endOffset);
          state.end=range.toString().length;
        }
      }
    }
    return state;
  }
  function restoreCollabFocus(state){
    var node=state&&nodes[state.nodeId];
    if(!node||!node.el) return;
    var target=Array.prototype.find.call(node.el.querySelectorAll('[data-f]'),function(item){ return item.getAttribute('data-f')===state.field; });
    if(!target||!target.focus) return;
    if(state.editable&&state.field==='headTitle'&&canEditCanvas()){
      beginInlineRename(node,true);
      target=node.el.querySelector('[data-f="headTitle"]');
    }
    target.focus();
    if(state.editable&&state.start!=null&&window.getSelection&&document.createRange){
      var textNode=target.firstChild||target.appendChild(document.createTextNode(''));
      var textLength=(textNode.textContent||'').length, start=Math.min(state.start,textLength), end=Math.min(state.end==null?start:state.end,textLength);
      var range=document.createRange(), selection=window.getSelection();
      range.setStart(textNode,start);
      range.setEnd(textNode,end);
      selection.removeAllRanges();
      selection.addRange(range);
    }else if(state.start!=null&&target.setSelectionRange){
      try{ target.setSelectionRange(state.start,state.end==null?state.start:state.end); }catch(e){}
    }
  }
  function applySyncedSnapshot(next,label){
    var focusState=captureCollabFocus();
    flushActiveCollabTitle();
    loading=true;
    try{ restoreSnapshot(next,{source:'trusted-collab'}); }
    finally{ loading=false; }
    setEditorReadonly(!canEditCanvas());
    restoreCollabFocus(focusState);
    updateState(label||'协作内容已同步');
  }
  function ensureCollabController(){
    if(collabController||!collabSync||!collabSync.createController) return collabController;
    collabController=collabSync.createController({
      clientId:collabClientId,
      transport:{
        save:function(boardId,batch){
          return authJson('/api/auth/canvas/boards/'+encodeURIComponent(boardId)+'/ops',{method:'POST',body:batch});
        },
        sync:function(boardId,since){
          return authJson('/api/auth/canvas/boards/'+encodeURIComponent(boardId)+'/sync?since='+encodeURIComponent(since),{timeout:6000});
        }
      },
      getSnapshot:snapshotForCollab,
      onState:function(state,detail){
        collabSyncGeneration=state.generation;
        collabSaving=state.saving;
        collabQueuedSnap=detail.pendingSnapshot;
        collabPendingBatch=detail.activeBatch;
        if(state.active){
          currentCollabVersion=state.version;
          collabBaseSnap=detail.baseSnapshot;
          if(detail.retrying) setSaveState('reconnecting');
          else if(state.saving||state.pending) setSaveState('syncing');
          else if(!state.polling) setSaveState(collabCanEdit()?'saved':'readonly');
        }
      },
      onBoard:function(board){
        currentCollabVersion=Number(board.version)||currentCollabVersion;
        currentCollabName=board.name||currentCollabName;
        if(board.role) setCurrentCollabRole(board.role);
        currentCollabMembers=board.members||currentCollabMembers;
        rememberCollabBoard(board);
      },
      onRole:function(role){
        setCurrentCollabRole(role);
        setEditorReadonly(!canEditCanvas());
        setSaveState(canEditCanvas()?'saved':'readonly');
      },
      onSnapshot:function(next){ applySyncedSnapshot(next,'协作内容已同步'); },
      onPoll:function(data){ setCollabOnlineCount(data.online_count); },
      onError:function(err,phase){
        if(phase==='save'){
          setSaveState('reconnecting');
          updateState((err&&err.message)||'协作同步暂时中断，正在重连');
        }else if(phase==='save-permanent'){
          if(err&&err.status===403){
            setCurrentCollabRole('viewer');
            setEditorReadonly(true);
            setSaveState('readonly');
            updateState('编辑权限已变更为只读');
          }else{
            setSaveState('error');
            updateState((err&&err.message)||'协作内容无法保存');
          }
        }
      }
    });
    return collabController;
  }
  function pollCollabOps(){
    if(currentBoardScope!=='collab'||!currentBoardId||!ensureCollabController()) return;
    if(collabSaving){ scheduleCollabPoll(collabSync.pollDelay(document.hidden)); return; }
    collabController.poll().then(function(outcome){
      if(outcome&&outcome.ignored) return;
      if(outcome&&outcome.failed){
        collabRetryCount++;
        setSaveState('reconnecting');
      }else{
        collabRetryCount=0;
        if(!collabSaving) setSaveState(collabCanEdit()?'saved':'readonly');
      }
      var delay=collabRetryCount?collabSync.retryDelay(collabRetryCount-1):collabSync.pollDelay(document.hidden);
      scheduleCollabPoll(delay);
    });
  }
  function sendCollabPresence(){
    if(currentBoardScope!=='collab'||!currentBoardId) return;
    var boardId=currentBoardId, generation=collabSyncGeneration;
    authJson('/api/auth/canvas/boards/'+encodeURIComponent(boardId)+'/presence',{method:'POST',body:{client_id:collabClientId},timeout:5000})
      .then(function(data){
        if(currentBoardScope==='collab'&&currentBoardId===boardId&&collabSyncGeneration===generation) setCollabOnlineCount(data.online_count);
      }).catch(function(){});
  }
  function startCollabSync(){
    var base=collabBaseSnap;
    stopCollabSync();
    if(currentBoardScope!=='collab'||!currentBoardId||!collabSync) return;
    collabBaseSnap=base;
    ensureCollabController().start({boardId:currentBoardId,version:currentCollabVersion,role:currentCollabRole,baseSnapshot:collabBaseSnap||emptySnapshot()});
    setCollabOnlineCount(1);
    sendCollabPresence();
    collabPresenceTimer=setInterval(sendCollabPresence,10000);
    scheduleCollabPoll(0);
  }
  function isUntitledCollabName(name){
    return !name||isUntitledBoardName(name)||String(name).indexOf('未命名')>=0;
  }
  function rememberCollabBoard(board){
    if(!board||!board.id) return;
    var idx=collabBoards.findIndex(function(b){ return b.id===board.id; });
    var item=Object.assign({}, idx>=0?collabBoards[idx]:{}, board);
    if(idx>=0) collabBoards[idx]=item; else collabBoards.unshift(item);
    collabBoards.sort(function(a,b){ return (b.updated_at||0)-(a.updated_at||0); });
  }
  function saveCollabDraft(snap){
    if(!currentBoardId) return;
    if(!collabCanEdit()){
      setSaveState('readonly');
      return;
    }
    if(!collabSync||!collabBaseSnap||!ensureCollabController()){ setSaveState('error'); return; }
    var ops=collabSync.diffSnapshots(collabBaseSnap,snap);
    var extraOps=[];
    var nextName=currentCollabName||'未命名协作画布';
    var inferred=inferBoardName(snap);
    if(inferred&&isUntitledCollabName(nextName)){
      nextName=inferred;
      extraOps.push({type:'board.rename',name:nextName});
    }
    if(!ops.length&&!extraOps.length){ setSaveState('saved'); return; }
    collabController.save(snap,extraOps);
  }
  function nodeAriaDisabled(node,readonly){
    if(!readonly) return false;
    return !(node&&node.type==='shortDrama'&&shortDramaModule.canOpenNode(node.params,false));
  }
  function setEditorReadonly(readonly){
    document.querySelectorAll('.nc-add,#ncRunAll,#ncTplSave,#ncTplLoad,#ncTplDelete,#ncTplImport,#ncPaste,#ncLayout,#ncClear,#ncResetDemo,#ncUndo,#ncRedo,#ncFsAdd,#ncFsUndo,#ncFsRedo,#ncFsRun,#ncFsTplMenu,#ncFsMore').forEach(function(el){
      if(el) el.disabled=!!readonly;
    });
    document.querySelectorAll('.nc-node input,.nc-node textarea,.nc-node select,.nc-node button').forEach(function(el){ el.disabled=!!readonly; });
    syncRunAllDisabled();
    document.querySelectorAll('.nc-node [data-f="openShortDrama"]').forEach(function(openShortDrama){
      var host=openShortDrama.closest('.nc-node'), node=host&&nodes[host.getAttribute('data-node-id')];
      openShortDrama.disabled=!!readonly&&!(node&&node.params.project_id);
    });
    document.querySelectorAll('.nc-node [data-f="openDigitalPresenter"]').forEach(function(button){
      var host=button.closest('.nc-node'), node=host&&nodes[host.getAttribute('data-node-id')];
      button.disabled=!!readonly&&!(node&&node.params.project_id);
    });
    document.querySelectorAll('.nc-node [data-f="headTitle"]').forEach(function(el){
      if(readonly){ el.setAttribute('contenteditable','false'); el.removeAttribute('spellcheck'); }
    });
    document.querySelectorAll('.nc-node').forEach(function(el){
      var node=nodes[el.getAttribute('data-node-id')];
      el.setAttribute('aria-disabled',nodeAriaDisabled(node,readonly)?'true':'false');
    });
    document.querySelectorAll('.nc-port,.nc-drop').forEach(function(el){ el.setAttribute('aria-disabled',readonly?'true':'false'); });
  }
  function migrateDraftToBoards(){
    if(getBoards().length) return;
    var draft=loadDraft();
    if(!draft||!Array.isArray(draft.nodes)||!draft.nodes.length) return;
    if(setBoards([{id:makeBoardId(),name:'未命名画布',updatedAt:Date.now(),data:draft}])){
      canvasStorage.removeDraft();
    }
  }
  function renderBoardHome(){
    if(!boardGrid) return;
    var query=(boardSearch&&boardSearch.value||'').trim().toLowerCase();
    if(boardSearch) boardSearch.placeholder=boardMode==='templates'?'请输入模板名称':(boardMode==='collab'?'请输入协作画布名称':'请输入画布名称');
    if(boardSort) boardSort.style.display=boardMode==='templates'?'none':'';
    if(boardMode==='templates'){
      renderTemplateHome(query);
      return;
    }
    if(boardMode==='collab'){
      renderCollabHome(query);
      return;
    }
    var list=getBoards();
    if(query) list=list.filter(function(b){ return String(b.name||'未命名画布').toLowerCase().indexOf(query)>=0; });
    if(boardSort&&boardSort.value==='name_asc'){
      list.sort(function(a,b){ return String(a.name||'').localeCompare(String(b.name||''),'zh-CN'); });
    }else{
      list.sort(function(a,b){ return (b.updatedAt||0)-(a.updatedAt||0); });
    }
    boardGrid.innerHTML='';
    var newCard=document.createElement('a');
    newCard.id='ncBoardNew';
    newCard.href='#';
    newCard.target='_blank';
    newCard.rel='noopener';
    newCard.className='nc-board-card nc-board-new';
    newCard.innerHTML='<span class="nc-board-plus">+</span><strong>新建画布</strong>';
    newCard.onclick=function(){ prepareNewBoardLink(newCard); };
    boardGrid.appendChild(newCard);
    list.forEach(function(board){
      var card=document.createElement('button'), img=boardPreview(board);
      card=document.createElement('div');
      card.className='nc-board-card';
      card.setAttribute('role','button');
      card.setAttribute('tabindex','0');
      card.setAttribute('data-board-id',board.id);
      card.innerHTML='<div class="nc-board-actions"><button class="nc-board-action" type="button" data-act="rename" title="重命名">改</button><button class="nc-board-action" type="button" data-act="copy" title="复制画布">复</button><button class="nc-board-action danger" type="button" data-act="delete" title="删除画布">×</button></div><div class="nc-board-thumb'+(img?'':' placeholder')+'" '+(img?'style="background-image:url('+String(img).replace(/"/g,'%22')+')"':'')+'>'+(img?'':'Shotlab')+'</div><div class="nc-board-info"><span class="nc-board-title">'+escapeHtml(board.name||'未命名画布')+'</span><span class="nc-board-time">'+escapeHtml(formatBoardTime(board.updatedAt))+'</span></div>';
      card.onclick=function(e){
        var act=e.target&&e.target.closest?e.target.closest('[data-act]'):null;
        if(act){ handleBoardAction(board.id,act.getAttribute('data-act')); return; }
        openBoardInNewTab(board.id);
      };
      card.onkeydown=function(e){
        if(e.key==='Enter'||e.key===' '){ e.preventDefault(); openBoardInNewTab(board.id); }
      };
      boardGrid.appendChild(card);
    });
  }
  function collabRoleLabel(role){
    return ({owner:'创建者',editor:'可编辑',viewer:'只读'})[role]||'协作';
  }
  function formatCollabTime(ts){
    if(!ts) return '更新时间：-';
    try{ return '更新时间：'+new Date(ts*1000).toLocaleString('zh-CN',{hour12:false}); }catch(e){ return '更新时间：-'; }
  }
  function renderCollabHome(query){
    boardGrid.innerHTML='';
    var newCard=document.createElement('button');
    newCard.id='ncCollabBoardNew';
    newCard.type='button';
    newCard.className='nc-board-card nc-board-new';
    newCard.innerHTML='<span class="nc-board-plus">+</span><strong>新建协作画布</strong>';
    newCard.onclick=createCollabBoard;
    boardGrid.appendChild(newCard);
    if(!collabLoaded&&!collabLoading) loadCollabBoards();
    if(collabLoading){
      var loadingEl=document.createElement('div');
      loadingEl.className='nc-board-empty';
      loadingEl.textContent='正在读取协作画布...';
      boardGrid.appendChild(loadingEl);
      return;
    }
    if(collabError){
      var errorEl=document.createElement('div');
      errorEl.className='nc-board-empty';
      errorEl.innerHTML=escapeHtml(collabError)+'<br><span style="font-size:12px;color:#94a4bb;">'+escapeHtml(collabErrorHint||'协作服务暂不可用，可先新建本地画布继续编辑。')+'</span>';
      boardGrid.appendChild(errorEl);
      return;
    }
    var list=collabBoards.slice();
    if(query) list=list.filter(function(b){ return String(b.name||'未命名协作画布').toLowerCase().indexOf(query)>=0; });
    if(boardSort&&boardSort.value==='name_asc'){
      list.sort(function(a,b){ return String(a.name||'').localeCompare(String(b.name||''),'zh-CN'); });
    }else{
      list.sort(function(a,b){ return (b.updated_at||0)-(a.updated_at||0); });
    }
    if(!list.length){
      var emptyEl=document.createElement('div');
      emptyEl.className='nc-board-empty';
      emptyEl.textContent=query?'没有找到匹配的协作画布':'暂无协作画布';
      boardGrid.appendChild(emptyEl);
      return;
    }
    list.forEach(function(board){
      var card=document.createElement('div');
      var canManage=board.role==='owner';
      card.className='nc-board-card';
      card.setAttribute('role','button');
      card.setAttribute('tabindex','0');
      card.setAttribute('data-collab-id',board.id);
      var actions=canManage?'<div class="nc-board-actions"><button class="nc-board-action wide" type="button" data-act="members" title="管理协作者">成员</button><button class="nc-board-action danger" type="button" data-act="delete" title="删除协作画布">×</button></div>':'';
      card.innerHTML=actions
        +'<div class="nc-board-thumb placeholder">Collab</div>'
        +'<div class="nc-board-info"><span class="nc-board-title">'+escapeHtml(board.name||'未命名协作画布')+'</span><span class="nc-board-time">'+escapeHtml(formatCollabTime(board.updated_at))+'</span>'
        +'<div class="nc-board-badges"><span class="nc-board-badge gold">'+escapeHtml(collabRoleLabel(board.role))+'</span><span class="nc-board-badge">'+((board.members_count||0)+1)+' 人</span></div></div>';
      card.onclick=function(e){
        var act=e.target&&e.target.closest?e.target.closest('[data-act]'):null;
        if(act){ handleCollabBoardAction(board,act.getAttribute('data-act')); return; }
        openCollabBoardInNewTab(board.id);
      };
      card.onkeydown=function(e){
        if(e.key==='Enter'||e.key===' '){ e.preventDefault(); openCollabBoardInNewTab(board.id); }
      };
      boardGrid.appendChild(card);
    });
  }
  function loadCollabBoards(force){
    if(collabLoading) return;
    if(collabLoaded&&!force) return;
    collabLoading=true;
    collabError='';
    collabErrorHint='';
    authJson('/api/auth/canvas/boards').then(function(data){
      collabBoards=Array.isArray(data.boards)?data.boards:[];
      collabLoaded=true;
    }).catch(function(err){
      collabLoaded=false;
      if(err&&err.status===403){
        collabError='登录状态已过期';
        collabErrorHint='请退出重新登录后再使用协作画布。';
      }else{
        collabError=(err&&err.message)||'协作画布读取失败';
      }
    }).finally(function(){
      collabLoading=false;
      if(boardMode==='collab') renderBoardHome();
    });
  }
  function createCollabBoard(){
    if(collabCreating) return;
    collabCreating=true;
    updateState('正在新建协作画布');
    authJson('/api/auth/canvas/boards',{method:'POST',body:{name:'未命名协作画布',data:emptySnapshot()}})
      .then(function(data){
        var board=data.board||{};
        rememberCollabBoard(board);
        collabLoaded=true;
        // 原地打开新建的板：openCollabBoardInNewTab 在 fetch 的异步 .then 里开新标签会被浏览器
        // 当弹窗拦截(非直接用户手势)→ 创建后「没有显示」。改成同页载入(openCollabBoard)不受拦。
        // 卡片点击打开走的是直接手势，仍用 openCollabBoardInNewTab、不受影响。
        openCollabBoard(board.id);
      }).catch(function(err){
        var msg=(err&&err.message)||'协作画布创建失败';
        collabError=msg;
        collabLoaded=true;
        if(err&&err.status===403){
          collabErrorHint='请退出重新登录后再使用协作画布。';
          updateState('登录状态已过期，请重新登录');
          openMessageDialog('协作画布','登录状态已过期（安全校验未通过），请退出重新登录后再使用协作画布。');
          return;
        }
        updateState('协作服务不可用，已新建本地画布');
        var id=createBoardRecord();
        openMessageDialog('协作画布', msg+'。当前本地服务未接通协作接口，已为你新建一个本地画布，后端服务启动后再使用协作画布。');
        openBoardInNewTab(id);
      }).finally(function(){
        collabCreating=false;
        if(boardMode==='collab') renderBoardHome();
      });
  }
  function handleCollabBoardAction(board, action){
    if(!board) return;
    if(action==='members'){
      openCollabMembersDialog(board.id);
      return;
    }
    if(action==='delete'){
      openConfirmDialog('删除协作画布','确定删除「'+(board.name||'未命名协作画布')+'」？此操作会移除所有协作者，且不可撤销。',function(){
        authJson('/api/auth/canvas/boards/'+encodeURIComponent(board.id),{method:'DELETE'}).then(function(){
          collabBoards=collabBoards.filter(function(item){ return item.id!==board.id; });
          renderBoardHome();
          updateState('协作画布已删除');
        }).catch(function(err){
          updateState((err&&err.message)||'协作画布删除失败');
        });
      });
    }
  }
  function updateCollabMemberData(board, members){
    board.members=members||[];
    board.members_count=board.members.length;
    rememberCollabBoard(board);
    if(currentBoardScope==='collab'&&currentBoardId===board.id) currentCollabMembers=board.members;
  }
  function openCollabMembersDialog(boardId){
    authJson('/api/auth/canvas/boards/'+encodeURIComponent(boardId)).then(function(data){
      renderCollabMembersDialog(data.board||{id:boardId,members:[]});
    }).catch(function(err){
      openMessageDialog('协作成员', (err&&err.message)||'读取协作者失败');
    });
  }
  function renderCollabMembersDialog(board){
    closeDialog();
    var canManage=board.role==='owner';
    var members=Array.isArray(board.members)?board.members:[];
    var memberHtml=members.length?members.map(function(m){
      var label=m.name||m.username||'?';
      return '<div class="nc-collab-member">'
        +'<span class="nc-collab-avatar">'+escapeHtml(String(label).slice(0,1).toUpperCase()||'?')+'</span>'
        +'<span><span class="nc-collab-name">'+escapeHtml(label)+'</span><span class="nc-collab-meta">@'+escapeHtml(m.username||'')+' · '+escapeHtml(m.account_id||'')+'</span></span>'
        +'<span class="nc-collab-role">'+escapeHtml(collabRoleLabel(m.role))+'</span>'
        +(canManage?'<button class="nc-collab-remove" type="button" data-remove-member="'+escapeHtml(m.username||'')+'">移除</button>':'')
        +'</div>';
    }).join(''):'<div class="nc-board-empty" style="padding:18px 8px;">暂无协作者</div>';
    var form=canManage?'<div class="nc-collab-form"><input data-f="accountId" placeholder="输入好友账号 ID"><select data-f="role"><option value="editor">可编辑</option><option value="viewer">只读</option></select><button type="button" data-f="invite">邀请</button></div>':'';
    var mask=document.createElement('div');
    mask.className='nc-dialog-mask';
    mask.innerHTML='<div class="nc-dialog nc-collab-dialog" role="dialog" aria-modal="true">'
      +'<h3>'+escapeHtml(board.name||'协作成员')+'</h3>'
      +'<p>创建者可以邀请已添加的好友加入当前画布。</p>'
      +form
      +'<div class="nc-collab-status" data-f="status"></div>'
      +'<div class="nc-collab-members">'+memberHtml+'</div>'
      +'<div class="nc-dialog-actions"><button type="button" data-f="cancel">关闭</button></div></div>';
    document.body.appendChild(mask);
    var status=mask.querySelector('[data-f="status"]');
    function setStatus(text, isError){
      if(!status) return;
      status.textContent=text||'';
      status.classList.toggle('error', !!isError);
    }
    mask.querySelector('[data-f="cancel"]').onclick=closeDialog;
    mask.addEventListener('mousedown',function(e){ if(e.target===mask) closeDialog(); });
    var invite=mask.querySelector('[data-f="invite"]');
    if(invite) invite.onclick=function(){
      var account=(mask.querySelector('[data-f="accountId"]')||{}).value||'';
      var role=(mask.querySelector('[data-f="role"]')||{}).value||'editor';
      if(!account.trim()){ setStatus('请填写好友账号 ID', true); return; }
      invite.disabled=true;
      setStatus('正在发送邀请...', false);
      authJson('/api/auth/canvas/boards/'+encodeURIComponent(board.id)+'/members',{method:'POST',body:{account_id:account,role:role}})
        .then(function(data){
          updateCollabMemberData(board,data.members||[]);
          renderCollabMembersDialog(board);
        }).catch(function(err){
          invite.disabled=false;
          setStatus((err&&err.message)||'邀请失败', true);
        });
    };
    mask.querySelectorAll('[data-remove-member]').forEach(function(btn){
      btn.onclick=function(){
        var username=btn.getAttribute('data-remove-member')||'';
        if(!username) return;
        btn.disabled=true;
        authJson('/api/auth/canvas/boards/'+encodeURIComponent(board.id)+'/members/'+encodeURIComponent(username),{method:'DELETE'})
          .then(function(data){
            updateCollabMemberData(board,data.members||[]);
            renderCollabMembersDialog(board);
          }).catch(function(err){
            btn.disabled=false;
            setStatus((err&&err.message)||'移除失败', true);
          });
      };
    });
  }
  function renderTemplateHome(query){
    boardGrid.innerHTML='';
    var list=normalizeTemplates(getTemplates());
    if(query) list=list.filter(function(t){ return String(t.name||'模板').toLowerCase().indexOf(query)>=0; });
    list.sort(function(a,b){ return (b.createdAt||0)-(a.createdAt||0); });
    if(!list.length){
      var empty=document.createElement('div');
      empty.className='nc-board-empty';
      empty.textContent=query?'没有找到匹配的模板':'暂无本地模板，可在画布中点击“模板 > 保存模板”创建';
      boardGrid.appendChild(empty);
      return;
    }
    list.forEach(function(t,i){
      var card=document.createElement('div'), count=(t.data&&t.data.nodes&&t.data.nodes.length)||0;
      card.className='nc-board-card nc-template-card';
      card.setAttribute('role','button');
      card.setAttribute('tabindex','0');
      card.innerHTML='<div class="nc-template-meta"><span class="nc-template-icon">模</span><span class="nc-template-name">'+escapeHtml(t.name||('模板 '+(i+1)))+'</span><span class="nc-template-detail">节点 '+count+' 个<br>'+escapeHtml(formatTemplateTime(t.createdAt))+'</span></div>'
        +'<div class="nc-template-actions"><button class="nc-template-btn" type="button" data-act="load">载入</button><button class="nc-template-btn" type="button" data-act="export">导出</button><button class="nc-template-btn danger" type="button" data-act="delete">删除</button></div>';
      card.onclick=function(e){
        var act=e.target&&e.target.closest?e.target.closest('[data-act]'):null;
        if(act){ handleTemplateHomeAction(t,act.getAttribute('data-act')); return; }
        handleTemplateHomeAction(t,'preview');
      };
      card.onkeydown=function(e){
        if(e.key==='Enter'||e.key===' '){ e.preventDefault(); handleTemplateHomeAction(t,'preview'); }
      };
      boardGrid.appendChild(card);
    });
  }
  function formatTemplateTime(ts){
    if(!ts) return '保存时间：-';
    try{ return '保存时间：'+new Date(ts).toLocaleString('zh-CN',{hour12:false}); }catch(e){ return '保存时间：-'; }
  }
  function templateTypeSummary(item){
    var counts={}, nodes=(item&&item.data&&item.data.nodes)||[];
    nodes.forEach(function(n){
      var name=(TYPE[n.type]&&TYPE[n.type].name)||n.type||'节点';
      counts[name]=(counts[name]||0)+1;
    });
    var parts=Object.keys(counts).map(function(k){ return k+' '+counts[k]; });
    return parts.length?parts.join(' · '):'空模板';
  }
  function previewTemplate(item){
    if(!item) return;
    closeDialog();
    var count=(item.data&&item.data.nodes&&item.data.nodes.length)||0;
    var edgeCount=(item.data&&item.data.edges&&item.data.edges.length)||0;
    var mask=document.createElement('div');
    mask.className='nc-dialog-mask';
    mask.innerHTML='<div class="nc-dialog" role="dialog" aria-modal="true">'
      +'<h3>'+escapeHtml(item.name||'模板预览')+'</h3>'
      +'<p>节点：'+count+' 个\n连线：'+edgeCount+' 条\n'+escapeHtml(formatTemplateTime(item.createdAt))+'\n\n'+escapeHtml(templateTypeSummary(item))+'</p>'
      +'<div class="nc-dialog-actions"><button type="button" data-f="cancel">关闭</button><button type="button" data-f="export">导出</button><button type="button" class="primary" data-f="load">载入为画布</button></div></div>';
    document.body.appendChild(mask);
    mask.querySelector('[data-f="cancel"]').onclick=closeDialog;
    mask.querySelector('[data-f="export"]').onclick=function(){ closeDialog(); exportTemplateItem(item); };
    mask.querySelector('[data-f="load"]').onclick=function(){ closeDialog(); createBoardFromTemplate(item); };
    mask.addEventListener('mousedown',function(e){ if(e.target===mask) closeDialog(); });
  }
  function handleTemplateHomeAction(item,act){
    if(!item) return;
    if(act==='preview'){
      previewTemplate(item);
      return;
    }
    if(act==='load'){
      createBoardFromTemplate(item);
      return;
    }
    if(act==='export'){
      exportTemplateItem(item);
      return;
    }
    if(act==='delete'){
      openConfirmDialog('删除模板','确定删除模板“'+(item.name||'模板')+'”？',function(){
        var list=normalizeTemplates(getTemplates()), idx=list.findIndex(function(t){
          return t.createdAt===item.createdAt && t.name===item.name;
        });
        if(idx<0) idx=list.findIndex(function(t){ return t.name===item.name; });
        if(idx<0) return;
        list.splice(idx,1);
        if(saveTemplates(list)) renderBoardHome();
        updateState('模板已删除');
      });
    }
  }
  function showEditor(){
    setCanvasView('workflow');
    if(wrap) wrap.classList.add('editing');
    if(editorView) editorView.style.display='';
    if(boardHome) boardHome.style.display='';
    if(!window.matchMedia||window.matchMedia('(min-width:901px)').matches) openSidePanel('agent',true);
    setTimeout(function(){ fitView(); scheduleMap(); },40);
  }
  function showBoardHome(){
    destroyAllShortDramaWorkspaces();
    digitalPresenterWorkspaceLifecycle.switchScope(currentShortDramaScopeKey());
    clearDigitalPresenterWorkspaceRefs();
    saveCurrentBoard();
    var wasCollab=currentBoardScope==='collab';
    stopCollabSync();
    currentBoardScope='local';
    currentBoardId=null;
    boardLastSeenUpdatedAt=0;
    boardConflict=false;
    currentCollabVersion=0;
    currentCollabRole='';
    currentCollabName='';
    currentCollabMembers=[];
    collabQueuedSnap=null;
    canvasStorage.saveActiveBoard('');
    clearBoardParam();
    if(localFullscreen) setLocalFullscreen(false);
    if(document.fullscreenElement&&document.exitFullscreen){ document.exitFullscreen().catch(function(){}); }
    if(wrap) wrap.classList.remove('editing');
    updateImageToolbar();
    setEditorReadonly(false);
    if(wasCollab){
      boardMode='collab';
      document.querySelectorAll('[data-board-tab]').forEach(function(t){ t.classList.toggle('on',t.getAttribute('data-board-tab')==='collab'); });
      loadCollabBoards(true);
    }
    renderBoardHome();
  }
  function createBoard(){
    var id=createBoardRecord();
    renderBoardHome();
    openBoardInNewTab(id);
  }
  function createBoardRecord(){
    saveCurrentBoard();
    var board={id:makeBoardId(),name:'未命名画布',updatedAt:Date.now(),data:emptySnapshot()};
    var list=getBoards();
    list.unshift(board);
    setBoards(list);
    return board.id;
  }
  function createBoardFromTemplate(item){
    if(item&&item.data&&(item.data.nodes||[]).some(function(n){return !nodeCreationPolicy.canMaterialize(n.type,'local-template');})){
      updateState('数字人口播模板仅可载入有编辑权限的协作画布');return false;
    }
    if(!item||!item.data){ updateState('没有可载入的模板'); return false; }
    saveCurrentBoard();
    var snap=sanitizeTemplateSnap(item.data);
    var board={id:makeBoardId(),name:cleanBoardName(item.name||'模板画布')||'模板画布',updatedAt:Date.now(),data:snap};
    var list=getBoards();
    list.unshift(board);
    if(!setBoards(list)){ updateState('模板载入失败：本地空间不足'); return false; }
    stopCollabSync();
    currentBoardScope='local';
    currentBoardId=board.id;
    boardLastSeenUpdatedAt=board.updatedAt||0;
    boardConflict=false;
    setBoardParam(board.id);
    canvasStorage.saveActiveBoard(board.id);
    loading=true;
    restoreSnapshot(snap);
    loading=false;
    history.clear();
    setSaveState('saved');
    showEditor();
    setEditorReadonly(false);
    setLocalFullscreen(true);
    updateState('模板已载入');
    return true;
  }
  function prepareNewBoardLink(link){
    if(!link) return;
    if(!link.getAttribute('data-board-id')){
      var id=createBoardRecord();
      link.setAttribute('data-board-id',id);
      link.href=boardUrl(id);
      setTimeout(function(){ renderBoardHome(); },500);
    }
  }
  function openBoard(id, isNew){
    var list=getBoards(), board=list.find(function(b){ return b.id===id; });
    if(!board) return false;
    stopCollabSync();
    currentBoardScope='local';
    currentBoardId=id;
    boardLastSeenUpdatedAt=board.updatedAt||0;
    boardConflict=false;
    canvasStorage.saveActiveBoard(id);
    loading=true;
    restoreSnapshot(board.data||emptySnapshot());
    loading=false;
    history.clear();
    updateState(isNew?'已新建画布':'已打开画布');
    setSaveState('saved');
    showEditor();
    setEditorReadonly(false);
    setLocalFullscreen(true);
    return true;
  }
  function openCollabBoard(id){
    if(!id) return false;
    stopCollabSync();
    var openGeneration=collabSyncGeneration;
    currentBoardScope='collab';
    currentBoardId=id;
    currentCollabVersion=0;
    currentCollabRole='';
    currentCollabName='';
    currentCollabMembers=[];
    boardConflict=false;
    setCollabParam(id);
    loading=true;
    updateState('正在打开协作画布');
    authJson('/api/auth/canvas/boards/'+encodeURIComponent(id)).then(function(data){
      if(currentBoardScope!=='collab'||currentBoardId!==id||collabSyncGeneration!==openGeneration) return;
      var board=data.board||{};
      currentBoardScope='collab';
      currentBoardId=board.id||id;
      currentCollabVersion=board.version||1;
      setCurrentCollabRole(board.role||'viewer');
      currentCollabName=board.name||'未命名协作画布';
      currentCollabMembers=board.members||[];
      collabBaseSnap=collabSync?collabSync.clone(board.data||emptySnapshot()):stateApi.cloneSnapshot(board.data||emptySnapshot());
      rememberCollabBoard(board);
      restoreSnapshot(board.data||emptySnapshot(),{source:'trusted-collab'});
      history.clear();
      setSaveState(collabCanEdit()?'saved':'readonly');
      showEditor();
      setEditorReadonly(!collabCanEdit());
      setLocalFullscreen(true);
      startCollabSync();
      updateState(collabCanEdit()?'协作画布已打开':'协作画布已打开（只读）');
    }).catch(function(err){
      if(currentBoardScope!=='collab'||currentBoardId!==id||collabSyncGeneration!==openGeneration) return;
      stopCollabSync();
      currentBoardScope='local';
      currentBoardId=null;
      clearBoardParam();
      renderBoardHome();
      updateState((err&&err.message)||'协作画布打开失败');
      openMessageDialog('协作画布', (err&&err.message)||'打开失败，请返回列表重试');
    }).finally(function(){
      if(currentBoardScope==='collab'&&currentBoardId===id) loading=false;
    });
    return true;
  }
  function openInitialBoardFromUrl(){
    var collabId=collabIdFromUrl();
    if(collabId) return openCollabBoard(collabId);
    var id=boardIdFromUrl();
    if(!id) return false;
    if(openBoard(id)) return true;
    clearBoardParam();
    renderBoardHome();
    updateState('画布不存在，请重新选择');
    return true;
  }
  function handleBoardAction(id, action){
    if(action==='rename') renameBoard(id);
    if(action==='copy') duplicateBoard(id);
    if(action==='delete') deleteBoard(id);
  }
  function renameBoard(id){
    var list=getBoards(), idx=list.findIndex(function(b){ return b.id===id; });
    if(idx<0) return;
    var oldName=list[idx].name||'未命名画布';
    openTextDialog({title:'重命名画布',value:oldName,placeholder:'请输入画布名称',max:40,onSave:function(next){
      next=cleanBoardName(next)||'未命名画布';
      var latest=getBoards(), latestIdx=latest.findIndex(function(b){ return b.id===id; });
      if(latestIdx<0) return;
      latest[latestIdx].name=next;
      latest[latestIdx].updatedAt=Date.now();
      if(setBoards(latest)){
        renderBoardHome();
        updateState('画布已重命名');
      }
    }});
  }
  function duplicateBoard(id){
    var list=getBoards(), board=list.find(function(b){ return b.id===id; });
    if(!board) return;
    var copy=stateApi.cloneSnapshot(board);
    copy.data=sanitizeShortDramaSnapshot(copy.data);
    copy.data.nodes=(copy.data.nodes||[]).map(function(node){ return node&&node.type==='digitalPresenter'?digitalPresenterModule.copyNodeData(node):node; });
    copy.id=makeBoardId();
    copy.name=cleanBoardName((board.name||'未命名画布')+' 副本')||'未命名画布 副本';
    copy.updatedAt=Date.now();
    list.unshift(copy);
    if(setBoards(list)){
      renderBoardHome();
      updateState('画布已复制');
    }
  }
  function deleteBoard(id){
    var list=getBoards(), board=list.find(function(b){ return b.id===id; });
    if(!board) return;
    var name=board.name||'未命名画布';
    openConfirmDialog('删除画布','确定删除画布「'+name+'」？此操作不可撤销。',function(){
      var latest=getBoards(), idx=latest.findIndex(function(b){ return b.id===id; });
      if(idx<0) return;
      latest.splice(idx,1);
      if(!setBoards(latest)) return;
      shortDramaProjectCoordinator.cleanupScope(shortDramaScopeKey('local',id));
      digitalPresenterProjectCoordinator.cleanupScope(shortDramaScopeKey('local',id));
      if(currentBoardId===id) showBoardHome();
      else renderBoardHome();
      updateState('画布已删除');
    });
  }
  function getTemplates(){
    var loaded=canvasStorage.loadTemplates();
    return loaded.ok?(loaded.value||[]):[];
  }
  function sanitizeShortDramaSnapshot(snap){
    snap=stateApi.cloneSnapshot(snap||{});
    snap.nodes=(snap.nodes||[]).map(function(node){
      return node&&node.type==='shortDrama'?shortDramaModule.sanitizeNodeData(node):node&&node.type==='digitalPresenter'?stateApi.sanitizeNodeData(node,{digitalPresenter:digitalPresenterModule.sanitizeNodeData}):node;
    });
    return snap;
  }
  function sanitizeTemplateSnap(snap){
    snap=sanitizeShortDramaSnapshot(snap);
    var valid={};
    (snap.nodes||[]).forEach(function(n){
      if(!n||!TYPE[n.type]) return;
      n.params=Object.assign({engine:'nb2',channel:'grok',ratio:'9:16',duration:'5',quality:'hd',title:'',remark:''},n.params||{});
      if(n.type==='shortDrama'){
        n.params=normalizeShortDramaNodeParams(n.params);
        n.outputs={};
      }
      if(n.type==='digitalPresenter'){
        var presenterCopy=digitalPresenterModule.copyNodeData(n);
        n.params=normalizeDigitalPresenterNodeParams(presenterCopy.params);
        n.outputs={};
      }
      n.outputs=n.outputs||{};
      n.image=null;
      if(n.outputs.image) delete n.outputs.image;
      if(n.outputs.video) delete n.outputs.video;
      if(n.outputs.video_url) delete n.outputs.video_url;
      if(n.type==='image') n.outputs={};
      n.state='';
      n.note='';
      valid[n.id]=true;
    });
    snap.nodes=(snap.nodes||[]).filter(function(n){ return n&&TYPE[n.type]; });
    snap.edges=(snap.edges||[]).filter(function(e){
      return e&&e.from&&e.to&&valid[e.from.node]&&valid[e.to.node]&&e.from.port===e.to.port;
    });
    snap.nid=snap.nid||0;
    snap.scroll={left:0,top:0};
    snap.zoom=1;
    snap.runLabel='模板';
    return snap;
  }
  function normalizeTemplates(list){
    return (Array.isArray(list)?list:[]).map(function(t,i){
      return {name:String((t&&t.name)||('模板 '+(i+1))).slice(0,40),createdAt:(t&&t.createdAt)||Date.now(),data:sanitizeTemplateSnap((t&&t.data)||{})};
    }).filter(function(t){ return t.data&&Array.isArray(t.data.nodes); });
  }
  function setTemplates(list){
    if(!canvasStorage.saveTemplates(normalizeTemplates(list)).ok){
      updateState('模板保存失败：空间不足');
      return false;
    }
    return true;
  }
  function saveTemplates(list){
    if(!setTemplates(list)) return false;
    renderTemplates();
    if(boardMode==='templates') renderBoardHome();
    return true;
  }
  function renderTemplates(){
    if(!tplSelect) return;
    var list=normalizeTemplates(getTemplates());
    tplSelect.innerHTML='<option value="">模板</option>'+list.map(function(t,i){ return '<option value="'+i+'">'+escapeHtml(t.name||('模板 '+(i+1)))+'</option>'; }).join('');
  }
  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g,function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; });
  }
  function nodeTypeLabel(type){
    return ({text:'文本',image:'图片',reverse:'反推',gen:'作图',video:'视频',videoAsset:'视频素材',shortDrama:'短剧',digitalPresenter:'数字人口播'})[type]||type||'节点';
  }
  function nodeStatusLabel(node){
    var s=node&&node.el&&node.el.getAttribute('data-state');
    return ({running:'运行中',done:'已完成',error:'失败'})[s]||'等待';
  }
  function nodeDisplayName(node){
    if(!node) return '未命名节点';
    return (node.params&&node.params.title)||((TYPE[node.type]&&TYPE[node.type].name)||nodeTypeLabel(node.type));
  }
  function renderStoryboard(){
    if(!storyGrid||canvasView!=='story') return;
    var ordered=graphApi.topologicalOrder(Object.keys(nodes).map(function(id){ return {id:id}; }),edges);
    if(storyMeta) storyMeta.textContent=ordered.length+' 个场景';
    if(!ordered.length){
      storyGrid.innerHTML='<div class="nc-story-empty"><strong>故事板还没有内容</strong><span>先在工作流中添加文本、图片或视频节点。</span><button type="button">返回工作流</button></div>';
      storyGrid.querySelector('button').onclick=function(){ setCanvasView('workflow'); };
      return;
    }
    var ready=0;
    storyGrid.innerHTML=ordered.map(function(id,index){
      var node=nodes[id], params=node.params||{}, output=node.outputs||{}, text='', image='', video=output.video||output.video_url||'', body='';
      if(node.type==='text'||node.type==='gen'||node.type==='video') text=String(params.text||'').trim();
      if(node.type==='reverse') text=String(output.prompt||'').trim();
      if(node.type==='image') image=node.image||output.image||'';
      if(node.type==='gen') image=output.image||'';
      var progress=node.type==='shortDrama'?Math.max(0,Math.min(100,Number(params.progress)||0)):0;
      var hasContent=!!(text||image||video||progress);
      if(hasContent) ready++;
      if(video) body='<div class="nc-story-media nc-story-video" data-story-video="'+index+'"><span>正在准备视频预览…</span></div>';
      else if(image) body='<div class="nc-story-media"><img src="'+escapeHtml(image)+'" alt="'+escapeHtml(nodeDisplayName(node))+'"></div>';
      else if(node.type==='shortDrama') body='<div class="nc-story-progress"><div><b>'+escapeHtml(params.stage||'未开始')+'</b><span>'+progress+'%</span></div><i><span style="width:'+progress+'%"></span></i><small>'+escapeHtml(params.ratio||'16:9')+' · '+escapeHtml(params.target_duration||60)+' 秒</small></div>';
      else if(node.type==='image'||node.type==='gen'||node.type==='video'||node.type==='videoAsset') body='<div class="nc-story-media empty"><span>'+({image:'等待图片素材',gen:'等待生成图片',video:'等待生成视频',videoAsset:'等待视频素材'}[node.type])+'</span></div>';
      if(text) body+='<p class="nc-story-copy">'+escapeHtml(text)+'</p>';
      if(!body) body='<p class="nc-story-copy muted">等待补充内容…</p>';
      var status=nodeStatusLabel(node); if(status==='等待'&&hasContent) status='已就绪';
      return '<article class="nc-story-card '+escapeHtml(node.type)+'"><div class="nc-story-card-head"><span class="nc-story-index">'+String(index+1).padStart(2,'0')+'</span><div><small>'+escapeHtml(nodeTypeLabel(node.type))+'</small><strong>'+escapeHtml(nodeDisplayName(node))+'</strong></div><em>'+escapeHtml(status)+'</em></div>'+body+'</article>';
    }).join('');
    if(storyMeta) storyMeta.textContent=ordered.length+' 个场景 · '+ready+' 个已就绪';
    storyGrid.querySelectorAll('[data-story-video]').forEach(function(box){
      var node=nodes[ordered[Number(box.getAttribute('data-story-video'))]], url=node&&node.outputs&&(node.outputs.video||node.outputs.video_url);
      playableAssetUrl(url).then(function(src){ if(box.isConnected&&canvasView==='story') box.innerHTML='<video controls playsinline preload="metadata" src="'+escapeHtml(src)+'"></video>'; })
        .catch(function(){ if(box.isConnected) box.innerHTML='<span>视频预览加载失败</span>'; });
    });
  }
  function setCanvasView(view){
    canvasView=view==='story'?'story':'workflow';
    if(canvasShell) canvasShell.classList.toggle('story-view',canvasView==='story');
    if(storyboard) storyboard.hidden=canvasView!=='story';
    document.querySelectorAll('[data-canvas-view]').forEach(function(button){ button.setAttribute('aria-pressed',String(button.getAttribute('data-canvas-view')===canvasView)); });
    if(canvasView==='story') renderStoryboard();
    else requestAnimationFrame(function(){ syncCanvasGrid(); scheduleMap(); updateImageToolbar(); });
    updateImageToolbar();
  }
  function openSidePanel(kind,keepOpen){
    if(!sidePanel||!sideBody) return;
    var beforeWidth=canvas&&canvas.clientWidth||0;
    activeSidePanel=!keepOpen&&activeSidePanel===kind?'':kind;
    document.querySelectorAll('.nc-side-tool').forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-side')===activeSidePanel); });
    sidePanel.classList.toggle('on', !!activeSidePanel);
    sidePanel.classList.toggle('agent', activeSidePanel==='agent');
    if(canvasShell) canvasShell.classList.toggle('agent-open',activeSidePanel==='agent');
    if(fsAgent) fsAgent.classList.toggle('on',activeSidePanel==='agent');
    sidePanel.setAttribute('aria-hidden', activeSidePanel?'false':'true');
    if(activeSidePanel) renderSidePanel();
    if(canvas&&beforeWidth) requestAnimationFrame(function(){
      var afterWidth=canvas.clientWidth;
      canvas.scrollLeft=Math.max(0,canvas.scrollLeft+(beforeWidth-afterWidth)/2);
      syncCanvasGrid(); scheduleMap(); updateImageToolbar();
    });
  }
  function closeSidePanel(){
    var beforeWidth=canvas&&canvas.clientWidth||0;
    activeSidePanel='';
    if(sidePanel) sidePanel.classList.remove('on');
    if(sidePanel) sidePanel.classList.remove('agent');
    if(sidePanel) sidePanel.setAttribute('aria-hidden','true');
    if(canvasShell) canvasShell.classList.remove('agent-open');
    if(fsAgent) fsAgent.classList.remove('on');
    document.querySelectorAll('.nc-side-tool').forEach(function(b){ b.classList.remove('on'); });
    if(canvas&&beforeWidth) requestAnimationFrame(function(){
      var afterWidth=canvas.clientWidth;
      canvas.scrollLeft=Math.max(0,canvas.scrollLeft+(beforeWidth-afterWidth)/2);
      syncCanvasGrid(); scheduleMap(); updateImageToolbar();
    });
  }
  function renderSidePanel(){
    if(!activeSidePanel||!sideBody) return;
    if(activeSidePanel==='agent') renderAgentPanel();
    if(activeSidePanel==='nodes') renderNodeManager();
    if(activeSidePanel==='assets') renderAssetManager();
    if(activeSidePanel==='export') renderExportPanel();
  }
  function renderNodeManager(){
    if(sideTitle) sideTitle.textContent='节点管理';
    var ids=Object.keys(nodes);
    if(!ids.length){ sideBody.innerHTML='<div class="nc-side-empty">当前画布还没有节点</div>'; return; }
    sideBody.innerHTML='<div class="nc-side-list">'+ids.map(function(id){
      var n=nodes[id], color=(TYPE[n.type]&&TYPE[n.type].color)||'#94a4bb';
      return '<button class="nc-side-item" type="button" data-node-jump="'+escapeHtml(id)+'"><span class="nc-side-dot" style="background:'+color+'"></span><span class="nc-side-main"><span class="nc-side-title">'+escapeHtml(nodeDisplayName(n))+'</span><span class="nc-side-meta">'+escapeHtml(nodeTypeLabel(n.type))+' · '+escapeHtml(nodeStatusLabel(n))+'</span></span></button>';
    }).join('')+'</div>';
    sideBody.querySelectorAll('[data-node-jump]').forEach(function(btn){
      btn.onclick=function(){
        var n=nodes[btn.getAttribute('data-node-jump')];
        if(n){ selectNode(n); focusNode(n); updateState('已定位节点'); }
      };
    });
  }
  function normalizeAssetItem(item, source){
    item=item||{};
    var url=item.url||item.image_url||item.video_url||item.cover_url||item.cover||item.file_url||item.preview_url||'';
    if(!url&&item.file) url='/api/gen/file/'+item.file;
    var type=item.assetType||item.type||item.kind||source||'image';
    if(item.video_url||String(url).match(/\.(mp4|webm|mov)(\?|$)/i)) type='video';
    return {type:type==='video'?'video':'image',title:item.title||item.name||item.prompt||item.text||'未命名资产',url:url,source:source||'账户资产'};
  }
  function collectCanvasAssets(){
    var list=[];
    Object.keys(nodes).forEach(function(id){
      var n=nodes[id], title=nodeDisplayName(n);
      if(n.image) list.push({type:'image',title:title,url:n.image,source:'画布素材'});
      if(n.outputs&&n.outputs.image) list.push({type:'image',title:title,url:n.outputs.image,source:'画布结果'});
      if(n.outputs&&n.outputs.video) list.push({type:'video',title:title,url:n.outputs.video,source:'画布结果'});
    });
    return list;
  }
  function fetchAccountAssets(){
    if(accountAssetsPromise) return accountAssetsPromise;
    accountAssetsPromise=Promise.all([
      apiClient.json('/api/gen/history?limit=60').catch(function(){ return {items:[]}; }),
      apiClient.json('/api/gen/video/assets?limit=60').catch(function(){ return {items:[]}; })
    ]).then(function(all){
      accountAssets=[];
      (all[0].items||[]).forEach(function(x){ accountAssets.push(normalizeAssetItem(x,'账户图片')); });
      (all[1].items||[]).forEach(function(x){ accountAssets.push(normalizeAssetItem(x,'账户视频')); });
      accountAssetsLoaded=true;
      accountAssetsPromise=null;
      if(activeSidePanel==='assets') renderAssetManager();
      return accountAssets;
    });
    return accountAssetsPromise;
  }
  function renderAssetManager(){
    if(sideTitle) sideTitle.textContent='我的资产';
    var assets=collectCanvasAssets().concat(accountAssets).filter(function(a){ return !!a.url; });
    if(!accountAssetsLoaded) fetchAccountAssets();
    if(!assets.length){
      sideBody.innerHTML='<div class="nc-side-empty">'+(accountAssetsLoaded?'暂无可展示资产':'正在读取账户资产...')+'</div>';
      return;
    }
    sideBody.innerHTML='<div class="nc-asset-hint">拖到画布创建图片或视频节点 · 单击预览</div><div class="nc-asset-grid">'+assets.map(function(a,i){
      var thumb=a.type==='image'?' style="background-image:url(\''+escapeHtml(a.url)+'\')"':'';
      return '<button class="nc-asset-card '+escapeHtml(a.type)+'" type="button" draggable="'+(canEditCanvas()?'true':'false')+'" data-asset-idx="'+i+'"><div class="nc-asset-thumb"'+thumb+'>'+(a.type==='video'?'视频':'')+'</div><div class="nc-asset-info"><b>'+escapeHtml(a.title)+'</b><span>'+escapeHtml(a.source)+'</span></div></button>';
    }).join('')+'</div>';
    sideBody.querySelectorAll('[data-asset-idx]').forEach(function(btn){
      btn.ondragstart=function(e){
        var a=assets[parseInt(btn.getAttribute('data-asset-idx'),10)];
        if(!canEditCanvas()||!a||!a.url){ e.preventDefault(); return; }
        dragAsset=a;
        if(e.dataTransfer){ e.dataTransfer.effectAllowed='copy'; e.dataTransfer.setData('text/plain',a.title||'黄雀资产'); }
        canvas.classList.add('asset-drop-ready');
      };
      btn.ondragend=clearAssetDragState;
      btn.onclick=function(){
        var a=assets[parseInt(btn.getAttribute('data-asset-idx'),10)];
        if(!a||!a.url) return;
        if(a.type==='video') playableAssetUrl(a.url).then(function(u){ window.open(u,'_blank'); }).catch(function(){ window.open(a.url,'_blank'); });
        else window.open(a.url,'_blank');
      };
    });
  }
  function clearAssetDragState(){
    dragAsset=null;
    canvas.classList.remove('asset-drop-ready','asset-drop-target');
  }
  function addAssetAt(asset,pt){
    if(!canEditCanvas()||!asset||!asset.url) return;
    pushUndo();
    var type=asset.type==='video'?'videoAsset':'image', outputs={}, data={params:{title:String(asset.title||'').slice(0,80)},outputs:outputs};
    if(type==='videoAsset') outputs.video=asset.url;
    else{ data.image=asset.url; outputs.image=asset.url; }
    var node=addNode(type,Math.max(0,pt.x-125),Math.max(0,pt.y-70),data);
    selectNode(node);
    updateState(type==='videoAsset'?'已添加视频资产':'已添加图片资产');
  }
  function canvasContentBounds(){
    return graphApi.contentBounds(Object.keys(nodes).map(function(id){
      var n=nodes[id];
      return {id:id,x:n.x,y:n.y,width:(n.el&&n.el.offsetWidth)||250,height:(n.el&&n.el.offsetHeight)||160};
    }));
  }
  function renderExportPanel(){
    if(sideTitle) sideTitle.textContent='导出全局预览';
    var b=canvasContentBounds();
    sideBody.innerHTML='<div class="nc-export-box"><div class="nc-export-preview">'+(b?('预计导出 '+Math.round(b.w)+' × '+Math.round(b.h)+' JPG'):'当前画布为空，暂无可导出内容')+'</div><button id="ncExportJpg" class="nc-export-btn" type="button" '+(b?'':'disabled')+'>导出 JPG</button><div class="nc-side-empty" style="padding:0 4px;text-align:left;">导出内容包含节点、连线和图片预览，不包含工具栏。</div></div>';
    var btn=document.getElementById('ncExportJpg');
    if(btn) btn.onclick=function(){ exportCanvasJpg(); };
  }
  function agentSessionKey(){ return currentBoardScope+':'+String(currentBoardId||'draft'); }
  function agentErrorText(error,fallback){
    var message=String(error&&error.message||'').trim();
    if(!message||message.length>180||/<(?:!doctype|html|body|head)\b/i.test(message)) return fallback;
    if(error&&error.status===401) return '请先登录后使用画布 Agent';
    return message;
  }
  function currentAgentSession(){
    var key=agentSessionKey();
    if(!agentSessions[key]) agentSessions[key]={key:key,messages:[],draft:'',quote:null,points:null,quoteLoading:false,quoteError:'',pending:false,status:'',plan:null,applied:false};
    return agentSessions[key];
  }
  function loadAgentIP12Context(){
    if(agentIP12Loaded||agentIP12Loading) return;
    agentIP12Loading=true; agentIP12Error='';
    apiClient.json('/api/gen/digital-ip/projects',{cache:'no-store'}).then(function(data){
      var projects=Array.isArray(data&&data.items)?data.items:[];
      agentIP12Context=agentModule.buildIP12Context(projects[0]);
      agentIP12Loaded=true;
    }).catch(function(error){
      agentIP12Context=null; agentIP12Loaded=true;
      agentIP12Error=agentErrorText(error,'IP12 资料读取失败');
    }).finally(function(){
      agentIP12Loading=false;
      if(activeSidePanel==='agent') renderAgentPanel();
    });
  }
  function agentPageContext(){
    return {page:'canvas',path:location.pathname,title:document.title,can_edit:canEditCanvas(),selected_count:Math.min(30,selectedNodeIds().length)};
  }
  function agentIP12Label(){
    if(agentIP12Loading||!agentIP12Loaded) return '正在读取 IP12';
    if(agentIP12Context) return '已结合 IP12 · '+agentIP12Context.title;
    return agentIP12Error||'暂无 IP12 资料';
  }
  function startAgentPrompt(prompt){
    var session=currentAgentSession();
    session.draft=String(prompt||'').trim();
    openSidePanel('agent',true);
    var input=sideBody&&sideBody.querySelector('[data-agent-input]');
    if(input){ input.focus(); input.setSelectionRange(input.value.length,input.value.length); }
  }
  function agentNodeContent(node){
    if(!node) return '';
    if(node.type==='text'||node.type==='gen'||node.type==='video') return (node.params&&node.params.text)||'';
    if(node.type==='reverse') return (node.outputs&&node.outputs.prompt)||'';
    if(node.type==='image') return '图片素材（首版不读取图像内容）';
    if(node.type==='shortDrama') return '短剧项目阶段：'+String(node.params&&node.params.stage||'未开始');
    return '';
  }
  function buildAgentSnapshot(){
    var selected=selectedNodeIds();
    return agentModule.createSnapshot({
      projectId:agentSessionKey(),scope:currentBoardScope,
      selectedNodeIds:selected,
      nodes:Object.keys(nodes).map(function(id){
        var node=nodes[id];
        return {id:id,type:node.type,title:nodeDisplayName(node),content:agentNodeContent(node)};
      }),
      edges:edges.map(function(edge){return {from_node_id:edge.from.node,to_node_id:edge.to.node};})
    });
  }
  function renderAgentPanel(){
    if(sideTitle) sideTitle.textContent='Agent 工作台';
    if(!agentModule){ sideBody.innerHTML='<div class="nc-side-empty">Agent 模块未加载</div>'; return; }
    if(!agentIP12Loaded&&!agentIP12Loading) loadAgentIP12Context();
    var session=currentAgentSession();
    var selectionCount=selectedNodeIds().length;
    var messages=session.messages.map(function(message){
      return '<div class="nc-agent-message '+escapeHtml(message.role)+'">'+escapeHtml(message.content)+'</div>';
    }).join('');
    var plan='', actions=session.plan&&session.plan.actions||[], guides=session.plan&&session.plan.guides||[];
    if(session.plan&&(actions.length||guides.length||(session.plan.warnings||[]).length)){
      plan='<div class="nc-agent-plan">'+actions.map(function(action){
        return '<label><input type="checkbox" data-agent-action="'+escapeHtml(action.id)+'" checked '+(session.applied?'disabled':'')+'><span>'+escapeHtml(agentModule.actionLabel(action))+'</span></label>';
      }).join('')+(guides.length?'<div class="nc-agent-guides"><strong>推荐下一步</strong>'+guides.map(function(guide,index){
        return '<button class="nc-agent-guide" type="button" data-agent-guide="'+index+'"><b>'+escapeHtml(guide.label)+'</b><span>'+escapeHtml(guide.reason)+'</span></button>';
      }).join('')+'</div>':'')+(session.plan.warnings||[]).map(function(warning){return '<div class="nc-agent-warning">'+escapeHtml(warning)+'</div>';}).join('')
        +(actions.length?'<button class="nc-agent-apply" type="button" '+(session.applied||!canEditCanvas()?'disabled':'')+'>'+(session.applied?'已应用，可撤销':'确认应用所选操作')+'</button>':'')+'</div>';
    }
    var quoteText=session.quoteLoading?'正在读取报价…':session.quoteError?session.quoteError:session.quote==null?'报价不可用':('本次 '+session.quote+' 点');
    var balance=session.points==null?'':('余额 '+session.points+' 点');
    var insufficient=session.quote!=null&&session.points!=null&&session.points<session.quote;
    var starters=!session.messages.length?'<div class="nc-agent-starters">'
      +'<button class="nc-agent-starter" type="button" data-agent-prompt="结合我的 IP12 和当前画布，规划第一条最适合我的短视频内容，并告诉我接下来去哪个页面继续。"><b>结合 IP12 规划</b>从客户资料到第一条内容</button>'
      +'<button class="nc-agent-starter" type="button" data-agent-prompt="帮我搭建一个短视频脚本工作流：先创建脚本，再创建画面和视频生成草稿。"><b>搭建短视频流程</b>脚本、画面到视频草稿</button>'
      +'<button class="nc-agent-starter" type="button" data-agent-prompt="分析我选中的节点，优化其中的文本内容并保留原意。"><b>优化选中内容</b>只处理当前选区</button>'
      +'<button class="nc-agent-starter" type="button" data-agent-prompt="帮我搭建一个产品图文方案，创建卖点文案和图片生成草稿。"><b>创建图文方案</b>卖点文案与作图草稿</button>'
      +'</div>':'';
    sideBody.innerHTML='<div class="nc-agent"><div class="nc-agent-hero"><span class="nc-agent-orb">✦</span><strong>从 IP12 到创作下一步</strong><p>Agent 结合当前页面和客户资料给出方案；图片与视频只建草稿，确认后才会修改画布。</p><div class="nc-agent-context"><span><i></i>'+(selectionCount?('已读取 '+selectionCount+' 个选中节点'):'已读取当前画布')+'</span><span>'+escapeHtml(agentIP12Label())+'</span></div></div>'+starters
      +'<div class="nc-agent-messages">'+(messages||'<div class="nc-agent-message">你可以直接描述目标，也可以从上面的常用场景开始。</div>')+(session.status?'<div class="nc-agent-message">'+escapeHtml(session.status)+'</div>':'')+plan+'</div>'
      +'<div class="nc-agent-compose"><textarea data-agent-input maxlength="2000" placeholder="描述你想完成的内容，或 @ 选中的节点…" '+(session.pending||session.quote==null?'disabled':'')+'>'+escapeHtml(session.draft||'')+'</textarea>'
      +'<div class="nc-agent-quote"><span>'+escapeHtml(quoteText)+'</span><span>'+escapeHtml(balance)+'</span></div>'
      +(session.quoteError?'<button class="nc-agent-apply" type="button" data-agent-retry>重新读取报价</button>':'')
      +'<button class="nc-agent-submit" type="button" '+(session.pending||session.quote==null||insufficient?'disabled':'')+'>'+(session.pending?'Agent 思考中…':insufficient?'点数不足':session.quote==null?'等待报价':('发送 · '+session.quote+' 点'))+'</button></div></div>';
    var input=sideBody.querySelector('[data-agent-input]');
    var submit=sideBody.querySelector('.nc-agent-submit');
    if(submit) submit.onclick=function(){ submitAgentTurn(input&&input.value); };
    if(input){
      input.oninput=function(){ session.draft=input.value; };
      input.onkeydown=function(e){ if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){ e.preventDefault(); submitAgentTurn(input.value); } };
    }
    sideBody.querySelectorAll('[data-agent-prompt]').forEach(function(btn){ btn.onclick=function(){ startAgentPrompt(btn.getAttribute('data-agent-prompt')); }; });
    sideBody.querySelectorAll('[data-agent-guide]').forEach(function(btn){ btn.onclick=function(){ followAgentGuide(guides[Number(btn.getAttribute('data-agent-guide'))]); }; });
    var apply=sideBody.querySelector('.nc-agent-apply:not([data-agent-retry])');
    if(apply) apply.onclick=function(){
      var ids=Array.prototype.map.call(sideBody.querySelectorAll('[data-agent-action]:checked'),function(box){return box.getAttribute('data-agent-action');});
      applyAgentPlan(session,ids);
    };
    var retry=sideBody.querySelector('[data-agent-retry]');
    if(retry) retry.onclick=function(){ session.quoteError=''; session.quote=null; loadAgentQuote(session); renderAgentPanel(); };
    if(session.quote==null&&!session.quoteLoading&&!session.quoteError) loadAgentQuote(session);
  }
  function followAgentGuide(guide){
    if(!guide) return;
    if(guide.target==='ip12'){
      location.href='/workbench/ip12.html'+(agentIP12Context?'?project='+encodeURIComponent(agentIP12Context.project_id):'');
      return;
    }
    var targets={
      script:{url:'/workbench/script.html?prefill=ip12',handoff:'script_studio'},
      image:{url:'/workbench/banana.html?prefill=ip12',handoff:'image_studio'},
      video:{url:'/workbench/video.html?prefill=ip12',handoff:'video_studio'}
    };
    var target=targets[guide.target];
    if(!target) return;
    try{
      sessionStorage.setItem('hq_ip12_product_handoff_v1',JSON.stringify({
        version:1,target:target.handoff,prompt:String(guide.prompt||'').slice(0,1000),created_at:Date.now()
      }));
    }catch(error){ updateState('浏览器无法暂存 Agent 草稿'); return; }
    location.href=target.url;
  }
  function loadAgentQuote(session){
    session.quoteLoading=true;
    apiClient.json('/api/gen/canvas-agent/quote',{method:'POST',body:{}}).then(function(data){
      session.quote=Number(data.cost);
      session.points=Number(data.points);
      session.quoteError='';
    }).catch(function(error){
      session.quote=null;
      session.quoteError=agentErrorText(error,'报价读取失败，请确认 Agent 服务已启用');
    }).finally(function(){
      session.quoteLoading=false;
      if(activeSidePanel==='agent'&&currentAgentSession()===session) renderAgentPanel();
    });
  }
  function agentHeaders(idempotencyKey){
    var headers={'Idempotency-Key':idempotencyKey};
    if(currentBoardScope==='collab'&&currentBoardId) headers['X-Canvas-Board-Id']=String(currentBoardId);
    return headers;
  }
  function retryAgentSubmission(body,headers,attempt){
    return apiClient.json('/api/gen/canvas_agent',{method:'POST',body:body,headers:headers,timeout:12000}).catch(function(error){
      var retryable=error&&(error.code==='timeout'||error.code==='idempotency_in_progress');
      if(!retryable||attempt>=4) throw error;
      return new Promise(function(resolve){setTimeout(resolve,800*(attempt+1));}).then(function(){return retryAgentSubmission(body,headers,attempt+1);});
    });
  }
  function submitAgentTurn(prompt){
    prompt=String(prompt||'').trim();
    var session=currentAgentSession();
    if(!prompt||session.pending||session.quote==null) return;
    var snapshot;
    try{ snapshot=buildAgentSnapshot(); }
    catch(error){ session.messages.push({role:'error',content:error.message}); renderAgentPanel(); return; }
    var history=session.messages.filter(function(message){return message.role==='user'||message.role==='assistant';}).slice(-8);
    var body=Object.assign({},snapshot,{prompt:prompt,history:history,quoted_cost:session.quote,page_context:agentPageContext()});
    if(agentIP12Context) body.ip12_context=agentIP12Context;
    var requestKey='canvas-agent-'+(window.crypto&&crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2));
    var headers=agentHeaders(requestKey);
    session.draft='';
    session.messages.push({role:'user',content:prompt});
    session.pending=true; session.status='正在读取画布并规划操作…'; session.plan=null; session.applied=false;
    renderAgentPanel();
    retryAgentSubmission(body,headers,0).then(function(data){
      if(!data.job_id) throw new Error(data.detail||'Agent 任务提交失败');
      session.points=data.points_left==null?session.points:Number(data.points_left);
      if(window.HQ&&HQ.refreshPoints) HQ.refreshPoints();
      return apiModule.poll({
        request:function(){return apiClient.json('/api/gen/job/'+data.job_id);},intervalMs:1400,maxMs:300000,
        inspect:function(job){
          if(job.status==='done') return {done:true,value:typeof job.result==='string'?JSON.parse(job.result):job.result};
          if(job.status==='error'||job.status==='failed') return {error:new Error(job.error||'Agent 思考失败，点数已自动退回')};
          return {pending:true};
        },
        onProgress:function(job,seconds){session.status='Agent 思考中，已用 '+seconds+' 秒…'; if(activeSidePanel==='agent'&&currentAgentSession()===session) renderAgentPanel();},
        timeoutError:function(){return new Error('Agent 响应超时，请稍后查看任务记录');}
      });
    }).then(function(result){
      var content=String(result&&result.content||'Agent 已完成分析。');
      session.messages.push({role:'assistant',content:content});
      session.plan=result&&result.plan||null;
      session.status=session.plan&&(session.plan.actions||[]).length?'请勾选并确认要应用的操作。':session.plan&&(session.plan.guides||[]).length?'请选择一个推荐的下一步。':'';
    }).catch(function(error){
      session.messages.push({role:'error',content:agentErrorText(error,'Agent 请求失败，请稍后重试')});
      session.status='';
    }).finally(function(){
      session.pending=false;
      session.quote=null; session.points=null; session.quoteError='';
      if(window.HQ&&HQ.refreshPoints) HQ.refreshPoints();
      if(activeSidePanel==='agent'&&currentAgentSession()===session) renderAgentPanel();
    });
  }
  function addAgentConnection(sourceId,targetId){
    var source=nodes[sourceId],target=nodes[targetId];
    if(!source||!target) return false;
    var ports=agentModule.connectionPorts(source.type,target.type);
    if(!ports) return false;
    var multiImage=ports.to==='image'&&(target.type==='gen'||target.type==='video');
    edges=edges.filter(function(edge){
      var duplicate=edge.from.node===sourceId&&edge.from.port===ports.from&&edge.to.node===targetId&&edge.to.port===ports.to;
      var sameInput=edge.to.node===targetId&&edge.to.port===ports.to;
      return multiImage?!duplicate:!sameInput;
    });
    edges.push({from:{node:sourceId,port:ports.from},to:{node:targetId,port:ports.to}});
    return true;
  }
  function applyAgentPlan(session,actionIds){
    if(!canEditCanvas()||!session.plan||session.applied) return;
    try{
      var current=buildAgentSnapshot();
      agentModule.validatePlan(current,session.plan);
      var wanted={}; (actionIds||[]).forEach(function(id){wanted[id]=true;});
      var actions=session.plan.actions.filter(function(action){return !!wanted[action.id];});
      if(!actions.length) throw new Error('请至少选择一个 Agent 操作');
      pushUndo();
      var base=viewportNodePoint(),created=[],nextSelection=null;
      actions.forEach(function(action,index){
        var node;
        if(action.type==='create_text_node'){
          node=addNode('text',base.x+index*34,base.y+index*34,{params:{title:action.title,text:action.content}});
          created.push(node.id);
        }else if(action.type==='update_text_node'){
          node=nodes[action.node_id];
          node.params.text=action.content;
          if(action.title) node.params.title=action.title;
          applyNodeUI(node);
        }else if(action.type==='create_generation_draft'){
          var type=action.mode==='image'?'gen':action.mode==='video'?'video':'text';
          node=addNode(type,base.x+index*34,base.y+index*34,{params:{title:action.title,text:action.prompt}});
          created.push(node.id);
          (action.connect_from||[]).forEach(function(sourceId){addAgentConnection(sourceId,node.id);});
        }else if(action.type==='connect_nodes'){
          addAgentConnection(action.from_node_id,action.to_node_id);
        }else if(action.type==='select_nodes'){
          nextSelection=action.node_ids;
        }
      });
      redraw(); refreshAllGenRefs();
      selectNodesByIds(created.length?created:(nextSelection||selectedNodeIds()));
      session.applied=true; session.status='操作已应用，可用底部撤销恢复。';
      updateState('Agent 操作已应用');
      if(activeSidePanel==='agent') renderAgentPanel();
    }catch(error){
      session.messages.push({role:'error',content:(error&&error.message)||'Agent 操作应用失败'});
      if(activeSidePanel==='agent') renderAgentPanel();
    }
  }
  function exportCanvasJpg(){
    var bounds=canvasContentBounds();
    if(!bounds){ updateState('画布为空'); return; }
    updateState('正在导出预览...');
    var exportNodes=Object.keys(nodes).map(function(id){
      var node=nodes[id],type=TYPE[node.type]||{};
      return {id:node.id,type:node.type,x:node.x,y:node.y,width:(node.el&&node.el.offsetWidth)||250,height:(node.el&&node.el.offsetHeight)||160,collapsed:node.el?node.el.classList.contains('collapsed'):!!node.collapsed,params:stateApi.cloneSnapshot(node.params||{}),outputs:shortDramaNodeOutputs(node),image:node.image||'',typeName:type.name||'',typeColor:type.color||''};
    });
    var exportEdges=edges.map(function(edge){
      var from=portCenter(edge.from.node,'out',edge.from.port),to=portCenter(edge.to.node,'in',edge.to.port);
      return from&&to?{from:{x:from.x,y:from.y},to:{x:to.x,y:to.y}}:null;
    }).filter(Boolean);
    return canvasExporter.exportJpeg({
      bounds:bounds,nodes:exportNodes,edges:exportEdges,theme:document.documentElement.getAttribute('data-theme')==='light'?'light':'dark',
      createCanvas:function(){return document.createElement('canvas');},loadImage:function(src){return canvasExporter.loadExportImage(src,{fetchBlob:function(url){return apiClient.asset(url);},createObjectURL:URL.createObjectURL.bind(URL),revokeObjectURL:URL.revokeObjectURL.bind(URL),createImage:function(){return new Image();}});},createObjectURL:URL.createObjectURL.bind(URL),revokeObjectURL:URL.revokeObjectURL.bind(URL),
      download:function(href,filename){var a=document.createElement('a');a.href=href;a.download=filename;document.body.appendChild(a);a.click();a.remove();},now:function(){return new Date();}
    }).then(function(){
      updateState('已导出 JPG');
    }).catch(function(err){
      console.error('canvas export failed',err);
      updateState('导出失败，请重试');
    });
  }
  function pushUndo(snap){
    if(!canEditCanvas()) return;
    if(restoring) return;
    history.push(snap||snapshot());
    updateState();
  }
  function restoreSnapshot(snap){
    if(!snap) return;
    var options=arguments[1]||{};
    var materializeSource=options.source||'restore';
    snap=sanitizeShortDramaSnapshot(snap);
    destroyAllShortDramaWorkspaces();
    digitalPresenterWorkspaceLifecycle.restoreScope(currentShortDramaScopeKey());
    clearDigitalPresenterWorkspaceRefs();
    restoring=true;
    var restoredNodes=(snap.nodes||[]).filter(function(n){
      return n&&nodeCreationPolicy.canMaterialize(n.type,materializeSource);
    });
    var restoredIds={};restoredNodes.forEach(function(n){restoredIds[n.id]=true;});
    Object.keys(nodes).forEach(function(id){ if(nodes[id]&&nodes[id].el) nodes[id].el.remove(); });
    nodes={}; edges=stateApi.cloneSnapshot(snap.edges||[]); nid=snap.nid||0; pendingPort=null; dragPort=null; selectedNode=null; selectedNodes={}; selectedEdge=-1; runLabel=snap.runLabel||'就绪';
    edges=edges.filter(function(e){
      return e&&e.from&&e.to&&restoredIds[e.from.node]&&restoredIds[e.to.node];
    });
    restoredNodes.forEach(function(n){ addNode(n.type,n.x,n.y,n,materializeSource); });
    if(snap.zoom){ zoom=Math.max(.5,Math.min(1.6,snap.zoom)); inner.style.transform='scale('+zoom+')'; }
    if(snap.scroll){ canvas.scrollLeft=snap.scroll.left||0; canvas.scrollTop=snap.scroll.top||0; }
    syncCanvasGrid();
    redraw(); refreshAllGenRefs(); updateSelectedRegion();
    updateState('已撤销');
    restoring=false;
  }
  function appendTemplateToCanvas(item){
    if(!canEditCanvas()) return;
    if(!item||!item.data){ updateState('没有可载入的模板'); return; }
    var snap=sanitizeTemplateSnap(item.data), list=snap.nodes||[];
    if(list.some(function(n){return !nodeCreationPolicy.canMaterialize(n.type,'create');})){
      updateState('当前画布无权载入模板中的数字人口播节点');return;
    }
    if(!list.length){ updateState('模板为空'); return; }
    pushUndo();
    var minX=Infinity, minY=Infinity, maxX=0, maxY=0;
    Object.keys(nodes).forEach(function(id){
      var n=nodes[id], w=(n.el&&n.el.offsetWidth)||250, h=(n.el&&n.el.offsetHeight)||160;
      maxX=Math.max(maxX,n.x+w); maxY=Math.max(maxY,n.y+h);
    });
    list.forEach(function(n){
      minX=Math.min(minX,n.x||0);
      minY=Math.min(minY,n.y||0);
    });
    if(!isFinite(minX)) minX=0;
    if(!isFinite(minY)) minY=0;
    var target=Object.keys(nodes).length?{x:maxX+140,y:Math.max(80,viewportTopLeftPoint().y+80)}:viewportCenterPoint();
    var idMap={}, created=[];
    list.forEach(function(n){
      var data=stateApi.cloneSnapshot(n);
      var oldId=data.id;
      data.id=currentBoardScope==='collab'&&collabSync?collabSync.makeNodeId(collabNodeSeed,nid+1):'n'+(nid+1);
      data.x=target.x+((n.x||0)-minX);
      data.y=target.y+((n.y||0)-minY);
      idMap[oldId]=data.id;
      created.push(addNode(data.type,data.x,data.y,data));
    });
    (snap.edges||[]).forEach(function(e){
      if(!e||!e.from||!e.to) return;
      var from=idMap[e.from.node], to=idMap[e.to.node];
      if(from&&to) edges.push({from:{node:from,port:e.from.port},to:{node:to,port:e.to.port}});
    });
    redraw();
    refreshAllGenRefs();
    if(created[0]){ selectNode(created[0]); focusNode(created[0]); }
    saveCurrentBoard(snapshot());
    scheduleSave();
    updateState('模板已追加到画布');
  }
  function undo(){
    if(!canEditCanvas()) return;
    var snap=history.undo(snapshot());
    if(!snap) return;
    restoreSnapshot(snap);
    updateState('已撤销');
  }
  function redo(){
    if(!canEditCanvas()) return;
    var snap=history.redo(snapshot());
    if(!snap) return;
    restoreSnapshot(snap);
    updateState('已重做');
  }

  // ---------- 连线绘制 ----------
  function portCenter(nodeId, kind, port){
    var n=nodes[nodeId]; if(!n) return null;
    var p=n.el.querySelector('.nc-port[data-kind="'+kind+'"][data-port="'+port+'"]'); if(!p) return null;
    return { x:n.x + p.offsetLeft + 6.5, y:n.y + p.offsetTop + 6.5 };
  }
  function edgePath(a,b){
    var dx=Math.max(40,Math.abs(b.x-a.x)*0.5);
    return 'M'+a.x+','+a.y+' C'+(a.x+dx)+','+a.y+' '+(b.x-dx)+','+b.y+' '+b.x+','+b.y;
  }
  function redraw(){
    var s='';
    edges.forEach(function(e,i){
      var a=portCenter(e.from.node,'out',e.from.port), b=portCenter(e.to.node,'in',e.to.port);
      if(!a||!b) return;
      var d=edgePath(a,b), cls=i===selectedEdge?' nc-edge-sel':'';
      s+='<path class="nc-edge-line'+cls+'" d="'+d+'" fill="none" stroke="rgba(231,178,76,.55)" stroke-width="2"/>';
      s+='<path class="nc-edge-hit" data-edge="'+i+'" d="'+d+'" fill="none" stroke="rgba(255,255,255,0)" stroke-width="14"/>';
    });
    if(dragPort&&dragPort.active&&dragPort.start){
      s+='<path d="'+edgePath(dragPort.start,{x:dragPort.x,y:dragPort.y})+'" fill="none" stroke="rgba(45,212,191,.72)" stroke-width="2.5" stroke-dasharray="6 6"/>';
    }
    svg.innerHTML=s;
    scheduleMap();
    updateState();
  }
  function scheduleMap(){
    if(mapDirty) return;
    mapDirty=true;
    requestAnimationFrame(function(){ mapDirty=false; drawMap(); });
  }
  function drawMap(){
    if(!mapSvg||!canvas) return;
    var w=164, h=104, iw=innerWidth(), ih=innerHeight();
    var sx=w/iw, sy=h/ih, s='';
    Object.keys(nodes).forEach(function(id){
      var n=nodes[id], nw=Math.max(3,(n.el.offsetWidth||250)*sx), nh=Math.max(3,(n.el.offsetHeight||150)*sy);
      s+='<rect class="nc-map-node'+(n.type==='image'?' img':'')+'" x="'+(n.x*sx).toFixed(1)+'" y="'+(n.y*sy).toFixed(1)+'" width="'+nw.toFixed(1)+'" height="'+nh.toFixed(1)+'" rx="1.6"/>';
    });
    var vx=(canvas.scrollLeft-CANVAS_VIEW_PAD)/zoom*sx, vy=(canvas.scrollTop-CANVAS_VIEW_PAD)/zoom*sy, vw=canvas.clientWidth/zoom*sx, vh=canvas.clientHeight/zoom*sy;
    s+='<rect class="nc-map-view" x="'+Math.max(0,vx).toFixed(1)+'" y="'+Math.max(0,vy).toFixed(1)+'" width="'+Math.min(w,vw).toFixed(1)+'" height="'+Math.min(h,vh).toFixed(1)+'" rx="2"/>';
    mapSvg.innerHTML=s;
  }
  function moveViewFromMap(e){
    if(!map) return;
    var r=map.getBoundingClientRect(), x=(e.clientX-r.left)/r.width, y=(e.clientY-r.top)/r.height;
    canvas.scrollLeft=Math.max(0,CANVAS_VIEW_PAD+x*innerWidth()*zoom-canvas.clientWidth/2);
    canvas.scrollTop=Math.max(0,CANVAS_VIEW_PAD+y*innerHeight()*zoom-canvas.clientHeight/2);
    scheduleMap();
  }

  // ---------- 建节点 ----------
  function addNode(type, x, y, data,materializeSource){
    materializeSource=materializeSource||'create';
    if(!nodeCreationPolicy.canMaterialize(type,materializeSource)) return null;
    if(type==='shortDrama'&&data) data=shortDramaModule.sanitizeNodeData(data);
    if(type==='digitalPresenter'&&data) data=stateApi.sanitizeNodeData(data,{digitalPresenter:digitalPresenterModule.sanitizeNodeData});
    var t=TYPE[type], nextNid=++nid, id=currentBoardScope==='collab'&&collabSync?collabSync.makeNodeId(collabNodeSeed,nextNid):'n'+nextNid;
    if(data&&data.id){ id=data.id; var m=String(id).match(/^n(\d+)$/); if(m) nid=Math.max(nid,parseInt(m[1],10)); }
    var fallback=x==null||y==null?viewportNodePoint():null;
    var node={ id:id, type:type, x:(x==null?fallback.x:x), y:(y==null?fallback.y:y), width:Number(data&&data.width)||0, height:Number(data&&data.height)||0, collapsed:!!(data&&data.collapsed), params:Object.assign({engine:'nb2',channel:'grok',ratio:'16:9',duration:'5',quality:'hd',title:'',remark:''},(data&&data.params)||{}), outputs:stateApi.cloneSnapshot((data&&data.outputs)||{}), image:(data&&data.image)||null };
    if(type==='shortDrama') node.params=normalizeShortDramaNodeParams(node.params);
    if(type==='digitalPresenter') node.params=normalizeDigitalPresenterNodeParams(node.params);
    var el=document.createElement('div'); el.className='nc-node'+(type==='shortDrama'?' nc-node-short-drama':type==='digitalPresenter'?' nc-node-digital-presenter':''); el.style.left=node.x+'px'; el.style.top=node.y+'px';
    var body='';
    if(type==='text') body='<textarea class="nc-in" data-f="text" rows="3" placeholder="输入提示词，作为下游作图的词…"></textarea>';
    if(type==='image') body='<label class="nc-drop" data-f="drop"><input type="file" accept="image/*" data-f="file" style="display:none">点击上传<br>或按 Ctrl+V 粘贴</label>';
    if(type==='reverse') body='<div class="nc-lab">输入：图片 → 输出：提示词</div><button class="nc-go" data-f="run">反推提示词（2点）</button><textarea class="nc-in" data-f="out" rows="3" placeholder="反推结果会出现在这里" style="margin-top:8px;"></textarea>';
    if(type==='gen') body='<div class="nc-lab">引擎</div><div class="nc-seg" data-f="engine"><span class="nc-chip on" data-v="nb2">纳米香蕉 2</span><span class="nc-chip" data-v="pro">纳米香蕉 Pro</span><span class="nc-chip" data-v="gpt">黄雀引擎 2</span><span class="nc-chip" data-v="zelong">泽龙AI</span></div>'
      +'<div class="nc-seg" data-f="ratio"><span class="nc-chip on" data-v="9:16">9:16</span><span class="nc-chip" data-v="1:1">1:1</span><span class="nc-chip" data-v="16:9">16:9</span><span class="nc-chip" data-v="3:4">3:4</span></div>'
      +'<div class="nc-refbar" data-f="refs"><span>参考图 0 张</span><div class="nc-refthumbs"></div></div>'
      +'<textarea class="nc-in" data-f="text" rows="2" placeholder="提示词（也可由上游文本/反推节点连入）"></textarea>'
      +'<button class="nc-go" data-f="run">生成图片</button><div class="nc-drop" data-f="result" style="display:none;"></div>';
    if(type==='video') body='<div class="nc-lab">模型</div><div class="nc-seg" data-f="channel"><span class="nc-chip on" data-v="grok">果肉视频</span><span class="nc-chip" data-v="micro">豆姐视频</span></div>'
      +'<div class="nc-seg" data-f="ratio"><span class="nc-chip" data-v="9:16">9:16</span><span class="nc-chip on" data-v="16:9">16:9</span><span class="nc-chip" data-v="1:1">1:1</span></div>'
      +'<div data-f="videoWarn" style="margin-top:8px; font-size:12px; line-height:1.55; color:#b5892f;">果肉视频当前优先建议 16:9（横屏），其余比例暂时大概率失败</div>'
      +'<div class="nc-seg" data-f="duration"><span class="nc-chip on" data-v="5">5s</span><span class="nc-chip" data-v="10">10s</span></div>'
      +'<div class="nc-refbar" data-f="refs"><span>参考图 0 张</span><div class="nc-refthumbs"></div></div>'
      +'<textarea class="nc-in" data-f="text" rows="2" placeholder="视频提示词（也可由上游文本/反推节点连入）"></textarea>'
      +'<button class="nc-go" data-f="run">生成视频</button><div class="nc-video-result" data-f="videoResult"></div>';
    if(type==='videoAsset') body='<div class="nc-video-result" data-f="videoResult"></div>';
    if(type==='shortDrama') body='<div class="nc-short-drama-summary"><div><span>画幅与时长</span><strong data-f="shortDramaRatio"></strong></div><div><span>当前阶段</span><strong data-f="shortDramaStage"></strong></div><div><span>完成进度</span><strong data-f="shortDramaProgress"></strong></div><div><span>点数</span><strong data-f="shortDramaPoints"></strong></div></div>'
      +'<button class="nc-go nc-short-drama-open" type="button" data-f="openShortDrama">打开短剧工作区</button>';
    if(type==='digitalPresenter') body='<div class="nc-digital-presenter-summary"><div><span>画幅与时长</span><strong data-f="digitalPresenterRatio"></strong></div><div><span>当前阶段</span><strong data-f="digitalPresenterStage"></strong></div><div><span>完成进度</span><strong data-f="digitalPresenterProgress"></strong></div><div><span>点数</span><strong data-f="digitalPresenterPoints"></strong></div></div>'
      +'<button class="nc-go" type="button" data-f="openDigitalPresenter">打开数字人口播工作区</button>';
    el.innerHTML='<div class="nc-head" data-f="head"><span style="display:flex;align-items:center;gap:7px;min-width:0;"><span class="dot" style="background:'+t.color+'"></span><span class="nc-node-title" data-f="headTitle">'+escapeHtml(node.params.title||t.name)+'</span><span class="nc-remark-mark" data-f="remarkMark" title="有备注">注</span></span><span class="nc-actions"><span class="nc-fold" data-f="fold" title="折叠/展开">−</span><span class="nc-x" data-f="del">×</span></span></div>'
      +'<div class="nc-body">'+body+'<div class="nc-note" data-f="note"></div></div>';
    ['n','ne','e','se','s','sw','w','nw'].forEach(function(direction){ var handle=document.createElement('button'); handle.type='button'; handle.className='nc-resize-handle nc-resize-'+direction; handle.setAttribute('data-resize',direction); handle.setAttribute('aria-label','调整节点大小'); el.appendChild(handle); });
    if(node.width&&node.height){ el.style.width=node.width+'px'; el.style.height=node.height+'px'; el.classList.add('resized'); }
    inner.appendChild(el); node.el=el;
    // ports
    (t.ins||[]).forEach(function(p,i){ var d=document.createElement('div'); d.className='nc-port pin'+(p==='image'?' img':''); d.setAttribute('data-kind','in'); d.setAttribute('data-port',p); d.title='输入:'+p; d.style.top=(46+i*22)+'px'; el.appendChild(d); });
    (t.outs||[]).forEach(function(p,i){ var d=document.createElement('div'); d.className='nc-port pout'+(p==='image'?' img':''); d.setAttribute('data-kind','out'); d.setAttribute('data-port',p); d.title='输出:'+p; d.style.top=(46+i*22)+'px'; el.appendChild(d); });
    nodes[id]=node; applyNodeUI(node); wireNode(node);
    if(data&&data.state) node.el.setAttribute('data-state',data.state);
    if(data&&data.note) noteOf(node,data.note);
    setCollapsed(node,node.collapsed,true);
    ensureNodeVisibleBounds(node);
    redraw(); refreshAllGenRefs(); updateState();
    return node;
  }
  function setCollapsed(node, collapsed, silent){
    if(!node||!node.el) return;
    node.collapsed=!!collapsed;
    node.el.classList.toggle('collapsed', node.collapsed);
    var f=node.el.querySelector('[data-f="fold"]');
    if(f) f.textContent=node.collapsed?'+':'−';
    if(!silent){ redraw(); updateState(node.collapsed?'节点已折叠':'节点已展开'); }
  }
  function toggleCollapsed(node){
    if(!canEditCanvas()) return;
    if(!node) return;
    pushUndo();
    setCollapsed(node,!node.collapsed);
  }
  function applyNodeUI(node){
    var el=node.el;
    refreshNodeMeta(node);
    var txt=el.querySelector('[data-f="text"]');
    if(txt) txt.value=node.params.text||node.outputs.prompt||'';
    var out=el.querySelector('[data-f="out"]');
    if(out) out.value=node.outputs.prompt||'';
    el.querySelectorAll('.nc-seg').forEach(function(seg){
      var f=seg.getAttribute('data-f'), val=node.params[f];
      seg.querySelectorAll('.nc-chip').forEach(function(c){ c.classList.toggle('on', c.getAttribute('data-v')===val); });
    });
    var drop=el.querySelector('[data-f="drop"]');
    if(drop&&node.image){ drop.style.backgroundImage='url('+node.image+')'; drop.innerHTML=''; }
    var result=el.querySelector('[data-f="result"]');
    if(result&&node.outputs.image){ result.style.display='block'; result.style.backgroundImage='url("'+node.outputs.image+'")'; result.innerHTML=''; }
    var videoResult=el.querySelector('[data-f="videoResult"]');
    if(videoResult&&node.outputs.video) renderVideoResult(node,node.outputs.video);
    refreshGenRefs(node);
    refreshVideoNodeHint(node);
    refreshShortDramaNode(node);
    refreshDigitalPresenterNode(node);
  }
  function inputVals(nodeId, port){
    return edges.filter(function(e){ return e.to.node===nodeId && e.to.port===port; }).map(function(e){
      var up=nodes[e.from.node];
      return up&&up.outputs?up.outputs[e.from.port]:null;
    }).filter(function(v){ return !!v; });
  }
  function refImagesForNode(node){
    if(!node||(node.type!=='gen'&&node.type!=='video')) return [];
    return inputVals(node.id,'image');
  }
  function refreshGenRefs(node){
    if(!node||(node.type!=='gen'&&node.type!=='video')||!node.el) return;
    var bar=node.el.querySelector('[data-f="refs"]');
    if(!bar) return;
    var refs=refImagesForNode(node), label=bar.querySelector('span'), thumbs=bar.querySelector('.nc-refthumbs');
    bar.classList.toggle('has-ref',refs.length>0);
    if(label) label.textContent='参考图 '+refs.length+' 张';
    if(!thumbs) return;
    thumbs.innerHTML='';
    refs.slice(0,3).forEach(function(src){
      var d=document.createElement('div');
      d.className='nc-refthumb';
      d.style.backgroundImage='url("'+String(src).replace(/"/g,'%22')+'")';
      thumbs.appendChild(d);
    });
    if(refs.length>3){
      var more=document.createElement('div');
      more.className='nc-refmore';
      more.textContent='+'+(refs.length-3);
      thumbs.appendChild(more);
    }
  }
  function refreshAllGenRefs(){
    Object.keys(nodes).forEach(function(id){ refreshGenRefs(nodes[id]); });
  }
  function refreshNodeMeta(node){
    if(!node||!node.el) return;
    var title=(node.params.title||'').trim();
    var remark=(node.params.remark||'').trim();
    var h=node.el.querySelector('[data-f="headTitle"]');
    if(h) h.textContent=title||TYPE[node.type].name;
    node.el.classList.toggle('has-remark', !!remark);
    var mark=node.el.querySelector('[data-f="remarkMark"]');
    if(mark) mark.title=remark?('备注：'+remark):'有备注';
    node.el.title=remark?('备注：'+remark):'';
  }
  function noteOf(node,msg,color){ var n=node.el.querySelector('[data-f="note"]'); if(n){ n.textContent=msg||''; n.style.color=color||'#5c6b82'; } }
  function innerWidth(){ return inner.offsetWidth||CANVAS_BASE_W; }
  function innerHeight(){ return inner.offsetHeight||CANVAS_BASE_H; }
  function syncCanvasGrid(){
    canvas.style.setProperty('--grid-minor',(24*zoom)+'px');
    canvas.style.setProperty('--grid-major',(120*zoom)+'px');
    canvas.style.setProperty('--grid-x',(CANVAS_VIEW_PAD-canvas.scrollLeft)+'px');
    canvas.style.setProperty('--grid-y',(CANVAS_VIEW_PAD-canvas.scrollTop)+'px');
  }
  function ensureCanvasBounds(x,y){
    var nextW=Math.max(innerWidth(),Math.ceil(x+CANVAS_GROW_PAD));
    var nextH=Math.max(innerHeight(),Math.ceil(y+CANVAS_GROW_PAD));
    var changed=false;
    if(nextW>innerWidth()){ inner.style.width=nextW+'px'; changed=true; }
    if(nextH>innerHeight()){ inner.style.height=nextH+'px'; changed=true; }
    if(changed) scheduleMap();
  }
  function ensureNodeVisibleBounds(node){
    if(!node||!node.el) return;
    ensureCanvasBounds(node.x+(node.el.offsetWidth||260),node.y+(node.el.offsetHeight||180));
  }
  function setConnectHints(port){
    document.querySelectorAll('.nc-port.can-connect,.nc-port.dragging').forEach(function(p){ p.classList.remove('can-connect','dragging'); });
    if(!port) return;
    document.querySelectorAll('.nc-port[data-kind="in"][data-port="'+port+'"]').forEach(function(p){ p.classList.add('can-connect'); });
  }
  function clearConnectHints(){ setConnectHints(null); canvas.classList.remove('connecting'); }
  function connectEdge(from,to){
    if(!canEditCanvas()) return false;
    if(!from||!to||from.port!==to.port||from.node===to.node) return false;
    var multiImage=to.port==='image' && nodes[to.node] && (nodes[to.node].type==='gen'||nodes[to.node].type==='video');
    edges=edges.filter(function(e){
      var sameEdge=e.from.node===from.node && e.from.port===from.port && e.to.node===to.node && e.to.port===to.port;
      var sameInput=e.to.node===to.node && e.to.port===to.port;
      return multiImage?!sameEdge:!sameInput;
    });
    edges.push({from:{node:from.node,port:from.port}, to:{node:to.node,port:to.port}});
    pendingPort=null; redraw(); refreshAllGenRefs(); updateState(multiImage?'已添加参考图':'已连线');
    return true;
  }
  function innerPoint(ev){
    var r=inner.getBoundingClientRect();
    return {x:(ev.clientX-r.left)/zoom,y:(ev.clientY-r.top)/zoom};
  }
  function clientPointToInner(x,y){
    var r=inner.getBoundingClientRect();
    return {x:(x-r.left)/zoom,y:(y-r.top)/zoom};
  }
  function inputPortAt(ev, port){
    var el=document.elementFromPoint(ev.clientX,ev.clientY);
    var p=el&&el.closest?el.closest('.nc-port[data-kind="in"]'):null;
    if(p&&p.getAttribute('data-port')===port){
      var n=p.closest('.nc-node');
      if(n) return {node:n.getAttribute('data-node-id'), port:port};
    }
    return nearbyInputPort(ev,port);
  }
  function nearbyInputPort(ev,port){
    var pt=clientPointToInner(ev.clientX,ev.clientY), best=null, bestD=Infinity;
    document.querySelectorAll('.nc-port[data-kind="in"][data-port="'+port+'"]').forEach(function(p){
      var n=p.closest('.nc-node'), id=n&&n.getAttribute('data-node-id');
      if(!id||!nodes[id]||dragPort&&dragPort.from&&dragPort.from.node===id) return;
      var c=portCenter(id,'in',port);
      if(!c) return;
      var dx=c.x-pt.x, dy=c.y-pt.y, d=Math.sqrt(dx*dx+dy*dy);
      if(d<bestD){ bestD=d; best={node:id,port:port}; }
    });
    return bestD<=34?best:null;
  }
  function setZoom(next, cx, cy){
    next=Math.max(.5,Math.min(1.6,next));
    if(Math.abs(next-zoom)<.001) return;
    var old=zoom;
    cx=cx==null?canvas.clientWidth/2:cx;
    cy=cy==null?canvas.clientHeight/2:cy;
    var wx=(canvas.scrollLeft+cx-CANVAS_VIEW_PAD)/old, wy=(canvas.scrollTop+cy-CANVAS_VIEW_PAD)/old;
    zoom=next;
    inner.style.transform='scale('+zoom+')';
    canvas.scrollLeft=CANVAS_VIEW_PAD+wx*zoom-cx;
    canvas.scrollTop=CANVAS_VIEW_PAD+wy*zoom-cy;
    syncCanvasGrid();
    scheduleMap();
    updateState();
  }
  function zoomText(){
    return Math.round(zoom*100)+'%';
  }
  function syncZoomInputs(){
    [zoomLabel,fsZoomLabel].forEach(function(input){
      if(!input) return;
      if(document.activeElement===input) return;
      input.value=zoomText();
    });
  }
  function parseZoomInput(value){
    var raw=String(value||'').trim().replace(/\s+/g,'');
    if(!raw) return null;
    var hasPercent=/%$/.test(raw);
    raw=raw.replace(/%$/,'');
    var n=parseFloat(raw);
    if(!isFinite(n)||n<=0) return null;
    var next=(hasPercent||n>10)?n/100:n;
    return Math.max(.5,Math.min(1.6,next));
  }
  function applyZoomInput(input){
    if(!input) return;
    var next=parseZoomInput(input.value);
    if(next==null){ input.value=zoomText(); syncZoomInputs(); return; }
    setZoom(next);
    input.value=zoomText();
    syncZoomInputs();
  }
  function bindZoomInput(input){
    if(!input) return;
    input.addEventListener('pointerdown',stopUiEvent);
    input.addEventListener('mousedown',stopUiEvent);
    input.addEventListener('click',stopUiEvent);
    input.addEventListener('focus',function(){ input.select(); });
    input.addEventListener('blur',function(){ applyZoomInput(input); });
    input.addEventListener('keydown',function(e){
      e.stopPropagation();
      if(e.key==='Enter'){ e.preventDefault(); applyZoomInput(input); input.blur(); }
      if(e.key==='Escape'){ e.preventDefault(); syncZoomInputs(); input.blur(); }
    });
  }
  function selectEdge(i){
    selectNode(null);
    selectedEdge=(i==null?-1:i);
    pendingPort=null;
    clearConnectHints();
    redraw();
    if(selectedEdge>=0) updateState('已选连线');
  }
  function delSelectedEdge(){
    if(!canEditCanvas()) return;
    if(selectedEdge<0||!edges[selectedEdge]) return;
    pushUndo();
    edges.splice(selectedEdge,1);
    selectedEdge=-1;
    redraw(); refreshAllGenRefs();
    updateState('已删除连线');
  }
  function selectedNodeIds(){
    var ids=Object.keys(selectedNodes||{}).filter(function(id){ return !!nodes[id]; });
    if(!ids.length&&selectedNode&&nodes[selectedNode]) ids=[selectedNode];
    return ids;
  }
  function deleteSelectedNodes(){
    if(!canEditCanvas()) return;
    var ids=selectedNodeIds();
    if(!ids.length) return;
    pushUndo();
    ids.forEach(function(id){
      destroyShortDramaWorkspace(nodes[id]);
      destroyDigitalPresenterWorkspace(nodes[id]);
      if(nodes[id]&&nodes[id].el) nodes[id].el.remove();
      delete nodes[id];
    });
    edges=edges.filter(function(e){ return ids.indexOf(e.from.node)<0 && ids.indexOf(e.to.node)<0; });
    selectedNode=null; selectedNodes={}; selectedEdge=-1;
    redraw(); refreshAllGenRefs(); updateSelectedRegion();
    updateState('已删除 '+ids.length+' 个节点');
  }
  function setSelectedCollapsed(collapsed){
    if(!canEditCanvas()) return;
    var ids=selectedNodeIds();
    if(!ids.length) return;
    pushUndo();
    ids.forEach(function(id){ if(nodes[id]) setCollapsed(nodes[id],collapsed,true); });
    redraw();
    updateSelectedRegion();
    updateState(collapsed?'已批量折叠':'已批量展开');
  }
  function toggleSelectedCollapsed(){
    var ids=selectedNodeIds();
    if(ids.length>1){
      var shouldCollapse=ids.some(function(id){ return nodes[id]&&!nodes[id].collapsed; });
      setSelectedCollapsed(shouldCollapse);
      return;
    }
    if(ids[0]&&nodes[ids[0]]) toggleCollapsed(nodes[ids[0]]);
  }
  function copyNode(){
    var ids=selectedNodeIds();
    if(!ids.length) return;
    if(ids.length>1){
      var set={};
      ids.forEach(function(id){ set[id]=true; });
      clipNode={
        multi:true,
        nodes:ids.map(function(id){
          var n=nodes[id];
          return {id:n.id,type:n.type,x:n.x,y:n.y,collapsed:n.collapsed,params:stateApi.cloneSnapshot(n.type==='shortDrama'?normalizeShortDramaNodeParams(n.params):n.type==='digitalPresenter'?digitalPresenterModule.copyNodeData(n).params:n.params||{}),outputs:shortDramaNodeOutputs(n),image:n.image||null,note:(n.el.querySelector('[data-f="note"]')||{}).textContent||''};
        }),
        edges:edges.filter(function(e){ return set[e.from.node]&&set[e.to.node]; }).map(function(e){ return stateApi.cloneSnapshot(e); })
      };
      updateState('已复制 '+ids.length+' 个节点');
      return;
    }
    var n=nodes[ids[0]];
    clipNode={type:n.type,params:stateApi.cloneSnapshot(n.type==='shortDrama'?normalizeShortDramaNodeParams(n.params):n.type==='digitalPresenter'?digitalPresenterModule.copyNodeData(n).params:n.params||{}),outputs:shortDramaNodeOutputs(n),image:n.image||null,note:(n.el.querySelector('[data-f="note"]')||{}).textContent||''};
    updateState('已复制节点');
  }
  function pasteNode(){
    if(!canEditCanvas()) return;
    if(!clipNode) return;
    var pastedNodes=clipNode.multi?(clipNode.nodes||[]):[clipNode];
    if(pastedNodes.some(function(n){return !nodeCreationPolicy.canMaterialize(n.type,'create');})){
      updateState('数字人口播节点仅可粘贴到有编辑权限的协作画布');return;
    }
    pushUndo();
    if(clipNode.multi){
      var copied=stateApi.cloneSnapshot(clipNode), minX=Infinity, minY=Infinity, idMap={}, made=[];
      copied.nodes.forEach(function(n){ minX=Math.min(minX,n.x||0); minY=Math.min(minY,n.y||0); });
      if(!isFinite(minX)) minX=0;
      if(!isFinite(minY)) minY=0;
      var viewStart=viewportTopLeftPoint();
      var base=selectedNode&&nodes[selectedNode]?{x:nodes[selectedNode].x+40,y:nodes[selectedNode].y+40}:{x:viewStart.x+90,y:viewStart.y+90};
      copied.nodes.forEach(function(n){
        var data=stateApi.cloneSnapshot(n), oldId=data.id;
        data.id=currentBoardScope==='collab'&&collabSync?collabSync.makeNodeId(collabNodeSeed,nid+1):'n'+(nid+1);
        data.x=Math.max(0,base.x+((n.x||0)-minX));
        data.y=Math.max(0,base.y+((n.y||0)-minY));
        idMap[oldId]=data.id;
        made.push(addNode(data.type,data.x,data.y,data));
      });
      (copied.edges||[]).forEach(function(e){
        var from=idMap[e.from.node], to=idMap[e.to.node];
        if(from&&to) edges.push({from:{node:from,port:e.from.port},to:{node:to,port:e.to.port}});
      });
      redraw(); refreshAllGenRefs();
      selectNodesByIds(made.map(function(n){ return n.id; }));
      updateState('已粘贴 '+made.length+' 个节点');
      return;
    }
    var base=selectedNode&&nodes[selectedNode]?nodes[selectedNode]:null;
    var data=stateApi.cloneSnapshot(clipNode);
    var viewStart=viewportTopLeftPoint();
    var x=base?base.x+34:viewStart.x+90, y=base?base.y+34:viewStart.y+90;
    var node=addNode(data.type,Math.max(0,x),Math.max(0,y),data);
    selectNode(node);
    updateState('已粘贴节点');
  }
  function duplicateNode(id){
    if(!canEditCanvas()) return;
    if(!nodes[id]) return;
    selectNode(nodes[id]);
    copyNode();
    pasteNode();
  }
  function autoLayout(){
    if(!canEditCanvas()) return;
    var ids=Object.keys(nodes);
    if(!ids.length) return;
    pushUndo();
    var positions=graphApi.computeAutoLayout(ids.map(function(id){ return {id:id}; }),edges);
    ids.forEach(function(id){
      var n=nodes[id], position=positions[id];
      if(position){
        n.x=position.x; n.y=position.y;
        n.el.style.left=n.x+'px'; n.el.style.top=n.y+'px';
      }
    });
    redraw();
    updateState('已自动整理');
  }
  function fitView(){
    var ids=Object.keys(nodes);
    if(!ids.length){ setZoom(1); centerEmptyView(); syncCanvasGrid(); return; }
    var minX=Infinity,minY=Infinity,maxX=0,maxY=0;
    ids.forEach(function(id){
      var n=nodes[id], w=n.el.offsetWidth||250, h=n.el.offsetHeight||160;
      minX=Math.min(minX,n.x); minY=Math.min(minY,n.y); maxX=Math.max(maxX,n.x+w); maxY=Math.max(maxY,n.y+h);
    });
    var pad=80, bw=Math.max(1,maxX-minX+pad*2), bh=Math.max(1,maxY-minY+pad*2);
    var next=Math.min(1.2,Math.max(.5,Math.min(canvas.clientWidth/bw,canvas.clientHeight/bh)));
    setZoom(next);
    canvas.scrollLeft=Math.max(0,CANVAS_VIEW_PAD+((minX+maxX)/2)*zoom-canvas.clientWidth/2);
    canvas.scrollTop=Math.max(0,CANVAS_VIEW_PAD+((minY+maxY)/2)*zoom-canvas.clientHeight/2);
    syncCanvasGrid();
    updateState('已适应视图');
  }
  function fullscreenElement(){
    return document.fullscreenElement||document.webkitFullscreenElement||null;
  }
  function canFullscreen(el){
    return !!(el&&(el.requestFullscreen||el.webkitRequestFullscreen));
  }
  function updateFullscreenUI(){
    var on=fullscreenElement()===wrap||localFullscreen;
    if(fullscreenBtn) fullscreenBtn.textContent=on?'退出全屏':'全屏';
    scheduleMap();
  }
  function setLocalFullscreen(on){
    localFullscreen=!!on;
    document.body.classList.toggle('nc-local-fullscreen', localFullscreen);
    updateFullscreenUI();
    setTimeout(function(){ scheduleMap(); },80);
  }
  function toggleFullscreen(){
    if(!wrap) return;
    if(localFullscreen){ setLocalFullscreen(false); return; }
    if(fullscreenElement()){
      var exit=document.exitFullscreen||document.webkitExitFullscreen;
      if(exit) exit.call(document);
      return;
    }
    if(!canFullscreen(wrap)){ setLocalFullscreen(true); updateState('已进入全屏'); return; }
    var req=wrap.requestFullscreen||wrap.webkitRequestFullscreen;
    var p=req.call(wrap);
    if(p&&p.catch) p.catch(function(){ setLocalFullscreen(true); updateState('已进入全屏'); });
  }
  function focusNode(node){
    if(!node) return;
    setCanvasView('workflow');
    canvas.scrollLeft=Math.max(0,CANVAS_VIEW_PAD+(node.x+(node.el.offsetWidth||250)/2)*zoom-canvas.clientWidth/2);
    canvas.scrollTop=Math.max(0,CANVAS_VIEW_PAD+(node.y+(node.el.offsetHeight||160)/2)*zoom-canvas.clientHeight/2);
    syncCanvasGrid();
    scheduleMap();
  }
  function hideMenu(){ if(menu){ menu.classList.remove('on'); menu.innerHTML=''; } }
  function commandShortcut(key,shift){
    var mac=/Mac|iPhone|iPad/.test((navigator&&navigator.platform)||'');
    return mac?(shift?'⇧⌘'+key:'⌘'+key):(shift?'Ctrl+Shift+'+key:'Ctrl+'+key);
  }
  var MENU_ICONS={
    text:'<path pathLength="1" class="nc-icon-base" d="M4 6h16"/><path pathLength="1" class="nc-icon-detail" d="M4 12h12M4 18h9"/>',
    image:'<rect pathLength="1" class="nc-icon-base" x="3" y="4" width="18" height="16" rx="2"/><circle pathLength="1" class="nc-icon-detail" cx="8.5" cy="9" r="1.8"/><path pathLength="1" class="nc-icon-detail" d="m5 18 5-5 3 3 2-2 4 4"/>',
    reverse:'<rect pathLength="1" class="nc-icon-base" x="3" y="4" width="14" height="13" rx="2"/><path pathLength="1" class="nc-icon-detail" d="m5 15 4-4 3 3 2-2 2 2M18 9.5a4.5 4.5 0 1 1-.7 6.5M18 9.5v4h-4"/>',
    generate:'<rect pathLength="1" class="nc-icon-base" x="3" y="5" width="15" height="15" rx="2"/><path pathLength="1" class="nc-icon-detail" d="m5 18 4-4 3 3 2-2 2 2"/><path pathLength="1" class="nc-icon-spark" d="m19 2 .7 1.8 1.8.7-1.8.7L19 8l-.7-1.8-1.8-.7 1.8-.7Z"/>',
    video:'<rect pathLength="1" class="nc-icon-base" x="3" y="5" width="17" height="14" rx="2"/><path pathLength="1" class="nc-icon-detail" d="m9.5 9 5 3-5 3Z"/><path pathLength="1" class="nc-icon-spark" d="m19 2 .6 1.5 1.5.6-1.5.6-.6 1.5-.6-1.5-1.5-.6 1.5-.6Z"/>',
    drama:'<rect pathLength="1" class="nc-icon-base" x="3" y="5" width="18" height="14" rx="2"/><path pathLength="1" class="nc-icon-detail" d="M8 5v14M16 5v14M3 9h5M16 9h5M3 15h5M16 15h5m-5-5 3 2-3 2Z"/>',
    upload:'<path pathLength="1" class="nc-icon-base" d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/><path pathLength="1" class="nc-icon-detail" d="M12 16V4m-4 4 4-4 4 4"/>',
    assets:'<path pathLength="1" class="nc-icon-base" d="m12 3 8 4-8 4-8-4Z"/><path pathLength="1" class="nc-icon-detail" d="m4 12 8 4 8-4M4 17l8 4 8-4"/>',
    add:'<circle pathLength="1" class="nc-icon-base" cx="8" cy="8" r="3"/><circle pathLength="1" class="nc-icon-detail" cx="17.5" cy="15.5" r="2.5"/><path pathLength="1" class="nc-icon-detail" d="m10.5 10 5 4M7 17v5m-2.5-2.5h5"/>',
    undo:'<path pathLength="1" class="nc-icon-base" d="M9 7 4 12l5 5"/><path pathLength="1" class="nc-icon-detail" d="M5 12h8a6 6 0 0 1 6 6v1"/>',
    redo:'<path pathLength="1" class="nc-icon-base" d="m15 7 5 5-5 5"/><path pathLength="1" class="nc-icon-detail" d="M19 12h-8a6 6 0 0 0-6 6v1"/>',
    paste:'<rect pathLength="1" class="nc-icon-base" x="5" y="4" width="14" height="17" rx="2"/><path pathLength="1" class="nc-icon-detail" d="M9 4.5V3h6v1.5M9 9h6M9 13h6M9 17h4"/>',
    note:'<path pathLength="1" class="nc-icon-base" d="M5 3h9l5 5v13H5Z M14 3v5h5"/><path pathLength="1" class="nc-icon-detail" d="m8 17 1-4 6-6 2 2-6 6Z"/>',
    preview:'<path pathLength="1" class="nc-icon-base" d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle pathLength="1" class="nc-icon-detail" cx="12" cy="12" r="2.5"/>',
    copy:'<rect pathLength="1" class="nc-icon-base" x="8" y="8" width="12" height="12" rx="2"/><path pathLength="1" class="nc-icon-detail" d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    remove:'<path pathLength="1" class="nc-icon-base" d="M5 7h14m-9-3h4l1 3H9Zm-3 3 1 14h8l1-14"/><path pathLength="1" class="nc-icon-detail" d="M10 11v6M14 11v6"/>',
    portrait:'<path pathLength="1" class="nc-icon-base" d="M7 20v-2.2a5 5 0 0 1 10 0V20M8 8a4 4 0 1 0 8 0 4 4 0 0 0-8 0Z"/><path pathLength="1" class="nc-icon-detail" d="M10.5 8.5h.01M13.5 8.5h.01M10.5 11.5c.8.7 2.2.7 3 0"/><path pathLength="1" class="nc-icon-spark" d="m19 3 .6 1.5 1.5.6-1.5.6-.6 1.5-.6-1.5-1.5-.6 1.5-.6Z"/>',
    light:'<circle pathLength="1" class="nc-icon-base" cx="12" cy="12" r="3.5"/><path pathLength="1" class="nc-icon-detail" d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8m10.6 10.6 1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8"/>',
    angle:'<path pathLength="1" class="nc-icon-base" d="M4 7.5 9 5l5 2.5L9 10Zm6 5 5-2.5 5 2.5-5 2.5Z"/><path pathLength="1" class="nc-icon-detail" d="M4 7.5V13l5 2.5V10m6 5v4.5l5-2.5v-4.5"/><path pathLength="1" class="nc-icon-spark" d="m4 18 .6 1.4 1.4.6-1.4.6L4 22l-.6-1.4L2 20l1.4-.6Z"/>',
    grid:'<path pathLength="1" class="nc-icon-base" d="M4 4h16v16H4Z"/><path pathLength="1" class="nc-icon-detail" d="M9.3 4v16m5.4-16v16M4 9.3h16M4 14.7h16"/><path pathLength="1" class="nc-icon-spark" d="m20 2 .5 1.2 1.2.5-1.2.5-.5 1.2-.5-1.2-1.2-.5 1.2-.5Z"/>',
    more:'<circle pathLength="1" class="nc-icon-base" cx="5" cy="12" r="1"/><circle pathLength="1" class="nc-icon-detail" cx="12" cy="12" r="1"/><circle pathLength="1" class="nc-icon-detail" cx="19" cy="12" r="1"/>',
    download:'<path pathLength="1" class="nc-icon-base" d="M5 20h14"/><path pathLength="1" class="nc-icon-detail" d="M12 3v12m-4-4 4 4 4-4"/>'
  };
  function menuIcon(name){
    var shapes=MENU_ICONS[name];
    return shapes?'<span class="nc-menu-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round">'+shapes+'</svg></span>':'';
  }
  function imageNodeSource(node){
    return node&&(node.image||(node.outputs&&node.outputs.image))||'';
  }
  function createImageActionDraft(source,type,options){
    if(!source||!canEditCanvas()||!imageNodeSource(source)) return;
    options=options||{};
    pushUndo();
    var branches=edges.filter(function(edge){ return edge.from.node===source.id&&edge.from.port==='image'; }).length;
    var params=Object.assign({title:options.title||TYPE[type].name},options.params||{});
    var node=addNode(type,source.x+(source.el.offsetWidth||250)+120,Math.max(0,source.y+Math.min(branches,4)*48),{params:params});
    connectEdge({node:source.id,port:'image'},{node:node.id,port:'image'});
    selectNode(node);
    focusNode(node);
    updateState(options.status||'已创建并连线，确认参数后再生成');
  }
  function createImageGenDraft(source,title,prompt,ratio){
    createImageActionDraft(source,'gen',{
      title:title,
      params:{text:prompt,ratio:ratio||(source.params&&source.params.ratio)||'16:9',quality:'hd'},
      status:'已创建「'+title+'」草稿，确认参数后再生成'
    });
  }
  function showImageActionMenu(button,label,items){
    var rect=button.getBoundingClientRect();
    showMenu(items,rect.left,rect.bottom+8,{focus:true,label:label});
  }
  function portraitActionItems(source){
    return [
      {icon:'portrait',label:'自然肤质',run:function(){ createImageGenDraft(source,'人像优化 · 自然肤质','保持参考图人物身份、五官、发型和服装一致；采用自然肤质与真实皮肤纹理，轻微优化瑕疵，避免磨皮、塑料感和面部变形。'); }},
      {icon:'portrait',label:'清透修饰',run:function(){ createImageGenDraft(source,'人像优化 · 清透修饰','保持参考图人物身份和五官完全一致；清透修饰皮肤并均匀肤色，保留真实毛孔和自然细节，避免过度磨皮与假面感。'); }},
      {icon:'portrait',label:'真实肌理',run:function(){ createImageGenDraft(source,'人像优化 · 真实肌理','保持参考图人物身份和五官完全一致；强化真实肌理、自然毛孔和面部细节，肤色真实，避免磨皮和蜡像感。'); }}
    ];
  }
  function lightingActionItems(source){
    return [
      {icon:'light',label:'柔和补光',run:function(){ createImageGenDraft(source,'打光优化 · 柔和补光','保持参考图主体、构图和场景一致；增加柔和自然的面部与环境补光，压住过曝和死黑区域，保留真实光影层次。'); }},
      {icon:'light',label:'自然匹配',run:function(){ createImageGenDraft(source,'打光优化 · 自然匹配','保持参考图主体和构图一致；让人物与环境光向、色温和明暗关系自然匹配，融合真实，避免抠图感。'); }},
      {icon:'light',label:'氛围强化',run:function(){ createImageGenDraft(source,'打光优化 · 氛围强化','保持参考图主体和构图一致；强化有层次的电影氛围光、轮廓光与环境光，主体清晰，避免过度炫光。'); }}
    ];
  }
  function triggerImageDownload(source){
    var src=imageNodeSource(source);
    if(!src) return;
    function download(href,revoke){
      var link=document.createElement('a');
      link.href=href;
      link.download=canvasExporter.safeFilename(nodeDisplayName(source)||'huangque-image')+'.png';
      document.body.appendChild(link);
      link.click();
      link.remove();
      if(revoke) setTimeout(function(){ URL.revokeObjectURL(href); },1000);
      updateState('图片已下载');
    }
    if(src.indexOf('data:image/')===0){ download(src,false); return; }
    apiClient.asset(src).then(function(blob){ download(URL.createObjectURL(blob),true); })
      .catch(function(){ window.open(src,'_blank','noopener'); updateState('已打开图片，请在新页面保存'); });
  }
  function showImageMoreMenu(button,source){
    var items=[];
    if(source.type==='image'){
      items.push({icon:'upload',label:'本地替换图片',run:function(){ chooseNodeImage(source,'图片已更换'); }});
      items.push({icon:'assets',label:'从资产列表选择',run:function(){ openNodeAssetPicker(source); }});
    }
    items.push({icon:'preview',label:'查看大图',run:function(){ window.open(imageNodeSource(source),'_blank','noopener'); }});
    items.push({icon:'download',label:'下载图片',run:function(){ triggerImageDownload(source); }});
    items.push({icon:'note',label:(source.params.remark||'').trim()?'编辑备注':'添加备注',run:function(){ editNodeRemark(source); }});
    items.push({kind:'separator'});
    items.push({icon:'remove',label:'删除图片节点',run:function(){ openConfirmDialog('删除图片节点','节点和相关连线会一起删除，仍可通过撤销恢复。',function(){ delNode(source.id); }); }});
    showImageActionMenu(button,'图片更多操作',items);
  }
  function selectedImageActionNode(){
    var node=selectedNode&&nodes[selectedNode];
    if(!node||!wrap.classList.contains('editing')||!canEditCanvas()||canvasView!=='workflow') return null;
    if((node.type!=='image'&&node.type!=='gen')||!imageNodeSource(node)) return null;
    return node;
  }
  function ensureImageToolbar(){
    if(imageToolbar) return imageToolbar;
    imageToolbar=document.createElement('div');
    imageToolbar.className='nc-image-toolbar';
    imageToolbar.setAttribute('role','toolbar');
    imageToolbar.setAttribute('aria-label','图片下一步操作');
    imageToolbar.innerHTML=''
      +'<button type="button" data-image-action="reverse" title="根据图片反推提示词">'+menuIcon('reverse')+'<span>反推</span></button>'
      +'<button type="button" data-image-action="continue" title="以当前图片作为参考继续作图">'+menuIcon('generate')+'<span>继续作图</span></button>'
      +'<button type="button" data-image-action="portrait" title="调整人像肤质与细节">'+menuIcon('portrait')+'<span>人像优化</span><b>NEW</b></button>'
      +'<button type="button" data-image-action="lighting" title="调整人物与环境光影">'+menuIcon('light')+'<span>打光</span></button>'
      +'<button type="button" data-image-action="angle" title="生成身份一致的多角度设定图">'+menuIcon('angle')+'<span>多角度</span></button>'
      +'<button type="button" data-image-action="grid" title="生成同一主体的九宫格画面">'+menuIcon('grid')+'<span>九宫格</span></button>'
      +'<button type="button" data-image-action="video" title="以当前图片创建视频草稿">'+menuIcon('video')+'<span>转视频</span></button>'
      +'<button type="button" class="more" data-image-action="more" title="更多图片操作">'+menuIcon('more')+'<span>更多</span></button>';
    imageToolbar.addEventListener('pointerdown',function(event){ event.stopPropagation(); });
    imageToolbar.addEventListener('click',function(event){
      var button=event.target&&event.target.closest&&event.target.closest('[data-image-action]');
      var source=selectedImageActionNode();
      if(!button||!source) return;
      event.preventDefault();
      event.stopPropagation();
      var action=button.getAttribute('data-image-action');
      if(action==='reverse') createImageActionDraft(source,'reverse',{title:'反推提示词',status:'已创建反推节点，点击反推时才扣点'});
      if(action==='continue') createImageGenDraft(source,'继续作图','基于参考图继续创作，保持主体身份、服装、核心构图和画面风格一致，提升细节、光影与整体完成度。');
      if(action==='portrait') showImageActionMenu(button,'人像优化',portraitActionItems(source));
      if(action==='lighting') showImageActionMenu(button,'打光优化',lightingActionItems(source));
      if(action==='angle') createImageGenDraft(source,'多角度设定','保持参考图人物或主体的身份、造型、服装、比例和风格一致，生成正面、侧面、背面三视图，使用纯净统一背景，细节一致，适合角色设定。');
      if(action==='grid') createImageGenDraft(source,'九宫格分镜','保持参考图人物或主体的身份、服装和风格一致，生成 3×3 九宫格：九个连续但不同的镜头角度、景别和表情，构图清晰，每格边界整齐，不添加文字。','1:1');
      if(action==='video') createImageActionDraft(source,'video',{title:'图片转视频',params:{text:'保持主体身份、外观与场景一致，添加自然、稳定的轻微动作和镜头运动，避免形变、闪烁和画面跳变。',channel:'grok',ratio:'16:9',duration:'5'},status:'已创建视频草稿，确认参数后再生成'});
      if(action==='more') showImageMoreMenu(button,source);
    });
    document.body.appendChild(imageToolbar);
    return imageToolbar;
  }
  function updateImageToolbar(){
    var node=selectedImageActionNode();
    if(!node){ if(imageToolbar) imageToolbar.classList.remove('on'); return; }
    var bar=ensureImageToolbar(), nodeRect=node.el.getBoundingClientRect(), canvasRect=canvas.getBoundingClientRect();
    if(nodeRect.right<canvasRect.left||nodeRect.left>canvasRect.right||nodeRect.bottom<canvasRect.top||nodeRect.top>canvasRect.bottom){ bar.classList.remove('on'); return; }
    bar.classList.add('on');
    bar.setAttribute('data-node-id',node.id);
    var minLeft=Math.max(8,canvasRect.left+8), maxRight=Math.min(window.innerWidth-8,canvasRect.right-8);
    bar.style.maxWidth=Math.max(220,maxRight-minLeft)+'px';
    var width=bar.offsetWidth, height=bar.offsetHeight;
    bar.style.left=Math.max(minLeft,Math.min(nodeRect.left+(nodeRect.width-width)/2,maxRight-width))+'px';
    var top=nodeRect.top-height-10;
    if(top<Math.max(8,canvasRect.top+8)) top=Math.min(canvasRect.bottom-height-8,nodeRect.bottom+10);
    bar.style.top=Math.max(8,top)+'px';
  }
  function showMenu(items,x,y,options){
    if(!menu) return;
    options=options||{};
    menu.innerHTML='';
    menu.className='nc-menu'+(options.variant?' '+options.variant:'');
    menu.setAttribute('aria-label',options.label||'操作菜单');
    items.forEach(function(it){
      if(it.kind==='title'||it.kind==='section'){
        var heading=document.createElement('div');
        heading.className=it.kind==='title'?'nc-menu-title':'nc-menu-section';
        heading.textContent=it.label||'';
        heading.setAttribute('role','presentation');
        menu.appendChild(heading);
        return;
      }
      if(it.kind==='separator'){
        var separator=document.createElement('div');
        separator.className='nc-menu-separator';
        separator.setAttribute('role','separator');
        menu.appendChild(separator);
        return;
      }
      var b=document.createElement('button');
      b.type='button';
      b.setAttribute('role','menuitem');
      if(it.title) b.title=it.title;
      if(it.disabled){ b.disabled=true; b.setAttribute('aria-disabled','true'); }
      b.innerHTML=(it.icon?menuIcon(it.icon):(it.key?'<span class="nc-menu-k">'+escapeHtml(it.key)+'</span>':''))+'<span class="nc-menu-t">'+escapeHtml(it.label)+'</span>'+(it.shortcut?'<span class="nc-menu-shortcut">'+escapeHtml(it.shortcut)+'</span>':'');
      b.onmousedown=function(e){ e.preventDefault(); };
      b.onclick=function(e){ e.stopPropagation(); if(it.disabled) return; hideMenu(); it.run(); };
      menu.appendChild(b);
    });
    menu.classList.add('on');
    var width=menu.offsetWidth, height=menu.offsetHeight;
    var left=Math.max(8,Math.min(x,window.innerWidth-width-8));
    var top=options.anchor==='above'?y-height:y;
    menu.style.left=left+'px';
    menu.style.top=Math.max(8,Math.min(top,window.innerHeight-height-8))+'px';
    menu.onkeydown=function(e){
      if(e.key!=='ArrowDown'&&e.key!=='ArrowUp') return;
      e.preventDefault();
      var buttons=Array.prototype.slice.call(menu.querySelectorAll('button:not(:disabled)'));
      if(!buttons.length) return;
      var current=buttons.indexOf(document.activeElement), step=e.key==='ArrowDown'?1:-1;
      if(current<0) buttons[step>0?0:buttons.length-1].focus();
      else buttons[(current+step+buttons.length)%buttons.length].focus();
    };
    if(options.focus){
      var first=menu.querySelector('button:not(:disabled)');
      if(first) first.focus();
    }
  }
  function showMenuFromButton(btn,items){
    if(!btn) return;
    var r=btn.getBoundingClientRect();
    showMenu(items,r.left,r.top-10,{anchor:'above',focus:true});
  }
  function addNodeMenuItems(pt){
    var items=[
      {icon:'text',label:'文本',title:'添加文本提示词节点',run:function(){ addAt('text',pt); }},
      {icon:'image',label:'图片',title:'上传或粘贴素材图',run:function(){ addAt('image',pt); }},
      {icon:'reverse',label:'图片反推',title:'根据图片生成提示词',run:function(){ addAt('reverse',pt); }},
      {icon:'generate',label:'图片生成',title:'根据提示词和参考图生成图片',run:function(){ addAt('gen',pt); }},
      {icon:'video',label:'视频生成',title:'根据提示词和参考图生成视频',run:function(){ addAt('video',pt); }},
      {icon:'drama',label:'短剧生产',title:'创建短剧项目',run:function(){ addAt('shortDrama',pt); }}
    ];
    if(nodeCreationPolicy.canCreate('digitalPresenter')) items.push({key:'播',label:'数字人口播',title:'创建数字人口播项目',run:function(){ addAt('digitalPresenter',pt); }});
    return items;
  }
  function showAddNodeMenu(pt,x,y,anchor){
    var items=[{kind:'title',label:'添加节点'}].concat(addNodeMenuItems(pt),[
      {kind:'section',label:'添加资源'},
      {icon:'upload',label:'上传图片',run:function(){ uploadImageAt(pt); }},
      {icon:'assets',label:'打开我的资产',run:function(){ openSidePanel('assets',true); }}
    ]);
    showMenu(items,x,y,{variant:'add',anchor:anchor,focus:true,label:'添加节点'});
  }
  function connectedNodeMenuItems(from,pt){
    var compatible=from&&from.port==='prompt'?[['gen','作图'],['video','视频']]:from&&from.port==='image'?[['reverse','反推'],['gen','作图'],['video','视频']]:[];
    return compatible.map(function(item){
      return {icon:item[0]==='gen'?'generate':item[0],label:'创建'+item[1]+'节点并连线',run:function(){
        pushUndo();
        var node=addNode(item[0],Math.max(0,pt.x+28),Math.max(0,pt.y-48));
        connectEdge(from,{node:node.id,port:from.port});
        selectNode(node);
        updateState('已创建并连线');
      }};
    });
  }
  function stopUiEvent(e){
    if(!e) return;
    e.stopPropagation();
  }
  function clearTextSelection(){
    if(window.getSelection){ try{ window.getSelection().removeAllRanges(); }catch(e){} }
  }
  function stopSelectEvent(e){
    if(!e) return;
    e.preventDefault();
    e.stopPropagation();
    clearTextSelection();
  }
  function bindButton(btn,fn){
    if(!btn) return;
    btn.addEventListener('pointerdown',stopUiEvent);
    btn.addEventListener('mousedown',stopUiEvent);
    btn.addEventListener('click',function(e){
      e.preventDefault();
      e.stopPropagation();
      fn(e);
    });
  }
  function viewportTopLeftPoint(){
    return {x:Math.max(0,(canvas.scrollLeft-CANVAS_VIEW_PAD)/zoom),y:Math.max(0,(canvas.scrollTop-CANVAS_VIEW_PAD)/zoom)};
  }
  function viewportCenterPoint(){
    return {x:Math.max(0,(canvas.scrollLeft+canvas.clientWidth/2-CANVAS_VIEW_PAD)/zoom),y:Math.max(0,(canvas.scrollTop+canvas.clientHeight/2-CANVAS_VIEW_PAD)/zoom)};
  }
  function viewportNodePoint(){
    var center=viewportCenterPoint(), offset=(Object.keys(nodes).length%5)*36;
    return {x:Math.max(0,center.x-125+offset),y:Math.max(0,center.y-80+offset)};
  }
  function centerEmptyView(){
    canvas.scrollLeft=Math.max(0,CANVAS_VIEW_PAD+(innerWidth()*zoom-canvas.clientWidth)/2);
    canvas.scrollTop=Math.max(0,CANVAS_VIEW_PAD+(innerHeight()*zoom-canvas.clientHeight)/2);
  }
  function closeDialog(){
    var old=document.querySelector('.nc-dialog-mask');
    if(old) old.remove();
  }
  function openTextDialog(opts){
    closeDialog();
    var mask=document.createElement('div');
    mask.className='nc-dialog-mask';
    var field=opts.multiline?'textarea':'input';
    mask.innerHTML='<div class="nc-dialog" role="dialog" aria-modal="true">'
      +'<h3>'+escapeHtml(opts.title||'编辑')+'</h3>'
      +'<'+field+' data-f="dialogInput" '+(opts.multiline?'rows="5"':'type="text"')+' maxlength="'+(opts.max||300)+'" placeholder="'+escapeHtml(opts.placeholder||'')+'"></'+field+'>'
      +'<div class="nc-dialog-actions"><button type="button" data-f="cancel">取消</button><button type="button" class="primary" data-f="ok">保存</button></div>'
      +'</div>';
    document.body.appendChild(mask);
    var input=mask.querySelector('[data-f="dialogInput"]');
    input.value=opts.value||'';
    function done(){
      var value=input.value;
      closeDialog();
      if(opts.onSave) opts.onSave(value);
    }
    mask.querySelector('[data-f="cancel"]').onclick=closeDialog;
    mask.querySelector('[data-f="ok"]').onclick=done;
    mask.addEventListener('mousedown',function(e){ if(e.target===mask) closeDialog(); });
    mask.addEventListener('keydown',function(e){
      if(e.key==='Escape'){ e.preventDefault(); closeDialog(); }
      if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){ e.preventDefault(); done(); }
    });
    setTimeout(function(){ input.focus(); input.select(); },0);
  }
  function openMessageDialog(title,text){
    closeDialog();
    var mask=document.createElement('div');
    mask.className='nc-dialog-mask';
    mask.innerHTML='<div class="nc-dialog" role="dialog" aria-modal="true">'
      +'<h3>'+escapeHtml(title||'提示')+'</h3><p>'+escapeHtml(text||'')+'</p>'
      +'<div class="nc-dialog-actions"><button type="button" class="primary" data-f="ok">知道了</button></div></div>';
    document.body.appendChild(mask);
    mask.querySelector('[data-f="ok"]').onclick=closeDialog;
    mask.addEventListener('mousedown',function(e){ if(e.target===mask) closeDialog(); });
  }
  function openConfirmDialog(title,text,onOk){
    closeDialog();
    var mask=document.createElement('div');
    mask.className='nc-dialog-mask';
    mask.innerHTML='<div class="nc-dialog" role="dialog" aria-modal="true">'
      +'<h3>'+escapeHtml(title||'确认')+'</h3><p>'+escapeHtml(text||'')+'</p>'
      +'<div class="nc-dialog-actions"><button type="button" data-f="cancel">取消</button><button type="button" class="primary" data-f="ok">确认</button></div></div>';
    document.body.appendChild(mask);
    mask.querySelector('[data-f="cancel"]').onclick=closeDialog;
    mask.querySelector('[data-f="ok"]').onclick=function(){ closeDialog(); if(onOk) onOk(); };
    mask.addEventListener('mousedown',function(e){ if(e.target===mask) closeDialog(); });
  }
  function canvasPointFromClient(e){
    var r=inner.getBoundingClientRect();
    if(!Object.keys(nodes).length&&(e.clientX<r.left||e.clientY<r.top)){
      centerEmptyView();
      r=inner.getBoundingClientRect();
    }
    return {x:Math.max(0,(e.clientX-r.left)/zoom),y:Math.max(0,(e.clientY-r.top)/zoom)};
  }
  canvas.addEventListener('dragover',function(e){
    if(!dragAsset||!canEditCanvas()) return;
    e.preventDefault();
    if(e.dataTransfer) e.dataTransfer.dropEffect='copy';
    canvas.classList.add('asset-drop-target');
  });
  canvas.addEventListener('drop',function(e){
    if(!dragAsset||!canEditCanvas()) return;
    e.preventDefault();
    var asset=dragAsset, pt=canvasPointFromClient(e);
    clearAssetDragState();
    addAssetAt(asset,pt);
  });
  function addAt(type,pt){
    var node=null;
    nodeCreationPolicy.run(type,function(){
      pushUndo();
      node=addNode(type,pt.x,pt.y);
      selectNode(node);
      updateState('已添加');
    });
    return node;
  }
  function uploadImageAt(pt){
    var node=addAt('image',pt);
    if(node) chooseNodeImage(node,'图片已上传');
  }
  function beginInlineRename(node,preserveSelection){
    if(!canEditCanvas()) return;
    if(!node||!node.el) return;
    var title=node.el.querySelector('[data-f="headTitle"]');
    if(!title||title.getAttribute('contenteditable')==='true') return;
    var oldName=node.params.title||'', defaultName=(TYPE[node.type]&&TYPE[node.type].name)||nodeTypeLabel(node.type);
    title.setAttribute('contenteditable','true');
    title.setAttribute('spellcheck','false');
    title.textContent=oldName||defaultName;
    var cancelled=false;
    function finish(){
      if(!canEditCanvas()){
        title.setAttribute('contenteditable','false');
        title.removeAttribute('spellcheck');
        title.onkeydown=null;
        title.onblur=null;
        title.textContent=node.params.title||defaultName;
        return;
      }
      var next=cancelled?oldName:(collabSync?collabSync.normalizeNodeTitle(title.textContent,defaultName):String(title.textContent||'').replace(/\s+/g,' ').trim().slice(0,40));
      title.setAttribute('contenteditable','false');
      title.removeAttribute('spellcheck');
      title.onkeydown=null;
      title.onblur=null;
      if(!cancelled&&next!==oldName){
        pushUndo();
        node.params.title=next;
        refreshNodeMeta(node);
        updateState(next?'节点名已修改':'节点名已恢复默认');
      }else{
        title.textContent=oldName||defaultName;
      }
    }
    title.onkeydown=function(e){
      e.stopPropagation();
      if(e.key==='Enter'){ e.preventDefault(); title.blur(); }
      else if(e.key==='Escape'){ e.preventDefault(); cancelled=true; title.blur(); }
    };
    title.onblur=finish;
    title.focus();
    if(preserveSelection) return;
    var range=document.createRange(), selection=window.getSelection();
    range.selectNodeContents(title);
    selection.removeAllRanges();
    selection.addRange(range);
  }
  function editNodeRemark(node){
    if(!canEditCanvas()) return;
    if(!node) return;
    var oldRemark=node.params.remark||'';
    openTextDialog({title:'节点备注',value:oldRemark,placeholder:'写给自己或同事看的说明',multiline:true,max:300,onSave:function(next){
      next=next.trim().slice(0,300);
      if(next===oldRemark) return;
      pushUndo();
      node.params.remark=next;
      refreshNodeMeta(node);
      updateState(next?'备注已保存':'备注已清空');
    }});
  }
  function viewNodeRemark(node){
    var remark=node&&node.params?(node.params.remark||'').trim():'';
    if(!remark){ updateState('暂无备注'); return; }
    openMessageDialog('节点备注',remark);
  }
  function deleteNodeRemark(node){
    if(!canEditCanvas()) return;
    if(!node||!(node.params.remark||'').trim()) return;
    openConfirmDialog('删除备注','删除这个节点的备注？',function(){
      pushUndo();
      node.params.remark='';
      refreshNodeMeta(node);
      updateState('备注已删除');
    });
  }
  function chooseNodeImage(node,label){
    if(!canEditCanvas()) return;
    if(!node||node.type!=='image') return;
    var input=document.createElement('input');
    input.type='file';
    input.accept='image/*';
    input.style.position='fixed';
    input.style.left='-20px';
    input.style.top='-20px';
    input.style.width='1px';
    input.style.height='1px';
    input.style.opacity='0.01';
    input.onchange=function(){
      var f=input.files&&input.files[0];
      input.remove();
      if(f) imgToNode(node,f,label||'图片已上传');
    };
    input.oncancel=function(){ input.remove(); };
    document.body.appendChild(input);
    input.click();
  }
  function replaceNodeImageWithAsset(node,asset){
    if(!canEditCanvas()) return;
    if(!node||node.type!=='image'||!asset||!asset.url) return;
    if(node.image===asset.url){ updateState('当前节点已使用该图片'); return; }
    pushUndo();
    node.image=asset.url;
    node.outputs=node.outputs||{};
    node.outputs.image=asset.url;
    applyNodeUI(node);
    refreshAllGenRefs();
    redraw();
    updateState('已从资产列表更换图片');
  }
  function openNodeAssetPicker(node){
    if(!node||node.type!=='image') return;
    closeDialog();
    var mask=document.createElement('div');
    mask.className='nc-dialog-mask';
    mask.innerHTML='<div class="nc-dialog nc-asset-picker" role="dialog" aria-modal="true" aria-label="从资产列表选择图片">'
      +'<h3>从资产列表选择图片</h3>'
      +'<div class="nc-asset-picker-body" data-f="assetPicker"><div class="nc-side-empty">正在读取账户图片...</div></div>'
      +'<div class="nc-dialog-actions"><button type="button" data-f="cancel">取消</button></div></div>';
    document.body.appendChild(mask);
    var body=mask.querySelector('[data-f="assetPicker"]');
    function renderPicker(){
      var images=accountAssets.filter(function(a){ return a.type==='image'&&!!a.url; });
      if(!images.length){ body.innerHTML='<div class="nc-side-empty">资产列表中暂无图片</div>'; return; }
      body.innerHTML='<div class="nc-asset-grid">'+images.map(function(a,i){
        return '<button class="nc-asset-card image" type="button" data-picker-asset="'+i+'"><div class="nc-asset-thumb" style="background-image:url(\''+escapeHtml(a.url)+'\')"></div><div class="nc-asset-info"><b>'+escapeHtml(a.title)+'</b><span>'+escapeHtml(a.source)+'</span></div></button>';
      }).join('')+'</div>';
      body.querySelectorAll('[data-picker-asset]').forEach(function(btn){
        btn.onclick=function(){
          var asset=images[parseInt(btn.getAttribute('data-picker-asset'),10)];
          closeDialog();
          replaceNodeImageWithAsset(node,asset);
        };
      });
    }
    mask.querySelector('[data-f="cancel"]').onclick=closeDialog;
    mask.addEventListener('mousedown',function(e){ if(e.target===mask) closeDialog(); });
    if(accountAssetsLoaded) renderPicker();
    else fetchAccountAssets().then(renderPicker).catch(function(){ body.innerHTML='<div class="nc-side-empty">资产读取失败，请稍后重试</div>'; });
  }
  function menuForNode(e,node){
    e.preventDefault();
    if(!canEditCanvas()){
      selectNode(node);
      if((node.params.remark||'').trim()) showMenu([{icon:'preview',label:'查看备注',run:function(){ viewNodeRemark(node); }}],e.clientX,e.clientY);
      return;
    }
    var groupIds=selectedNodes[node.id]?selectedNodeIds():[];
    if(groupIds.length>1){
      showMenu([
        {icon:'copy',label:'批量复制 '+groupIds.length+' 个节点',run:function(){ copyNode(); }},
        {label:'批量折叠',run:function(){ setSelectedCollapsed(true); }},
        {label:'批量展开',run:function(){ setSelectedCollapsed(false); }},
        {icon:'remove',label:'批量删除',run:function(){ deleteSelectedNodes(); }}
      ],e.clientX,e.clientY);
      return;
    }
    selectNode(node);
    var items=[
      {icon:'note',label:(node.params.remark||'').trim()?'编辑备注':'添加备注',run:function(){ editNodeRemark(node); }}
    ];
    if(node.type==='image'){
      items.push({icon:'upload',label:'本地上传图片',run:function(){ chooseNodeImage(node,'图片已更换'); }});
      items.push({icon:'assets',label:'从资产列表选择',run:function(){ openNodeAssetPicker(node); }});
    }
    if((node.params.remark||'').trim()){
      items.push({icon:'preview',label:'查看备注',run:function(){ viewNodeRemark(node); }});
      items.push({icon:'remove',label:'删除备注',run:function(){ deleteNodeRemark(node); }});
    }
    showMenu(items,e.clientX,e.clientY);
  }
  function menuForCanvas(e){
    e.preventDefault();
    if(!canEditCanvas()){ showMenu([{icon:'preview',label:'适应视图',run:function(){ fitView(); }}],e.clientX,e.clientY); return; }
    var pt=canvasPointFromClient(e);
    var x=e.clientX, y=e.clientY;
    showMenu([
      {icon:'upload',label:'上传图片',run:function(){ uploadImageAt(pt); }},
      {icon:'assets',label:'保存到我的资产',disabled:true,title:'当前仅支持打开资产管理'},
      {icon:'add',label:'添加节点',run:function(){ showAddNodeMenu(pt,x,y); }},
      {kind:'separator'},
      {icon:'undo',label:'撤销',shortcut:commandShortcut('Z'),disabled:!history.canUndo(),run:function(){ undo(); }},
      {icon:'redo',label:'重做',shortcut:commandShortcut('Z',true),disabled:!history.canRedo(),run:function(){ redo(); }},
      {icon:'paste',label:'粘贴',shortcut:commandShortcut('V'),disabled:!clipNode,run:function(){ pasteNode(); }}
    ],x,y,{variant:'context',focus:true,label:'画布操作'});
  }
  function menuForEdge(e,i){
    e.preventDefault();
    selectedEdge=i; redraw();
    if(!canEditCanvas()) return;
    showMenu([
      {icon:'remove',label:'删除连线',run:function(){ delSelectedEdge(); }}
    ],e.clientX,e.clientY);
  }
  function defaultTemplateName(){
    var typed=tplName&&tplName.value.trim();
    if(typed) return typed;
    var inferred=inferBoardName(snapshot());
    if(inferred) return inferred;
    return '';
  }
  function saveTemplateWithName(name){
    name=String(name||'').trim().slice(0,40);
    if(!name){ updateState('请填写模板名字'); return; }
    var list=normalizeTemplates(getTemplates());
    list.push({name:name,createdAt:Date.now(),data:sanitizeTemplateSnap(templateSnapshot())});
    if(!saveTemplates(list)) return;
    if(tplSelect) tplSelect.value=String(list.length-1);
    if(tplName) tplName.value='';
    updateState('模板已保存');
  }
  function saveTemplate(){
    openTextDialog({title:'填写模板名字',value:defaultTemplateName(),placeholder:'请输入模板名字',max:40,onSave:function(name){
      saveTemplateWithName(name);
    }});
  }
  function selectedTemplate(){
    if(!tplSelect||tplSelect.value==='') return null;
    var idx=parseInt(tplSelect.value,10), list=normalizeTemplates(getTemplates());
    return {idx:idx,list:list,item:list[idx]};
  }
  function loadTemplateItem(item){
    if(!canEditCanvas()) return;
    if(!item||!item.data){ updateState('没有可载入的模板'); return; }
    if(!currentBoardId){ createBoardFromTemplate(item); return; }
    appendTemplateToCanvas(item);
  }
  function loadTemplate(){
    var t=selectedTemplate();
    if(!t||!t.item){ updateState('请先选择模板'); return; }
    loadTemplateItem(t.item);
  }
  function openTemplateLoadDialog(list){
    closeDialog();
    list=normalizeTemplates(list);
    if(!list.length){
      openMessageDialog('载入模板','暂无本地模板，请先点击“模板 > 保存模板”创建。');
      updateState('暂无模板');
      return;
    }
    var mask=document.createElement('div');
    mask.className='nc-dialog-mask';
    mask.innerHTML='<div class="nc-dialog" role="dialog" aria-modal="true">'
      +'<h3>选择要载入的模板</h3>'
      +'<div data-f="templateList" style="display:flex;flex-direction:column;gap:8px;max-height:320px;overflow:auto;"></div>'
      +'<div class="nc-dialog-actions"><button type="button" data-f="cancel">取消</button></div></div>';
    var box=mask.querySelector('[data-f="templateList"]');
    list.forEach(function(t,i){
      var count=(t.data&&t.data.nodes&&t.data.nodes.length)||0;
      var btn=document.createElement('button');
      btn.type='button';
      btn.style.cssText='display:flex;justify-content:space-between;gap:12px;width:100%;padding:10px 11px;border:1px solid rgba(148,164,187,.18);border-radius:8px;background:#0b1220;color:#eaf1fa;text-align:left;cursor:pointer;font:inherit;';
      btn.innerHTML='<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+escapeHtml(t.name||('模板 '+(i+1)))+'</span><span style="flex:none;color:#94a4bb;">'+count+' 节点</span>';
      btn.onclick=function(){
        closeDialog();
        if(tplSelect) tplSelect.value=String(i);
        loadTemplateItem(t);
      };
      box.appendChild(btn);
    });
    document.body.appendChild(mask);
    mask.querySelector('[data-f="cancel"]').onclick=closeDialog;
    mask.addEventListener('mousedown',function(e){ if(e.target===mask) closeDialog(); });
  }
  function showTemplateLoadMenu(anchor){
    var list=normalizeTemplates(getTemplates());
    openTemplateLoadDialog(list);
  }
  function deleteTemplate(){
    var t=selectedTemplate();
    if(!t||!t.item){ updateState('请先选择模板'); return; }
    t.list.splice(t.idx,1);
    if(!saveTemplates(t.list)) return;
    if(tplSelect) tplSelect.value='';
    updateState('模板已删除');
  }
  function exportTemplate(){
    var name=(tplName&&tplName.value.trim())||(selectedTemplate()&&selectedTemplate().item&&selectedTemplate().item.name)||'画布模板';
    exportTemplateItem({name:name,createdAt:Date.now(),data:sanitizeTemplateSnap(templateSnapshot())});
  }
  function exportTemplateItem(item){
    var name=(item&&item.name)||'画布模板';
    var data={name:name,createdAt:(item&&item.createdAt)||Date.now(),data:sanitizeTemplateSnap((item&&item.data)||templateSnapshot())};
    var blob=new Blob([canvasExporter.serializeTemplate(data)],{type:'application/json'});
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download=canvasExporter.safeFilename(name)+'.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function(){ URL.revokeObjectURL(a.href); },1000);
    updateState('模板已导出');
  }
  function importTemplateFile(file){
    if(!file) return;
    var reader=new FileReader();
    reader.onload=function(){
      try{
        var parsed=canvasExporter.parseTemplate(reader.result,file.name.replace(/\.json$/i,''));
        var name=parsed.name, snap=sanitizeTemplateSnap(parsed.data);
        var list=normalizeTemplates(getTemplates());
        list.push({name:name,createdAt:Date.now(),data:snap});
        if(!saveTemplates(list)) return;
        if(tplSelect) tplSelect.value=String(list.length-1);
        loadTemplateItem({name:name,data:snap});
        updateState('模板已导入');
      }catch(e){
        updateState('导入失败：JSON格式错误');
      }finally{
        if(tplImportFile) tplImportFile.value='';
      }
    };
    reader.readAsText(file);
  }

  // ---------- 节点交互 ----------
  function clearAlignmentGuides(){
    if(guideX) guideX.style.display='none';
    if(guideY) guideY.style.display='none';
  }
  function paintAlignmentGuides(movingIds){
    if(!graphApi||!graphApi.alignmentGuides) return;
    var list=Object.keys(nodes).map(function(id){ var node=nodes[id],rect=nodeRect(node); return {id:id,x:node.x,y:node.y,width:rect.w,height:rect.h}; });
    var guides=graphApi.alignmentGuides(list,movingIds,6/Math.max(zoom,.15));
    if(guideX){ guideX.style.display=guides.x==null?'none':'block'; if(guides.x!=null) guideX.style.left=guides.x+'px'; }
    if(guideY){ guideY.style.display=guides.y==null?'none':'block'; if(guides.y!=null) guideY.style.top=guides.y+'px'; }
  }
  function wireNode(node){
    var el=node.el, head=el.querySelector('[data-f="head"]');
    el.setAttribute('data-node-id',node.id);
    head.addEventListener('pointerdown', function(e){
      if(!canEditCanvas()) return;
      if(e.target&&e.target.getAttribute('data-f')==='del') return;
      if(e.target&&e.target.getAttribute('data-f')==='fold') return;
      if(e.target&&e.target.closest&&e.target.closest('[data-f="headTitle"][contenteditable="true"]')) return;
      var groupIds=selectedNodes[node.id]?selectedNodeIds():[node.id];
      if(groupIds.length<=1) selectNode(node);
      e.preventDefault();
      var sx=e.clientX, sy=e.clientY, moved=false, before=snapshot();
      var origins={};
      groupIds.forEach(function(id){ if(nodes[id]) origins[id]={x:nodes[id].x,y:nodes[id].y}; });
      function mv(ev){
        var dx=(ev.clientX-sx)/zoom, dy=(ev.clientY-sy)/zoom;
        if(Math.abs(dx)+Math.abs(dy)>2) moved=true;
        groupIds.forEach(function(id){
          var n=nodes[id], o=origins[id];
          if(!n||!o) return;
          n.x=Math.max(0,o.x+dx); n.y=Math.max(0,o.y+dy);
          n.el.style.left=n.x+'px'; n.el.style.top=n.y+'px';
          ensureNodeVisibleBounds(n);
        });
        redraw();
        updateSelectedRegion();
        paintAlignmentGuides(groupIds);
      }
      function up(){ clearAlignmentGuides(); if(moved) pushUndo(before); window.removeEventListener('pointermove',mv); window.removeEventListener('pointerup',up); }
      window.addEventListener('pointermove',mv); window.addEventListener('pointerup',up);
    });
    el.querySelectorAll('[data-resize]').forEach(function(handle){
      handle.addEventListener('pointerdown',function(e){
        if(!canEditCanvas()||!graphApi||!graphApi.resizeNodeRect) return;
        e.preventDefault(); e.stopPropagation();
        selectNode(node);
        var direction=handle.getAttribute('data-resize'),sx=e.clientX,sy=e.clientY,moved=false,before=snapshot();
        var start={x:node.x,y:node.y,width:el.offsetWidth||250,height:el.offsetHeight||160};
        function mv(ev){
          var rect=graphApi.resizeNodeRect(start,direction,(ev.clientX-sx)/zoom,(ev.clientY-sy)/zoom,{minWidth:220,maxWidth:520,minHeight:80,maxHeight:720});
          if(Math.abs(ev.clientX-sx)+Math.abs(ev.clientY-sy)>2) moved=true;
          node.x=Math.max(0,rect.x); node.y=Math.max(0,rect.y); node.width=rect.width; node.height=rect.height;
          el.style.left=node.x+'px'; el.style.top=node.y+'px'; el.style.width=node.width+'px'; el.style.height=node.height+'px'; el.classList.add('resized');
          ensureNodeVisibleBounds(node); redraw(); updateSelectedRegion();
        }
        function up(){
          window.removeEventListener('pointermove',mv); window.removeEventListener('pointerup',up);
          if(moved){ pushUndo(before); updateState('已调整节点大小'); }
        }
        window.addEventListener('pointermove',mv); window.addEventListener('pointerup',up);
      });
    });
    el.querySelector('[data-f="del"]').onclick=function(){ if(!canEditCanvas()) return; delNode(node.id); };
    el.querySelector('[data-f="fold"]').onclick=function(e){ e.stopPropagation(); toggleCollapsed(node); };
    el.addEventListener('dblclick',function(e){
      if(!canEditCanvas()) return;
      if(!e.target||!e.target.closest) return;
      if(e.target.closest('[data-f="headTitle"]')){
        e.preventDefault();
        e.stopPropagation();
        beginInlineRename(node);
        return;
      }
      if(e.target.closest('[data-f="head"]')) toggleCollapsed(node);
    });
    el.addEventListener('contextmenu',function(e){ menuForNode(e,node); });
    el.addEventListener('mousedown', function(){ if(!selectedNodes[node.id]) selectNode(node); });
    // ports: 点输出口→点输入口 连线
    el.querySelectorAll('.nc-port').forEach(function(p){
      p.addEventListener('pointerdown', function(ev){
        if(!canEditCanvas()) return;
        if(ev.button!==0) return;
        var kind=p.getAttribute('data-kind'), port=p.getAttribute('data-port');
        if(kind!=='out') return;
        ev.preventDefault();
        ev.stopPropagation();
        clearTextSelection();
        if(p.setPointerCapture){ try{ p.setPointerCapture(ev.pointerId); }catch(e){} }
        var pt=innerPoint(ev);
        dragPort={from:{node:node.id,port:port}, start:portCenter(node.id,'out',port), x:pt.x, y:pt.y, active:false, sx:ev.clientX, sy:ev.clientY};
        p.classList.add('dragging');
        function mv(moveEv){
          if(!dragPort) return;
          var mpt=innerPoint(moveEv);
          var snapTarget=nearbyInputPort(moveEv,port);
          if(snapTarget){
            var sc=portCenter(snapTarget.node,'in',port);
            dragPort.x=sc?sc.x:mpt.x; dragPort.y=sc?sc.y:mpt.y;
          }else{
            dragPort.x=mpt.x; dragPort.y=mpt.y;
          }
          if(!dragPort.active && Math.abs(moveEv.clientX-dragPort.sx)+Math.abs(moveEv.clientY-dragPort.sy)>4){
            dragPort.active=true; suppressPortClick=true; canvas.classList.add('connecting'); setConnectHints(port);
          }
          if(dragPort.active&&window.getSelection){ try{ window.getSelection().removeAllRanges(); }catch(e){} }
          if(dragPort.active) redraw();
        }
        function up(upEv){
          window.removeEventListener('pointermove',mv); window.removeEventListener('pointerup',up);
          if(p.releasePointerCapture){ try{ p.releasePointerCapture(ev.pointerId); }catch(e){} }
          var wasActive=dragPort&&dragPort.active;
          if(wasActive){
            var target=inputPortAt(upEv,port);
            if(target){ pushUndo(); connectEdge(dragPort.from,target); }
            else{
              var from=dragPort.from,pt=innerPoint(upEv),items=connectedNodeMenuItems(from,pt);
              if(items.length){ showMenu(items,upEv.clientX,upEv.clientY); updateState('选择要创建的下游节点'); }
              else updateState('当前输出暂无可连接的节点');
            }
          }
          dragPort=null; redraw(); clearConnectHints();
          setTimeout(function(){ suppressPortClick=false; },0);
        }
        window.addEventListener('pointermove',mv); window.addEventListener('pointerup',up);
      });
      p.addEventListener('click', function(ev){ ev.preventDefault(); ev.stopPropagation(); clearTextSelection();
        if(!canEditCanvas()) return;
        if(suppressPortClick) return;
        var kind=p.getAttribute('data-kind'), port=p.getAttribute('data-port');
        if(kind==='out'){ pendingPort={node:node.id, port:port}; setConnectHints(port); noteOf(node,'已选输出口「'+port+'」，点一个输入口连上','#2dd4bf'); }
        else if(kind==='in' && pendingPort){
          if(pendingPort.port===port){ pushUndo(); connectEdge(pendingPort,{node:node.id,port:port}); clearConnectHints(); }
          else noteOf(node,'端口类型不匹配：'+pendingPort.port+' 不能连到 '+port,'#f4708a');
        }
      });
    });
    // 表单
    var txt=el.querySelector('[data-f="text"]'); if(txt){
      txt.addEventListener('focus',function(){ node._editSnap=snapshot(); node._editValue=txt.value; });
      txt.addEventListener('blur',function(){ if(node._editSnap&&txt.value!==node._editValue) pushUndo(node._editSnap); node._editSnap=null; });
      txt.oninput=function(){ if(!canEditCanvas()) return; node.params.text=txt.value; if(node.type==='text') node.outputs.prompt=txt.value; scheduleSave(); };
    }
    var out=el.querySelector('[data-f="out"]'); if(out){
      out.addEventListener('focus',function(){ node._editSnap=snapshot(); node._editValue=out.value; });
      out.addEventListener('blur',function(){ if(node._editSnap&&out.value!==node._editValue) pushUndo(node._editSnap); node._editSnap=null; });
      out.oninput=function(){ if(!canEditCanvas()) return; node.outputs.prompt=out.value; scheduleSave(); };
    }
    el.querySelectorAll('.nc-seg').forEach(function(seg){ var f=seg.getAttribute('data-f');
      seg.querySelectorAll('.nc-chip').forEach(function(c){ c.onclick=function(){ if(!canEditCanvas()) return; if(node.params[f]!==c.getAttribute('data-v')) pushUndo(); seg.querySelectorAll('.nc-chip').forEach(function(x){x.classList.remove('on');}); c.classList.add('on'); node.params[f]=c.getAttribute('data-v'); refreshVideoNodeHint(node); scheduleSave(); }; }); });
    var openShortDrama=el.querySelector('[data-f="openShortDrama"]');
    if(openShortDrama) openShortDrama.onclick=function(e){ e.stopPropagation(); openShortDramaWorkspace(node).catch(function(){}); };
    var openDigitalPresenter=el.querySelector('[data-f="openDigitalPresenter"]');
    if(openDigitalPresenter) openDigitalPresenter.onclick=function(e){ e.stopPropagation(); openDigitalPresenterWorkspace(node).catch(function(){}); };
    // 图片节点上传/粘贴
    var file=el.querySelector('[data-f="file"]'), drop=el.querySelector('[data-f="drop"]');
    if(file){ file.onchange=function(){ if(!canEditCanvas()) return; var label=node._imageActionLabel||'图片已上传'; node._imageActionLabel=''; imgToNode(node, file.files&&file.files[0], label); }; }
    if(drop){ drop.setAttribute('tabindex','0'); drop.addEventListener('paste', function(e){ if(!canEditCanvas()) return; var it=(e.clipboardData&&e.clipboardData.items)||[]; for(var i=0;i<it.length;i++){ if(it[i].kind==='file'&&it[i].type&&it[i].type.indexOf('image/')===0){ var f=it[i].getAsFile(); if(f){ e.preventDefault(); imgToNode(node,f); return; } } } }); }
    if(drop){ drop.addEventListener('paste', function(e){ if(!canEditCanvas()) return; var it=(e.clipboardData&&e.clipboardData.items)||[], sawFile=false, sawImage=false; for(var i=0;i<it.length;i++){ if(it[i].kind==='file'){ sawFile=true; if(it[i].type&&it[i].type.indexOf('image/')===0) sawImage=true; } } if(sawFile&&!sawImage){ e.preventDefault(); rejectImageFile(node,'请粘贴 PNG/JPG/WebP 图片'); } }); }
    var run=el.querySelector('[data-f="run"]'); if(run) run.onclick=function(){ if(!canEditCanvas()) return; runNode(node.id); };
  }
  function rejectImageFile(node,msg){
    if(!node) return;
    delete node.image;
    if(node.outputs) delete node.outputs.image;
    var d=node.el&&node.el.querySelector('[data-f="drop"]');
    if(d){ d.style.backgroundImage=''; d.innerHTML='点击上传<br>或按 Ctrl+V 粘贴'; }
    refreshAllGenRefs();
    setNodeState(node,'error',msg||'请上传 PNG/JPG/WebP 图片','#f4708a');
    updateState('图片格式不支持');
  }
  function validateImageFile(f){
    if(!f) return Promise.reject(new Error('请选择图片文件'));
    var allowType=/^image\/(png|jpe?g|webp)$/i.test(f.type||'');
    return f.slice(0,12).arrayBuffer().then(function(buf){
      var b=new Uint8Array(buf), type='';
      if(b[0]===0x89&&b[1]===0x50&&b[2]===0x4e&&b[3]===0x47) type='png';
      else if(b[0]===0xff&&b[1]===0xd8&&b[2]===0xff) type='jpg';
      else if(b[0]===0x52&&b[1]===0x49&&b[2]===0x46&&b[3]===0x46&&b[8]===0x57&&b[9]===0x45&&b[10]===0x42&&b[11]===0x50) type='webp';
      if(!allowType||!type) throw new Error('请上传 PNG/JPG/WebP 图片');
      return f;
    });
  }
  function imgToNode(node, f, label){ if(!canEditCanvas()) return; if(!f) return; var before=snapshot();
    validateImageFile(f).then(function(file){
      var url=URL.createObjectURL(file), im=new Image();
      im.onload=function(){ var mx=IMAGE_SAVE_MAX,s=Math.min(1,mx/Math.max(im.width,im.height)),cv=document.createElement('canvas');
        cv.width=Math.max(1,Math.round(im.width*s)); cv.height=Math.max(1,Math.round(im.height*s));
        var ctx=cv.getContext('2d'); ctx.fillStyle='#fff'; ctx.fillRect(0,0,cv.width,cv.height); ctx.drawImage(im,0,0,cv.width,cv.height);
        pushUndo(before);
        var durl=cv.toDataURL('image/jpeg',IMAGE_SAVE_QUALITY); node.image=durl; node.outputs.image=durl;
        var d=node.el.querySelector('[data-f="drop"]'); if(d){ d.style.backgroundImage='url('+durl+')'; d.innerHTML=''; }
        refreshAllGenRefs();
        updateState(label||'图片已上传');
        try{URL.revokeObjectURL(url);}catch(e){} };
      im.onerror=function(){ try{URL.revokeObjectURL(url);}catch(e){} rejectImageFile(node,'图片无法读取，请更换 PNG/JPG/WebP 图片'); };
      im.src=url;
    }).catch(function(e){ rejectImageFile(node,(e&&e.message)||'请上传 PNG/JPG/WebP 图片'); });
  }
  function selectNode(node){
    Object.keys(nodes).forEach(function(k){ nodes[k].el.classList.remove('sel'); });
    selectedNodes={};
    selectedNode=node?node.id:null;
    selectedEdge=-1;
    if(node) node.el.classList.add('sel');
    updateSelectedRegion();
    updateImageToolbar();
  }
  function selectNodesByIds(ids){
    selectedNodes={};
    selectedNode=null;
    selectedEdge=-1;
    Object.keys(nodes).forEach(function(k){ nodes[k].el.classList.remove('sel'); });
    (ids||[]).forEach(function(id){
      if(nodes[id]&&nodes[id].el){
        selectedNodes[id]=true;
        nodes[id].el.classList.add('sel');
      }
    });
    var keys=Object.keys(selectedNodes);
    if(keys.length===1) selectedNode=keys[0];
    updateSelectedRegion();
    updateImageToolbar();
    updateState(keys.length?('已选中 '+keys.length+' 个节点'):'就绪');
  }
  function updateSelectedRegion(){
    if(!selectedRegion) return;
    var ids=Object.keys(selectedNodes||{}).filter(function(id){ return !!nodes[id]; });
    if(ids.length<2){
      selectedRegion.style.display='none';
      updateImageToolbar();
      return;
    }
    var minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
    ids.forEach(function(id){
      var r=nodeRect(nodes[id]);
      minX=Math.min(minX,r.x); minY=Math.min(minY,r.y);
      maxX=Math.max(maxX,r.x+r.w); maxY=Math.max(maxY,r.y+r.h);
    });
    var pad=10;
    selectedRegion.style.display='block';
    selectedRegion.style.left=(minX-pad)+'px';
    selectedRegion.style.top=(minY-pad)+'px';
    selectedRegion.style.width=(maxX-minX+pad*2)+'px';
    selectedRegion.style.height=(maxY-minY+pad*2)+'px';
    updateImageToolbar();
  }
  function rectsIntersect(a,b){
    return a.x<=b.x+b.w && a.x+a.w>=b.x && a.y<=b.y+b.h && a.y+a.h>=b.y;
  }
  function nodeRect(node){
    return {x:node.x,y:node.y,w:(node.el&&node.el.offsetWidth)||250,h:(node.el&&node.el.offsetHeight)||160};
  }
  function selectionRect(a,b){
    var x=Math.min(a.x,b.x), y=Math.min(a.y,b.y);
    return {x:x,y:y,w:Math.abs(a.x-b.x),h:Math.abs(a.y-b.y)};
  }
  function renderSelectionBox(rect){
    if(!selectionBox) return;
    selectionBox.style.display='block';
    selectionBox.style.left=rect.x+'px';
    selectionBox.style.top=rect.y+'px';
    selectionBox.style.width=rect.w+'px';
    selectionBox.style.height=rect.h+'px';
  }
  function hideSelectionBox(){
    if(selectionBox) selectionBox.style.display='none';
    if(canvas) canvas.classList.remove('selecting');
  }
  function delNode(id){
    if(!canEditCanvas()) return;
    if(nodes[id]){
      pushUndo();
      destroyShortDramaWorkspace(nodes[id]);
      destroyDigitalPresenterWorkspace(nodes[id]);
      nodes[id].el.remove();delete nodes[id];
      if(selectedNode===id) selectedNode=null;
      delete selectedNodes[id];
      edges=edges.filter(function(e){ return e.from.node!==id && e.to.node!==id; });
      redraw();refreshAllGenRefs();updateSelectedRegion();updateState('已更新');
    }
  }
  function clearCanvas(){
    if(!canEditCanvas()) return;
    destroyAllShortDramaWorkspaces();
    destroyAllDigitalPresenterWorkspaces();
    Object.keys(nodes).forEach(function(id){ if(nodes[id]&&nodes[id].el) nodes[id].el.remove(); });
    nodes={}; edges=[]; pendingPort=null; selectedNode=null; selectedNodes={}; selectedEdge=-1; redraw(); updateSelectedRegion(); updateState('空画布');
  }
  function resetDemo(){
    clearCanvas();
    var t=addNode('text',60,60), r=addNode('reverse',360,230), img=addNode('image',60,260), g=addNode('gen',690,90);
    var ta=t.el.querySelector('[data-f="text"]');
    if(ta){ ta.value='科技焕肤 · 逆龄新生，高级感美业海报，金色光效，9:16 竖版，留白标题区'; t.params.text=ta.value; t.outputs.prompt=ta.value; }
    edges.push({from:{node:t.id,port:'prompt'}, to:{node:g.id,port:'prompt'}});
    edges.push({from:{node:img.id,port:'image'}, to:{node:r.id,port:'image'}});
    redraw(); refreshAllGenRefs(); updateState('示例已重置');
    fitView();
  }
  svg.addEventListener('click', function(e){
    var hit=e.target&&e.target.closest?e.target.closest('.nc-edge-hit'):null;
    if(!hit) return;
    e.stopPropagation();
    selectEdge(parseInt(hit.getAttribute('data-edge'),10));
  });
  svg.addEventListener('contextmenu', function(e){
    var hit=e.target&&e.target.closest?e.target.closest('.nc-edge-hit'):null;
    if(!hit) return;
    menuForEdge(e,parseInt(hit.getAttribute('data-edge'),10));
  });
  canvas.addEventListener('click', function(e){
    if(suppressCanvasClick){ e.preventDefault(); suppressCanvasClick=false; return; }
    if(e.target===canvas||e.target===inner){ pendingPort=null; selectedEdge=-1; selectNode(null); redraw(); }
  });
  canvas.addEventListener('dblclick', function(e){
    var t=e.target;
    if(t&&t.closest&&t.closest('.nc-node,.nc-port,.nc-edge-hit,button,a,input,textarea,.nc-empty-card,.nc-menu')) return;
    if(!canEditCanvas()) return;
    e.preventDefault();
    e.stopPropagation();
    showAddNodeMenu(canvasPointFromClient(e),e.clientX,e.clientY);
  });
  if(selectedRegion){
    selectedRegion.addEventListener('pointerdown', function(e){
      if(!canEditCanvas()) return;
      if(e.button!==0) return;
      var ids=selectedNodeIds();
      if(ids.length<2) return;
      e.preventDefault();
      e.stopPropagation();
      hideMenu();
      var sx=e.clientX, sy=e.clientY, moved=false, before=snapshot();
      var origins={};
      ids.forEach(function(id){ if(nodes[id]) origins[id]={x:nodes[id].x,y:nodes[id].y}; });
      selectedRegion.classList.add('dragging');
      function mv(ev){
        var dx=(ev.clientX-sx)/zoom, dy=(ev.clientY-sy)/zoom;
        if(Math.abs(dx)+Math.abs(dy)>2) moved=true;
        ids.forEach(function(id){
          var n=nodes[id], o=origins[id];
          if(!n||!o) return;
          n.x=Math.max(0,o.x+dx); n.y=Math.max(0,o.y+dy);
          n.el.style.left=n.x+'px'; n.el.style.top=n.y+'px';
          ensureNodeVisibleBounds(n);
        });
        redraw();
        updateSelectedRegion();
        paintAlignmentGuides(ids);
      }
      function up(){
        if(moved) pushUndo(before);
        clearAlignmentGuides();
        selectedRegion.classList.remove('dragging');
        window.removeEventListener('pointermove',mv);
        window.removeEventListener('pointerup',up);
      }
      window.addEventListener('pointermove',mv);
      window.addEventListener('pointerup',up);
    });
    selectedRegion.addEventListener('contextmenu', function(e){
      var ids=selectedNodeIds();
      if(ids.length<2) return;
      e.preventDefault();
      e.stopPropagation();
      showMenu([
        {icon:'copy',label:'批量复制 '+ids.length+' 个节点',run:function(){ copyNode(); }},
        {label:'批量折叠',run:function(){ setSelectedCollapsed(true); }},
        {label:'批量展开',run:function(){ setSelectedCollapsed(false); }},
        {icon:'remove',label:'批量删除',run:function(){ deleteSelectedNodes(); }}
      ],e.clientX,e.clientY);
    });
  }
  canvas.addEventListener('contextmenu', function(e){
    var t=e.target;
    if(t&&t.closest&&t.closest('.nc-node,.nc-port,.nc-edge-hit,.nc-empty-card,.nc-menu')) return;
    menuForCanvas(e);
  });
  canvas.addEventListener('pointerdown', function(e){
    if(e.button!==0) return;
    var t=e.target;
    hideMenu();
    if(t&&t.closest&&t.closest('.nc-node,.nc-port,.nc-edge-hit,button,a,input,textarea,.nc-empty-card,.nc-menu')) return;
    e.preventDefault();
    pendingPort=null; clearConnectHints();
    if(e.ctrlKey||e.metaKey){
      var start=innerPoint(e), moved=false;
      canvas.classList.add('selecting');
      renderSelectionBox({x:start.x,y:start.y,w:0,h:0});
      function smv(ev){
        var cur=innerPoint(ev), rect=selectionRect(start,cur);
        if(rect.w+rect.h>3) moved=true;
        renderSelectionBox(rect);
      }
      function sup(ev){
        window.removeEventListener('pointermove',smv);
        window.removeEventListener('pointerup',sup);
        var end=innerPoint(ev), rect=selectionRect(start,end), ids=[];
        hideSelectionBox();
        if(moved){
          Object.keys(nodes).forEach(function(id){
            if(rectsIntersect(rect,nodeRect(nodes[id]))) ids.push(id);
          });
          selectNodesByIds(ids);
          suppressCanvasClick=true;
          setTimeout(function(){ suppressCanvasClick=false; },80);
        }else{
          selectNode(null);
        }
      }
      window.addEventListener('pointermove',smv);
      window.addEventListener('pointerup',sup);
      return;
    }
    selectNode(null);
    var sx=e.clientX, sy=e.clientY, sl=canvas.scrollLeft, st=canvas.scrollTop;
    canvas.classList.add('panning');
    function mv(ev){ canvas.scrollLeft=sl-(ev.clientX-sx); canvas.scrollTop=st-(ev.clientY-sy); scheduleMap(); }
    function up(){ scheduleMap(); canvas.classList.remove('panning'); window.removeEventListener('pointermove',mv); window.removeEventListener('pointerup',up); }
    window.addEventListener('pointermove',mv); window.addEventListener('pointerup',up);
  });
  canvas.addEventListener('wheel',function(e){
    e.preventDefault();
    if(e.ctrlKey||e.metaKey){
      var r=canvas.getBoundingClientRect();
      setZoom(zoom*(e.deltaY>0?.9:1.1),e.clientX-r.left,e.clientY-r.top);
      return;
    }
    var dx=e.deltaX||0, dy=e.deltaY||0;
    if(e.shiftKey&&!dx){ dx=dy; dy=0; }
    canvas.scrollLeft+=dx;
    canvas.scrollTop+=dy;
    scheduleMap();
  },{passive:false});
  canvas.addEventListener('selectstart',function(e){
    var t=e.target, tag=(t&&t.tagName||'').toLowerCase();
    if(tag==='input'||tag==='textarea'||(t&&(t.isContentEditable||(t.closest&&t.closest('[contenteditable="true"]'))))) return;
    if(canvas.classList.contains('connecting')||canvas.classList.contains('selecting')||dragPort||(t&&t.closest&&t.closest('.nc-port,.nc-go,.nc-chip,.nc-head,.nc-lab,.nc-node-title,.nc-menu'))){ e.preventDefault(); }
  });
  canvas.addEventListener('dragstart',function(e){ if(canvas.classList.contains('connecting')||dragPort){ e.preventDefault(); } });
  canvas.addEventListener('scroll',function(){ syncCanvasGrid(); scheduleMap(); updateImageToolbar(); },{passive:true});
  window.addEventListener('resize',function(){ scheduleMap(); updateImageToolbar(); });
  document.addEventListener('click',function(e){ if(!menu||!menu.classList.contains('on')) return; if(e.target&&e.target.closest&&e.target.closest('.nc-menu')) return; hideMenu(); });
  document.addEventListener('keydown',function(e){ if(e.key==='Escape'){ hideMenu(); if(localFullscreen) showBoardHome(); } });
  if(map){
    map.addEventListener('pointerdown',function(e){
      e.preventDefault();
      moveViewFromMap(e);
      function mv(ev){ moveViewFromMap(ev); }
      function up(){ window.removeEventListener('pointermove',mv); window.removeEventListener('pointerup',up); }
      window.addEventListener('pointermove',mv);
      window.addEventListener('pointerup',up);
    });
  }

  // ---------- 取上游输入 ----------
  function inputVal(nodeId, port){
    var e=edges.find(function(x){ return x.to.node===nodeId && x.to.port===port; });
    if(!e) return null; var up=nodes[e.from.node]; if(!up) return null;
    return up.outputs[e.from.port];
  }
  function inputEdge(nodeId, port){
    return edges.find(function(x){ return x.to.node===nodeId && x.to.port===port; });
  }
  function missingInput(node, port, allowPendingUpstream){
    var e=inputEdge(node.id,port);
    if(port==='image' && node.image) return '';
    if(port==='prompt' && (node.params.text||'').trim()) return '';
    if(!e) return port==='prompt'?'缺少提示词输入':'缺少图片输入';
    var up=nodes[e.from.node];
    if(!up) return '上游节点不存在';
    if(!up.outputs[e.from.port]) return allowPendingUpstream ? '' : '上游未完成：'+((TYPE[up.type]&&TYPE[up.type].name)||up.type);
    return '';
  }
  function validateNodeInputs(node, mark, allowPendingUpstream){
    if(!node) return true;
    var msg='';
    if(node.type==='reverse') msg=missingInput(node,'image',allowPendingUpstream);
    if(node.type==='gen') msg=missingInput(node,'prompt',allowPendingUpstream);
    if(node.type==='video') msg=missingInput(node,'prompt',allowPendingUpstream);
    if(msg){
      if(mark) setNodeState(node,'error',msg,'#f4708a');
      return false;
    }
    return true;
  }
  function validateCanvasInputs(){
    var bad=[];
    Object.keys(nodes).forEach(function(id){
      var n=nodes[id];
      if((n.type==='reverse'||n.type==='gen'||n.type==='video')&&!validateNodeInputs(n,true,true)) bad.push(n);
    });
    if(bad.length){
      selectNode(bad[0]);
      focusNode(bad[0]);
      updateState('有节点缺少输入');
      return false;
    }
    return true;
  }

  // ---------- 运行单个节点（返回 Promise，供并行/串行）----------
  function runNode(id){
    if(!canEditCanvas()) return Promise.resolve();
    var node=nodes[id]; if(!node) return Promise.resolve();
    if(!validateNodeInputs(node,true)){ updateState('缺少输入'); return Promise.reject('缺少输入'); }
    if(node.type==='reverse'){
      var img=inputVal(id,'image')||node.image;
      if(!img){ setNodeState(node,'error','请把一个图片节点连到输入口','#f4708a'); updateState('缺少图片'); return Promise.reject('无图'); }
      var b64=img.indexOf(',')>=0?img.split(',')[1]:img;
      var run=node.el.querySelector('[data-f="run"]'); run.disabled=true; setNodeState(node,'running','反推中…','#2dd4bf'); updateState('运行中');
      return apiClient.json('/api/gen/reverse',{method:'POST',body:{image:b64}})
        .then(function(d){ run.disabled=false;
          if(!d.prompt) throw new Error(d.detail||'反推失败');
          node.outputs.prompt=d.prompt; var o=node.el.querySelector('[data-f="out"]'); if(o) o.value=d.prompt;
          setNodeState(node,'done','反推完成','#2bd576'); updateState('已完成'); if(window.HQ&&HQ.refreshPoints) HQ.refreshPoints(); })
        .catch(function(e){ run.disabled=false; setNodeState(node,'error','失败：'+(e.message||e),'#f4708a'); updateState('有错误'); throw e; });
    }
    if(node.type==='gen'){
      var prompt=inputVal(id,'prompt')||node.params.text||'';
      if(!prompt.trim()){ setNodeState(node,'error','需要提示词（自己填或从上游连入）','#f4708a'); updateState('缺少提示词'); return Promise.reject('无词'); }
      var refImgs=refImagesForNode(node); var refImg=refImgs[0]||''; var eng=node.params.engine||'nb2';
      var bp={prompt:prompt.trim(), ratio:node.params.ratio||'9:16', quality:node.params.quality||'hd', count:1};
      if(refImg) bp.image=refImg.indexOf(',')>=0?refImg.split(',')[1]:refImg;
      if(refImgs.length>1) bp.images=refImgs.map(function(img){ return img.indexOf(',')>=0?img.split(',')[1]:img; });
      var endpoint, gbtn=node.el.querySelector('[data-f="run"]');
      if(eng==='gpt'||eng==='zelong'){
        endpoint='/api/gen/image';
        if(eng==='zelong') bp.provider='zelong';
      } else {
        endpoint='/api/gen/banana'; bp.model=eng;
      }
      gbtn.disabled=true; setNodeState(node,'running','提交中…','#2dd4bf'); updateState('运行中');
      return apiClient.json(endpoint,{method:'POST',body:bp})
        .catch(function(error){
          var data=error&&error.data||{};
          if(error&&error.status===402) throw makeRunNodeError('点数不足',{code:'insufficient_points'});
          if(error&&error.status===429) throw makeRunNodeError(data.detail||'任务排队中，请稍后再试',{
            code:data.code||(data.active_jobs!=null?'active_job_cap':'queue_full'),
            retryable:true,
            retryAfterMs:data.retry_after_ms||RUN_ALL_RETRY_MS
          });
          throw error;
        })
        .then(function(data){
          if(!data.job_id) throw makeRunNodeError(data.detail||'提交失败',{code:data.code});
          return apiModule.poll({
            request:function(){ return apiClient.json('/api/gen/job/'+data.job_id); },
            intervalMs:3000,
            maxMs:420000,
            inspect:function(d){
              if(d.status==='done') return {done:true,value:typeof d.result==='string'?JSON.parse(d.result):d.result};
              if(d.status==='error'||d.status==='failed') return {error:makeRunNodeError(d.error||'生成失败',{code:d.code||'job_failed'})};
              return {pending:true};
            },
            onProgress:function(d,sec){ setNodeState(node,'running','生成中… 已用 '+sec+'s','#2dd4bf'); },
            timeoutError:function(){ return makeRunNodeError('超时',{code:'timeout'}); }
          });
        })
        .then(function(result){ gbtn.disabled=false;
          var url=(result&&(result.url||(result.urls&&result.urls[0])))||'';
          node.outputs.image=url;
          var box=node.el.querySelector('[data-f="result"]'); if(box){ box.style.display='block'; box.style.backgroundImage='url("'+url+'?t='+Date.now()+'")'; box.innerHTML=''; }
          setNodeState(node,'done','出图完成','#2bd576'); updateState('已完成'); if(window.HQ&&HQ.refreshPoints) HQ.refreshPoints(); })
        .catch(function(e){
          gbtn.disabled=false;
          if(e&&e.retryable){
            setNodeState(node,'running',(e.message||'任务排队中，请稍后再试')+' · 稍后自动重试','#2dd4bf');
            updateState('等待队列空位');
            throw e;
          }
          setNodeState(node,'error','失败：'+(e.message||e),'#f4708a'); updateState('有错误'); throw e;
        });
    }
    if(node.type==='video'){
      var videoPrompt=inputVal(id,'prompt')||node.params.text||'';
      if(!videoPrompt.trim()){ setNodeState(node,'error','需要视频提示词（自己填或从上游连入）','#f4708a'); updateState('缺少提示词'); return Promise.reject('无词'); }
      var videoRefs=refImagesForNode(node).slice(0,4);
      var videoChannel=node.params.channel||'grok';
      var payload={channel:videoChannel,prompt:videoPrompt.trim()};
      if(videoChannel==='grok') payload.ratio=node.params.ratio||'16:9';
      if(videoRefs.length) payload.reference_images=videoRefs.map(function(img){ return img.indexOf(',')>=0?img.split(',')[1]:img; });
      var vbtn=node.el.querySelector('[data-f="run"]');
      vbtn.disabled=true; setNodeState(node,'running','提交中...','#2dd4bf'); updateState('运行中');
      return apiClient.json('/api/gen/xiaole_video',{method:'POST',body:payload})
        .catch(function(error){
          var data=error&&error.data||{};
          if(error&&error.status===402) throw makeRunNodeError('点数不足',{code:'insufficient_points'});
          if(error&&error.status===429) throw makeRunNodeError(data.detail||'任务排队中，请稍后再试',{
            code:data.code||(data.active_jobs!=null?'active_job_cap':'queue_full'),
            retryable:true,
            retryAfterMs:data.retry_after_ms||RUN_ALL_RETRY_MS
          });
          throw error;
        })
        .then(function(data){
          if(!data.job_id) throw makeRunNodeError(data.detail||'提交失败',{code:data.code});
          return apiModule.poll({
            request:function(){ return apiClient.json('/api/gen/job/'+data.job_id); },
            intervalMs:3000,
            maxMs:900000,
            inspect:function(d){
              if(d.status==='done') return {done:true,value:typeof d.result==='string'?JSON.parse(d.result):d.result};
              if(d.status==='error'||d.status==='failed') return {error:makeRunNodeError(d.error||'生成失败',{code:d.code||'job_failed'})};
              return {pending:true};
            },
            onProgress:function(d,sec){ setNodeState(node,'running','生成中，已用 '+sec+'s','#2dd4bf'); },
            timeoutError:function(){ return makeRunNodeError('超时',{code:'timeout'}); }
          });
        })
        .then(function(result){ vbtn.disabled=false;
          var url=(result&&(result.video_url||result.source_video_url||result.url||(result.urls&&result.urls[0])))||'';
          if(!url) throw makeRunNodeError('未返回视频地址',{code:'missing_video_url'});
          node.outputs.video=url;
          node.outputs.video_url=url;
          renderVideoResult(node,url);
          setNodeState(node,'done','视频生成完成','#2bd576'); updateState('已完成'); if(window.HQ&&HQ.refreshPoints) HQ.refreshPoints(); })
        .catch(function(e){
          vbtn.disabled=false;
          if(e&&e.retryable){
            setNodeState(node,'running',(e.message||'任务排队中，请稍后再试')+' · 稍后自动重试','#2dd4bf');
            updateState('等待队列空位');
            throw e;
          }
          setNodeState(node,'error','失败：'+normalizeVideoNodeError(node,e),'#f4708a'); updateState('有错误'); throw e;
        });
    }
    return Promise.resolve();
  }

  // ---------- 循环依赖检测 ----------
  function detectCycle(){
    return graphApi.detectCycle(Object.keys(nodes).map(function(id){ return {id:id}; }),edges);
  }
  function markCycle(ids){
    if(!ids||!ids.length) return;
    ids.forEach(function(id){
      var n=nodes[id];
      if(n) setNodeState(n,'error','检测到循环依赖，请删除其中一条连线','#f4708a');
    });
    if(nodes[ids[0]]){
      selectNode(nodes[ids[0]]);
      focusNode(nodes[ids[0]]);
    }
    updateState('检测到循环依赖');
  }
  // ---------- 运行全部：按依赖分波并行 ----------
  function runAllExecutableNodes(){
    return Object.keys(nodes).map(function(k){ return nodes[k]; }).filter(function(n){
      return n && (n.type==='reverse' || n.type==='gen' || n.type==='video');
    });
  }
  function runAllReady(node){ // 所有输入口都有上游输出
    var t=TYPE[node.type]; if(!t.ins) return true;
    return t.ins.every(function(p){
      var matched=edges.filter(function(x){ return x.to.node===node.id && x.to.port===p; });
      var e=matched[0];
      if(p==='image' && node.image) return true;           // 图片节点自带图
      if(p==='prompt' && (node.params.text||'').trim()) return true; // 自填词
      if(!e) return p==='image'?true:false;                // image 可选、prompt 必须(除非自填)
      if(p==='image' && (node.type==='gen'||node.type==='video')) return matched.every(function(x){ return !!(nodes[x.from.node] && nodes[x.from.node].outputs[x.from.port]); });
      return !!(nodes[e.from.node] && nodes[e.from.node].outputs[e.from.port]);
    });
  }
  function finishRunAllBatch(label){
    clearRunAllRetry();
    runAllBatch=null;
    setRunAllBusy(false);
    updateState(label||'已完成');
  }
  function startRunAllBatch(){
    if(runAllBatch){ updateState('批次运行中，请等待当前任务完成'); return; }
    if(!validateCanvasInputs()) return;
    var cycle=detectCycle();
    if(cycle.length){ markCycle(cycle); return; }
    runAllExecutableNodes().forEach(resetNodeForRunAll);
    runAllBatch={done:{},running:{},failed:{},retryAt:{}};
    setRunAllBusy(true);
    updateState('排队中');
    runAllBatch.tick=function(){
      if(!runAllBatch) return;
      var batch=runAllBatch, all=runAllExecutableNodes(), now=Date.now();
      var pending=all.filter(function(n){ return !batch.done[n.id]; });
      var active=Object.keys(batch.running).some(function(k){ return batch.running[k]; });
      var waiting=pending.filter(function(n){ return !batch.running[n.id] && batch.retryAt[n.id] && batch.retryAt[n.id]>now; });
      var runnable=pending.filter(function(n){
        return !batch.running[n.id] && (!batch.retryAt[n.id] || batch.retryAt[n.id]<=now) && runAllReady(n);
      });
      var remoteActive=Object.keys(batch.running).reduce(function(sum,id){
        return sum + (batch.running[id] && isRunAllRemoteNode(nodes[id]) ? 1 : 0);
      },0);
      var remoteSlots=Math.max(0, RUN_ALL_REMOTE_LIMIT-remoteActive);
      var localRunnable=runnable.filter(function(n){ return !isRunAllRemoteNode(n); });
      var remoteRunnable=runnable.filter(function(n){ return isRunAllRemoteNode(n); }).slice(0, remoteSlots);
      var toStart=localRunnable.concat(remoteRunnable);
      if(!toStart.length){
        if(waiting.length){
          var nextDelay=waiting.reduce(function(min,n){
            var left=(batch.retryAt[n.id]||now)-now;
            return Math.min(min, Math.max(800,left));
          }, RUN_ALL_RETRY_MS);
          updateState(active?'运行中（等待队列空位）':'等待队列空位');
          scheduleRunAllRetry(nextDelay);
          return;
        }
        if(active){ updateState('运行中'); return; }
        if(Object.keys(batch.failed).length || pending.length){ finishRunAllBatch('部分节点失败'); return; }
        finishRunAllBatch('已完成');
        return;
      }
      updateState('运行中');
      toStart.forEach(function(n){
        batch.running[n.id]=true;
        delete batch.retryAt[n.id];
        runNode(n.id).then(function(){
          if(!runAllBatch || runAllBatch!==batch) return;
          batch.done[n.id]=true;
          batch.running[n.id]=false;
          delete batch.failed[n.id];
          delete batch.retryAt[n.id];
          batch.tick();
        }).catch(function(err){
          if(!runAllBatch || runAllBatch!==batch) return;
          batch.running[n.id]=false;
          if(err&&err.retryable){
            batch.retryAt[n.id]=Date.now()+(err.retryAfterMs||RUN_ALL_RETRY_MS);
            setNodeState(n,'running',(err.message||'任务排队中，请稍后再试')+' · 稍后自动重试','#2dd4bf');
            batch.tick();
            return;
          }
          batch.done[n.id]=true;
          batch.failed[n.id]=true;
          batch.tick();
        });
      });
    };
    runAllBatch.tick();
  }
  if(runAllBtn) runAllBtn.onclick=startRunAllBatch;

  // ---------- 顶部添加 ----------
  function bindCanvasAddButton(button){
    if(!button) return;
    button.onclick=function(){
      var type=button.getAttribute('data-add');
      var created=nodeCreationPolicy.run(type,function(){
        pushUndo();addNode(type);updateState('已添加');
      });
      if(!created&&type==='digitalPresenter'&&currentBoardScope!=='collab'){
        updateState('数字人口播项目请在协作画布中创建');
      }
    };
  }
  function addDigitalPresenterEntry(){
    digitalPresenterEntryEnabled=true;
    [document.querySelector('.nc-group[aria-label="添加节点"]'),document.querySelector('.nc-empty-card')].forEach(function(host){
      if(!host) return;
      var button=document.createElement('button');button.type='button';button.className='nc-add';
      button.setAttribute('data-add','digitalPresenter');button.title='创建数字人口播项目';button.textContent='+ 数字人口播';
      bindCanvasAddButton(button);host.appendChild(button);
    });
  }
  var digitalPresenterEntryRegistrar=digitalPresenterModule.createEntryRegistrar(addDigitalPresenterEntry);
  function registerDigitalPresenterEntry(capability){ return digitalPresenterEntryRegistrar.register(capability); }
  document.querySelectorAll('.nc-add').forEach(bindCanvasAddButton);
  apiClient.json('/api/gen/digital-presenter/capability').then(registerDigitalPresenterEntry).catch(function(){});
  document.getElementById('ncClear').onclick=function(){ if(!canEditCanvas()) return; if(Object.keys(nodes).length){ pushUndo(); clearCanvas(); } };
  if(cleanupStorageBtn) cleanupStorageBtn.onclick=function(){ cleanupLocalSpace(); };
  document.getElementById('ncResetDemo').onclick=function(){ if(!canEditCanvas()) return; pushUndo(); resetDemo(); };
  document.getElementById('ncCopy').onclick=function(){ copyNode(); };
  document.getElementById('ncPaste').onclick=function(){ pasteNode(); };
  document.getElementById('ncLayout').onclick=function(){ autoLayout(); };
  document.getElementById('ncZoomOut').onclick=function(){ setZoom(zoom-.1); };
  document.getElementById('ncZoomIn').onclick=function(){ setZoom(zoom+.1); };
  document.getElementById('ncZoomFit').onclick=function(){ fitView(); };
  bindZoomInput(zoomLabel);
  bindZoomInput(fsZoomLabel);
  if(fullscreenBtn) fullscreenBtn.onclick=function(){ toggleFullscreen(); };
  if(backHomeBtn) backHomeBtn.onclick=function(){ showBoardHome(); };
  if(boardSearch) boardSearch.oninput=function(){ renderBoardHome(); };
  if(boardSort) boardSort.onchange=function(){ renderBoardHome(); };
  document.querySelectorAll('[data-board-tab]').forEach(function(tab){
    tab.onclick=function(){
      boardMode=tab.getAttribute('data-board-tab')||'mine';
      document.querySelectorAll('[data-board-tab]').forEach(function(t){ t.classList.toggle('on',t===tab); });
      renderBoardHome();
    };
  });
  bindButton(fsAdd,function(){
    var r=fsAdd.getBoundingClientRect();
    showAddNodeMenu(viewportNodePoint(),r.left,r.top-10,'above');
  });
  bindButton(fsAgent,function(){ openSidePanel('agent'); });
  bindButton(fsUndo,function(){ undo(); });
  bindButton(fsRedo,function(){ redo(); });
  bindButton(fsZoomOut,function(){ setZoom(zoom-.1); });
  bindButton(fsZoomIn,function(){ setZoom(zoom+.1); });
  bindButton(fsFit,function(){ fitView(); });
  function templateMenuItems(anchor){
    return [
      {label:'保存模板',run:function(){ saveTemplate(); }},
      {label:'载入模板',run:function(){ showTemplateLoadMenu(anchor); }},
      {label:'删除模板',run:function(){ deleteTemplate(); }},
      {label:'导出模板',run:function(){ exportTemplate(); }},
      {label:'导入模板',run:function(){ if(tplImportFile) tplImportFile.click(); }}
    ];
  }
  function moreMenuItems(){
    return [
      {label:'自动整理',run:function(){ autoLayout(); }},
      {label:'清空画布',run:function(){ if(Object.keys(nodes).length){ pushUndo(); clearCanvas(); } }}
    ];
  }
  bindButton(fsTplMenu,function(){
    showMenuFromButton(fsTplMenu,templateMenuItems(fsTplMenu));
  });
  bindButton(fsRun,function(){ document.getElementById('ncRunAll').click(); });
  bindButton(fsExit,function(){ showBoardHome(); });
  bindButton(fsMore,function(){
    showMenuFromButton(fsMore,moreMenuItems());
  });
  bindButton(sideTplMenu,function(){
    closeSidePanel();
    showMenuFromButton(sideTplMenu,templateMenuItems(sideTplMenu));
  });
  bindButton(sideMore,function(){
    closeSidePanel();
    showMenuFromButton(sideMore,moreMenuItems());
  });
  document.querySelectorAll('.nc-side-tool[data-side]').forEach(function(btn){
    btn.onclick=function(e){ e.stopPropagation(); openSidePanel(btn.getAttribute('data-side')); };
  });
  document.querySelectorAll('[data-agent-start]').forEach(function(btn){
    btn.onclick=function(e){ e.stopPropagation(); startAgentPrompt(btn.getAttribute('data-agent-start')); };
  });
  document.querySelectorAll('[data-canvas-view]').forEach(function(btn){
    btn.onclick=function(){ setCanvasView(btn.getAttribute('data-canvas-view')); };
  });
  if(sideClose) sideClose.onclick=function(e){ e.stopPropagation(); closeSidePanel(); };
  if(sidePanel){
    sidePanel.addEventListener('pointerdown',stopUiEvent);
    sidePanel.addEventListener('mousedown',stopUiEvent);
    sidePanel.addEventListener('click',function(e){ e.stopPropagation(); });
  }
  var sideTools=document.getElementById('ncSideTools');
  if(sideTools){
    sideTools.addEventListener('pointerdown',stopUiEvent);
    sideTools.addEventListener('mousedown',stopUiEvent);
    sideTools.addEventListener('click',function(e){ e.stopPropagation(); });
  }
  document.addEventListener('fullscreenchange',updateFullscreenUI);
  document.addEventListener('webkitfullscreenchange',updateFullscreenUI);
  var fsDock=document.getElementById('ncFsDock');
  if(fsDock){
    fsDock.addEventListener('pointerdown',stopUiEvent);
    fsDock.addEventListener('mousedown',stopUiEvent);
    fsDock.addEventListener('click',stopUiEvent);
    fsDock.addEventListener('contextmenu',function(e){ e.preventDefault(); e.stopPropagation(); });
  }
  document.getElementById('ncTplSave').onclick=function(){ saveTemplate(); };
  document.getElementById('ncTplLoad').onclick=function(){ loadTemplate(); };
  document.getElementById('ncTplDelete').onclick=function(){ deleteTemplate(); };
  document.getElementById('ncTplExport').onclick=function(){ exportTemplate(); };
  document.getElementById('ncTplImport').onclick=function(){ if(tplImportFile) tplImportFile.click(); };
  if(tplImportFile) tplImportFile.onchange=function(){ importTemplateFile(tplImportFile.files&&tplImportFile.files[0]); };
  if(undoBtn) undoBtn.onclick=function(){ undo(); };
  if(redoBtn) redoBtn.onclick=function(){ redo(); };
  window.addEventListener('storage',function(e){
    if(e.key!==storageApi.DEFAULT_KEYS.boards||!currentBoardId||currentBoardScope!=='local') return;
    var list=getBoards(), board=list.find(function(b){ return b.id===currentBoardId; });
    var latestAt=board&&board.updatedAt||0;
    if(latestAt && boardLastSeenUpdatedAt && latestAt!==boardLastSeenUpdatedAt){
      boardConflict=true;
      setSaveState('conflict');
      updateState('另一个标签页已更新，请刷新后继续编辑');
    }
  });
  document.addEventListener('visibilitychange',function(){
    if(currentBoardScope!=='collab'||!currentBoardId) return;
    scheduleCollabPoll(0);
    if(!document.hidden) sendCollabPresence();
  });
  window.addEventListener('beforeunload',function(){ destroyAllShortDramaWorkspaces();destroyAllDigitalPresenterWorkspaces();stopCollabSync(); });
  document.addEventListener('keydown',function(e){
    var tag=(e.target&&e.target.tagName||'').toLowerCase();
    var editing=tag==='input'||tag==='textarea'||tag==='select'||!!(e.target&&(e.target.isContentEditable||(e.target.closest&&e.target.closest('[contenteditable="true"]'))));
    var key=String(e.key).toLowerCase(), command=e.ctrlKey||e.metaKey;
    if(!canEditCanvas()&&!editing&&((command&&(key==='z'||key==='y'||key==='v'))||e.key==='Delete'||e.key==='Backspace')){
      e.preventDefault();
      return;
    }
    if((e.ctrlKey||e.metaKey)&&String(e.key).toLowerCase()==='z' && !editing){
      e.preventDefault(); if(e.shiftKey) redo(); else undo();
    }
    if((e.ctrlKey||e.metaKey)&&String(e.key).toLowerCase()==='y' && !editing){
      e.preventDefault(); redo();
    }
    if((e.ctrlKey||e.metaKey)&&String(e.key).toLowerCase()==='c' && !editing){
      e.preventDefault(); copyNode();
    }
    if((e.ctrlKey||e.metaKey)&&String(e.key).toLowerCase()==='v' && !editing){
      e.preventDefault(); pasteNode();
    }
    if((e.key==='Delete'||e.key==='Backspace') && !editing){
      if(selectedEdge>=0){ e.preventDefault(); delSelectedEdge(); }
      else if(selectedNodeIds().length){ e.preventDefault(); deleteSelectedNodes(); }
    }
  });

  renderTemplates();
  migrateDraftToBoards();
  if(!openInitialBoardFromUrl()){
    renderBoardHome();
    updateState('请选择或新建画布');
  }
  return;
  var draft=loadDraft();
  if(draft&&Array.isArray(draft.nodes)){
    loading=true;
    restoreSnapshot(draft);
    loading=false;
    updateState('已恢复草稿');
  }else{
    resetDemo();
  }
})();
