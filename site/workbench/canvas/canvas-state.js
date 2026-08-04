(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.state=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function cloneSnapshot(value){ return value==null?value:JSON.parse(JSON.stringify(value)); }
  function sanitizeNodeData(node,sanitizers){
    var copy=cloneSnapshot(node||{}), sanitizer=sanitizers&&sanitizers[copy.type];
    return typeof sanitizer==='function'?sanitizer(copy):copy;
  }
  function createHistory(options){
    options=options||{};
    var limit=Math.max(1,Number(options.limit)||60), undoStack=[], redoStack=[];
    function cap(list){ while(list.length>limit) list.shift(); }
    return {
      push:function(snapshot){ if(snapshot==null) return; undoStack.push(cloneSnapshot(snapshot)); cap(undoStack); redoStack=[]; },
      undo:function(current){ if(!undoStack.length) return null; if(current!=null){ redoStack.push(cloneSnapshot(current)); cap(redoStack); } return cloneSnapshot(undoStack.pop()); },
      redo:function(current){ if(!redoStack.length) return null; if(current!=null){ undoStack.push(cloneSnapshot(current)); cap(undoStack); } return cloneSnapshot(redoStack.pop()); },
      clear:function(){ undoStack=[]; redoStack=[]; },
      canUndo:function(){ return undoStack.length>0; },
      canRedo:function(){ return redoStack.length>0; }
    };
  }
  return {cloneSnapshot:cloneSnapshot,sanitizeNodeData:sanitizeNodeData,createHistory:createHistory};
});
