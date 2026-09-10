/* Pure catalog rules shared by the channel workspace and its regression tests. */
(function(root){
  const categories=[['all','全部'],['image','图片生成'],['video','视频生成'],['avatar','数字人'],['audio','音频与配音'],['text','文本与助手'],['collect','采集与解析'],['process','视频处理'],['other','其他服务']];
  function classify(text){
    const rules=[['image',/图片|生图|绘图/],['video',/视频生成/],['avatar',/数字人|数字化 IP|数字化IP|形象|口播/],['audio',/音频|配音|语音|声音|音乐/],['text',/文本|文案|助手|语言|提示词/],['collect',/采集|解析|搜索|抓取/],['process',/视频处理|成片|剪辑|合成|字幕|换装|背景/]];
    const found=rules.filter(([,rule])=>rule.test(text||'')).map(([key])=>key);
    return found.length?found:['other'];
  }
  function checkLabel(check,now=Date.now()/1000){
    if(!check)return '未验证';
    if(!check.updated||now-Number(check.updated)>86400)return '证据已过期';
    return {passed:'通过',failed:'失败',unknown:'结果未知',running:'检测中',queued:'排队中',blocked:'条件未满足'}[check.state]||'未验证';
  }
  function catalog(data,legacy){
    return (legacy||[]).map(c=>({...c,uid:'legacy:'+c.key,source:'legacy',supplier:c.name,
      categories:classify(c.category),enabled:c.accepts_new_jobs!==false&&data.legacy_controls?.[c.key]?.enabled!==false,retired:c.accepts_new_jobs===false||data.legacy_controls?.[c.key]?.enabled===false,
      scope:data.legacy_scopes?.[c.key],version:data.legacy_controls?.[c.key]?.revision||0,control:data.legacy_controls?.[c.key],
      connection_type:'unknown',health:c.evidence?.label||'未验证',attention:['fail','warn','neutral'].includes(c.evidence?.state)||!c.evidence,
      checks:[],model:c.model||'按功能配置'})).concat((data.items||[]).map(c=>({...c,uid:'managed:'+c.id,source:'managed',
        supplier:c.supplier||'未标注供应商',connection_type:c.connection_type||'unknown',
        categories:[data.adapters?.[c.adapter]?.kind==='image'?'image':'video'],retired:!c.enabled,deleted:!!c._lifecycle?.deleted,
        attention:c.health!=='成品核验通过',features:(data.mappings||[]).filter(m=>m.channel===c.id).map(m=>m.label||m.front)})));
  }
  function filter(rows,f){return rows.filter(c=>(f.status==='deleted'?c.deleted:!c.deleted)&&(f.history||['disabled','deleted'].includes(f.status)||!c.retired)&&(f.category==='all'||c.categories.includes(f.category))&&(!f.supplier||c.supplier===f.supplier)&&(!f.transport||c.connection_type===f.transport)&&(!f.status||f.status==='deleted'||(f.status==='enabled'?c.enabled:f.status==='disabled'?!c.enabled:c.attention))&&(!f.q||[c.name,c.supplier,c.model,...(c.features||[])].join(' ').toLowerCase().includes(f.q.toLowerCase())))}
  function compatible(data,kind){return (data.items||[]).filter(c=>!c._lifecycle?.deleted&&data.adapters?.[c.adapter]?.kind===kind)}
  root.ChannelCatalog={categories,classify,checkLabel,catalog,filter,compatible};
})(typeof window==='undefined'?globalThis:window);
