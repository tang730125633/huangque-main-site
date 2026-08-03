(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaD5Api=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var ASSEMBLY='/api/gen/short-drama/assembly';
  var COMPLETION='/api/gen/short-drama/completion';
  var SOUND_DESIGN='/api/gen/short-drama/sound-design';
  var PLAYBACK='/api/gen/short-drama/playback';

  function text(value){ return String(value==null?'':value); }
  function normalizeError(error){
    var result=error instanceof Error?error:new Error(text(error)||'请求失败');
    result.status=Number(error&&error.status)||0;
    result.code=text(error&&error.code||error&&error.body&&error.body.code);
    result.detail=text(
      error&&error.detail||
      error&&error.body&&error.body.detail||
      error&&error.message||
      error
    );
    if(!result.message) result.message=result.detail||'请求失败';
    return result;
  }
  function key(prefix){
    return text(prefix||'d5')+'-'+Date.now().toString(36)+'-'+
      Math.random().toString(36).slice(2,12);
  }
  function createApi(options){
    options=options||{};
    var client=options.client;
    var boardId=text(options.boardId);
    var destroyed=false,generation=0;
    if(!client||typeof client.json!=='function'){
      throw new Error('D-5 工作区缺少已认证 API 客户端');
    }
    function scoped(path,requestOptions){
      if(destroyed) return Promise.reject(new Error('workspace destroyed'));
      var request=requestOptions?Object.assign({},requestOptions):{};
      request.headers=Object.assign({},request.headers||{});
      if(boardId) request.headers['X-Canvas-Board-Id']=boardId;
      if(!Object.keys(request.headers).length) delete request.headers;
      return Promise.resolve(client.json(path,request)).catch(function(error){
        throw normalizeError(error);
      });
    }
    function load(projectId){
      var current=++generation;
      var query='?project_id='+encodeURIComponent(projectId);
      return Promise.all([scoped(ASSEMBLY+query),scoped(PLAYBACK+query)])
        .then(function(results){
          results[0].playback=results[1]===results[0]?null:results[1];
          return {generation:current,result:results[0]};
        });
    }
    function remux(body,idempotencyKey){
      return scoped(PLAYBACK+'/remux',{
        method:'POST',
        headers:{'Idempotency-Key':idempotencyKey},
        body:body
      });
    }
    function playbackJob(projectId,jobId){
      return scoped(
        PLAYBACK+'/jobs/'+encodeURIComponent(jobId)+
        '?project_id='+encodeURIComponent(projectId)
      );
    }
    function selectPlayback(projectId,versionId){
      return scoped(
        PLAYBACK+'/versions/'+encodeURIComponent(versionId)+'/select',
        {method:'PUT',body:{project_id:projectId}}
      );
    }
    function preview(body,idempotencyKey){
      return scoped(ASSEMBLY+'/preview',{
        method:'POST',
        headers:{'Idempotency-Key':idempotencyKey},
        body:body
      });
    }
    function saveConfig(body){
      return scoped(ASSEMBLY+'/config',{method:'PUT',body:body});
    }
    function audioAssets(projectId){
      return scoped(
        ASSEMBLY+'/audio-assets?project_id='+
        encodeURIComponent(text(projectId))+'&limit=120'
      );
    }
    function soundDesign(projectId){
      return scoped(SOUND_DESIGN+'?project_id='+encodeURIComponent(projectId));
    }
    function analyzeSoundDesign(body){
      return scoped(SOUND_DESIGN+'/analyze',{method:'POST',body:body});
    }
    function saveSoundSuggestions(body){
      return scoped(SOUND_DESIGN+'/suggestions',{method:'PUT',body:body});
    }
    function quoteSoundEffects(body){
      return scoped(SOUND_DESIGN+'/quote',{method:'POST',body:body});
    }
    function generateSoundEffects(body,idempotencyKey){
      return scoped(SOUND_DESIGN+'/jobs',{
        method:'POST',
        headers:{'Idempotency-Key':idempotencyKey},
        body:body
      });
    }
    function applySoundEffects(body){
      return scoped(SOUND_DESIGN+'/apply',{method:'POST',body:body});
    }
    function quoteFinal(body){
      return scoped(ASSEMBLY+'/final-quote',{method:'POST',body:body});
    }
    function exportFinal(body,idempotencyKey){
      return scoped(ASSEMBLY+'/export',{
        method:'POST',
        headers:{'Idempotency-Key':idempotencyKey},
        body:body
      });
    }
    function confirmFinal(body){
      return scoped(ASSEMBLY+'/confirm',{method:'POST',body:body});
    }
    function completionReadiness(projectId){
      return scoped(
        COMPLETION+'/readiness?project_id='+encodeURIComponent(projectId)
      );
    }
    function confirmCompletion(body,idempotencyKey){
      return scoped(COMPLETION+'/confirm',{
        method:'POST',
        headers:{'Idempotency-Key':idempotencyKey},
        body:body
      });
    }
    function completion(projectId){
      return scoped(COMPLETION+'?project_id='+encodeURIComponent(projectId));
    }
    function job(jobId){
      return scoped('/api/gen/job/'+encodeURIComponent(jobId));
    }
    function asset(path){
      if(!client||typeof client.asset!=='function'){
        return Promise.reject(new Error('当前客户端不支持受保护媒体读取'));
      }
      var request={};
      if(boardId) request.headers={'X-Canvas-Board-Id':boardId};
      return Promise.resolve(client.asset(path,request)).catch(function(error){
        throw normalizeError(error);
      });
    }
    function destroy(){ destroyed=true;generation+=1; }
    return {
      scoped:scoped,
      load:load,
      saveConfig:saveConfig,
      audioAssets:audioAssets,
      soundDesign:soundDesign,
      analyzeSoundDesign:analyzeSoundDesign,
      saveSoundSuggestions:saveSoundSuggestions,
      quoteSoundEffects:quoteSoundEffects,
      generateSoundEffects:generateSoundEffects,
      applySoundEffects:applySoundEffects,
      remux:remux,
      playbackJob:playbackJob,
      selectPlayback:selectPlayback,
      preview:preview,
      quoteFinal:quoteFinal,
      exportFinal:exportFinal,
      confirmFinal:confirmFinal,
      completionReadiness:completionReadiness,
      confirmCompletion:confirmCompletion,
      completion:completion,
      job:job,
      asset:asset,
      nextGeneration:function(){ return ++generation; },
      generation:function(){ return generation; },
      destroy:destroy
    };
  }
  function createMutationCoordinator(){
    var tail=Promise.resolve(),destroyed=false;
    function run(name,operation){
      if(destroyed) return Promise.reject(new Error('coordinator destroyed'));
      var current=tail.catch(function(){}).then(function(){
        if(destroyed) throw new Error('coordinator destroyed');
        return operation(name);
      });
      tail=current;
      return current;
    }
    return {
      run:run,
      destroy:function(){ destroyed=true; },
      idle:function(){ return tail.catch(function(){}); }
    };
  }
  return {
    ASSEMBLY_PATH:ASSEMBLY,
    COMPLETION_PATH:COMPLETION,
    SOUND_DESIGN_PATH:SOUND_DESIGN,
    PLAYBACK_PATH:PLAYBACK,
    normalizeError:normalizeError,
    createIdempotencyKey:key,
    createApi:createApi,
    createMutationCoordinator:createMutationCoordinator
  };
});
