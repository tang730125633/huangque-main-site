(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.api=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function apiError(message,options){
    options=options||{}; var error=new Error(message||'request failed');
    error.status=options.status||0; error.code=options.code||'request_failed'; error.data=options.data||null;
    return error;
  }
  function createClient(options){
    options=options||{};
    var fetchImpl=options.fetchImpl, tokenProvider=options.tokenProvider||function(){return '';};
    var Controller=options.AbortControllerImpl, later=options.setTimeoutImpl||setTimeout, cancel=options.clearTimeoutImpl||clearTimeout;
    function request(path,requestOptions,wantBlob,publicAsset){
      requestOptions=requestOptions||{};
      var headers=publicAsset?Object.assign({},requestOptions.headers||{}):Object.assign({'Accept':'application/json','Authorization':'Bearer '+tokenProvider()},requestOptions.headers||{});
      var body=requestOptions.body, callerSignal=requestOptions.signal, controller=!callerSignal&&Controller?new Controller():null, timer=null;
      if(body!==undefined&&!wantBlob){ headers['Content-Type']='application/json'; body=JSON.stringify(body); }
      if(controller) timer=later(function(){ controller.abort(); },requestOptions.timeout||8000);
      return fetchImpl(path,{method:requestOptions.method||'GET',credentials:publicAsset?'include':'same-origin',cache:'no-store',headers:headers,body:body,signal:requestOptions.signal||(controller&&controller.signal)})
        .then(function(response){
          if(wantBlob){ if(!response.ok) throw apiError('HTTP '+response.status,{status:response.status}); return response.blob(); }
          return response.text().then(function(text){
            var data={}; try{data=text?JSON.parse(text):{};}catch(e){data={detail:text||response.statusText};}
            if(!response.ok) throw apiError(data.detail||('HTTP '+response.status),{status:response.status,code:data.code,data:data});
            return data;
          });
        }).catch(function(error){
          if(error&&error.name==='AbortError') throw apiError('request aborted',{code:callerSignal?'aborted':'timeout'});
          throw error;
        }).finally(function(){ if(timer) cancel(timer); });
    }
    return {json:function(path,opts){return request(path,opts,false,false);},asset:function(path,opts){return request(path,opts,true,String(path||'').indexOf('/api/gen/file/')!==0);}};
  }
  function poll(options){
    options=options||{};
    var request=options.request, inspect=options.inspect||function(){return {pending:true};};
    var now=options.now||Date.now, repeat=options.setIntervalImpl||setInterval, stop=options.clearIntervalImpl||clearInterval;
    var maxMs=options.maxMs==null?0:options.maxMs, intervalMs=options.intervalMs||3000, started=now(), timer=null, settled=false;
    return new Promise(function(resolve,reject){
      function finish(callback,value){
        if(settled) return;
        settled=true;
        if(timer!=null) stop(timer);
        callback(value);
      }
      function timedOut(){ return now()-started>maxMs; }
      function rejectTimeout(){
        var error=options.timeoutError?options.timeoutError():apiError('request timed out',{code:'timeout'});
        finish(reject,error);
      }
      function tick(){
        Promise.resolve().then(request).then(function(value){
          if(settled) return;
          var elapsedMs=now()-started, outcome;
          try{ outcome=inspect(value,Math.round(elapsedMs/1000))||{pending:true}; }
          catch(error){ finish(reject,error); return; }
          if(outcome.done){ finish(resolve,outcome.value); return; }
          if(outcome.error){ finish(reject,outcome.error); return; }
          if(timedOut()){ rejectTimeout(); return; }
          if(options.onProgress) options.onProgress(value,Math.round(elapsedMs/1000));
        },function(){
          if(!settled&&timedOut()) rejectTimeout();
        });
      }
      timer=repeat(tick,intervalMs);
    });
  }
  function quotePaidSubmission(options){
    options=options||{};
    var client=options.client;
    if(!client||typeof client.json!=='function'||!options.quotePath||!options.submitPath){
      return Promise.reject(new Error('quoted submission requires client and routes'));
    }
    var payload=options.payload||{};
    return Promise.resolve(client.json(options.quotePath,{method:'POST',body:payload})).then(function(quote){
      var cost=quote&&quote.cost;
      if(typeof cost!=='number'||!isFinite(cost)||Math.floor(cost)!==cost||cost<0){
        throw new Error('服务端报价无效，请稍后重试');
      }
      if(typeof options.onQuote==='function') options.onQuote(cost,quote);
      var accepted=typeof options.confirm==='function'?options.confirm(cost,quote):false;
      return Promise.resolve(accepted).then(function(ok){
        if(!ok) return null;
        if(typeof options.submit==='function') return options.submit(cost,quote);
        return client.json(options.submitPath,{method:'POST',body:payload});
      });
    });
  }
  return {createClient:createClient,apiError:apiError,poll:poll,quotePaidSubmission:quotePaidSubmission};
});
