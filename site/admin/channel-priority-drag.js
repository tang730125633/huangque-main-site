(function(){
  'use strict';
  // One sortable list is one operation. Never drag engines across model groups.
  window.ChannelPriorityDrag=function(root,commit,busy){
    let drag=null;
    const selector='[data-cm-priority-channel]',anchor='[data-cm-priority-anchor]';
    const key=row=>row.dataset.cmPriorityChannel||'@original';
    function finish(cancel){
      const d=drag;if(!d)return;drag=null;
      d.ghost.remove();
      d.rows.forEach(row=>{row.style.transform='';row.classList.remove('cm-sort-moving','cm-sort-placeholder')});
      if(d.handle.hasPointerCapture?.(d.pointerId))d.handle.releasePointerCapture(d.pointerId);
      if(!cancel&&d.order.some((id,i)=>id!==d.original[i])){
        const originalSortable=d.rows.some(row=>row.dataset.cmPriorityChannel==='@original');
        if(originalSortable)commit(d.operation,d.order);
        else if(d.order[0]!=='@original')commit(d.operation,d.order.filter(id=>id!=='@original'));
      }
    }
    root.addEventListener('pointerdown',event=>{
      const handle=event.target.closest('.cm-priority-drag');
      if(!handle||event.button!==0||busy()||drag)return;
      const row=handle.closest(selector),list=row?.parentElement;if(!list)return;
      const rows=Array.from(list.querySelectorAll(selector+','+anchor));if(rows.length<2)return;
      const rects=rows.map(item=>item.getBoundingClientRect());
      const from=rows.indexOf(row),rect=rects[from],original=rows.map(key);
      const ghost=row.cloneNode(true);ghost.removeAttribute('id');ghost.inert=true;ghost.setAttribute('aria-hidden','true');
      ghost.classList.add('cm-sort-ghost');
      Object.assign(ghost.style,{left:rect.left+'px',top:rect.top+'px',width:rect.width+'px',height:rect.height+'px'});
      document.body.appendChild(ghost);
      row.classList.add('cm-sort-placeholder');rows.forEach(item=>item.classList.add('cm-sort-moving'));
      drag={handle,pointerId:event.pointerId,row,rows,rects,ghost,from,original,order:[...original],operation:row.dataset.cmPriorityOperation,offset:event.clientY-rect.top,listRect:list.getBoundingClientRect()};
      handle.setPointerCapture(event.pointerId);event.preventDefault();
    });
    root.addEventListener('pointermove',event=>{
      const d=drag;if(!d||event.pointerId!==d.pointerId)return;
      if(!d.row.isConnected){finish(true);return}
      const top=event.clientY-d.offset,center=top+d.rects[d.from].height/2;
      d.ghost.style.top=top+'px';
      const rest=d.original.filter((_,i)=>i!==d.from);
      let index=0;
      d.rects.forEach((rect,i)=>{if(i!==d.from&&center>rect.top+rect.height/2)index++});
      rest.splice(index,0,d.original[d.from]);d.order=rest;
      const gap=d.rects.length>1?Math.max(0,d.rects[1].top-d.rects[0].bottom):0;
      let cursor=d.rects[0].top;
      rest.forEach(id=>{const i=d.original.indexOf(id);if(i!==d.from)d.rows[i].style.transform='translateY('+(cursor-d.rects[i].top)+'px)';cursor+=d.rects[i].height+gap});
    });
    root.addEventListener('pointerup',event=>{if(drag&&event.pointerId===drag.pointerId){const r=drag.listRect;finish(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top-40||event.clientY>r.bottom+40)}});
    root.addEventListener('pointercancel',()=>finish(true));
    root.addEventListener('lostpointercapture',()=>finish(true));
    window.addEventListener('blur',()=>finish(true));
    // Scrolling invalidates captured geometry; cancel instead of saving wrong order.
    window.addEventListener('scroll',()=>finish(true),true);
    root.addEventListener('keydown',event=>{if(event.key==='Escape')finish(true)});
  };
})();
