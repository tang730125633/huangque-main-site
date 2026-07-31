(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.agent=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var MAX_NODES=60, MAX_EDGES=120, MAX_ACTIONS=12;

  function digest(value){
    var text=JSON.stringify(value), hash=2166136261;
    for(var i=0;i<text.length;i++){
      hash^=text.charCodeAt(i);
      hash=Math.imul(hash,16777619);
    }
    return ('00000000'+(hash>>>0).toString(16)).slice(-8);
  }
  function sameIds(left,right){
    left=(left||[]).slice().sort(); right=(right||[]).slice().sort();
    return left.length===right.length&&left.every(function(id,index){return id===right[index];});
  }
  function createSnapshot(input){
    input=input||{};
    var selected=(input.selectedNodeIds||[]).slice(0,30), selectedSet={};
    selected.forEach(function(id){selectedSet[id]=true;});
    var source=input.nodes||[];
    if(source.length>MAX_NODES&&!selected.length) throw new Error('画布节点超过 60 个，请先选中要处理的节点');
    if(selected.length) source=source.filter(function(node){return !!selectedSet[node.id];});
    if(source.length>MAX_NODES) throw new Error('选中的节点超过 60 个，请缩小范围');
    var ids={}, nodes=source.map(function(node){
      ids[node.id]=true;
      return {id:String(node.id),type:String(node.type),title:String(node.title||'').slice(0,120),content:String(node.content||'').slice(0,5000),selected:!!selectedSet[node.id]};
    });
    var edges=(input.edges||[]).filter(function(edge){return ids[edge.from_node_id]&&ids[edge.to_node_id];}).slice(0,MAX_EDGES);
    var snapshot={project_id:String(input.projectId||''),scope:String(input.scope||'local'),selected_node_ids:selected.filter(function(id){return !!ids[id];}),nodes:nodes,edges:edges};
    snapshot.snapshot_digest=digest(snapshot);
    return snapshot;
  }
  function compatible(sourceType,targetType){
    var outputs={text:['prompt'],image:['image'],reverse:['prompt'],gen:['image'],video:[],shortDrama:[]};
    var inputs={text:[],image:[],reverse:['image'],gen:['prompt','image'],video:['prompt','image'],shortDrama:[]};
    return (outputs[sourceType]||[]).some(function(port){return (inputs[targetType]||[]).indexOf(port)>=0;});
  }
  function validatePlan(snapshot,plan){
    if(!plan||plan.project_id!==snapshot.project_id||plan.snapshot_digest!==snapshot.snapshot_digest) throw new Error('画布已发生变化，请重新让 Agent 分析');
    if(!sameIds(plan.selected_node_ids,snapshot.selected_node_ids)) throw new Error('画布选区已发生变化，请重新让 Agent 分析');
    if(!Array.isArray(plan.actions)||plan.actions.length>MAX_ACTIONS) throw new Error('Agent 操作数量超过限制');
    var nodes={}, actionIds={}; snapshot.nodes.forEach(function(node){nodes[node.id]=node;});
    plan.actions.forEach(function(action){
      if(!action||!action.id||actionIds[action.id]) throw new Error('Agent 操作标识无效');
      actionIds[action.id]=true;
      if(action.type==='update_text_node'&&(!nodes[action.node_id]||nodes[action.node_id].type!=='text'||snapshot.selected_node_ids.indexOf(action.node_id)<0)) throw new Error('Agent 只能修改当前选中的文本节点');
      if(action.type==='connect_nodes'&&(!nodes[action.from_node_id]||!nodes[action.to_node_id]||!compatible(nodes[action.from_node_id].type,nodes[action.to_node_id].type))) throw new Error('Agent 连线节点不存在或端口不兼容');
      if(action.type==='select_nodes'&&(action.node_ids||[]).some(function(id){return !nodes[id];})) throw new Error('Agent 选中了不存在的节点');
      if(action.type==='create_generation_draft'&&['text','image','video'].indexOf(action.mode)<0) throw new Error('Agent 生成草稿类型无效');
      if(['create_text_node','update_text_node','create_generation_draft','connect_nodes','select_nodes'].indexOf(action.type)<0) throw new Error('Agent 返回了不允许的操作');
    });
    return true;
  }
  function actionLabel(action){
    if(action.type==='create_text_node') return '新增文本：'+(action.title||'未命名');
    if(action.type==='update_text_node') return '修改文本节点：'+action.node_id;
    if(action.type==='create_generation_draft') return '创建'+({text:'文本',image:'图片',video:'视频'}[action.mode]||'生成')+'草稿：'+(action.title||'未命名');
    if(action.type==='connect_nodes') return '连线：'+action.from_node_id+' → '+action.to_node_id;
    if(action.type==='select_nodes') return '选中 '+(action.node_ids||[]).length+' 个节点';
    return '未知操作';
  }
  function connectionPorts(sourceType,targetType){
    if((sourceType==='text'||sourceType==='reverse')&&(targetType==='gen'||targetType==='video')) return {from:'prompt',to:'prompt'};
    if((sourceType==='image'||sourceType==='gen')&&(targetType==='reverse'||targetType==='gen'||targetType==='video')) return {from:'image',to:'image'};
    return null;
  }
  return {createSnapshot:createSnapshot,validatePlan:validatePlan,actionLabel:actionLabel,connectionPorts:connectionPorts,digest:digest};
});
