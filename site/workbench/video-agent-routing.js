(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.HQVideoAgentRouting=api;
})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  var ROUTES=Object.freeze({
    talking:Object.freeze({function:'talking'}),
    create:Object.freeze({function:'grok'}),
    story:Object.freeze({function:'cinematic',cineMode:'open'}),
    motion:Object.freeze({function:'cinematic',cineMode:'motion'}),
    tryon:Object.freeze({function:'tryon'}),
    compose:Object.freeze({function:'talking',href:'one-click-video.html'})
  });
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
  function activateRoute(route,options){
    options=options||{};var effective=effectiveRoute(route,options.healthKnown,options.minimaxAvailable,options.fallback);
    if(!effective||effective.href)return effective;
    if(options.onFunction)options.onFunction(effective.function||'talking');
    if(effective.function==='cinematic'&&options.onCineMode&&(effective.cineMode==='open'||effective.cineMode==='motion'))options.onCineMode(effective.cineMode);
    return effective;
  }
  return {ROUTES:ROUTES,effectiveRoute:effectiveRoute,cinematicMode:cinematicMode,handoffTarget:handoffTarget,activateRoute:activateRoute};
});
