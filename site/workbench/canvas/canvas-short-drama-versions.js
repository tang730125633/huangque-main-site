(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaVersions=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function number(value){ var result=Number(value);return isFinite(result)?result:0; }
  function label(version){
    if(!version) return '暂无版本';
    return (version.kind==='final'?'1080p 正式成片':'720p 预览')+
      ' · v'+number(version.version);
  }
  function statusLabel(status){
    return ({
      queued:'排队中',running:'生成中',succeeded:'已完成',failed:'失败',
      stale:'已过期',rendering:'生成中'
    })[status]||'未知状态';
  }
  function compareSummary(current,viewing){
    if(!current||!viewing) return [];
    var result=[];
    [
      ['规格','width','height'],
      ['时长','duration_ms'],
      ['成本','cost']
    ].forEach(function(fields){
      var name=fields[0],left=fields.slice(1).map(function(key){
        return current[key]==null?'—':current[key];
      }).join('×'),right=fields.slice(1).map(function(key){
        return viewing[key]==null?'—':viewing[key];
      }).join('×');
      if(left!==right) result.push(name+'：'+left+' → '+right);
    });
    if(current.kind!==viewing.kind){
      result.unshift('类型：'+label(current)+' → '+label(viewing));
    }
    return result;
  }
  function group(versions){
    versions=Array.isArray(versions)?versions:[];
    return {
      final:versions.filter(function(item){ return item.kind==='final'; }),
      preview:versions.filter(function(item){ return item.kind==='preview'; })
    };
  }
  return {
    label:label,
    statusLabel:statusLabel,
    compareSummary:compareSummary,
    group:group
  };
});
