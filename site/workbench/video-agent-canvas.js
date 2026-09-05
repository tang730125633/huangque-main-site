(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.HQVideoAgentCanvas=api;
})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  function clampPosition(x,y){
    x=Number(x);y=Number(y);
    return {x:Math.max(0,Math.min(1,Number.isFinite(x)?x:0)),y:Math.max(0,Math.min(1,Number.isFinite(y)?y:0))};
  }
  function safeCardSize(value,fallback){
    value=value&&typeof value==='object'?value:{};
    fallback=fallback&&typeof fallback==='object'?fallback:{width:320,height:320};
    var width=Number(value.width),height=Number(value.height);
    return {width:Math.round(Math.max(260,Math.min(560,Number.isFinite(width)?width:fallback.width))),height:Math.round(Math.max(220,Math.min(520,Number.isFinite(height)?height:fallback.height)))};
  }
  function move(item,key,largeStep){
    if(!item||!/^Arrow(Left|Right|Up|Down)$/.test(key))return false;
    var step=largeStep?.05:.01,x=Number(item.x),y=Number(item.y);
    if(key==='ArrowLeft')x-=step;else if(key==='ArrowRight')x+=step;else if(key==='ArrowUp')y-=step;else y+=step;
    var next=clampPosition(x,y);item.x=next.x;item.y=next.y;return true;
  }
  function resize(item,key,largeStep,fallback){
    if(!item||!/^Arrow(Left|Right|Up|Down)$/.test(key))return false;
    var step=largeStep?40:10,width=Number(item.width),height=Number(item.height);
    if(key==='ArrowLeft')width-=step;else if(key==='ArrowRight')width+=step;else if(key==='ArrowUp')height-=step;else height+=step;
    var next=safeCardSize({width:width,height:height},fallback);item.width=next.width;item.height=next.height;return true;
  }
  return {clampPosition:clampPosition,safeCardSize:safeCardSize,move:move,resize:resize};
});
