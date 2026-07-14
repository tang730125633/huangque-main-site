(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root) root.HQCanvasCollabSync=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function clone(value){
    return value==null?value:JSON.parse(JSON.stringify(value));
  }

  function same(left,right){
    return JSON.stringify(left)===JSON.stringify(right);
  }

  function plainObject(value){
    return !!value&&Object.prototype.toString.call(value)==='[object Object]';
  }

  function mergePatch(target,patch){
    var result=plainObject(target)?clone(target):{};
    Object.keys(patch||{}).forEach(function(key){
      var value=patch[key];
      if(value===null) delete result[key];
      else if(plainObject(value)) result[key]=mergePatch(result[key],value);
      else result[key]=clone(value);
    });
    return result;
  }

  function listById(list){
    var map={};
    (list||[]).forEach(function(item){ if(item&&item.id) map[item.id]=item; });
    return map;
  }

  function edgeKey(edge){
    var from=(edge&&edge.from)||{}, to=(edge&&edge.to)||{};
    return String(from.node||'')+':'+String(from.port||'')+'->'+String(to.node||'')+':'+String(to.port||'');
  }

  function edgesByKey(list){
    var map={};
    (list||[]).forEach(function(item){ var key=edgeKey(item); if(key!==':->:') map[key]=item; });
    return map;
  }

  function changedFields(base,next,skipId){
    var patch={};
    var keys={};
    Object.keys(base||{}).forEach(function(key){ keys[key]=true; });
    Object.keys(next||{}).forEach(function(key){ keys[key]=true; });
    Object.keys(keys).forEach(function(key){
      if(skipId&&key==='id') return;
      var before=base&&base[key], after=next&&next[key];
      var beforeDeleted=!Object.prototype.hasOwnProperty.call(base||{},key)||before===null;
      var afterDeleted=!Object.prototype.hasOwnProperty.call(next||{},key)||after===null;
      if(beforeDeleted&&afterDeleted) return;
      if(same(before,after)) return;
      if(!Object.prototype.hasOwnProperty.call(next||{},key)) patch[key]=null;
      else if(plainObject(before)&&plainObject(after)){
        var nested=changedFields(before,after,false);
        if(Object.keys(nested).length) patch[key]=nested;
      }else patch[key]=clone(after);
    });
    return patch;
  }

  function diffSnapshots(base,next){
    base=base||{}; next=next||{};
    var ops=[], oldNodes=listById(base.nodes), newNodes=listById(next.nodes);
    Object.keys(newNodes).sort().forEach(function(id){
      if(!oldNodes[id]) ops.push({type:'node.create',node:clone(newNodes[id])});
    });
    Object.keys(newNodes).sort().forEach(function(id){
      if(!oldNodes[id]) return;
      var patch=changedFields(oldNodes[id],newNodes[id],true);
      if(Object.keys(patch).length) ops.push({type:'node.patch',id:id,fields:patch});
    });
    Object.keys(oldNodes).sort().forEach(function(id){
      if(!newNodes[id]) ops.push({type:'node.delete',id:id});
    });

    var oldEdges=edgesByKey(base.edges), newEdges=edgesByKey(next.edges);
    Object.keys(newEdges).sort().forEach(function(id){
      if(!oldEdges[id]) ops.push({type:'edge.create',id:id,edge:clone(newEdges[id])});
      else{
        var patch=changedFields(oldEdges[id],newEdges[id],true);
        if(Object.keys(patch).length) ops.push({type:'edge.patch',id:id,fields:patch});
      }
    });
    Object.keys(oldEdges).sort().forEach(function(id){
      if(!newEdges[id]) ops.push({type:'edge.delete',id:id});
    });
    return ops;
  }

  function applyOps(snapshot,ops){
    var result=clone(snapshot||{})||{};
    result.nodes=Array.isArray(result.nodes)?result.nodes:[];
    result.edges=Array.isArray(result.edges)?result.edges:[];
    (ops||[]).forEach(function(op){
      if(!op||!op.type) return;
      var index;
      if(op.type==='node.create'&&op.node&&op.node.id){
        index=result.nodes.findIndex(function(item){ return item.id===op.node.id; });
        if(index<0) result.nodes.push(clone(op.node));
      }else if(op.type==='node.patch'&&op.id){
        index=result.nodes.findIndex(function(item){ return item.id===op.id; });
        if(index>=0) result.nodes[index]=mergePatch(result.nodes[index],op.fields||{});
      }else if(op.type==='node.delete'&&op.id){
        result.nodes=result.nodes.filter(function(item){ return item.id!==op.id; });
        result.edges=result.edges.filter(function(item){
          return !(item&&item.from&&item.from.node===op.id)&&!(item&&item.to&&item.to.node===op.id);
        });
      }else if(op.type==='edge.create'&&op.edge){
        if(!result.edges.some(function(item){ return edgeKey(item)===(op.id||edgeKey(op.edge)); })) result.edges.push(clone(op.edge));
      }else if(op.type==='edge.patch'&&op.id){
        index=result.edges.findIndex(function(item){ return edgeKey(item)===op.id; });
        if(index>=0) result.edges[index]=mergePatch(result.edges[index],op.fields||{});
      }else if(op.type==='edge.delete'&&op.id){
        result.edges=result.edges.filter(function(item){ return edgeKey(item)!==op.id; });
      }
    });
    return result;
  }

  function makeNodeId(clientId,counter){
    var raw=String(clientId||'client').toLowerCase().replace(/[^a-z0-9]/g,'')||'client';
    var safe=raw.slice(0,48);
    return 'n_'+safe+'_'+Math.max(1,Number(counter)||1);
  }

  function mergeRemote(base,current,ops){
    var localOps=diffSnapshots(base,current);
    var remoteBase=applyOps(base,ops);
    return {base:remoteBase,current:applyOps(remoteBase,localOps),localOps:localOps};
  }

  function remoteOps(batches,clientId){
    var result=[];
    (batches||[]).forEach(function(batch){
      if(!batch||batch.client_id===clientId) return;
      result=result.concat(clone(batch.ops||[]));
    });
    return result;
  }

  function pollDelay(hidden){
    return hidden?3000:800;
  }

  function retryDelay(attempt){
    return Math.min(8000,1000*Math.pow(2,Math.max(0,Number(attempt)||0)));
  }

  function makeBatch(clientId,baseVersion,ops,idFactory){
    var suffix=(idFactory||function(){ return Date.now().toString(36)+Math.random().toString(36).slice(2,8); })();
    return {op_id:String(clientId)+'-'+String(suffix),client_id:String(clientId),base_version:Number(baseVersion)||0,ops:clone(ops||[])};
  }

  function canEditCanvas(scope,role){
    return scope!=='collab'||role==='owner'||role==='editor';
  }

  function normalizeNodeTitle(text,defaultName){
    var value=String(text||'').replace(/\s+/g,' ').trim().slice(0,40);
    return value===String(defaultName||'')?'':value;
  }

  function createController(options){
    options=options||{};
    var transport=options.transport||{};
    var scheduleRetry=options.scheduleRetry||function(fn,delay){ return setTimeout(fn,delay); };
    var cancelRetry=options.cancelRetry||function(handle){ clearTimeout(handle); };
    var generation=0, session=null, activeBatch=null, pendingSave=null;
    var retryHandle=null, retryAttempt=0, pollRequest=null;

    function canEdit(){
      return !!session&&(session.role==='owner'||session.role==='editor');
    }

    function state(){
      return {
        active:!!session,
        boardId:session&&session.boardId||null,
        generation:generation,
        pending:!!pendingSave,
        polling:!!pollRequest,
        saving:!!activeBatch,
        version:session?session.version:0
      };
    }

    function notifyState(){
      if(options.onState) options.onState(state(),{
        activeBatch:activeBatch&&clone(activeBatch.batch),
        baseSnapshot:session&&clone(session.baseSnapshot),
        pendingSnapshot:pendingSave&&clone(pendingSave.snapshot),
        retrying:retryHandle!=null
      });
    }

    function clearRetry(){
      if(retryHandle!=null) cancelRetry(retryHandle);
      retryHandle=null;
    }

    function matches(request){
      return !!session&&request.generation===session.generation&&request.boardId===session.boardId;
    }

    function start(config){
      config=config||{};
      clearRetry();
      generation++;
      session={
        boardId:String(config.boardId||''),
        generation:generation,
        version:Number(config.version)||0,
        role:config.role||'viewer',
        baseSnapshot:clone(config.baseSnapshot||{})||{}
      };
      activeBatch=null;
      pendingSave=null;
      pollRequest=null;
      retryAttempt=0;
      notifyState();
      return generation;
    }

    function stop(){
      clearRetry();
      generation++;
      session=null;
      activeBatch=null;
      pendingSave=null;
      pollRequest=null;
      retryAttempt=0;
      notifyState();
    }

    function failSave(request,error){
      if(!matches(request)||activeBatch!==request) return;
      var status=Number(error&&error.status)||0;
      if(status>=400&&status<500&&status!==408&&status!==429){
        clearRetry();
        activeBatch=null;
        pendingSave=null;
        retryAttempt=0;
        if(options.onError) options.onError(error,'save-permanent');
        notifyState();
        return;
      }
      retryAttempt++;
      clearRetry();
      if(options.onError) options.onError(error,'save');
      retryHandle=scheduleRetry(function(){
        retryHandle=null;
        if(!matches(request)||activeBatch!==request) return;
        sendActive(request);
      },retryDelay(retryAttempt-1));
      notifyState();
    }

    function acceptSave(request,data){
      if(!matches(request)||activeBatch!==request) return;
      var board=data&&data.board;
      if(!board||!board.data){
        failSave(request,new Error('authoritative board missing from save response'));
        return;
      }
      clearRetry();
      var live=options.getSnapshot?options.getSnapshot():request.snapshot;
      var postSendOps=canEdit()?diffSnapshots(request.snapshot,live):[];
      session.baseSnapshot=clone(board.data);
      session.version=Number(board.version)||Number(data.version)||session.version;
      session.role=board.role||session.role;
      var next=canEdit()?applyOps(session.baseSnapshot,postSendOps):clone(session.baseSnapshot);
      if(pendingSave) pendingSave.snapshot=clone(next);
      else if(postSendOps.length) pendingSave={snapshot:clone(next),extraOps:[]};
      activeBatch=null;
      retryAttempt=0;
      if(options.onBoard) options.onBoard(clone(board),data);
      if(options.onSnapshot) options.onSnapshot(clone(next),{source:'save',board:clone(board)});
      notifyState();
      pumpSave();
    }

    function sendActive(request){
      var result;
      if(!matches(request)||activeBatch!==request) return;
      try{
        result=transport.save(request.boardId,request.batch);
      }catch(error){
        failSave(request,error);
        return;
      }
      Promise.resolve(result).then(function(data){
        acceptSave(request,data);
      },function(error){
        failSave(request,error);
      });
    }

    function pumpSave(){
      if(!session||!canEdit()||activeBatch||pollRequest||!pendingSave) return;
      var queued=pendingSave;
      pendingSave=null;
      var ops=diffSnapshots(session.baseSnapshot,queued.snapshot).concat(clone(queued.extraOps||[]));
      if(!ops.length){
        notifyState();
        return;
      }
      var batch=makeBatch(options.clientId,session.version,ops,options.idFactory);
      activeBatch={
        batch:batch,
        boardId:session.boardId,
        generation:session.generation,
        snapshot:clone(queued.snapshot)
      };
      retryAttempt=0;
      notifyState();
      sendActive(activeBatch);
    }

    function save(snapshot,extraOps){
      if(!session||!canEdit()) return false;
      pendingSave={snapshot:clone(snapshot||{}),extraOps:clone(extraOps||[])};
      notifyState();
      pumpSave();
      return true;
    }

    function acceptPoll(request,data){
      if(!matches(request)||pollRequest!==request) return {ignored:true};
      var current=options.getSnapshot?options.getSnapshot():session.baseSnapshot;
      var nextBase, next, changed=false, previousRole=session.role;
      if(data&&data.role) session.role=data.role;
      if(data&&data.board&&data.board.role) session.role=data.board.role;
      if(data&&data.reset&&data.board&&data.board.data){
        nextBase=clone(data.board.data);
        changed=true;
      }else{
        var incoming=remoteOps(data&&data.batches,options.clientId);
        nextBase=applyOps(session.baseSnapshot,incoming);
        changed=incoming.length>0;
      }
      if(canEdit()) next=applyOps(nextBase,diffSnapshots(session.baseSnapshot,current));
      else next=clone(nextBase);
      session.baseSnapshot=nextBase;
      session.version=Math.max(session.version,Number(data&&data.version)||Number(data&&data.board&&data.board.version)||0);
      if(previousRole!==session.role&&!canEdit()) changed=true;
      if(!canEdit()) pendingSave=null;
      else if(pendingSave) pendingSave.snapshot=clone(next);
      if(previousRole!==session.role&&options.onRole) options.onRole(session.role,previousRole);
      if(data&&data.board&&options.onBoard) options.onBoard(clone(data.board),data);
      if(changed&&options.onSnapshot) options.onSnapshot(clone(next),{source:'poll',reset:!!(data&&data.reset)});
      if(options.onPoll) options.onPoll(data||{},changed);
      return {changed:changed,ignored:false};
    }

    function poll(){
      if(!session||activeBatch||pollRequest||typeof transport.sync!=='function') return Promise.resolve({skipped:true});
      var request={boardId:session.boardId,generation:session.generation,since:session.version};
      pollRequest=request;
      notifyState();
      var result;
      try{
        result=transport.sync(request.boardId,request.since);
      }catch(error){
        result=Promise.reject(error);
      }
      request.promise=Promise.resolve(result).then(function(data){
        return acceptPoll(request,data);
      },function(error){
        if(!matches(request)||pollRequest!==request) return {ignored:true};
        if(options.onError) options.onError(error,'poll');
        return {error:error,failed:true};
      }).then(function(outcome){
        if(matches(request)&&pollRequest===request){
          pollRequest=null;
          notifyState();
          pumpSave();
        }
        return outcome;
      });
      return request.promise;
    }

    return {
      getBaseSnapshot:function(){ return session&&clone(session.baseSnapshot); },
      getPendingBatch:function(){ return activeBatch&&clone(activeBatch.batch); },
      getPendingSnapshot:function(){ return pendingSave&&clone(pendingSave.snapshot); },
      getState:state,
      poll:poll,
      save:save,
      start:start,
      stop:stop
    };
  }

  return {
    applyOps:applyOps,
    canEditCanvas:canEditCanvas,
    clone:clone,
    createController:createController,
    diffSnapshots:diffSnapshots,
    edgeKey:edgeKey,
    makeBatch:makeBatch,
    makeNodeId:makeNodeId,
    mergePatch:mergePatch,
    mergeRemote:mergeRemote,
    normalizeNodeTitle:normalizeNodeTitle,
    pollDelay:pollDelay,
    remoteOps:remoteOps,
    retryDelay:retryDelay
  };
});
