(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaLocks=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function lockState(workspace,version,canEdit){
    var completed=!!workspace&&workspace.stage==='completed';
    var current=!!workspace&&!!version&&(
      version.kind==='final'?
        version.version===workspace.current_final_version:
        version.version===workspace.current_preview_version
    );
    var locked=completed||
      (!!workspace&&workspace.preview_locked&&version&&version.kind==='preview'&&current)||
      (!!version&&version.kind==='final'&&current&&version.status==='succeeded');
    var active=!!workspace&&!!workspace.active_job;
    var ready=!!version&&version.status==='succeeded';
    return {
      locked:locked,
      current:current,
      canLock:canEdit===true&&!completed&&!active&&ready&&current&&!locked&&
        !!workspace.actions.can_lock_preview,
      canUnlock:false,
      reason:completed?'项目已完成，全部成果只读':
        active?'任务运行中，不能改变锁定状态':
        !version?'请先选择一个版本':
        !current?'历史版本只读；请查看当前版本':
        !ready?'版本尚未完成':
        locked?'当前版本已锁定':
        workspace.actions.can_lock_preview?
          '可以锁定当前版本':'当前后端阶段不开放独立锁定'
    };
  }
  return {lockState:lockState};
});
