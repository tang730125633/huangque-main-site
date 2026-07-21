(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.storage=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var DEFAULT_KEYS={
    draft:'hq_canvas_draft_v2', templates:'hq_canvas_templates_v2',
    boards:'hq_canvas_boards_v1', activeBoard:'hq_canvas_active_id'
  };
  function result(value){ return {ok:true,value:value}; }
  function failure(code,error){ return {ok:false,error:{code:code,message:String(error&&error.message||error||code)}}; }
  function quota(error){ return error&&(error.name==='QuotaExceededError'||error.code===22||error.code===1014); }
  function stripHeavyOutputs(snapshot){
    var copy=snapshot==null?snapshot:JSON.parse(JSON.stringify(snapshot));
    (copy&&copy.nodes||[]).forEach(function(node){
      if(!node||!node.outputs) return;
      if(node.type==='gen') delete node.outputs.image;
      if(node.type==='video'){ delete node.outputs.video; delete node.outputs.video_url; }
    });
    return copy;
  }
  function createStorage(options){
    options=options||{}; var storage=options.storage, keys=Object.assign({},DEFAULT_KEYS,options.keys||{});
    function current(){ return typeof storage==='function'?storage():storage; }
    function read(key,fallback){
      var raw; try{ raw=current().getItem(key); }catch(error){ return failure('storage_unavailable',error); }
      if(raw==null||raw==='') return result(fallback);
      try{ return result(JSON.parse(raw)); }catch(error){ return failure('corrupt_json',error); }
    }
    function write(key,value){
      var raw;
      try{ raw=JSON.stringify(value); }
      catch(error){ return failure('serialization_failed',error); }
      try{ current().setItem(key,raw); return result(value); }
      catch(error){ return failure(quota(error)?'quota_exceeded':'storage_unavailable',error); }
    }
    function remove(key){ try{ current().removeItem(key); return result(null); }catch(error){ return failure('storage_unavailable',error); } }
    return {
      loadDraft:function(){return read(keys.draft,null);}, saveDraft:function(v){return write(keys.draft,v);}, removeDraft:function(){return remove(keys.draft);},
      loadBoards:function(){return read(keys.boards,[]);}, saveBoards:function(v){return write(keys.boards,v);},
      loadTemplates:function(){return read(keys.templates,[]);}, saveTemplates:function(v){return write(keys.templates,v);},
      loadActiveBoard:function(){ try{return result(current().getItem(keys.activeBoard)||'');}catch(e){return failure('storage_unavailable',e);} },
      saveActiveBoard:function(v){ try{ if(v) current().setItem(keys.activeBoard,String(v)); else current().removeItem(keys.activeBoard); return result(v||''); }catch(e){return failure(quota(e)?'quota_exceeded':'storage_unavailable',e);} }
    };
  }
  return {DEFAULT_KEYS:DEFAULT_KEYS,createStorage:createStorage,stripHeavyOutputs:stripHeavyOutputs};
});
