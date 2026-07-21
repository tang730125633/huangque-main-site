(function(root,factory){
  var exporter=factory();
  if(typeof module==='object'&&module.exports) module.exports=exporter;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.exporter=exporter; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function clone(value){ return value==null?value:JSON.parse(JSON.stringify(value)); }
  function safeFilename(value){ return String(value||'canvas-template').replace(/[\\/:*?"<>|]+/g,'-'); }
  function serializeTemplate(item,now){
    item=item||{};
    return JSON.stringify({version:1,name:item.name||'画布模板',createdAt:item.createdAt||(now||Date.now)(),data:clone(item.data)},null,2);
  }
  function parseTemplate(text,fallbackName){
    var parsed=JSON.parse(text), snapshot=parsed&&parsed.data&&parsed.data.nodes?parsed.data:(parsed&&parsed.nodes?parsed:null);
    if(!snapshot||!Array.isArray(snapshot.nodes)) throw new Error('模板格式不正确');
    return {name:String(parsed.name||fallbackName||'导入模板').slice(0,40),data:clone(snapshot)};
  }
  function wrappedLines(measure,text,maxWidth,maxLines){
    var lines=[],line='';
    String(text||'').trim().split(/\r?\n/).forEach(function(part,index,parts){
      Array.from(part).forEach(function(ch){
        var next=line+ch;
        if(line&&measure(next)>maxWidth){ lines.push(line); line=ch; }
        else line=next;
      });
      if(index<parts.length-1){ lines.push(line); line=''; }
    });
    if(line) lines.push(line);
    return lines.slice(0,maxLines);
  }
  function nodeImageSource(node){
    if(!node) return '';
    if(node.type==='image') return node.image||node.outputs&&node.outputs.image||'';
    if(node.type==='gen') return node.outputs&&node.outputs.image||'';
    return '';
  }
  function loadExportImage(src,options){
    src=String(src||''); options=options||{};
    if(!src) return Promise.resolve(null);
    function fromUrl(url,revoke){
      return new Promise(function(resolve){
        var cleaned=false;
        function cleanup(){
          if(!revoke||cleaned) return;
          cleaned=true;
          try{options.revokeObjectURL(url);}catch(e){}
        }
        try{
          var image=options.createImage();
          image.onload=function(){cleanup();resolve(image);};
          image.onerror=function(){cleanup();resolve(null);};
          image.src=url;
        }catch(error){cleanup();resolve(null);}
      });
    }
    if(src.indexOf('data:image/')===0||src.indexOf('blob:')===0) return fromUrl(src,false);
    return Promise.resolve().then(function(){return options.fetchBlob(src);}).then(function(blob){
      return fromUrl(options.createObjectURL(blob),true);
    }).catch(function(){return null;});
  }
  function roundRect(ctx,x,y,w,h,r){
    r=Math.min(r,w/2,h/2);
    ctx.beginPath();
    ctx.moveTo(x+r,y);
    ctx.arcTo(x+w,y,x+w,y+h,r);
    ctx.arcTo(x+w,y+h,x,y+h,r);
    ctx.arcTo(x,y+h,x,y,r);
    ctx.arcTo(x,y,x+w,y,r);
    ctx.closePath();
  }
  function drawWrappedText(ctx,text,x,y,maxWidth,lineHeight,maxLines){
    var lines=wrappedLines(function(value){return ctx.measureText(value).width;},text,maxWidth,maxLines);
    lines.forEach(function(value,index){
      if(index===maxLines-1&&ctx.measureText(value).width>maxWidth){
        while(value.length&&ctx.measureText(value+'…').width>maxWidth) value=value.slice(0,-1);
        value+='…';
      }
      ctx.fillText(value,x,y+index*lineHeight);
    });
  }
  function drawImage(ctx,img,x,y,w,h){
    if(!img) return false;
    var scale=Math.max(w/img.naturalWidth,h/img.naturalHeight);
    var dw=img.naturalWidth*scale,dh=img.naturalHeight*scale;
    ctx.save();
    roundRect(ctx,x,y,w,h,8); ctx.clip();
    ctx.drawImage(img,x+(w-dw)/2,y+(h-dh)/2,dw,dh);
    ctx.restore();
    return true;
  }
  function drawNode(ctx,node,img,theme){
    var x=node.x,y=node.y,w=node.width||250,h=node.height||160;
    var light=theme==='light',palette=light?{
      card:'#ffffff',border:'#d9e1eb',head:'#f8fafc',text:'#182235',muted:'#66758a',field:'#f4f7fb'
    }:{card:'#0b1018',border:'#273244',head:'#0e1520',text:'#eaf1fa',muted:'#7e8da2',field:'#080d14'};
    ctx.save();
    ctx.shadowColor=light?'rgba(26,38,58,.15)':'rgba(0,0,0,.45)';
    ctx.shadowBlur=18; ctx.shadowOffsetY=8;
    roundRect(ctx,x,y,w,h,12); ctx.fillStyle=palette.card; ctx.fill();
    ctx.shadowColor='transparent'; ctx.strokeStyle=palette.border; ctx.lineWidth=1; ctx.stroke();
    roundRect(ctx,x,y,w,36,12); ctx.fillStyle=palette.head; ctx.fill();
    ctx.beginPath(); ctx.moveTo(x,y+36); ctx.lineTo(x+w,y+36); ctx.strokeStyle=palette.border; ctx.stroke();
    ctx.beginPath(); ctx.arc(x+15,y+18,4,0,Math.PI*2); ctx.fillStyle=node.typeColor||'#e7b24c'; ctx.fill();
    ctx.fillStyle=palette.text; ctx.font='700 13px "Microsoft YaHei",sans-serif'; ctx.textBaseline='middle';
    var title=node.params&&node.params.title||node.typeName||'节点';
    ctx.fillText(String(title).slice(0,24),x+26,y+18);
    if(node.collapsed){ ctx.restore(); return; }
    var bx=x+10,by=y+47,bw=w-20,bh=Math.max(30,h-57);
    if(img){
      drawImage(ctx,img,bx,by,bw,bh);
    }else if(node.type==='image'||node.type==='gen'){
      roundRect(ctx,bx,by,bw,bh,8); ctx.fillStyle=palette.field; ctx.fill();
      ctx.strokeStyle=palette.border; ctx.setLineDash([4,4]); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle=palette.muted; ctx.font='12px "Microsoft YaHei",sans-serif'; ctx.textAlign='center';
      ctx.fillText(node.type==='image'?'图片未载入':'暂无生成结果',x+w/2,by+bh/2);
      ctx.textAlign='left';
    }else{
      roundRect(ctx,bx,by,bw,bh,8); ctx.fillStyle=palette.field; ctx.fill();
      ctx.fillStyle=palette.text; ctx.font='12px "Microsoft YaHei",sans-serif'; ctx.textBaseline='top';
      var text=node.params&&node.params.text||node.outputs&&node.outputs.prompt||'';
      if(node.type==='video'&&!text) text='视频生成节点';
      drawWrappedText(ctx,text||'暂无内容',bx+10,by+10,bw-20,19,Math.max(1,Math.floor((bh-18)/19)));
    }
    ctx.restore();
  }
  function timestamp(now){
    var value=typeof now==='function'?now():new Date();
    return (value instanceof Date?value:new Date(value)).toISOString().slice(0,19).replace(/[:T]/g,'-');
  }
  function exportJpeg(options){
    options=options||{};
    var bounds=options.bounds,nodes=options.nodes||[],edges=options.edges||[],theme=options.theme==='light'?'light':'dark',later=options.setTimeoutImpl||setTimeout;
    if(!bounds) return Promise.reject(new Error('canvas bounds unavailable'));
    var sources={};
    nodes.forEach(function(node){var src=nodeImageSource(node);if(src) sources[src]=null;});
    return Promise.all(Object.keys(sources).map(function(src){
      return Promise.resolve().then(function(){return options.loadImage(src);}).catch(function(){return null;}).then(function(img){sources[src]=img;});
    })).then(function(){
      var maxSide=Math.max(bounds.w,bounds.h),pixelScale=Math.min(2,4096/maxSide,Math.sqrt(16000000/(bounds.w*bounds.h)));
      pixelScale=Math.max(.25,pixelScale);
      var canvas=options.createCanvas();
      canvas.width=Math.max(1,Math.ceil(bounds.w*pixelScale)); canvas.height=Math.max(1,Math.ceil(bounds.h*pixelScale));
      var ctx=canvas.getContext('2d');
      if(!ctx) throw new Error('canvas context unavailable');
      ctx.scale(pixelScale,pixelScale);
      ctx.fillStyle=theme==='light'?'#f5f8fc':'#070b13'; ctx.fillRect(0,0,bounds.w,bounds.h);
      ctx.fillStyle=theme==='light'?'rgba(116,137,164,.22)':'rgba(148,164,187,.12)';
      for(var gx=12;gx<bounds.w;gx+=24){for(var gy=12;gy<bounds.h;gy+=24){ctx.fillRect(gx,gy,1,1);}}
      ctx.save(); ctx.translate(-bounds.x,-bounds.y);
      edges.forEach(function(edge){
        var a=edge&&edge.from,b=edge&&edge.to;
        if(!a||!b) return;
        var dx=Math.max(40,Math.abs(b.x-a.x)*.5);
        ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.bezierCurveTo(a.x+dx,a.y,b.x-dx,b.y,b.x,b.y);
        ctx.strokeStyle=theme==='light'?'rgba(225,166,45,.72)':'rgba(231,178,76,.58)'; ctx.lineWidth=2; ctx.stroke();
      });
      nodes.forEach(function(node){var src=nodeImageSource(node);drawNode(ctx,node,sources[src]||null,theme);});
      ctx.restore();
      return new Promise(function(resolve,reject){
        canvas.toBlob(function(blob){
          if(!blob){reject(new Error('canvas blob unavailable'));return;}
          var filename='canvas-preview-'+timestamp(options.now)+'.jpg',url=options.createObjectURL(blob);
          try{
            options.download(url,filename);
            resolve({filename:filename,blob:blob});
          }catch(error){reject(error);}
          finally{later(function(){options.revokeObjectURL(url);},1500);}
        },'image/jpeg',.92);
      });
    });
  }
  return {serializeTemplate:serializeTemplate,parseTemplate:parseTemplate,safeFilename:safeFilename,wrappedLines:wrappedLines,nodeImageSource:nodeImageSource,loadExportImage:loadExportImage,exportJpeg:exportJpeg};
});
