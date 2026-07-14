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

  function changedFields(base,next){
    var patch={};
    Object.keys(next||{}).forEach(function(key){
      if(key==='id') return;
      if(!same(base&&base[key],next[key])) patch[key]=clone(next[key]);
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
      var patch=changedFields(oldNodes[id],newNodes[id]);
      if(Object.keys(patch).length) ops.push({type:'node.patch',id:id,fields:patch});
    });
    Object.keys(oldNodes).sort().forEach(function(id){
      if(!newNodes[id]) ops.push({type:'node.delete',id:id});
    });

    var oldEdges=edgesByKey(base.edges), newEdges=edgesByKey(next.edges);
    Object.keys(newEdges).sort().forEach(function(id){
      if(!oldEdges[id]) ops.push({type:'edge.create',id:id,edge:clone(newEdges[id])});
      else{
        var patch=changedFields(oldEdges[id],newEdges[id]);
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
        if(index>=0) result.nodes[index]=Object.assign({},result.nodes[index],clone(op.fields||{}));
      }else if(op.type==='node.delete'&&op.id){
        result.nodes=result.nodes.filter(function(item){ return item.id!==op.id; });
        result.edges=result.edges.filter(function(item){
          return !(item&&item.from&&item.from.node===op.id)&&!(item&&item.to&&item.to.node===op.id);
        });
      }else if(op.type==='edge.create'&&op.edge){
        if(!result.edges.some(function(item){ return edgeKey(item)===(op.id||edgeKey(op.edge)); })) result.edges.push(clone(op.edge));
      }else if(op.type==='edge.patch'&&op.id){
        index=result.edges.findIndex(function(item){ return edgeKey(item)===op.id; });
        if(index>=0) result.edges[index]=Object.assign({},result.edges[index],clone(op.fields||{}));
      }else if(op.type==='edge.delete'&&op.id){
        result.edges=result.edges.filter(function(item){ return edgeKey(item)!==op.id; });
      }
    });
    return result;
  }

  function makeNodeId(clientId,counter){
    var safe=String(clientId||'client').toLowerCase().replace(/[^a-z0-9]/g,'').slice(0,12)||'client';
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

  return {
    applyOps:applyOps,
    clone:clone,
    diffSnapshots:diffSnapshots,
    edgeKey:edgeKey,
    makeBatch:makeBatch,
    makeNodeId:makeNodeId,
    mergeRemote:mergeRemote,
    pollDelay:pollDelay,
    remoteOps:remoteOps,
    retryDelay:retryDelay
  };
});
