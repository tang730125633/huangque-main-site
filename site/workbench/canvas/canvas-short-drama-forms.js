(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaForms=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function clone(value){
    if(Array.isArray(value)) return value.map(clone);
    if(value&&typeof value==='object'){
      var result={};Object.keys(value).forEach(function(key){ result[key]=clone(value[key]); });
      return result;
    }
    return value;
  }
  function equal(left,right){ return JSON.stringify(left)===JSON.stringify(right); }
  function createDraft(values,revision,versionId){
    var base=clone(values||{}),current=clone(values||{});
    return {
      get:function(){ return clone(current); },
      set:function(path,value){
        var cursor=current,parts=String(path||'').split('.');
        parts.forEach(function(part,index){
          if(index===parts.length-1) cursor[part]=value;
          else{
            cursor[part]=cursor[part]&&typeof cursor[part]==='object'?
              cursor[part]:{};
            cursor=cursor[part];
          }
        });
      },
      dirty:function(){ return !equal(base,current); },
      reset:function(nextValues,nextRevision,nextVersionId){
        base=clone(nextValues||{});current=clone(nextValues||{});
        revision=nextRevision;versionId=nextVersionId;
      },
      meta:function(){ return {revision:revision,versionId:versionId}; },
      validate:function(){
        var errors={};
        var volume=Number(current&&current.bgm&&current.bgm.volume);
        if(!isFinite(volume)||volume<0||volume>1){
          errors['bgm.volume']='背景音乐音量必须在 0 到 1 之间';
        }
        return errors;
      }
    };
  }
  return {createDraft:createDraft};
});
