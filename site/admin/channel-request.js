/* 请求层工具：超时、非 JSON 识别。与业务无关，可独立用模拟 transport 做测试。
   写请求超时只报“结果待核对”，由调用方决定，绝不自动重试。响应排序由调用方用序号自行防护。 */
(function(root){
  'use strict';
  function isNonJsonResponse(v){
    // 与 index.html 的 api() 约定一致：JSON 解析失败时返回 {detail:'响应不是 JSON'}。
    return !!v&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).length===1&&v.detail==='响应不是 JSON';
  }
  function createClient(transport,opts){
    opts=opts||{};
    const readTimeout=Number(opts.readTimeout)||20000;
    const writeTimeout=Number(opts.writeTimeout)||15000;
    function timeout(promise,ms,message){
      let timer;
      const t=new Promise(function(_,reject){timer=setTimeout(function(){reject(Object.assign(new Error(message||'请求超时'),{name:'TimeoutError',timedOut:true}))},ms)});
      return Promise.race([promise,t]).finally(function(){clearTimeout(timer)});
    }
    function send(path,method,body,ms){
      const opt={method:method};
      if(body!==undefined){opt.headers={'Content-Type':'application/json'};opt.body=JSON.stringify(body)}
      const p=Promise.resolve().then(function(){return transport(path,opt)});
      return timeout(p,ms,method==='GET'?'读取超时':'请求超时').then(function(data){
        if(isNonJsonResponse(data))throw Object.assign(new Error('响应不是 JSON'),{name:'NonJsonError',nonJson:true});
        return data;
      });
    }
    return {
      get:function(path){return send(path,'GET',undefined,readTimeout)},
      post:function(path,body){return send(path,'POST',body,writeTimeout)}
    };
  }
  function createLatestGuard(){
    let seq=0;
    return {begin:function(){return ++seq},isLatest:function(n){return n===seq}};
  }
  root.ChannelRequest={createClient:createClient,isNonJsonResponse:isNonJsonResponse,createLatestGuard:createLatestGuard};
})(typeof window==='undefined'?globalThis:window);
