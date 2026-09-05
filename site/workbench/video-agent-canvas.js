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
  function bindPointerResize(options){
    var event=options.event,item=options.item,card=options.card,host=options.host;if(!event||!item||!card||event.button!==0)return false;
    event.preventDefault();event.stopPropagation();var scale=Number(options.scale)||1,state={startX:event.clientX,startY:event.clientY,width:card.offsetWidth,height:card.offsetHeight};card.classList.add('resizing');
    var onMove=function(e){var size=safeCardSize({width:state.width+(e.clientX-state.startX)/scale,height:state.height+(e.clientY-state.startY)/scale},state);item.width=size.width;item.height=size.height;card.style.width=size.width+'px';card.style.height=size.height+'px';};
    var onEnd=function(){card.classList.remove('resizing');host.removeEventListener('pointermove',onMove);host.removeEventListener('pointerup',onEnd);host.removeEventListener('pointercancel',onEnd);if(options.onEnd)options.onEnd();};
    host.addEventListener('pointermove',onMove);host.addEventListener('pointerup',onEnd);host.addEventListener('pointercancel',onEnd);return true;
  }
  function bindPointerDrag(options){
    var event=options.event,item=options.item,card=options.card,stage=options.stage,host=options.host;if(!event||!item||!card||!stage||event.button!==0)return false;
    event.preventDefault();var stageRect=stage.getBoundingClientRect(),cardRect=card.getBoundingClientRect(),scale=Number(options.scale)||1,viewport=options.viewport||{x:0,y:0},state={offsetX:(event.clientX-cardRect.left)/scale,offsetY:(event.clientY-cardRect.top)/scale,width:card.offsetWidth,height:card.offsetHeight};card.classList.add('dragging');
    var onMove=function(e){var usableX=Math.max(1,stageRect.width-state.width-16),usableY=Math.max(1,stageRect.height-state.height-104),worldX=(e.clientX-stageRect.left-viewport.x)/scale,worldY=(e.clientY-stageRect.top-viewport.y)/scale,pos=clampPosition((worldX-state.offsetX-8)/usableX,(worldY-state.offsetY-76)/usableY);item.x=pos.x;item.y=pos.y;card.style.left=Math.round(pos.x*usableX+8)+'px';card.style.top=Math.round(pos.y*usableY+76)+'px';};
    var onEnd=function(){card.classList.remove('dragging');host.removeEventListener('pointermove',onMove);host.removeEventListener('pointerup',onEnd);host.removeEventListener('pointercancel',onEnd);if(options.onEnd)options.onEnd();};
    host.addEventListener('pointermove',onMove);host.addEventListener('pointerup',onEnd);host.addEventListener('pointercancel',onEnd);return true;
  }
  return {clampPosition:clampPosition,safeCardSize:safeCardSize,move:move,resize:resize,bindPointerResize:bindPointerResize,bindPointerDrag:bindPointerDrag};
});
