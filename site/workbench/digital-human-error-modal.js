(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.DigitalHumanErrorModal=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function create(options){
    options=options||{};
    var doc=options.document||(typeof document!=='undefined'?document:null);
    var modal=options.root||(doc&&doc.querySelector?doc.querySelector(options.rootSelector||'#error'):null);
    if(!modal)return {show:function(){},close:function(){},isOpen:function(){return false;}};
    var message=options.message||(modal.querySelector&&modal.querySelector('#errorMessage'));
    var closeButton=options.closeButton||(modal.querySelector&&modal.querySelector('[data-error-close]'));
    var backdrop=options.backdrop||(modal.querySelector&&modal.querySelector('[data-error-backdrop]'));
    var dialog=options.dialog||(modal.querySelector&&modal.querySelector('[role="dialog"]'));
    var previousFocus=null;
    var previousOverflow='';
    var bound=false;
    function setOpen(open){
      var wasOpen=!modal.hidden;
      modal.hidden=!open;
      if(modal.setAttribute)modal.setAttribute('aria-hidden',open?'false':'true');
      if(modal.classList){if(open)modal.classList.add('show');else modal.classList.remove('show');}
      if(doc&&doc.body){
        if(open){if(!wasOpen)previousOverflow=doc.body.style&&doc.body.style.overflow||'';if(doc.body.classList)doc.body.classList.add('error-modal-open');if(doc.body.style)doc.body.style.overflow='hidden';}
        else{if(doc.body.classList)doc.body.classList.remove('error-modal-open');if(doc.body.style)doc.body.style.overflow=previousOverflow;}
      }
    }
    function close(){
      if(modal.hidden)return;
      setOpen(false);
      if(previousFocus&&typeof previousFocus.focus==='function')previousFocus.focus();
    }
    function onKey(event){if(event&&event.key==='Escape'&&!modal.hidden){if(event.preventDefault)event.preventDefault();close();}}
    function bind(){
      if(bound)return;
      bound=true;
      if(closeButton&&closeButton.addEventListener)closeButton.addEventListener('click',close);
      if(backdrop&&backdrop.addEventListener)backdrop.addEventListener('click',close);
      if(doc&&doc.addEventListener)doc.addEventListener('keydown',onKey);
    }
    function show(text){
      bind();
      previousFocus=doc&&doc.activeElement||null;
      if(message)message.textContent=String(text==null?'':text);
      setOpen(true);
      if(closeButton&&typeof closeButton.focus==='function')closeButton.focus();
      else if(dialog&&typeof dialog.focus==='function')dialog.focus();
      return modal;
    }
    bind();
    setOpen(false);
    return {show:show,close:close,isOpen:function(){return !modal.hidden;},getMessage:function(){return message?message.textContent:'';}};
  }
  return {create:create};
});
