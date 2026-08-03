(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root) root.HQShortDramaCenter=api;
  if(root&&root.document) root.addEventListener('DOMContentLoaded',function(){ api.mount(root.document); });
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  var runtimeRoot=typeof globalThis!=='undefined'?globalThis:this;
  var STAGES=['setup','character_review','script_review','storyboard_review','visual_review','voice_review','video_review','assembly_review','completed'];
  var LABELS={setup:'项目设置',character_review:'角色确认',script_review:'剧本输入',storyboard_review:'分镜确认',visual_review:'画面确认',voice_review:'配音字幕',video_review:'视频确认',assembly_review:'成片确认',completed:'已交付'};
  function text(value){ return String(value==null?'':value); }
  function escapeHtml(value){ return text(value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
  function normalizeProject(raw){
    raw=raw||{};
    return {
      id:text(raw.id),title:text(raw.title)||'未命名短剧',synopsis:text(raw.synopsis),
      stage:STAGES.indexOf(raw.stage)>=0?raw.stage:'setup',board_id:raw.board_id==null?null:text(raw.board_id),
      ratio:text(raw.ratio)||'16:9',target_duration:Number(raw.target_duration)||0,
      shot_count:Number(raw.shot_count)||0,revision:Number(raw.revision)||0,
      spent_points:Number(raw.spent_points)||0,updated_at:text(raw.updated_at)
    };
  }
  function progress(project){ var index=STAGES.indexOf(normalizeProject(project).stage); return Math.round(((index<0?0:index)+1)/STAGES.length*100); }
  function filterProjects(projects,query,stage){
    query=text(query).trim().toLowerCase(); stage=text(stage);
    return (projects||[]).map(normalizeProject).filter(function(project){
      return project.board_id===null&&(!stage||project.stage===stage)&&(!query||(project.title+' '+project.synopsis).toLowerCase().indexOf(query)>=0);
    });
  }
  function metrics(projects){
    var items=filterProjects(projects,'','');
    return {
      all:items.length,
      active:items.filter(function(p){return p.stage!=='setup'&&p.stage!=='completed';}).length,
      blocked:items.filter(function(p){return p.stage==='setup';}).length,
      done:items.filter(function(p){return p.stage==='completed';}).length
    };
  }
  function deleteErrorMessage(error){
    if(error&&error.code==='short_drama_unapplied_paid_job') return '该短剧仍有未结束或未退款的付费任务，暂时不能删除。';
    if(error&&error.code==='revision_conflict') return '短剧已在其他页面更新，请刷新后再删除。';
    return error&&error.message?error.message:'短剧删除失败，请稍后重试。';
  }
  function createPayload(form){
    function value(name){ return text(form&&form.elements&&form.elements[name]&&form.elements[name].value).trim(); }
    return {
      title:value('title'),synopsis:value('synopsis'),ratio:value('ratio')||'16:9',
      target_duration:Number(value('target_duration'))||30,shot_count:Number(value('shot_count'))||6,
      visual_style:value('visual_style')||'电影感写实'
    };
  }
  function compactIdea(value){
    return text(value).replace(/\s+/g,' ').trim().replace(/[。！？!?]+$/,'');
  }
  function ideaTitle(value,index){
    var clean=compactIdea(value).replace(/^(我想|想做|希望|我要|我喜欢)/,'').slice(0,12);
    return clean||['未寄出的信','最后一次选择','灯亮之前'][index||0];
  }
  function buildRecommendations(messages){
    var ideas=(messages||[]).map(compactIdea).filter(Boolean);
    var topic=ideas[0]||'普通人的一次重要选择';
    var tone=ideas[1]||'真实、有情绪张力';
    var ending=ideas[2]||'结尾带来合理反转';
    return [
      {
        id:'steady',label:'方案 A · 情感共鸣',title:ideaTitle(topic,0),
        premise:'围绕“'+topic+'”，从一个看似平常的关系切入，让人物在'+tone+'的冲突中重新理解彼此，'+ending+'。',
        reason:'人物关系清楚，观众容易快速进入故事。',style:'电影感写实'
      },
      {
        id:'conflict',label:'方案 B · 强冲突',title:ideaTitle(topic,1)+'的真相',
        premise:'围绕“'+topic+'”，开场直接抛出无法回避的事件，用连续选择放大矛盾；整体保持'+tone+'，并让'+ending+'成为最后的情绪落点。',
        reason:'前几秒就有钩子，更适合短视频传播。',style:'现实主义电影感'
      },
      {
        id:'creative',label:'方案 C · 创意反转',title:ideaTitle(topic,2)+'以后',
        premise:'把“'+topic+'”放进一个带有误导信息的情境，观众先跟随人物形成判断，再通过细节揭开另一层原因；气质偏'+tone+'，'+ending+'。',
        reason:'结构更有记忆点，适合追求新鲜感的用户。',style:'克制悬念电影感'
      }
    ];
  }
  function advisorStep(messages){
    var count=(messages||[]).filter(function(item){return compactIdea(item);}).length;
    if(count===0) return {message:'先不用想完整故事。你最想创作哪一类内容？可以说家庭、悬疑、校园、职场，或者任何你感兴趣的方向。',quick:['家庭情感','悬疑反转','校园成长','职场现实']};
    if(count===1) return {message:'这个方向可以展开。你希望观众看完是什么感受？例如温暖治愈、紧张压迫、爽感反击，或者笑中带泪。',quick:['温暖治愈','紧张悬疑','爽感反击','笑中带泪']};
    if(count===2) return {message:'明白了。最后一个问题：你偏好什么结局——圆满、反转、留白，还是人物完成成长？',quick:['温暖圆满','合理反转','克制留白','人物成长']};
    return {message:'信息已经足够。我整理了三个方向，并说明了各自适合的原因。你可以先选一个，再进入创作设置继续修改。',recommendations:buildRecommendations(messages)};
  }
  function importedTitle(value,filename){
    var fromFile=text(filename).replace(/\.[^.]+$/,'').trim();
    if(fromFile&&fromFile.length<=80)return fromFile;
    var first=text(value).split(/\r?\n/).map(function(line){return line.trim();}).find(function(line){
      return line&&line.length<=40&&!/^(场景|内景|外景|第.{0,8}[场幕集]|INT\.|EXT\.)/i.test(line);
    });
    return (first||'导入剧本').replace(/^[《「]|[》」]$/g,'').slice(0,80);
  }
  function analyzeImportedScript(value,filename){
    var source=text(value).replace(/\r\n?/g,'\n').trim();
    if(source.length<8)throw new Error('请上传或粘贴至少 8 个字的剧本内容。');
    if(source.length>50000)throw new Error('单次最多导入 50,000 字，请先拆分过长的剧本。');
    var lines=source.split('\n').map(function(line){return line.trim();}).filter(Boolean);
    var names={},sceneCount=0,dialogueCount=0;
    lines.forEach(function(line){
      if(/^(场景\s*[一二三四五六七八九十\d]*|内景|外景|第.{0,8}[场幕集]|INT\.|EXT\.)/i.test(line))sceneCount+=1;
      var matched=line.match(/^([^\s：:，,。！？!?（）()]{1,12})\s*[：:]/);
      if(matched&&!/^(时间|地点|场景|镜头|旁白|画外音|字幕|动作)$/i.test(matched[1])){names[matched[1]]=true;dialogueCount+=1;}
    });
    var compact=source.replace(/\s+/g,'');
    var duration=compact.length<=500?30:compact.length<=1000?45:60;
    var shots=duration===30?6:duration===45?8:10;
    var characters=Object.keys(names);
    var warnings=[];
    if(!sceneCount)warnings.push('没有识别到明确的场景标题，助手会在工作区帮你补充分场。');
    if(!characters.length)warnings.push('没有识别到“人物：对白”格式，人物关系需要进入工作区后确认。');
    if(source.length>6500)warnings.push('原稿较长，将作为完整快照导入，不会按聊天记录截断。');
    var summary=lines.slice(0,8).join(' ').replace(/\s+/g,' ').slice(0,260);
    if(summary.length<8)summary=source.slice(0,260);
    return {
      title:importedTitle(source,filename),source:source,filename:text(filename),
      character_count:characters.length,characters:characters.slice(0,20),scene_count:sceneCount||1,
      dialogue_count:dialogueCount,duration:duration,shot_count:shots,warnings:warnings,
      synopsis:summary,summary:'已读取 '+source.length.toLocaleString()+' 字，识别到 '+characters.length+' 个人物、'+(sceneCount||1)+' 个场景。确认后助手会先复述理解，再与你核实需要保留或优化的内容。'
    };
  }
  function importProjectPayload(form,analysis,mode){
    function value(name){return text(form&&form.elements&&form.elements[name]&&form.elements[name].value).trim();}
    return {
      title:value('title')||analysis.title,synopsis:analysis.synopsis,ratio:value('ratio')||'16:9',
      target_duration:Number(value('target_duration'))||analysis.duration,
      shot_count:Number(value('shot_count'))||analysis.shot_count,
      visual_style:value('visual_style')||'电影感写实',source_text:text(analysis.source),
      filename:text(analysis.filename),import_mode:mode==='optimize'?'optimize':'faithful'
    };
  }
  function newImportKey(){
    var cryptoObject=runtimeRoot&&runtimeRoot.crypto;
    if(cryptoObject&&typeof cryptoObject.randomUUID==='function')return 'script-import-'+cryptoObject.randomUUID();
    return 'script-import-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  }
  function decodePdfString(raw,hex){
    var bytes=[];
    if(hex){
      var clean=raw.replace(/\s+/g,'');if(clean.length%2)clean+='0';
      for(var h=0;h<clean.length;h+=2)bytes.push(parseInt(clean.slice(h,h+2),16));
    }else{
      for(var i=0;i<raw.length;i++){
        if(raw[i]!=='\\'){bytes.push(raw.charCodeAt(i)&255);continue;}
        var next=raw[++i]||'';
        if(/[0-7]/.test(next)){var oct=next;while(i+1<raw.length&&oct.length<3&&/[0-7]/.test(raw[i+1]))oct+=raw[++i];bytes.push(parseInt(oct,8));}
        else bytes.push(({n:10,r:13,t:9,b:8,f:12}[next]||next.charCodeAt(0)||0)&255);
      }
    }
    if(bytes[0]===254&&bytes[1]===255){var out='';for(var j=2;j+1<bytes.length;j+=2)out+=String.fromCharCode(bytes[j]*256+bytes[j+1]);return out;}
    try{return new TextDecoder('utf-8',{fatal:true}).decode(new Uint8Array(bytes));}catch(ignore){return new TextDecoder('latin1').decode(new Uint8Array(bytes));}
  }
  function pdfOperatorText(value){
    var found=[],match,literal=/\(((?:\\.|[^\\)])*)\)\s*Tj/g,hex=/<([0-9A-Fa-f\s]+)>\s*Tj/g,array=/\[((?:.|\n)*?)\]\s*TJ/g;
    while((match=literal.exec(value)))found.push(decodePdfString(match[1],false));
    while((match=hex.exec(value)))found.push(decodePdfString(match[1],true));
    while((match=array.exec(value))){var part=match[1],item,re=/\(((?:\\.|[^\\)])*)\)|<([0-9A-Fa-f\s]+)>/g;while((item=re.exec(part)))found.push(decodePdfString(item[1]||item[2],!!item[2]));}
    return found.join(' ').replace(/\s+/g,' ').trim();
  }
  var MAX_DECOMPRESSED_ENTRY_BYTES=2*1024*1024,MAX_PDF_TOTAL_BYTES=4*1024*1024,MAX_PDF_STREAMS=32,MAX_COMPRESSION_RATIO=200;
  function limitError(message){var error=new Error(message);error.code='decompression_limit';return error;}
  async function readLimitedStream(stream,limit,message){
    var reader=stream.getReader(),chunks=[],total=0;
    try{
      while(true){
        var result=await reader.read();if(result.done)break;
        var chunk=result.value instanceof Uint8Array?result.value:new Uint8Array(result.value||0);
        total+=chunk.byteLength;
        if(total>limit){await reader.cancel();throw limitError(message);}
        chunks.push(chunk);
      }
    }catch(error){try{await reader.cancel();}catch(ignore){}throw error;}
    var output=new Uint8Array(total),offset=0;
    chunks.forEach(function(chunk){output.set(chunk,offset);offset+=chunk.byteLength;});
    return output;
  }
  async function inflateLimited(data,format,limit,message){
    if(typeof DecompressionStream==='undefined')throw new Error('当前浏览器不支持安全的流式解压，请改用粘贴文本。');
    var stream=new Blob([data]).stream().pipeThrough(new DecompressionStream(format));
    return readLimitedStream(stream,limit,message);
  }
  async function extractPdfText(buffer){
    var bytes=new Uint8Array(buffer),latin=new TextDecoder('latin1').decode(bytes),parts=[pdfOperatorText(latin)],match,stream=/<<([\s\S]*?)>>\s*stream\r?\n/g,totalInflated=0,streamCount=0;
    while((match=stream.exec(latin))){
      if(match[1].indexOf('FlateDecode')<0)continue;
      streamCount+=1;if(streamCount>MAX_PDF_STREAMS)throw limitError('PDF 压缩流数量过多，已停止读取。');
      var start=match.index+match[0].length,lengthMatch=match[1].match(/\/Length\s+(\d+)/),end;
      if(lengthMatch){
        var declared=Number(lengthMatch[1]);
        if(!Number.isSafeInteger(declared)||declared<0||declared>1024*1024)throw limitError('PDF 压缩流大小异常，已停止读取。');
        end=start+declared;
        if(end>bytes.length||!/^[\r\n\s]*endstream/.test(latin.slice(end,end+32)))throw limitError('PDF 压缩流边界无效。');
      }else{
        end=latin.indexOf('endstream',start);
        if(end<0||end-start>1024*1024)throw limitError('PDF 压缩流边界无效。');
        while(end>start&&/[\r\n]/.test(latin[end-1]))end-=1;
      }
      var compressed=bytes.slice(start,end);
      var remaining=MAX_PDF_TOTAL_BYTES-totalInflated;
      if(remaining<=0)throw limitError('PDF 解压后的累计内容过大，已停止读取。');
      var inflated=await inflateLimited(compressed,'deflate',Math.min(MAX_DECOMPRESSED_ENTRY_BYTES,remaining),'PDF 单个压缩流解压后过大，已停止读取。');
      if(compressed.byteLength&&inflated.byteLength/compressed.byteLength>MAX_COMPRESSION_RATIO)throw limitError('PDF 压缩比异常，已停止读取。');
      totalInflated+=inflated.byteLength;
      parts.push(pdfOperatorText(new TextDecoder('latin1').decode(inflated)));
    }
    var result=parts.filter(Boolean).join('\n').trim();
    if(result.length<8)throw new Error('这个 PDF 没有可读取的文本层，可能是扫描件。请复制其中的文字后粘贴导入。');
    return result;
  }
  async function extractDocxText(buffer){
    var bytes=new Uint8Array(buffer),view=new DataView(buffer),eocd=-1;
    for(var i=bytes.length-22;i>=Math.max(0,bytes.length-65557);i--){if(view.getUint32(i,true)===0x06054b50){eocd=i;break;}}
    if(eocd<0||eocd+22>bytes.length)throw new Error('无法读取这个 DOCX 文件，请确认文件没有损坏。');
    var commentLength=view.getUint16(eocd+20,true),count=view.getUint16(eocd+10,true),centralSize=view.getUint32(eocd+12,true),centralOffset=view.getUint32(eocd+16,true);
    if(eocd+22+commentLength>bytes.length||view.getUint16(eocd+4,true)!==0||view.getUint16(eocd+6,true)!==0||count>2048||centralOffset+centralSize>eocd)throw limitError('DOCX 中央目录边界无效。');
    var cursor=centralOffset,directoryEnd=centralOffset+centralSize,entry=null,decoder=new TextDecoder('utf-8');
    for(var n=0;n<count;n++){
      if(cursor+46>directoryEnd||view.getUint32(cursor,true)!==0x02014b50)throw limitError('DOCX 中央目录条目无效。');
      var flags=view.getUint16(cursor+8,true),method=view.getUint16(cursor+10,true),compressed=view.getUint32(cursor+20,true),uncompressed=view.getUint32(cursor+24,true),nameLength=view.getUint16(cursor+28,true),extraLength=view.getUint16(cursor+30,true),entryCommentLength=view.getUint16(cursor+32,true),local=view.getUint32(cursor+42,true);
      var next=cursor+46+nameLength+extraLength+entryCommentLength;if(next>directoryEnd)throw limitError('DOCX 中央目录长度无效。');
      var name=decoder.decode(bytes.slice(cursor+46,cursor+46+nameLength));
      if(name==='word/document.xml')entry={flags:flags,method:method,compressed:compressed,uncompressed:uncompressed,local:local,name:name};
      cursor=next;
    }
    if(cursor!==directoryEnd||!entry)throw new Error('DOCX 中没有找到正文内容。');
    if((entry.flags&1)!==0||![0,8].includes(entry.method))throw limitError('DOCX 正文使用了不安全或不支持的压缩方式。');
    if(entry.compressed>1024*1024||entry.uncompressed>MAX_DECOMPRESSED_ENTRY_BYTES||(entry.compressed&&entry.uncompressed/entry.compressed>MAX_COMPRESSION_RATIO))throw limitError('DOCX 正文解压后过大，已停止读取。');
    if(entry.local+30>centralOffset||view.getUint32(entry.local,true)!==0x04034b50)throw limitError('DOCX 本地文件头偏移无效。');
    var localName=view.getUint16(entry.local+26,true),localExtra=view.getUint16(entry.local+28,true),start=entry.local+30+localName+localExtra;
    if(start>centralOffset||start+entry.compressed>centralOffset)throw limitError('DOCX 正文压缩数据边界无效。');
    var localEntryName=decoder.decode(bytes.slice(entry.local+30,entry.local+30+localName));
    if(localEntryName!==entry.name)throw limitError('DOCX 文件头名称不一致。');
    var data=bytes.slice(start,start+entry.compressed),xmlBytes;
    if(entry.method===0)xmlBytes=data;
    else if(entry.method===8)xmlBytes=await inflateLimited(data,'deflate-raw',MAX_DECOMPRESSED_ENTRY_BYTES,'DOCX 正文解压后过大，已停止读取。');
    else throw new Error('当前浏览器无法解压这个 DOCX，请改用粘贴文本。');
    if(entry.uncompressed!==xmlBytes.byteLength)throw limitError('DOCX 正文声明大小与实际内容不一致。');
    var xml=decoder.decode(xmlBytes).replace(/<w:tab[^>]*\/>/g,'\t').replace(/<w:br[^>]*\/>/g,'\n').replace(/<\/w:p>/g,'\n').replace(/<[^>]+>/g,'');
    return xml.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/\n{3,}/g,'\n\n').trim();
  }
  async function readScriptFile(file){
    if(!file)throw new Error('请选择剧本文件。');
    if(file.size>1024*1024)throw new Error('文件不能超过 1MB，请精简后重试。');
    var extension=text(file.name).toLowerCase().split('.').pop(),result='';
    if(['txt','md','markdown'].indexOf(extension)>=0)result=await file.text();
    else if(extension==='docx')result=await extractDocxText(await file.arrayBuffer());
    else if(extension==='pdf')result=await extractPdfText(await file.arrayBuffer());
    else throw new Error('暂不支持这种文件格式，请使用 TXT、Markdown、DOCX 或 PDF。');
    if(text(result).trim().length<8)throw new Error('没有从文件中读取到足够的剧本文字。');
    return text(result).trim();
  }
  function createClient(fetchImpl){
    fetchImpl=fetchImpl||(typeof fetch==='function'?fetch.bind(globalThis):null);
    if(!fetchImpl) throw new Error('fetch unavailable');
    function request(path,options){
      options=options||{};
      var headers=Object.assign({'Accept':'application/json','Authorization':'Bearer __cookie__'},options.headers||{});
      var body=options.body;
      if(body!==undefined){ headers['Content-Type']='application/json'; body=JSON.stringify(body); }
      return fetchImpl(path,{method:options.method||'GET',credentials:'same-origin',cache:'no-store',headers:headers,body:body})
        .then(function(response){return response.text().then(function(raw){
          var data={};try{data=raw?JSON.parse(raw):{};}catch(e){data={};}
          if(!response.ok){
            var looksLikeHtml=/^\s*<!doctype html|^\s*<html/i.test(raw||'');
            var message=looksLikeHtml?'本地接口未连接，请启动开发代理后刷新页面。':data.detail||('请求失败（HTTP '+response.status+'）');
            var error=new Error(message);error.status=response.status;error.code=data.code||'request_failed';throw error;
          }
          return data;
        });});
    }
    return {
      list:function(){return request('/api/gen/short-drama/projects?page=1&page_size=50');},
      create:function(payload){return request('/api/gen/short-drama/projects',{method:'POST',body:payload});},
      workspace:function(id){return request('/api/gen/short-drama/conversation?project_id='+encodeURIComponent(id));},
      importProject:function(payload,idempotencyKey){return request('/api/gen/short-drama/projects/import',{method:'POST',headers:{'Idempotency-Key':idempotencyKey},body:payload});},
      deleteProject:function(project){
        project=normalizeProject(project);
        return request('/api/gen/short-drama/project/delete',{
          method:'POST',body:{project_id:project.id,revision:project.revision}
        });
      }
    };
  }
  function projectUrl(id){ return 'short-drama.html?project='+encodeURIComponent(text(id)); }
  function cardHtml(project){
    project=normalizeProject(project);
    return '<article class="short-drama-card" tabindex="0" data-project-id="'+escapeHtml(project.id)+'">'+
      '<div class="short-drama-card-top"><span class="short-drama-stage">'+escapeHtml(LABELS[project.stage])+'</span><span>R'+project.revision+'</span></div>'+
      '<h2>'+escapeHtml(project.title)+'</h2><p>'+escapeHtml(project.synopsis||'暂无故事简介')+'</p>'+
      '<div class="short-drama-progress"><span style="width:'+progress(project)+'%"></span></div>'+
      '<div class="short-drama-card-foot"><span>'+escapeHtml(project.ratio)+' · '+project.target_duration+' 秒 · '+project.shot_count+' 镜</span><span>'+project.spent_points+' 点</span></div></article>';
  }

  function mount(doc,options){
    options=options||{};var client=options.client||createClient(options.fetchImpl);var projects=[];
    var grid=doc.getElementById('shortDramaGrid'),empty=doc.getElementById('shortDramaEmpty'),notice=doc.getElementById('shortDramaNotice');
    var dialog=doc.getElementById('shortDramaDialog'),form=doc.getElementById('shortDramaForm'),drawer=doc.getElementById('shortDramaDrawer');
    var startOptions=doc.getElementById('shortDramaStartOptions'),inspiration=doc.getElementById('shortDramaInspiration');
    var importSection=doc.getElementById('shortDramaImport'),importEditor=doc.getElementById('shortDramaImportEditor'),importForm=doc.getElementById('shortDramaImportForm');
    var importText=doc.getElementById('shortDramaImportText'),importFile=doc.getElementById('shortDramaImportFile'),importDrop=doc.getElementById('shortDramaImportDrop');
    var ideaForm=doc.getElementById('shortDramaIdeaForm'),ideaInput=doc.getElementById('shortDramaIdeaInput');
    var chat=doc.getElementById('shortDramaIdeaChat'),quickReplies=doc.getElementById('shortDramaIdeaQuickReplies');
    var recommendations=doc.getElementById('shortDramaRecommendations'),ideaMessages=[],selectedProjectId='',importFilename='',importAnalysis=null,pendingImportKey='';
    var deleteButton=doc.getElementById('shortDramaDeleteProject');
    var confirmDelete=options.confirmImpl||function(message){return typeof runtimeRoot.confirm==='function'&&runtimeRoot.confirm(message);};
    function setNotice(message,isError){notice.textContent=message||'';notice.classList.toggle('error',!!isError);notice.hidden=!message;}
    function render(){
      var shown=filterProjects(projects,doc.getElementById('shortDramaSearch').value,doc.getElementById('shortDramaStageFilter').value);
      grid.innerHTML=shown.map(cardHtml).join('');empty.hidden=shown.length>0||projects.length>0;
      var totals=metrics(projects);['All','Active','Blocked','Done'].forEach(function(k){doc.getElementById('shortDramaMetric'+k).textContent=totals[k.toLowerCase()];});
    }
    function showProject(id){
      var project=projects.map(normalizeProject).find(function(item){return item.id===id;});if(!project)return;
      selectedProjectId=project.id;
      doc.getElementById('shortDramaDrawerTitle').textContent=project.title;
      doc.getElementById('shortDramaDrawerMeta').innerHTML='<dt>当前阶段</dt><dd>'+escapeHtml(LABELS[project.stage])+'</dd><dt>项目规格</dt><dd>'+escapeHtml(project.ratio)+' · '+project.target_duration+' 秒 · '+project.shot_count+' 镜</dd><dt>当前版本</dt><dd>R'+project.revision+'</dd><dt>累计使用</dt><dd>'+project.spent_points+' 点</dd>';
      doc.getElementById('shortDramaOpenProject').href=projectUrl(project.id);drawer.hidden=false;
    }
    function deleteSelectedProject(){
      var project=projects.map(normalizeProject).find(function(item){return item.id===selectedProjectId;});
      if(!project)return Promise.resolve(false);
      if(!confirmDelete('删除后《'+project.title+'》将从短剧创作列表中移除，已消耗点数不会退回。确认删除？'))return Promise.resolve(false);
      deleteButton.disabled=true;setNotice('',false);
      return client.deleteProject(project).then(function(){
        projects=projects.filter(function(item){return text(item&&item.id)!==project.id;});
        selectedProjectId='';drawer.hidden=true;render();setNotice('短剧已删除。',false);return true;
      }).catch(function(error){setNotice(deleteErrorMessage(error),true);return false;})
        .finally(function(){deleteButton.disabled=false;});
    }
    function load(){
      setNotice('正在加载项目…',false);
      return client.list().then(function(result){projects=(result&&result.items)||[];setNotice('',false);render();
        var selected=new URLSearchParams((runtimeRoot.location&&runtimeRoot.location.search)||'').get('project');
        if(selected&&runtimeRoot.HQShortDramaWorkspace){
          doc.querySelector('.short-drama-center').classList.add('workspace-mode');
          runtimeRoot.HQShortDramaWorkspace.mount(doc,{projectId:selected,fetchImpl:options.fetchImpl});
        }else if(selected)showProject(selected);
      }).catch(function(error){if(error&&error.status===401&&runtimeRoot.location)runtimeRoot.location.href='../login.html?next='+encodeURIComponent(runtimeRoot.location.pathname+runtimeRoot.location.search);else setNotice(error.message||'项目加载失败',true);throw error;});
    }
    function chatBubble(role,message){
      var node=doc.createElement('div');node.className='short-drama-chat-bubble '+role;
      node.innerHTML='<span>'+(role==='assistant'?'创作助手':'你')+'</span><p>'+escapeHtml(message)+'</p>';
      chat.appendChild(node);chat.scrollTop=chat.scrollHeight;
    }
    function renderQuickReplies(items){
      quickReplies.innerHTML=(items||[]).map(function(item){
        return '<button type="button" data-idea-reply="'+escapeHtml(item)+'">'+escapeHtml(item)+'</button>';
      }).join('');
      quickReplies.hidden=!(items&&items.length);
    }
    function renderRecommendations(items){
      recommendations.innerHTML='<div class="short-drama-recommendation-lead"><strong>为你推荐 3 个方向</strong><span>选择后仍可修改</span></div>'+
        (items||[]).map(function(item){
          return '<article class="short-drama-recommendation-card"><span>'+escapeHtml(item.label)+'</span><h3>'+escapeHtml(item.title)+'</h3><p>'+escapeHtml(item.premise)+'</p><small>推荐理由：'+escapeHtml(item.reason)+'</small><button class="short-drama-primary" type="button" data-recommendation="'+escapeHtml(item.id)+'">采用这个方向</button></article>';
        }).join('');
      recommendations.hidden=!(items&&items.length);
    }
    function setCreateHeading(eyebrow,title,lead){
      doc.getElementById('shortDramaCreateEyebrow').textContent=eyebrow;
      doc.getElementById('shortDramaCreateTitle').textContent=title;
      doc.getElementById('shortDramaCreateLead').textContent=lead;
    }
    function showCreateStep(step){
      startOptions.hidden=step!=='choice';inspiration.hidden=step!=='inspiration';form.hidden=step!=='idea';importSection.hidden=step!=='import';
      if(step==='choice') setCreateHeading('NEW PROJECT','你想怎样开始？','选择最符合当前状态的方式，后面的制作流程完全一致。');
      if(step==='idea') setCreateHeading('CREATE WITH AN IDEA','创建短剧设置','先说清故事方向，其余角色、分镜和成片设置会逐步出现。');
      if(step==='inspiration') setCreateHeading('CREATIVE ADVISOR','和创作助手聊一聊','不用一次想完整；助手会逐步了解偏好，再给出三个可选方向。');
      if(step==='import') setCreateHeading('IMPORT A SCRIPT','导入已有剧本','上传文件或粘贴原稿，助手会先识别内容，再与你确认如何成片。');
    }
    function resetCreate(){
      form.reset();ideaMessages=[];chat.innerHTML='';recommendations.innerHTML='';recommendations.hidden=true;
      importText.value='';importFile.value='';importFilename='';importAnalysis=null;pendingImportKey='';importEditor.hidden=false;importForm.hidden=true;importForm.reset();
      doc.getElementById('shortDramaImportCount').textContent='0';doc.getElementById('shortDramaImportFileName').hidden=true;doc.getElementById('shortDramaImportError').hidden=true;
      doc.getElementById('shortDramaSelectedDirection').hidden=true;
      var opening=advisorStep([]);chatBubble('assistant',opening.message);renderQuickReplies(opening.quick);
      showCreateStep('choice');
    }
    function openCreate(){resetCreate();dialog.showModal();}
    function submitIdea(value){
      value=compactIdea(value);if(!value)return;
      chatBubble('user',value);ideaMessages.push(value);ideaInput.value='';
      var reply=advisorStep(ideaMessages);chatBubble('assistant',reply.message);
      renderQuickReplies(reply.quick||[]);renderRecommendations(reply.recommendations||[]);
    }
    function selectRecommendation(id){
      var selected=buildRecommendations(ideaMessages).find(function(item){return item.id===id;});if(!selected)return;
      form.elements.title.value=selected.title;form.elements.synopsis.value=selected.premise;
      form.elements.visual_style.value=selected.style;
      var summary=doc.getElementById('shortDramaSelectedDirection');
      summary.innerHTML='<span>已选择 '+escapeHtml(selected.label)+'</span><strong>'+escapeHtml(selected.title)+'</strong><p>'+escapeHtml(selected.reason)+'</p>';
      summary.hidden=false;showCreateStep('idea');
    }
    function showImportError(message){var node=doc.getElementById('shortDramaImportError');node.textContent=message||'';node.hidden=!message;}
    function updateImportCount(){doc.getElementById('shortDramaImportCount').textContent=text(importText.value).length.toLocaleString();importAnalysis=null;pendingImportKey='';showImportError('');}
    function loadImportFile(file){
      if(!file)return Promise.resolve();
      var choose=doc.getElementById('shortDramaImportChoose');choose.disabled=true;choose.textContent='正在读取…';showImportError('');
      return readScriptFile(file).then(function(content){
        importFilename=file.name;importText.value=content;updateImportCount();
        var label=doc.getElementById('shortDramaImportFileName');doc.getElementById('shortDramaImportFileText').textContent='已读取：'+file.name+' · '+content.length.toLocaleString()+' 字';label.hidden=false;
      }).catch(function(error){importFile.value='';importFilename='';showImportError(error.message||'文件读取失败，请改用粘贴文本。');})
        .finally(function(){choose.disabled=false;choose.textContent='选择文件';});
    }
    function analyzeImport(){
      try{
        importAnalysis=analyzeImportedScript(importText.value,importFilename);showImportError('');
        doc.getElementById('shortDramaImportCharacters').textContent=importAnalysis.character_count;
        doc.getElementById('shortDramaImportScenes').textContent=importAnalysis.scene_count;
        doc.getElementById('shortDramaImportDuration').textContent=importAnalysis.duration;
        doc.getElementById('shortDramaImportShots').textContent=importAnalysis.shot_count;
        doc.getElementById('shortDramaImportSummary').textContent=importAnalysis.summary;
        var warning=doc.getElementById('shortDramaImportWarnings');warning.innerHTML=importAnalysis.warnings.map(function(item){return '<p>• '+escapeHtml(item)+'</p>';}).join('');warning.hidden=!importAnalysis.warnings.length;
        importForm.elements.title.value=importAnalysis.title;importForm.elements.target_duration.value=String(importAnalysis.duration);importForm.elements.shot_count.value=String(importAnalysis.shot_count);
        importEditor.hidden=true;importForm.hidden=false;
      }catch(error){showImportError(error.message||'剧本识别失败，请检查内容。');}
    }
    doc.getElementById('shortDramaCreate').addEventListener('click',openCreate);
    doc.querySelectorAll('[data-action="open-create"]').forEach(function(node){node.addEventListener('click',openCreate);});
    doc.querySelectorAll('[data-action="close-create"]').forEach(function(node){node.addEventListener('click',function(){dialog.close();});});
    doc.querySelectorAll('[data-action="back-create-choice"]').forEach(function(node){node.addEventListener('click',function(){showCreateStep('choice');});});
    doc.querySelectorAll('[data-create-mode]').forEach(function(node){node.addEventListener('click',function(){
      var mode=node.getAttribute('data-create-mode');showCreateStep(mode==='inspiration'?'inspiration':mode==='import'?'import':'idea');
    });});
    doc.getElementById('shortDramaImportChoose').addEventListener('click',function(){importFile.click();});
    importFile.addEventListener('change',function(){loadImportFile(importFile.files&&importFile.files[0]);});
    doc.getElementById('shortDramaRemoveImportFile').addEventListener('click',function(){
      importFile.value='';importFilename='';importAnalysis=null;pendingImportKey='';importText.value='';updateImportCount();
      doc.getElementById('shortDramaImportFileText').textContent='';doc.getElementById('shortDramaImportFileName').hidden=true;importText.focus();
    });
    importText.addEventListener('input',updateImportCount);
    ['dragenter','dragover'].forEach(function(name){importDrop.addEventListener(name,function(event){event.preventDefault();importDrop.classList.add('dragging');});});
    ['dragleave','drop'].forEach(function(name){importDrop.addEventListener(name,function(event){event.preventDefault();importDrop.classList.remove('dragging');});});
    importDrop.addEventListener('drop',function(event){loadImportFile(event.dataTransfer&&event.dataTransfer.files&&event.dataTransfer.files[0]);});
    doc.getElementById('shortDramaAnalyzeImport').addEventListener('click',analyzeImport);
    doc.getElementById('shortDramaEditImport').addEventListener('click',function(){importForm.hidden=true;importEditor.hidden=false;});
    ideaForm.addEventListener('submit',function(event){event.preventDefault();submitIdea(ideaInput.value);});
    quickReplies.addEventListener('click',function(event){var node=event.target.closest('[data-idea-reply]');if(node)submitIdea(node.getAttribute('data-idea-reply'));});
    recommendations.addEventListener('click',function(event){var node=event.target.closest('[data-recommendation]');if(node)selectRecommendation(node.getAttribute('data-recommendation'));});
    doc.querySelector('[data-action="close-drawer"]').addEventListener('click',function(){selectedProjectId='';drawer.hidden=true;});
    deleteButton.addEventListener('click',deleteSelectedProject);
    doc.getElementById('shortDramaSearch').addEventListener('input',render);doc.getElementById('shortDramaStageFilter').addEventListener('change',render);
    grid.addEventListener('click',function(event){var card=event.target.closest('[data-project-id]');if(card)showProject(card.getAttribute('data-project-id'));});
    grid.addEventListener('keydown',function(event){var card=event.target.closest('[data-project-id]');if(card&&(event.key==='Enter'||event.key===' ')){event.preventDefault();showProject(card.getAttribute('data-project-id'));}});
    form.addEventListener('submit',function(event){
      event.preventDefault();var submit=doc.getElementById('shortDramaSubmit');submit.disabled=true;setNotice('',false);
      client.create(createPayload(form)).then(function(project){
        dialog.close();form.reset();projects.unshift(normalizeProject(project));render();
        if(runtimeRoot.location) runtimeRoot.location.href=projectUrl(project.id);
        else showProject(text(project.id));
      })
        .catch(function(error){setNotice(error.message||'项目创建失败',true);}).finally(function(){submit.disabled=false;});
    });
    importForm.addEventListener('submit',function(event){
      event.preventDefault();if(!importAnalysis)return analyzeImport();
      var submit=doc.getElementById('shortDramaImportSubmit'),modeNode=doc.querySelector('input[name="import_mode"]:checked'),mode=modeNode?modeNode.value:'faithful';
      submit.disabled=true;submit.textContent='正在导入…';showImportError('');
      if(!pendingImportKey)pendingImportKey=newImportKey();
      var created=null;
      client.importProject(importProjectPayload(importForm,importAnalysis,mode),pendingImportKey).then(function(project){
        created=normalizeProject(project);pendingImportKey='';
        dialog.close();projects.unshift(created);render();
        if(runtimeRoot.location)runtimeRoot.location.href=projectUrl(created.id);else showProject(created.id);
      }).catch(function(error){
        showImportError('导入失败：'+(error.message||'请稍后重试。'));
      }).finally(function(){submit.disabled=false;submit.textContent='确认导入并进入工作区';});
    });
    load().catch(function(){});
    return {reload:load,render:render};
  }
  return {STAGES:STAGES,LABELS:LABELS,normalizeProject:normalizeProject,progress:progress,filterProjects:filterProjects,metrics:metrics,deleteErrorMessage:deleteErrorMessage,createPayload:createPayload,compactIdea:compactIdea,buildRecommendations:buildRecommendations,advisorStep:advisorStep,importedTitle:importedTitle,analyzeImportedScript:analyzeImportedScript,importProjectPayload:importProjectPayload,newImportKey:newImportKey,readLimitedStream:readLimitedStream,extractPdfText:extractPdfText,extractDocxText:extractDocxText,readScriptFile:readScriptFile,createClient:createClient,projectUrl:projectUrl,cardHtml:cardHtml,mount:mount};
});
