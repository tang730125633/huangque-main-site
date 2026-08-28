(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanUnifiedState=api;
})(typeof self!=='undefined'?self:this,function(){
  'use strict';
  function cleanName(slot){return String(slot&&slot.voice_name||'未命名音色').trim()||'未命名音色';}
  function selectCloneSlot(items,chooseReady,confirmOverwrite){
    var slots=(items||[]).filter(function(item){return item&&item.slot_id&&['active','failed','ready'].indexOf(item.status)>=0;});
    var empty=slots.filter(function(item){return item.status==='active'||item.status==='failed';});
    if(empty.length)return {slot:empty[0],overwrite_confirmed:false,overwrite_voice_name:''};
    var ready=slots.filter(function(item){return item.status==='ready';});
    if(!ready.length){
      if((items||[]).some(function(item){return item&&item.status==='training';}))throw new Error('当前音色槽位正在复刻其他音色，请等待完成后重试');
      throw new Error('当前账号没有音色槽位，请先在资产库购买一个音色槽位；本页面不会自动购买或扣除槽位点数');
    }
    var selectedId=String(chooseReady(ready)||'').trim();
    var selected=ready.find(function(item){return String(item.slot_id)===selectedId;});
    if(!selected)throw new Error('未选择要覆盖的已有音色，未提交复刻');
    var name=cleanName(selected);
    if(confirmOverwrite(selected,name)!==true)throw new Error('已取消覆盖已有音色，未提交复刻');
    return {slot:selected,overwrite_confirmed:true,overwrite_voice_name:name};
  }
  function consentMetadata(state,text,stage){
    var consent=state&&state.consent||{},sample=state&&state.sample||{},slot=state&&state.slot||{};
    return {
      digital_human_pipeline:'digital_human_video_voice',digital_human_stage:stage,
      digital_human_run_id:consent.run_id,digital_human_consent_token:consent.consent_token,
      digital_human_script:String(text||'').trim(),digital_human_video_asset_id:String(sample.video_asset_id||''),
      digital_human_video_sha256:sample.video_sha256,digital_human_sample_sha256:sample.sha256,
      digital_human_slot_id:slot.slot_id,clone_attempt_id:consent.clone_attempt_id
    };
  }
  function audioKeyPayload(state,text){
    return {text:String(text||'').trim(),voice:state.voiceKey,clone_attempt_id:state.consent.clone_attempt_id,sample_sha256:state.sample.sha256};
  }
  return {selectCloneSlot:selectCloneSlot,consentMetadata:consentMetadata,audioKeyPayload:audioKeyPayload};
});
