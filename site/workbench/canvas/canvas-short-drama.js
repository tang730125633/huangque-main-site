(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.shortDrama=api; }
})(typeof window!=='undefined'?window:null,function(){
  'use strict';
  var STAGES=['draft','characters_review','script_review','storyboard_review','stills_review'];

  function stageIndex(stage){ return Math.max(0,STAGES.indexOf(stage)); }

  function normalizeSettings(input){
    var value=Object.assign({
      ratio:'9:16',target_duration:30,shot_count:6,
      visual_style:'电影写实',target_platform:'抖音'
    },input||{});
    if(value.ratio!=='9:16'&&value.ratio!=='16:9') value.ratio='9:16';
    if([30,45,60].indexOf(Number(value.target_duration))<0) value.target_duration=30;
    else value.target_duration=Number(value.target_duration);
    var shotCount=Number(value.shot_count);
    if(!isFinite(shotCount)||Math.floor(shotCount)!==shotCount) shotCount=6;
    value.shot_count=Math.max(6,Math.min(10,shotCount));
    return value;
  }

  function summarizeProject(project){
    project=project||{};
    return [
      project.title||'新短剧',
      project.ratio||'9:16',
      String(project.target_duration||30)+'秒',
      project.stage||'draft'
    ].join(' · ');
  }

  function planningPayload(project){
    var settings=normalizeSettings(project);
    return {
      format:'short_drama',
      prompt:settings.synopsis||'',
      dur:String(settings.target_duration)+'s',
      ratio:settings.ratio,
      shot_count:settings.shot_count,
      style:settings.visual_style,
      platform:settings.target_platform
    };
  }

  function projectPath(id){ return '/api/gen/short-drama/project?id='+encodeURIComponent(id); }

  function jobError(data){
    var error=new Error(data&&data.error||data&&data.detail||'短剧策划生成失败');
    error.code=data&&data.code||'job_failed';
    error.data=data||null;
    return error;
  }

  function parseJobResult(data){
    var result=data&&data.result;
    return typeof result==='string'?JSON.parse(result):result;
  }

  function createClient(apiClient,pollFn){
    pollFn=pollFn||apiClient&&apiClient.poll;
    if(!apiClient||typeof apiClient.json!=='function'||typeof pollFn!=='function'){
      throw new Error('short drama client requires json and poll methods');
    }
    function applyPlan(projectId,revision,jobId){
      return apiClient.json('/api/gen/short-drama/apply-plan',{
        method:'POST',body:{project_id:projectId,revision:revision,job_id:jobId}
      });
    }
    return {
      list:function(){ return apiClient.json('/api/gen/short-drama/projects'); },
      get:function(projectId){ return apiClient.json(projectPath(projectId)); },
      create:function(project){ return apiClient.json('/api/gen/short-drama/projects',{method:'POST',body:project}); },
      update:function(projectId,revision,patch){
        return apiClient.json(projectPath(projectId),{method:'PUT',body:Object.assign({},patch||{},{revision:revision})});
      },
      applyPlan:applyPlan,
      confirm:function(projectId,revision,stage){
        return apiClient.json('/api/gen/short-drama/confirm',{
          method:'POST',body:{project_id:projectId,revision:revision,stage:stage}
        });
      },
      generatePlan:function(project){
        return apiClient.json('/api/gen/copy',{method:'POST',body:planningPayload(project)})
          .then(function(created){
            if(!created||!created.job_id) throw jobError(created);
            return pollFn({
              request:function(){ return apiClient.json('/api/gen/job/'+created.job_id); },
              intervalMs:3000,
              maxMs:420000,
              inspect:function(job){
                if(job&&job.status==='done') return {done:true,value:parseJobResult(job)};
                if(job&&(job.status==='error'||job.status==='failed')) return {error:jobError(job)};
                return {pending:true};
              }
            }).then(function(){ return applyPlan(project.id,project.revision,created.job_id); });
          });
      }
    };
  }

  function createWorkspace(options){
    options=options||{};
    return {
      projectId:options.projectId||null,
      client:createClient(options.apiClient,options.poll),
      destroy:function(){}
    };
  }

  return {
    normalizeSettings:normalizeSettings,
    stageIndex:stageIndex,
    summarizeProject:summarizeProject,
    planningPayload:planningPayload,
    createClient:createClient,
    createWorkspace:createWorkspace
  };
});
