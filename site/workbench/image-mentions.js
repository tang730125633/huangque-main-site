(function(){
  'use strict';
  var states=new WeakMap();
  function chinese(n){return ['一','二','三','四','五','六','七','八','九','十','十一','十二','十三','十四','十五','十六'][n-1]||String(n);}
  function trigger(value,cursor){return cursor>0&&String(value||'').charAt(cursor-1)==='@'?{start:cursor-1,end:cursor}:null;}
  function move(value,start,end,to){
    value=String(value||'');
    if(start<0||end<=start||end>value.length)return {value:value,cursor:to};
    var token=value.slice(start,end),rest=value.slice(0,start)+value.slice(end);
    if(to>end)to-=end-start;
    to=Math.max(0,Math.min(to,rest.length));
    return {value:rest.slice(0,to)+token+rest.slice(to),cursor:to+token.length};
  }
  function imageItem(item,index){
    return {src:(item&&typeof item==='object'?(item.url||item.data||item.preview):item)||'',name:(item&&item.name)||('图片'+chinese(index+1))};
  }
  function serialize(root){
    var out='';
    Array.from(root.childNodes||[]).forEach(function(node){
      if(node.nodeType===3){out+=node.nodeValue||'';return;}
      if(node.nodeType!==1)return;
      if(node.dataset&&node.dataset.token){out+=node.dataset.token;return;}
      if(node.tagName==='BR')return;
      out+=serialize(node);
      if((node.tagName==='DIV'||node.tagName==='P')&&node.nextSibling)out+='\n';
    });
    return out;
  }
  function pointOffset(editor,node,offset){
    try{
      var range=document.createRange();
      range.setStart(editor,0);range.setEnd(node,offset);
      var box=document.createElement('div');box.appendChild(range.cloneContents());
      return serialize(box);
    }catch(e){return '';}
  }
  function selectionOffsets(state){
    var sel=window.getSelection&&window.getSelection();
    if(!sel||!sel.rangeCount||!state.editor.contains(sel.anchorNode))return {start:state.textarea.selectionStart||0,end:state.textarea.selectionEnd||0};
    var a=pointOffset(state.editor,sel.anchorNode,sel.anchorOffset).length;
    var b=pointOffset(state.editor,sel.focusNode,sel.focusOffset).length;
    return {start:Math.min(a,b),end:Math.max(a,b)};
  }
  function setCaret(state,offset){
    offset=Math.max(0,Math.min(offset,state.textarea.value.length));
    var range=document.createRange(),left=offset,placed=false;
    Array.from(state.editor.childNodes).some(function(node){
      var len=node.nodeType===3?(node.nodeValue||'').length:String((node.dataset&&node.dataset.token)||'').length;
      if(left>len){left-=len;return false;}
      if(node.nodeType===3){range.setStart(node,left);}
      else if(left===0){range.setStartBefore(node);}
      else{range.setStartAfter(node);}
      placed=true;return true;
    });
    if(!placed)range.selectNodeContents(state.editor),range.collapse(false);else range.collapse(true);
    var sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);
    state.textarea.selectionStart=state.textarea.selectionEnd=offset;
  }
  function chipAt(state,offset,direction){
    var pos=0,found=null;
    Array.from(state.editor.childNodes).some(function(node){
      var token=node.nodeType===1&&node.dataset&&node.dataset.token;
      var len=token?token.length:(node.nodeValue||'').length;
      if(token&&((direction==='backward'&&pos+len===offset)||(direction==='forward'&&pos===offset))){found={node:node,start:pos,end:pos+len};return true;}
      pos+=len;return false;
    });
    return found;
  }
  function clearSelected(state){
    if(state.selected)state.selected.classList.remove('is-selected');
    state.selected=null;
  }
  function selectChip(state,chip){
    clearSelected(state);state.selected=chip;chip.classList.add('is-selected');
  }
  function makeChip(state,index){
    var token='@图片'+index,item=imageItem((state.getImages()||[])[index-1],index-1);
    var chip=document.createElement('span');chip.className='hq-image-chip';chip.contentEditable='false';chip.dataset.token=token;chip.setAttribute('role','button');chip.setAttribute('aria-label',token+'，可拖动，按两次退格删除');
    if(item.src){var img=document.createElement('img');img.alt='';img.src=item.src;chip.appendChild(img);}
    var label=document.createElement('span');label.textContent='@图片 '+index;chip.appendChild(label);
    chip.title=item.name;
    chip.addEventListener('click',function(e){e.preventDefault();if(state.suppressClick){state.suppressClick=false;return;}selectChip(state,chip);state.editor.focus();setCaret(state,Array.from(state.editor.childNodes).slice(0,Array.from(state.editor.childNodes).indexOf(chip)+1).reduce(function(n,x){return n+(x.dataset&&x.dataset.token?x.dataset.token.length:(x.nodeValue||'').length);},0));});
    chip.addEventListener('mousedown',function(e){
      if(e.button!==0)return;
      var pos=0;Array.from(state.editor.childNodes).some(function(node){if(node===chip)return true;pos+=(node.dataset&&node.dataset.token?node.dataset.token.length:(node.nodeValue||'').length);return false;});
      state.pointer={chip:chip,start:pos,end:pos+token.length,x:e.clientX,y:e.clientY,active:false};
    });
    return chip;
  }
  function render(state,value,caret,landStart){
    var scroll=state.editor.scrollTop,match,last=0,re=/@图片([1-9]\d*)/g;state.editor.textContent='';
    while((match=re.exec(value))){if(match.index>last)state.editor.appendChild(document.createTextNode(value.slice(last,match.index)));var chip=makeChip(state,Number(match[1]));state.editor.appendChild(chip);if(match.index===landStart)chip.classList.add('is-settling');last=match.index+match[0].length;}
    if(last<value.length)state.editor.appendChild(document.createTextNode(value.slice(last)));
    state.editor.scrollTop=scroll;clearSelected(state);if(caret!=null)setCaret(state,caret);
  }
  function fireInput(state,value,range,rerender){
    var max=Number(state.textarea.maxLength)||0;if(max>0&&value.length>max)value=value.slice(0,max);
    state.textarea.value=value;state.textarea.selectionStart=Math.min(range.start,value.length);state.textarea.selectionEnd=Math.min(range.end,value.length);
    if(rerender)render(state,value,state.textarea.selectionEnd);
    state.syncing=true;state.textarea.dispatchEvent(new Event('input',{bubbles:true}));state.syncing=false;
  }
  function relay(state,e){
    var forwarded=new Event(e.type,{bubbles:false,cancelable:true});
    ['clipboardData','dataTransfer'].forEach(function(key){if(e[key])Object.defineProperty(forwarded,key,{value:e[key]});});
    state.textarea.dispatchEvent(forwarded);if(forwarded.defaultPrevented)e.preventDefault();
  }
  function caretFromPoint(state,e){
    var hit=document.elementFromPoint&&document.elementFromPoint(e.clientX,e.clientY),chip=hit&&hit.closest&&hit.closest('.hq-image-chip');
    if(chip&&state.editor.contains(chip)){
      var pos=0;Array.from(state.editor.childNodes).some(function(node){if(node===chip)return true;pos+=(node.dataset&&node.dataset.token?node.dataset.token.length:(node.nodeValue||'').length);return false;});
      var rect=chip.getBoundingClientRect(),target=e.clientX<rect.left+rect.width/2?pos:pos+chip.dataset.token.length;setCaret(state,target);return target;
    }
    var range=document.caretRangeFromPoint&&document.caretRangeFromPoint(e.clientX,e.clientY);
    if(!range&&document.caretPositionFromPoint){var p=document.caretPositionFromPoint(e.clientX,e.clientY);if(p){range=document.createRange();range.setStart(p.offsetNode,p.offset);range.collapse(true);}}
    if(range&&state.editor.contains(range.startContainer)){var sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);var offset=selectionOffsets(state).start;state.textarea.selectionStart=state.textarea.selectionEnd=offset;return offset;}
    return selectionOffsets(state).start;
  }
  function insert(textarea,index,range){
    if(!textarea)return;
    var state=states.get(textarea),start=range?range.start:(textarea.selectionStart==null?textarea.value.length:textarea.selectionStart),end=range?range.end:(textarea.selectionEnd==null?start:textarea.selectionEnd),token='@图片'+index;
    if(!state){textarea.setRangeText(token,start,end,'end');textarea.focus();textarea.dispatchEvent(new Event('input',{bubbles:true}));return;}
    var value=textarea.value.slice(0,start)+token+textarea.value.slice(end),cursor=start+token.length;
    fireInput(state,value,{start:cursor,end:cursor},false);render(state,value,cursor,start);state.editor.focus();
  }
  function bind(textarea,getImages){
    if(!textarea||states.has(textarea))return states.get(textarea);
    var editor=document.createElement('div'),menu=document.createElement('div');
    var state={textarea:textarea,editor:editor,getImages:getImages,menu:menu,active:0,current:null,selected:null,pointer:null,suppressClick:false,syncing:false,composing:false};states.set(textarea,state);
    editor.className='hq-image-editor';editor.contentEditable='true';editor.setAttribute('role','textbox');editor.setAttribute('aria-multiline','true');editor.setAttribute('aria-label',textarea.getAttribute('aria-label')||'提示词');editor.dataset.placeholder=textarea.placeholder||'';
    Array.from(textarea.attributes).forEach(function(a){if(a.name==='style')editor.setAttribute('style',a.value);});
    textarea.classList.add('hq-image-mention-source');textarea.insertAdjacentElement('afterend',editor);
    menu.className='hq-image-mention-menu';menu.setAttribute('role','listbox');menu.hidden=true;document.body.appendChild(menu);
    function close(){menu.hidden=true;state.current=null;}
    function choose(index){if(state.current)insert(textarea,index,state.current);close();}
    function paint(){Array.from(menu.children).forEach(function(el,i){el.classList.toggle('on',i===state.active);});}
    function show(range){
      var images=getImages()||[];if(!images.length){close();return;}state.current=range;state.active=0;menu.textContent='';
      images.forEach(function(raw,i){
        var item=imageItem(raw,i),b=document.createElement('button'),img=document.createElement('img'),copy=document.createElement('span'),title=document.createElement('strong'),name=document.createElement('small'),token=document.createElement('em');
        b.type='button';b.setAttribute('role','option');img.alt='';img.src=item.src;title.textContent='图片 '+(i+1);name.textContent=item.name;copy.appendChild(title);copy.appendChild(name);token.textContent='@图片'+(i+1);b.appendChild(img);b.appendChild(copy);b.appendChild(token);
        b.onmousedown=function(e){e.preventDefault();choose(i+1);};menu.appendChild(b);
      });
      var sel=window.getSelection(),rect=sel&&sel.rangeCount?sel.getRangeAt(0).getBoundingClientRect():editor.getBoundingClientRect();if(!rect.width&&!rect.height)rect=editor.getBoundingClientRect();
      menu.hidden=false;menu.style.left=Math.max(8,Math.min(rect.left,innerWidth-258))+'px';menu.style.top=Math.max(8,Math.min(rect.bottom+8,innerHeight-menu.offsetHeight-8))+'px';paint();
    }
    function syncEditor(){
      var offsets=selectionOffsets(state),value=serialize(editor),cursor=offsets.end,at=trigger(value,cursor);if(!at&&value.charAt(value.length-1)==='@')at={start:value.length-1,end:value.length};fireInput(state,value,offsets,false);if(at)show(at);else close();if(!state.composing)render(state,textarea.value,textarea.selectionEnd);
    }
    editor.addEventListener('compositionstart',function(){state.composing=true;});
    editor.addEventListener('compositionend',function(){state.composing=false;syncEditor();});
    editor.addEventListener('input',function(){if(!state.composing){syncEditor();return;}var r=selectionOffsets(state),value=serialize(editor),at=trigger(value,r.end);if(!at&&value.charAt(value.length-1)==='@')at={start:value.length-1,end:value.length};fireInput(state,value,r,false);if(at)show(at);});
    editor.addEventListener('keyup',function(){var r=selectionOffsets(state),at=trigger(textarea.value,r.end);if(at)show(at);});
    editor.addEventListener('keydown',function(e){
      if(!menu.hidden){if(e.key==='Escape'){e.preventDefault();close();return;}if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();state.active=(state.active+(e.key==='ArrowDown'?1:-1)+menu.children.length)%menu.children.length;paint();return;}if(e.key==='Enter'){e.preventDefault();choose(state.active+1);return;}}
      if(e.key==='Backspace'||e.key==='Delete'){
        var offsets=selectionOffsets(state),hit=offsets.start===offsets.end&&chipAt(state,offsets.start,e.key==='Backspace'?'backward':'forward');
        if(hit){e.preventDefault();if(state.selected===hit.node){hit.node.classList.add('is-removing');setTimeout(function(){var value=textarea.value.slice(0,hit.start)+textarea.value.slice(hit.end);fireInput(state,value,{start:hit.start,end:hit.start},false);render(state,value,hit.start);},120);}else selectChip(state,hit.node);return;}
      }
      if(e.key==='Enter'){e.preventDefault();var r=selectionOffsets(state),value=textarea.value.slice(0,r.start)+'\n'+textarea.value.slice(r.end);fireInput(state,value,{start:r.start+1,end:r.start+1},false);render(state,value,r.start+1);return;}
      clearSelected(state);
    });
    editor.addEventListener('mousedown',function(e){if(!e.target.closest('.hq-image-chip'))clearSelected(state);});
    editor.addEventListener('paste',function(e){
      var items=Array.from((e.clipboardData&&e.clipboardData.items)||[]),hasImage=items.some(function(x){return x.type&&x.type.indexOf('image/')===0;});
      if(hasImage){relay(state,e);return;}e.preventDefault();var text=(e.clipboardData&&e.clipboardData.getData('text/plain'))||'',r=selectionOffsets(state),value=textarea.value.slice(0,r.start)+text+textarea.value.slice(r.end);fireInput(state,value,{start:r.start+text.length,end:r.start+text.length},false);render(state,value,r.start+text.length);
    });
    document.addEventListener('mousemove',function(e){
      var p=state.pointer;if(!p)return;
      if(!p.active&&Math.hypot(e.clientX-p.x,e.clientY-p.y)<4)return;
      if(!p.active){p.active=true;clearSelected(state);p.chip.classList.add('is-dragging');editor.classList.add('is-dragover');}
      e.preventDefault();caretFromPoint(state,e);
    });
    document.addEventListener('mouseup',function(e){
      var p=state.pointer;state.pointer=null;if(!p||!p.active)return;
      e.preventDefault();var to=caretFromPoint(state,e),result=move(textarea.value,p.start,p.end,to),land=result.cursor-(p.end-p.start);state.suppressClick=true;fireInput(state,result.value,{start:result.cursor,end:result.cursor},false);render(state,result.value,result.cursor,land);editor.classList.remove('is-dragover');setTimeout(function(){state.suppressClick=false;},0);
    });
    editor.addEventListener('dragover',function(e){caretFromPoint(state,e);relay(state,e);});
    editor.addEventListener('drop',function(e){
      caretFromPoint(state,e);relay(state,e);
    });
    editor.addEventListener('dragleave',function(e){if(!editor.contains(e.relatedTarget))editor.classList.remove('is-dragover');});
    editor.addEventListener('blur',function(){setTimeout(function(){close();clearSelected(state);},140);});
    textarea.addEventListener('input',function(){if(!state.syncing)render(state,textarea.value,textarea.selectionEnd);});
    textarea.focus=function(){editor.focus();setCaret(state,textarea.selectionEnd==null?textarea.value.length:textarea.selectionEnd);};
    render(state,textarea.value,textarea.value.length);
    return {close:close,editor:editor};
  }
  var style=document.createElement('style');
  style.textContent='.hq-image-mention-source{display:none!important}.hq-image-editor{box-sizing:border-box;min-height:76px;white-space:pre-wrap;overflow-wrap:anywhere;overflow-y:auto;cursor:text}.hq-image-editor:empty:before{content:attr(data-placeholder);color:#5c6b82;pointer-events:none}.hq-image-editor:focus{outline:none}.hq-image-editor.is-dragover{box-shadow:inset 0 0 0 1px rgba(231,178,76,.42),0 0 0 3px rgba(231,178,76,.07)}.hq-image-chip{display:inline-flex;align-items:center;gap:5px;margin:0 3px;padding:3px 7px 3px 4px;border:1px solid rgba(231,178,76,.34);border-radius:8px;background:rgba(231,178,76,.1);color:#f4c96d;font:600 12px/1.3 inherit;vertical-align:middle;cursor:grab;user-select:none;transition:transform .16s ease,opacity .16s ease,border-color .16s ease,background .16s ease,box-shadow .16s ease}.hq-image-chip:hover{transform:translateY(-1px);border-color:rgba(231,178,76,.62);box-shadow:0 5px 16px rgba(0,0,0,.22)}.hq-image-chip img{width:20px;height:20px;border-radius:5px;object-fit:cover;background:#070b13}.hq-image-chip.is-selected{border-color:#e7b24c;background:rgba(231,178,76,.2);box-shadow:0 0 0 2px rgba(231,178,76,.12)}.hq-image-chip.is-dragging{opacity:.42;transform:scale(.94);cursor:grabbing}.hq-image-chip.is-settling{animation:hq-chip-land .24s cubic-bezier(.2,.9,.25,1.25)}.hq-image-chip.is-removing{animation:hq-chip-out .12s ease forwards}.hq-image-mention-menu{position:fixed;z-index:10020;width:250px;max-height:280px;overflow:auto;padding:6px;border:1px solid rgba(148,164,187,.28);border-radius:12px;background:#111827;box-shadow:0 18px 48px rgba(0,0,0,.45);animation:hq-menu-in .14s ease-out}.hq-image-mention-menu[hidden]{display:none}.hq-image-mention-menu button{width:100%;display:grid;grid-template-columns:38px minmax(0,1fr) auto;align-items:center;gap:9px;padding:7px;border:0;border-radius:8px;background:transparent;color:#eaf1fa;text-align:left;font:13px inherit;cursor:pointer}.hq-image-mention-menu button:hover,.hq-image-mention-menu button.on{background:rgba(231,178,76,.14)}.hq-image-mention-menu img{width:38px;height:38px;border-radius:7px;object-fit:cover;background:#070b13}.hq-image-mention-menu button>span{min-width:0;display:flex;flex-direction:column;gap:2px}.hq-image-mention-menu strong{font:600 13px inherit}.hq-image-mention-menu small{overflow:hidden;color:#77869b;font-size:10.5px;text-overflow:ellipsis;white-space:nowrap}.hq-image-mention-menu em{color:#e7b24c;font:normal 11px inherit}@keyframes hq-menu-in{from{opacity:0;transform:translateY(-4px) scale(.98)}to{opacity:1;transform:none}}@keyframes hq-chip-land{0%{opacity:.45;transform:translateY(-3px) scale(.94)}70%{transform:translateY(1px) scale(1.03)}100%{opacity:1;transform:none}}@keyframes hq-chip-out{to{opacity:0;transform:scale(.82)}}@media(prefers-reduced-motion:reduce){.hq-image-chip,.hq-image-mention-menu{animation:none!important;transition:none!important}}';
  document.head.appendChild(style);
  window.HQImageMentions={bind:bind,insert:insert,trigger:trigger,move:move,serialize:serialize};
})();
