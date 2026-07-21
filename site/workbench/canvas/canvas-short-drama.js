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

  function normalizeNodeParams(input){
    var value=input||{}, duration=Number(value.target_duration);
    if([30,45,60].indexOf(duration)<0) duration=30;
    return {
      project_id:value.project_id||value.id||null,
      title:String(value.title||'新短剧').slice(0,80),
      ratio:value.ratio==='16:9'?'16:9':'9:16',
      target_duration:duration,
      stage:String(value.stage||'draft'),
      progress:Math.max(0,Math.min(100,Number(value.progress)||0)),
      spent_points:Math.max(0,Number(value.spent_points)||0),
      estimated_points:Math.max(0,Number(value.estimated_points)||0)
    };
  }

  function cloneValue(value){
    if(Array.isArray(value)) return value.map(cloneValue);
    if(value&&typeof value==='object'){
      var copy={};
      Object.keys(value).forEach(function(key){ copy[key]=cloneValue(value[key]); });
      return copy;
    }
    return value;
  }

  function sanitizeNodeData(node){
    var copy=cloneValue(node||{});
    if(copy.type==='shortDrama'){
      copy.params=normalizeNodeParams(copy.params);
      copy.outputs={};
    }
    return copy;
  }

  function creationPayload(params){
    var summary=normalizeNodeParams(params);
    return {
      title:summary.title,
      synopsis:'请在短剧工作区完善故事梗概',
      ratio:summary.ratio,
      target_duration:summary.target_duration,
      shot_count:6
    };
  }

  function canOpenNode(params,canEdit){
    return !!(params&&params.project_id)||!!canEdit;
  }

  function createProjectCoordinator(options){
    options=options||{};
    if(typeof options.getNode!=='function'||typeof options.create!=='function'||typeof options.apply!=='function'){
      throw new Error('short drama project coordinator requires getNode, create, and apply methods');
    }
    var pending=Object.create(null);
    function ensure(nodeId,payload,canCreate){
      nodeId=String(nodeId||'');
      var current=options.getNode(nodeId);
      var projectId=current&&current.params&&current.params.project_id;
      if(projectId) return Promise.resolve(projectId);
      if(pending[nodeId]) return pending[nodeId];
      if(!canCreate) return Promise.reject(new Error('当前画布为只读，无法创建短剧项目'));
      var request=Promise.resolve().then(function(){ return options.create(payload); }).then(function(project){
        var createdId=project&&(project.id||project.project_id);
        if(!createdId) throw new Error('创建短剧项目失败');
        var live=options.getNode(nodeId);
        if(live) options.apply(live,project);
        return createdId;
      });
      pending[nodeId]=request;
      function clear(){ if(pending[nodeId]===request) delete pending[nodeId]; }
      request.then(clear,clear);
      return request;
    }
    return {
      ensure:ensure,
      hasPending:function(nodeId){ return !!pending[String(nodeId||'')]; }
    };
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
    normalizeNodeParams:normalizeNodeParams,
    sanitizeNodeData:sanitizeNodeData,
    creationPayload:creationPayload,
    canOpenNode:canOpenNode,
    createProjectCoordinator:createProjectCoordinator,
    stageIndex:stageIndex,
    summarizeProject:summarizeProject,
    planningPayload:planningPayload,
    createClient:createClient,
    createWorkspace:createWorkspace
  };
});
