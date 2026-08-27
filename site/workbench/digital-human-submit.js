(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanSubmit=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var retryableCodes={
    content_security_unavailable:true,
    content_security_text_unavailable:true,
    content_security_image_unavailable:true,
    content_security_token_unavailable:true,
  };
  function isSecurityRetryable(error){
    return !!(error&&Number(error.status)===503&&retryableCodes[String(error.code||'')]);
  }
  function wait(ms){return new Promise(function(resolve){setTimeout(resolve,ms);});}
  function withSecurityRetry(operation,options){
    options=options||{};
    var delays=Array.isArray(options.delays)?options.delays:[1200,3000];
    var attempt=0;
    function run(){
      return Promise.resolve().then(operation).catch(function(error){
        if(!isSecurityRetryable(error)||attempt>=delays.length)throw error;
        var delay=Math.max(0,Number(delays[attempt])||0);attempt++;
        if(options.onRetry)options.onRetry(attempt,delay,error);
        return wait(delay).then(run);
      });
    }
    return run();
  }
  function isCapacityRetryable(error){
    var message=String(error&&error.message||'');
    return !!(error&&Number(error.status)===429&&String(error.hqCode||'')==='HQ-RATE-001'&&(/任务正在排队|完成后再提交/.test(message)));
  }
  function withCapacityRetry(operation,options){
    options=options||{};
    var delays=Array.isArray(options.delays)?options.delays:[5000,10000,20000,30000];
    var attempt=0;
    function run(){
      return Promise.resolve().then(operation).catch(function(error){
        if(!isCapacityRetryable(error)||attempt>=delays.length)throw error;
        var delay=Math.max(0,Number(delays[attempt])||0);attempt++;
        if(options.onRetry)options.onRetry(attempt,delay,error);
        return wait(delay).then(run);
      });
    }
    return run();
  }
  function describe(error){
    var message=String(error&&error.message||error||'生成失败');
    if(isSecurityRetryable(error)&&message.indexOf('未扣点')<0){
      message+='；尚未创建任务、未扣点';
    }
    var requestId=String(error&&error.requestId||'').trim();
    if(requestId)message+='（请求编号：'+requestId+'）';
    return message;
  }
  return {isSecurityRetryable:isSecurityRetryable,withSecurityRetry:withSecurityRetry,isCapacityRetryable:isCapacityRetryable,withCapacityRetry:withCapacityRetry,describe:describe};
});
