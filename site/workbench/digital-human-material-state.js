(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanMaterialState=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function normalize(items,now){
    now=Number(now)||Math.floor(Date.now()/1000);
    return (Array.isArray(items)?items:[]).filter(function(item){
      return item&&/^img_[a-f0-9]{32}$/.test(String(item.upload_id||''))&&
        Number(item.expires_at)>now&&typeof item.name==='string';
    }).slice(0,6).map(function(item){return {
      upload_id:item.upload_id,name:item.name,sha256:String(item.sha256||''),
      expires_at:Number(item.expires_at),
    };});
  }
  function restore(items,phase,now){
    var saved=Array.isArray(items)?items:[];
    var normalized=normalize(saved,now);
    var approved=phase==='approved';
    var malformedFrozenMaterial=approved&&items!=null&&!Array.isArray(items);
    var lostFrozenMaterial=approved&&(malformedFrozenMaterial||normalized.length!==saved.length);
    return {
      items:normalized,
      valid:!lostFrozenMaterial,
      error:lostFrozenMaterial?'已确认的客户素材已过期或无效，不能自动改为 AI 补图；请放弃旧任务并重新设置':'',
    };
  }
  function canChange(phase,busy){return !busy&&phase!=='approved';}
  function canAnalyze(phase,busy){return canChange(phase,busy);}
  function canStart(phase,busy,hasPlan){return !busy&&!!hasPlan;}
  function restoreStartButton(button,phase){
    if(!button)throw new Error('start button is required');
    button.disabled=false;
    button.textContent=phase==='approved'?'继续上次未完成的生成':'确认方案并生成';
    return button;
  }
  return {normalize:normalize,restore:restore,canChange:canChange,canAnalyze:canAnalyze,canStart:canStart,restoreStartButton:restoreStartButton};
});
