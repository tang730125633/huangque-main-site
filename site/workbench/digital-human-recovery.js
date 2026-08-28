(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanRecovery=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function resume(options){
    if(!options.jobId)return options.launch(false);
    if(options.retryApproved)return options.launch(true);
    return options.poll(options.jobId).catch(function(error){
      if(error&&error.terminalJob)options.markTerminal();
      throw error;
    });
  }
  return {resume:resume};
});
