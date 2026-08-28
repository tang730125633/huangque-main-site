(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanVoiceState=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function keyForSlot(slot){return 'vip_'+String(slot&&slot.slot_id||'').replace(/[^a-zA-Z0-9_-]/g,'_');}
  function readyVoices(items){
    return (Array.isArray(items)?items:[]).filter(function(item){
      return item&&item.slot_id&&item.status==='ready'&&item.voice_id;
    }).map(function(item,index){return {
      key:keyForSlot(item),name:item.voice_name||('我的声音 '+(index+1)),preview_url:item.preview_url||'',
    };});
  }
  function snapshot(current){return {
    phase:String(current&&current.phase||'input'),
    voiceMode:String(current&&current.voiceMode||((current&&current.voiceKey)?'existing':'')),
    voiceKey:String(current&&current.voiceKey||''),
  };}
  function resolveLoaded(current,items){
    var frozen=snapshot(current),voices=readyVoices(items),locked=frozen.phase==='approved';
    if(locked){
      if(frozen.voiceMode==='existing'&&frozen.voiceKey){
        var found=voices.some(function(item){return item.key===frozen.voiceKey;});
        return {selection:frozen,voices:voices,locked:true,valid:found,error:found?'':'已冻结的个人声音当前不可用，请恢复该声音后再继续'};
      }
      if(frozen.voiceMode==='clone')return {selection:frozen,voices:voices,locked:true,valid:false,error:'重新复刻样音未完成，刷新后无法恢复本地样音文件，请重新开始本次制作'};
      return {selection:frozen,voices:voices,locked:true,valid:false,error:'已批准任务缺少冻结的声音参数，无法继续'};
    }
    if(frozen.voiceMode==='existing'&&frozen.voiceKey&&voices.some(function(item){return item.key===frozen.voiceKey;})){
      return {selection:frozen,voices:voices,locked:false,valid:true,error:''};
    }
    var next={phase:frozen.phase,voiceMode:voices.length?'existing':'clone',voiceKey:voices.length?voices[0].key:''};
    return {selection:next,voices:voices,locked:false,valid:true,error:''};
  }
  function loadFailed(current,message){
    var frozen=snapshot(current);
    return {selection:frozen,voices:[],locked:frozen.phase==='approved',valid:false,error:'读取资产库声音失败，声音选择未改变：'+String(message||'请稍后重试')};
  }
  function change(current,value){
    var frozen=snapshot(current);
    if(frozen.phase==='approved')return {accepted:false,selection:frozen,error:'生成已开始，声音选择已冻结'};
    var clone=value==='__clone__';
    return {accepted:true,selection:{phase:frozen.phase,voiceMode:clone?'clone':'existing',voiceKey:clone?'':String(value||'')},error:''};
  }
  function cloneStatusPayload(response){
    if(!response||typeof response!=='object'||Array.isArray(response))return {};
    if(response.result&&typeof response.result==='object'&&!Array.isArray(response.result))return response.result;
    return response;
  }
  function normalizeCloneStatus(response){
    var raw=cloneStatusPayload(response);
    return {
      status:String(raw.status||'').trim().toLowerCase(),
      clone_error:String(raw.clone_error||'').trim(),
      preview_url:String(raw.preview_url||'').trim(),
      attempt_id:String(raw.attempt_id||'').trim(),
      raw:raw,
    };
  }
  function cloneRetryDecision(response,hasSample){
    var normalized=normalizeCloneStatus(response),status=normalized.status;
    var result={status:status,action:'blocked',error:'',clone_error:normalized.clone_error,preview_url:normalized.preview_url};
    if(status==='ready'){
      result.action='reuse';
      return result;
    }
    if(status==='training'){
      result.action='poll';
      return result;
    }
    if(status==='active'||status==='failed'){
      if(hasSample){
        result.action='submit';
        return result;
      }
      result.error=normalized.clone_error||'\u9700\u8981\u91cd\u65b0\u4e0a\u4f20\u58f0\u97f3\u6837\u672c\u540e\u624d\u80fd\u590d\u523b';
      return result;
    }
    result.error='\u58f0\u97f3\u72b6\u6001\u54cd\u5e94\u5f02\u5e38'+(status?'\uff1a'+status:'\uff1a\u7f3a\u5c11 status');
    return result;
  }
  function restoredCloneDecision(response,markers){
    var normalized=normalizeCloneStatus(response),status=normalized.status;
    markers=markers||{};
    var accepted=!!markers.accepted;
    var expectedAttemptId=String(markers.attemptId||'').trim();
    if(expectedAttemptId&&normalized.attempt_id!==expectedAttemptId){
      return {action:'blocked',status:status,error:'声音复刻操作标识不匹配，请重新附加本次授权的原声音样本后重试'};
    }
    if(status==='ready'){
      return accepted
        ? {action:'reuse',status:status,error:''}
        : {action:'reattach',status:status,error:'上次样音提交结果无法确认，请重新附加本次授权的原声音样本后重试'};
    }
    if(status==='training'){
      return accepted
        ? {action:'poll',status:status,error:''}
        : {action:'reattach',status:status,error:'上次样音提交结果无法确认，请重新附加本次授权的原声音样本后重试'};
    }
    if(status==='failed'||status==='active')return {action:'reattach',status:status,error:normalized.clone_error||'请重新附加本次授权的原声音样本后重试'};
    return {action:'blocked',status:status,error:'声音状态响应异常'+(status?'：'+status:'：缺少 status')};
  }
  function cloneSubmitErrorDecision(error){
    if(error&&Number(error.status)===409&&String(error.code||'')==='idempotency_in_progress'){
      return {action:'retry',accepted:false};
    }
    return {action:'fail',accepted:false};
  }
  function failedAttemptTransition(response,expectedAttemptId,nextKey){
    var normalized=normalizeCloneStatus(response);
    expectedAttemptId=String(expectedAttemptId||'').trim();
    nextKey=String(nextKey||'').trim();
    var rotate=normalized.status==='failed'&&!!expectedAttemptId&&
      normalized.attempt_id===expectedAttemptId&&!!nextKey&&nextKey!==expectedAttemptId;
    return {rotate:rotate,key:rotate?nextKey:expectedAttemptId,
      submitted:rotate?false:null,accepted:rotate?false:null,progress:rotate?false:null,
      error:normalized.clone_error};
  }
  function submitCloneWithIdempotency(options){
    options=options||{};
    if(typeof options.submit!=='function')return Promise.reject(terminalError('声音复刻提交器配置不完整'));
    var maxAttempts=Math.max(1,Number(options.maxAttempts)||8),attempts=0;
    var delay=Math.max(0,Number(options.retryDelay)==Number(options.retryDelay)?Number(options.retryDelay):1000);
    var wait=typeof options.wait==='function'?options.wait:function(ms){return new Promise(function(resolve){setTimeout(resolve,ms);});};
    function attempt(){
      attempts++;
      return Promise.resolve().then(options.submit).catch(function(error){
        if(error&&error.generationCancelled)throw error;
        var decision=cloneSubmitErrorDecision(error);
        if(decision.action!=='retry'||attempts>=maxAttempts)throw error;
        if(typeof options.onRetry==='function')options.onRetry(attempts,error);
        return wait(delay).then(attempt);
      });
    }
    return attempt();
  }
  function terminalError(message){
    var error=new Error(String(message||'\u58f0\u97f3\u590d\u523b\u5931\u8d25'));
    error.cloneTerminal=true;
    return error;
  }
  function runCloneRecovery(options){
    options=options||{};
    if(typeof options.getStatus!=='function'||typeof options.submit!=='function'){
      return Promise.reject(terminalError('\u58f0\u97f3\u6062\u590d\u5668\u914d\u7f6e\u4e0d\u5b8c\u6574'));
    }
    var hasSample=!!options.hasSample,allowSubmit=!!options.allowSubmit,forceSubmit=!!options.forceSubmit;
    var expectedAttemptId=String(options.expectedAttemptId||'').trim();
    var requireProgressBeforeReady=!!options.requireProgressBeforeReady;
    var maxPolls=Math.max(1,Number(options.maxPolls)||180),maxErrors=Math.max(1,Number(options.maxErrors)||8);
    var wait=typeof options.wait==='function'?options.wait:function(ms){return new Promise(function(resolve){setTimeout(resolve,ms);});};
    var delay=Math.max(0,Number(options.pollDelay)==Number(options.pollDelay)?Number(options.pollDelay):3000);
    var polls=0,errors=0,submitted=!!options.initialSubmitted,submittedThisRun=false,progressObserved=!!options.initialProgress;
    function notify(status,detail){if(typeof options.onStatus==='function')options.onStatus(status,detail||{});}
    function later(next){return wait(delay).then(next);}
    function query(){
      return Promise.resolve().then(options.getStatus).then(function(response){
        errors=0;polls++;
        var normalized=normalizeCloneStatus(response),status=normalized.status;
        if(expectedAttemptId&&normalized.attempt_id!==expectedAttemptId){
          throw terminalError('声音复刻操作标识不匹配，请重新附加本次授权的原声音样本后重试');
        }
        notify(status||'invalid',{polls:polls,submitted:submitted});
        if(status==='ready'){
          if(requireProgressBeforeReady&&!progressObserved&&!submittedThisRun){
            throw terminalError('\u4e0a\u6b21\u6837\u97f3\u63d0\u4ea4\u7ed3\u679c\u65e0\u6cd5\u786e\u8ba4\uff0c\u8bf7\u91cd\u65b0\u9644\u52a0\u672c\u6b21\u6388\u6743\u7684\u539f\u58f0\u97f3\u6837\u672c\u540e\u91cd\u8bd5');
          }
          return {status:'ready',submitted:submitted,preview_url:normalized.preview_url};
        }
        if(status==='failed'){
          if(!expectedAttemptId&&!submittedThisRun&&allowSubmit&&hasSample)return submitOnce();
          var failed=terminalError(normalized.clone_error||'\u58f0\u97f3\u590d\u523b\u5931\u8d25');
          failed.cloneAttemptFailed=!!expectedAttemptId&&normalized.attempt_id===expectedAttemptId;
          failed.attempt_id=normalized.attempt_id;
          throw failed;
        }
        if(status==='active'){
          if(!submitted&&allowSubmit&&hasSample)return submitOnce();
          if(submitted){
            if(polls>=maxPolls)throw terminalError('\u58f0\u97f3\u590d\u523b\u7b49\u5f85\u8d85\u65f6\uff0c\u8bf7\u7a0d\u540e\u4ece\u672c\u6b65\u9aa4\u91cd\u8bd5');
            return later(query);
          }
          throw terminalError(hasSample?'\u58f0\u97f3\u590d\u523b\u5c1a\u672a\u5f00\u59cb':'\u9700\u8981\u91cd\u65b0\u4e0a\u4f20\u539f\u58f0\u97f3\u6837\u672c\u540e\u624d\u80fd\u590d\u523b');
        }
        if(status==='training'){
          progressObserved=true;
          if(polls>=maxPolls)throw terminalError('\u58f0\u97f3\u590d\u523b\u7b49\u5f85\u8d85\u65f6\uff0c\u8bf7\u7a0d\u540e\u4ece\u672c\u6b65\u9aa4\u91cd\u8bd5');
          return later(query);
        }
        throw terminalError('\u58f0\u97f3\u72b6\u6001\u54cd\u5e94\u5f02\u5e38'+(status?'\uff1a'+status:'\uff1a\u7f3a\u5c11 status'));
      }).catch(function(error){
        if(error&&error.generationCancelled)throw error;
        if(error&&error.cloneTerminal)throw error;
        errors++;
        if(errors>=maxErrors)throw error;
        notify('network-retry',{errors:errors,error:error});
        return later(query);
      });
    }
    function submitOnce(){
      if(submittedThisRun)return query();
      if(!hasSample)return Promise.reject(terminalError('\u8bf7\u91cd\u65b0\u4e0a\u4f20\u672c\u6b21\u6388\u6743\u7684\u539f\u58f0\u97f3\u6837\u672c'));
      submitted=true;submittedThisRun=true;notify('submitting',{submitted:true});
      return Promise.resolve().then(options.submit).catch(function(error){
        if(error&&error.generationCancelled)throw error;
        throw error;
      }).then(query);
    }
    return forceSubmit?submitOnce():query();
  }
  return {
    keyForSlot:keyForSlot,
    readyVoices:readyVoices,
    resolveLoaded:resolveLoaded,
    loadFailed:loadFailed,
    change:change,
    normalizeCloneStatus:normalizeCloneStatus,
    cloneRetryDecision:cloneRetryDecision,
    restoredCloneDecision:restoredCloneDecision,
    cloneSubmitErrorDecision:cloneSubmitErrorDecision,
    failedAttemptTransition:failedAttemptTransition,
    submitCloneWithIdempotency:submitCloneWithIdempotency,
    runCloneRecovery:runCloneRecovery,
  };
});
