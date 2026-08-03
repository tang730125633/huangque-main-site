(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaPoller=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function createPoller(options){
    options=options||{};
    var poll=options.poll;
    var onResult=typeof options.onResult==='function'?options.onResult:function(){};
    var onError=typeof options.onError==='function'?options.onError:function(){};
    var setTimer=options.setTimeout||setTimeout;
    var clearTimer=options.clearTimeout||clearTimeout;
    var documentRef=Object.prototype.hasOwnProperty.call(options,'document')?
      options.document:(typeof document!=='undefined'?document:null);
    var timer=null,active=false,destroyed=false,generation=0,failures=0;
    var baseDelay=Math.max(100,Number(options.baseDelay)||2000);
    var maxDelay=Math.max(baseDelay,Number(options.maxDelay)||5000);
    var hiddenDelay=Math.max(maxDelay,Number(options.hiddenDelay)||15000);
    if(typeof poll!=='function') throw new Error('poll function required');
    function visible(){
      return !documentRef||documentRef.visibilityState!=='hidden';
    }
    function delay(){
      if(!visible()) return hiddenDelay;
      return Math.min(maxDelay,Math.round(baseDelay*Math.pow(1.5,failures)));
    }
    function clear(){
      if(timer!=null){ clearTimer(timer);timer=null; }
    }
    function schedule(current){
      if(!active||destroyed||timer!=null||current!==generation) return;
      timer=setTimer(function(){
        timer=null;
        tick(current);
      },delay());
    }
    function tick(current){
      if(!active||destroyed||current!==generation) return Promise.resolve(null);
      return Promise.resolve(poll(current)).then(function(result){
        if(!active||destroyed||current!==generation) return null;
        failures=0;
        return Promise.resolve(onResult(result,current)).then(function(decision){
          if(decision===false||result&&result.terminal===true){
            stop();return result;
          }
          schedule(current);return result;
        });
      }).catch(function(error){
        if(!active||destroyed||current!==generation) return null;
        failures=Math.min(4,failures+1);
        onError(error,current);
        schedule(current);
        return null;
      });
    }
    function start(immediate){
      clear();active=true;failures=0;generation+=1;
      var current=generation;
      if(immediate===false) schedule(current);
      else tick(current);
      return current;
    }
    function stop(){ active=false;generation+=1;clear(); }
    function onVisibility(){
      if(!active||destroyed) return;
      clear();schedule(generation);
    }
    if(documentRef&&typeof documentRef.addEventListener==='function'){
      documentRef.addEventListener('visibilitychange',onVisibility);
    }
    return {
      start:start,
      stop:stop,
      isActive:function(){ return active&&!destroyed; },
      generation:function(){ return generation; },
      destroy:function(){
        stop();destroyed=true;
        if(documentRef&&typeof documentRef.removeEventListener==='function'){
          documentRef.removeEventListener('visibilitychange',onVisibility);
        }
      }
    };
  }
  return {createPoller:createPoller};
});
