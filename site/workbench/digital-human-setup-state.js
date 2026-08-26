(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanSetupState=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function view(phase,current){
    var locked=phase==='approved';
    var canAttachPhoto=locked&&current&&!gesturesComplete(current);
    var canAttachVoice=locked&&current&&current.voiceMode==='clone';
    return {locked:locked,photoDisabled:locked&&!canAttachPhoto,voiceSourceDisabled:locked,voiceSampleDisabled:locked&&!canAttachVoice,restartHidden:!locked};
  }
  function applyControls(nodes,phase,current){
    if(!nodes||!nodes.voiceSource||!nodes.voiceSample||!nodes.restart)throw new Error('setup controls are required');
    var next=view(phase,current);
    if(nodes.photo)nodes.photo.disabled=next.photoDisabled;
    nodes.voiceSource.disabled=next.voiceSourceDisabled;
    nodes.voiceSample.disabled=next.voiceSampleDisabled;
    nodes.restart.hidden=next.restartHidden;
    return next;
  }
  function gesturesComplete(current){
    var jobs=current&&current.jobs&&Array.isArray(current.jobs.gesture)?current.jobs.gesture:[];
    var failed=current&&current.failed&&Array.isArray(current.failed.gesture)?current.failed.gesture:[];
    var count=Number(current&&(current.segmentCount||(current.plan&&current.plan.segment_count))||3);
    if(count<1||count>3)count=3;
    if(jobs.length<count)return false;
    for(var i=0;i<count;i++){
      if(!Number.isFinite(Number(jobs[i]))||Number(jobs[i])<=0||failed[i]===true)return false;
    }
    return true;
  }
  function resolvePhotoRecovery(current,hasLocalPhoto){
    var locked=!!(current&&current.phase==='approved');
    var completed=gesturesComplete(current);
    if(locked&&!completed&&!hasLocalPhoto)return {
      locked:true,valid:false,requiresRestart:false,requiresAttachment:true,gesturesComplete:false,
      error:'请重新附加本次授权使用的原人物照片后继续；如需更换人物，请先放弃上次任务并重新设置',
    };
    return {locked:locked,valid:true,requiresRestart:false,requiresAttachment:false,gesturesComplete:completed,error:''};
  }
  function validatePhotoAttachment(current,digest){
    if(!current||current.phase!=='approved')return {accepted:true,error:''};
    var expected=String(current.photoSha256||'').trim().toLowerCase();
    var actual=String(digest||'').trim().toLowerCase();
    if(!expected)return {accepted:false,error:'原人物照片校验记录缺失，请放弃上次任务并重新设置'};
    if(actual!==expected)return {accepted:false,error:'所选照片与本次授权的原人物照片不一致；如需更换人物，请放弃上次任务并重新设置'};
    return {accepted:true,error:''};
  }
  function restart(current,confirmed){
    var state=Object.assign({},current||{});
    if(state.phase!=='approved'||!confirmed)return {changed:false,state:state,submit:false,charge:false,cancelRun:false};
    return {changed:true,state:{phase:'input'},submit:false,charge:false,cancelRun:true};
  }
  function canContinue(runEpoch,currentEpoch){return Number(runEpoch)===Number(currentEpoch);}
  function cancelled(){var error=new Error('本次生成已被用户放弃');error.generationCancelled=true;return error;}
  function runJobs(options){
    var items=options.items||[],ids=(options.ids||[]).slice(),keys=(options.keys||[]).slice(),failed=(options.failed||[]).slice(),results=[];
    var requestedLimit=Number(options.maxConcurrency),limit=Math.max(1,Math.min(items.length||1,Number.isFinite(requestedLimit)&&requestedLimit>0?Math.floor(requestedLimit):items.length||1)),nextIndex=0;
    function guard(){if(!canContinue(options.epoch,options.currentEpoch()))throw cancelled();}
    function commit(){guard();options.commit({ids:ids.slice(),keys:keys.slice(),failed:failed.slice()});}
    function launch(item,index,retry){
      guard();
      if(retry||!keys[index])keys[index]=options.key(index);
      failed[index]=false;ids[index]=0;commit();
      return options.submit(item,index,keys[index]).then(function(id){
        guard();ids[index]=id;commit();return options.poll(id);
      });
    }
    function runOne(item,index){
      var active=Number(ids[index])||0;
      return options.resume({
        jobId:active,
        retryApproved:!!failed[index],
        poll:options.poll,
        launch:function(retry){return launch(item,index,retry);},
        markTerminal:function(){guard();failed[index]=true;commit();}
      }).then(function(result){
        guard();results[index]=result;
        if(options.onCount)options.onCount(results.filter(Boolean).length,items.length);
      });
    }
    function worker(){
      guard();
      if(nextIndex>=items.length)return Promise.resolve();
      var index=nextIndex++;
      return runOne(items[index],index).then(worker);
    }
    var workers=[];
    for(var i=0;i<limit;i++)workers.push(worker());
    return Promise.all(workers).then(function(){guard();commit();return {ids:ids,results:results};}).catch(function(error){guard();throw error;});
  }
  return {view:view,applyControls:applyControls,gesturesComplete:gesturesComplete,resolvePhotoRecovery:resolvePhotoRecovery,validatePhotoAttachment:validatePhotoAttachment,restart:restart,canContinue:canContinue,runJobs:runJobs};
});
