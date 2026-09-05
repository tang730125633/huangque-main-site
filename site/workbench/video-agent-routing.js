(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.HQVideoAgentRouting=api;
})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  function effectiveRoute(route,healthKnown,minimaxAvailable,fallback){
    return route&&route.function==='minimax'&&healthKnown&&!minimaxAvailable?fallback:route;
  }
  function cinematicMode(route,hasVideo){
    if(route&&route.cineMode==='open')return 'open';
    if(route&&route.cineMode==='motion')return 'motion';
    return hasVideo?'motion':'open';
  }
  function handoffTarget(route){
    if(!route)return '';
    if(route.href)return 'link';
    return {talking:'talking',grok:'create',minimax:'create',cinematic:cinematicMode(route,false),tryon:'tryon'}[route.function]||'';
  }
  return {effectiveRoute:effectiveRoute,cinematicMode:cinematicMode,handoffTarget:handoffTarget};
});
