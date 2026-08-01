(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaCompletion=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function text(value){ return String(value==null?'':value); }
  function number(value){ var result=Number(value);return isFinite(result)?result:0; }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function formatTime(value){
    var timestamp=Number(value);
    if(!timestamp) return '时间未知';
    if(timestamp<1000000000000) timestamp*=1000;
    try{return new Date(timestamp).toLocaleString('zh-CN');}
    catch(ignore){return '时间未知';}
  }
  function blockerList(blockers){
    var items=Array.isArray(blockers)?blockers:[];
    if(!items.length){
      return '<li class="is-ready"><strong>交付门禁已通过</strong>'+
        '<span>确认时服务端会再次执行权威检查</span></li>';
    }
    return items.map(function(item){
      return '<li data-code="'+escapeHtml(item.code)+'"><strong>'+
        escapeHtml(item.message||item.code)+'</strong><span>'+
        escapeHtml(item.recommended_action||'修复后重新检查')+'</span></li>';
    }).join('');
  }
  function completionCard(completion){
    if(!completion) return '';
    return '<section class="nc-sdw-completion-card" data-state="completed">'+
      '<header><span aria-hidden="true">✓</span><div><strong>项目已确认交付</strong>'+
      '<small>完成后永久只读</small></div></header><dl>'+
      '<div><dt>交付编号</dt><dd><code>'+
      escapeHtml(completion.completion_id)+'</code></dd></div>'+
      '<div><dt>最终资产</dt><dd><code>'+
      escapeHtml(completion.asset_id)+'</code></dd></div>'+
      '<div><dt>完成者</dt><dd>'+escapeHtml(completion.completed_by)+'</dd></div>'+
      '<div><dt>完成时间</dt><dd>'+formatTime(completion.completed_at)+'</dd></div>'+
      '</dl><p>仍可播放、下载允许的资产和查看历史版本；所有创作写操作已关闭。</p>'+
      '</section>';
  }
  function confirmationDialog(readiness,project,ui){
    if(!ui.completionDialog) return '';
    var version=readiness.final_version||{};
    var asset=readiness.asset||{};
    var billing=readiness.billing||{};
    var acknowledged=ui.completionAcknowledged===true;
    return '<div class="nc-sdw-completion-modal" role="dialog" aria-modal="true" '+
      'aria-labelledby="nc-sdw-completion-title"><div>'+
      '<header><span>不可逆操作</span><h3 id="nc-sdw-completion-title">'+
      '确认完成并锁定项目</h3><p>完成后不能重新开启原项目，只能复制为新项目继续制作。</p></header>'+
      '<dl><div><dt>短剧</dt><dd>'+escapeHtml(project.title||readiness.project_id)+
      '</dd></div><div><dt>画幅 / 时长</dt><dd>'+
      escapeHtml(project.ratio||'--')+' / '+number(project.target_duration)+
      ' 秒</dd></div><div><dt>最终版本</dt><dd>'+
      escapeHtml(version.id||'--')+'</dd></div><div><dt>资产 ID</dt><dd><code>'+
      escapeHtml(asset.id||'--')+'</code></dd></div><div><dt>累计消耗</dt><dd>'+
      number(billing.spent_points)+' 点</dd></div></dl>'+
      '<button type="button" class="nc-sdw-completion-ack" '+
      'data-action="toggle-completion-ack" role="checkbox" aria-checked="'+
      acknowledged+'"><i aria-hidden="true">'+(acknowledged?'✓':'')+
      '</i><span>我已检查最终成片，确认该项目进入只读交付状态</span></button>'+
      '<footer><button type="button" data-action="cancel-completion">取消</button>'+
      '<button type="button" class="is-danger" data-action="submit-completion"'+
      (acknowledged?'':' disabled')+'>'+
      (ui.busyAction==='completion'?'正在确认…':'确认完成并锁定项目')+
      '</button></footer></div></div>';
  }
  function render(readiness,project,ui,canEdit,busy){
    readiness=readiness||{};
    project=project||{};
    ui=ui||{};
    if(readiness.completion){
      return completionCard(readiness.completion);
    }
    var enabled=readiness.feature_enabled===true;
    var ready=readiness.ready===true;
    var reason=!enabled?'完成确认功能尚未开放':
      !canEdit?'当前权限只读':
      !ready?'请先处理全部交付阻塞项':
      busy?'当前有操作处理中':'';
    return '<section class="nc-sdw-completion" data-ready="'+ready+'">'+
      '<header><span>D-6 阶段收口</span><strong>交付就绪检查</strong>'+
      '<button type="button" data-action="reload" aria-label="重新检查交付状态">↻</button>'+
      '</header><ul>'+blockerList(readiness.blockers)+'</ul>'+
      '<div class="nc-sdw-delivery-hash"><span>交付指纹</span><code>'+
      escapeHtml(text(readiness.delivery_hash).slice(0,16)||'尚未生成')+
      '</code></div><button type="button" class="nc-sdw-complete-button" '+
      'data-action="open-completion"'+
      (enabled&&ready&&canEdit&&!busy?'':' disabled')+
      (reason?' title="'+escapeHtml(reason)+'"':'')+
      '>确认完成并锁定项目</button>'+
      (reason?'<small class="nc-sdw-disabled-reason">'+escapeHtml(reason)+
        '</small>':'')+confirmationDialog(readiness,project,ui)+'</section>';
  }
  function request(readiness){
    readiness=readiness||{};
    return {
      project_id:readiness.project_id,
      revision:readiness.revision,
      final_version_id:readiness.final_version&&readiness.final_version.id,
      asset_id:readiness.asset&&readiness.asset.id,
      delivery_hash:readiness.delivery_hash,
      acknowledged:true
    };
  }
  return {
    render:render,
    request:request,
    blockerList:blockerList,
    completionCard:completionCard,
    formatTime:formatTime
  };
});
