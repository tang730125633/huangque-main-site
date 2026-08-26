(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanOneClickState=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function frozenMaterials(saved){
    var value=saved&&saved.customerUploads;
    return Array.isArray(value)?value.slice():value;
  }
  function persistableMaterials(saved,normalized,valid){
    if(!valid)return frozenMaterials(saved);
    return (Array.isArray(normalized)?normalized:[]).map(function(item){return {
      upload_id:item.upload_id,
      name:item.name,
      sha256:item.sha256,
      expires_at:item.expires_at,
    };});
  }
  function resumeJob(options){
    var marked=false;
    function markTerminal(error){
      if(error&&error.terminalJob&&!marked){
        marked=true;
        options.markTerminal();
      }
      throw error;
    }
    var jobId=Number(options.jobId)||0;
    var operation;
    if(!jobId)operation=function(){return options.launch(false);};
    else if(options.retryApproved)operation=function(){return options.launch(true);};
    else operation=function(){return options.poll(jobId);};
    return Promise.resolve().then(operation).catch(markTerminal);
  }
  function segmentCount(saved){
    var value=Number(saved&&(saved.segmentCount||(saved.plan&&saved.plan.segment_count))||3);
    return value>=1&&value<=3?value:3;
  }
  function expectedCount(saved,bucket){
    var count=segmentCount(saved);
    return bucket==='material'?count*2:count;
  }
  function invalidateGestureRecovery(saved,error){
    return invalidateRecovery(saved,error,'gesture','gesture_recovery_invalid',expectedCount(saved,'gesture'));
  }
  function canRecoverMaterials(saved){
    var count=expectedCount(saved,'material');
    return completeRecoveryJobs(saved,'material',count).length===count;
  }
  function invalidateRecovery(saved,error,bucket,code,count){
    if(!saved||!error||error.code!==code)return false;
    saved.failed=saved.failed||{};
    var jobs=saved.jobs&&Array.isArray(saved.jobs[bucket])?saved.jobs[bucket]:[];
    var failed=saved.failed&&Array.isArray(saved.failed[bucket])?saved.failed[bucket].slice():[];
    while(failed.length<count)failed.push(false);
    var invalid=Array.isArray(error.invalidJobIds)?error.invalidJobIds.map(Number):[];
    if(invalid.length){for(var i=0;i<count;i++)if(invalid.indexOf(Number(jobs[i]))>=0)failed[i]=true;}
    else for(var j=0;j<count;j++)failed[j]=true;
    saved.failed[bucket]=failed;
    return true;
  }
  function invalidateMaterialRecovery(saved,error){return invalidateRecovery(saved,error,'material','material_recovery_invalid',expectedCount(saved,'material'));}
  function completeRecoveryJobs(saved,bucket,count){
    if(!saved||saved.phase!=='approved'||!saved.consent)return [];
    var jobs=saved.jobs&&Array.isArray(saved.jobs[bucket])?saved.jobs[bucket]:[];
    var failed=saved.failed&&Array.isArray(saved.failed[bucket])?saved.failed[bucket]:[];
    if(jobs.length!==count)return [];
    var seen={};
    for(var i=0;i<count;i++){
      var jobId=Number(jobs[i]);
      if(!Number.isFinite(jobId)||jobId<=0||seen[jobId]||failed[i]===true)return [];
      seen[jobId]=true;
    }
    return jobs.map(Number);
  }
  function recoveryJobIds(saved,bucket,count){
    if(!saved||saved.phase!=='approved'||!saved.consent)return [];
    var jobs=saved.jobs&&Array.isArray(saved.jobs[bucket])?saved.jobs[bucket]:[];
    var failed=saved.failed&&Array.isArray(saved.failed[bucket])?saved.failed[bucket]:[];
    var result=[];
    for(var i=0;i<Math.min(count,jobs.length);i++){
      var jobId=Number(jobs[i]);
      if(Number.isFinite(jobId)&&jobId>0&&failed[i]!==true)result.push({index:i,job_id:jobId});
    }
    return result;
  }
  function invalidateVideoRecovery(saved,error){return invalidateRecovery(saved,error,'video','video_recovery_invalid',expectedCount(saved,'video'));}
  function retryLabel(error,step){
    var invalid=error&&Array.isArray(error.invalidJobIds)?error.invalidJobIds:[];
    if(error&&error.code==='video_recovery_invalid'&&invalid.length){
      return '重新生成失败的 '+invalid.length+' 段口播';
    }
    return step==='gestures'?'重试手势照':'从失败步骤再试一次';
  }
  function jobFailureDecision(job){
    job=job||{};
    var charged=Number(job.cost||0)>0;
    var refunded=job.refund_state==='refunded'||job.refunded===true;
    if(charged&&!refunded)return {action:'poll',refundConfirmed:false};
    return {action:'terminal',refundConfirmed:refunded||!charged};
  }
  return {
    frozenMaterials:frozenMaterials,
    persistableMaterials:persistableMaterials,
    segmentCount:segmentCount,
    expectedCount:expectedCount,
    resumeJob:resumeJob,
    invalidateGestureRecovery:invalidateGestureRecovery,
    canRecoverMaterials:canRecoverMaterials,
    invalidateMaterialRecovery:invalidateMaterialRecovery,
    completeRecoveryJobs:completeRecoveryJobs,
    recoveryJobIds:recoveryJobIds,
    invalidateVideoRecovery:invalidateVideoRecovery,
    retryLabel:retryLabel,
    jobFailureDecision:jobFailureDecision,
  };
});
