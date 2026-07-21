(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.graph=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function index(nodes){ var out={}; (nodes||[]).forEach(function(n){ if(n&&n.id) out[n.id]=n; }); return out; }
  function validEdges(nodes,edges){
    var byId=index(nodes);
    return (edges||[]).filter(function(e){ return e&&e.from&&e.to&&byId[e.from.node]&&byId[e.to.node]; });
  }
  function detectCycle(nodes,edges){
    var byId=index(nodes), usable=validEdges(nodes,edges), visiting={}, visited={}, stack=[], cycle=[];
    function dfs(id){
      if(cycle.length) return true;
      if(visiting[id]){ var at=stack.indexOf(id); cycle=stack.slice(at<0?0:at).concat(id); return true; }
      if(visited[id]) return false;
      visiting[id]=true; stack.push(id);
      usable.filter(function(e){ return e.from.node===id; }).some(function(e){ return dfs(e.to.node); });
      stack.pop(); visiting[id]=false; visited[id]=true;
      return !!cycle.length;
    }
    Object.keys(byId).some(dfs);
    return cycle.filter(function(id,i,list){ return list.indexOf(id)===i; });
  }
  function topologicalOrder(nodes,edges){
    var ids=(nodes||[]).filter(function(n){return n&&n.id;}).map(function(n){return n.id;});
    var usable=validEdges(nodes,edges), indegree={}, outgoing={}, result=[];
    ids.forEach(function(id){ indegree[id]=0; outgoing[id]=[]; });
    usable.forEach(function(e){ indegree[e.to.node]++; outgoing[e.from.node].push(e.to.node); });
    var queue=ids.filter(function(id){return indegree[id]===0;});
    while(queue.length){ var id=queue.shift(); result.push(id); outgoing[id].forEach(function(to){ if(--indegree[to]===0) queue.push(to); }); }
    return result.concat(ids.filter(function(id){return result.indexOf(id)<0;}));
  }
  function computeAutoLayout(nodes,edges,options){
    options=options||{};
    var ids=(nodes||[]).filter(function(n){return n&&n.id;}).map(function(n){return n.id;});
    var usable=validEdges(nodes,edges), level={}, outgoing={}, indegree={}, seen={}, buckets={}, positions={};
    ids.forEach(function(id){ level[id]=0; outgoing[id]=[]; indegree[id]=0; });
    usable.forEach(function(e){ indegree[e.to.node]++; outgoing[e.from.node].push(e.to.node); });
    var queue=ids.filter(function(id){return !indegree[id];});
    while(queue.length){ var id=queue.shift(); seen[id]=true; outgoing[id].forEach(function(to){ level[to]=Math.max(level[to],level[id]+1); if(--indegree[to]===0) queue.push(to); }); }
    ids.forEach(function(id){ if(!seen[id]) level[id]=level[id]||0; (buckets[level[id]]||(buckets[level[id]]=[])).push(id); });
    Object.keys(buckets).sort(function(a,b){return Number(a)-Number(b);}).forEach(function(value){
      buckets[value].forEach(function(id,i){ positions[id]={x:(options.startX||60)+Number(value)*(options.columnGap||310),y:(options.startY||60)+i*(options.rowGap||190)}; });
    });
    return positions;
  }
  function contentBounds(nodes,options){
    options=options||{}; if(!(nodes||[]).length) return null;
    var minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity,pad=options.padding==null?60:options.padding;
    nodes.forEach(function(n){ var w=n.width||250,h=n.height||160; minX=Math.min(minX,n.x||0); minY=Math.min(minY,n.y||0); maxX=Math.max(maxX,(n.x||0)+w); maxY=Math.max(maxY,(n.y||0)+h); });
    return {x:Math.max(0,minX-pad),y:Math.max(0,minY-pad),w:Math.max(360,maxX-minX+pad*2),h:Math.max(240,maxY-minY+pad*2)};
  }
  return {detectCycle:detectCycle,topologicalOrder:topologicalOrder,computeAutoLayout:computeAutoLayout,contentBounds:contentBounds};
});
