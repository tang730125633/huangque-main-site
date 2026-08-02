(function(){
  'use strict';
  function chinese(n){return ['一','二','三','四','五','六','七','八','九','十','十一','十二','十三','十四','十五','十六'][n-1]||String(n);}
  function trigger(value,cursor){return cursor>0&&String(value||'').charAt(cursor-1)==='@'?{start:cursor-1,end:cursor}:null;}
  function insert(textarea,index,range){
    if(!textarea)return;
    var start=range?range.start:(textarea.selectionStart==null?textarea.value.length:textarea.selectionStart);
    var end=range?range.end:(textarea.selectionEnd==null?start:textarea.selectionEnd);
    textarea.setRangeText('@图片'+index,start,end,'end');
    textarea.focus();
    textarea.dispatchEvent(new Event('input',{bubbles:true}));
  }
  function bind(textarea,getImages){
    if(!textarea)return;
    var menu=document.createElement('div'), active=0, current=null;
    menu.className='hq-image-mention-menu'; menu.setAttribute('role','listbox'); menu.hidden=true;
    document.body.appendChild(menu);
    function close(){menu.hidden=true;current=null;}
    function choose(index){if(current)insert(textarea,index,current);close();}
    function show(range){
      var images=(getImages()||[]); if(!images.length){close();return;}
      current=range;active=0;menu.innerHTML='';
      images.forEach(function(src,i){
        var b=document.createElement('button'); b.type='button'; b.setAttribute('role','option');
        b.innerHTML='<img alt=""> <span>图片'+chinese(i+1)+'</span><small>@图片'+(i+1)+'</small>';
        b.querySelector('img').src=src.url||src;
        b.onmousedown=function(e){e.preventDefault();choose(i+1);}; menu.appendChild(b);
      });
      var rect=textarea.getBoundingClientRect();
      menu.hidden=false;
      menu.style.left=Math.max(8,Math.min(rect.left,innerWidth-250))+'px';
      menu.style.top=Math.max(8,Math.min(rect.bottom+6,innerHeight-menu.offsetHeight-8))+'px';
      paint();
    }
    function paint(){Array.from(menu.children).forEach(function(el,i){el.classList.toggle('on',i===active);});}
    textarea.addEventListener('input',function(){var at=trigger(textarea.value,textarea.selectionStart);if(at)show(at);else close();});
    textarea.addEventListener('keydown',function(e){
      if(menu.hidden)return;
      if(e.key==='Escape'){e.preventDefault();close();return;}
      if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();active=(active+(e.key==='ArrowDown'?1:-1)+menu.children.length)%menu.children.length;paint();return;}
      if(e.key==='Enter'){e.preventDefault();choose(active+1);}
    });
    textarea.addEventListener('blur',function(){setTimeout(close,120);});
    return {close:close};
  }
  var style=document.createElement('style');
  style.textContent='.hq-image-mention-menu{position:fixed;z-index:10020;width:242px;max-height:260px;overflow:auto;padding:6px;border:1px solid rgba(148,164,187,.28);border-radius:12px;background:#111827;box-shadow:0 18px 48px rgba(0,0,0,.45)}.hq-image-mention-menu[hidden]{display:none}.hq-image-mention-menu button{width:100%;display:grid;grid-template-columns:34px 1fr auto;align-items:center;gap:9px;padding:7px;border:0;border-radius:8px;background:transparent;color:#eaf1fa;text-align:left;font:13px inherit;cursor:pointer}.hq-image-mention-menu button:hover,.hq-image-mention-menu button.on{background:rgba(231,178,76,.14)}.hq-image-mention-menu img{width:34px;height:34px;border-radius:6px;object-fit:cover;background:#070b13}.hq-image-mention-menu small{color:#e7b24c;font-size:11px}';
  document.head.appendChild(style);
  window.HQImageMentions={bind:bind,insert:insert,trigger:trigger};
})();
