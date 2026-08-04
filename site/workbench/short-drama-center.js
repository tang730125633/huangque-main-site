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
  function plannerProgress(messages,selected,preview){
    var answered=(messages||[]).map(compactIdea).filter(Boolean).length;
    var score=Math.min(45,answered*15)+(selected?25:0)+(preview?30:0);
    return {score:score,label:preview?'剧本待确认':selected?'方向待生成':'正在理解想法'};
  }
  function plannerDurations(total,count){
    total=Math.max(Number(total)||30,Number(count)||1);count=Math.max(1,Number(count)||1);
    var base=Math.floor(total/count),remaining=total-base*count,items=[];
    for(var i=0;i<count;i++)items.push(base+(i>=count-remaining?1:0));
    return items;
  }
  function plannerRoles(messages,selected){
    var source=((messages||[]).join('；')+'；'+text(selected&&selected.premise)).trim();
    var pattern=/外卖小哥|外卖员|女孩|男孩|女生|男生|妈妈|母亲|爸爸|父亲|妻子|丈夫|奶奶|爷爷|老师|学生|护士|医生|同事|老板|店员|老人|孩子|朋友/g;
    var found=source.match(pattern)||[],unique=[];
    found.forEach(function(item){if(unique.indexOf(item)<0)unique.push(item);});
    if(!unique.length)unique.push('主人公');
    if(unique.length===1)unique.push('关键人物');
    return unique.slice(0,4);
  }
  function plannerReadingSeconds(value){
    var characters=text(value).replace(/[\s，。！？、；：“”'…]/g,'').length;
    return characters?Math.round((0.45+characters/3.5)*100)/100:0;
  }
  function plannerDialogueSet(source,roles){
    source=text(source);var first=roles[0]||'主人公',second=roles[1]||'关键人物';
    if(/雨衣|便利店|下雨|雨天/.test(source))return [
      {speaker:first,text:'我该怎么回去？'},{speaker:second,text:'这件雨衣你先用。'},
      {speaker:first,text:'那你怎么办？'},{speaker:second,text:'别担心，我有办法。'},
      {speaker:first,text:'谢谢你。'},{speaker:'',text:''}
    ];
    if(/误会|真相|隐瞒/.test(source))return [
      {speaker:first,text:'你一直瞒着我？'},{speaker:second,text:'事情不是你想的那样。'},
      {speaker:first,text:'那真相是什么？'},{speaker:second,text:'我现在告诉你。'},
      {speaker:first,text:'原来我误会了。'},{speaker:'',text:''}
    ];
    return [
      {speaker:first,text:'事情怎么会这样？'},{speaker:second,text:'先听我说。'},
      {speaker:first,text:'我需要一个答案。'},{speaker:second,text:'这一次我不再回避。'},
      {speaker:first,text:'那就重新开始。'},{speaker:'',text:''}
    ];
  }
  function plannerPhase(index,count){
    if(index===0)return '开场钩子';if(index===count-1)return '结局兑现';
    return index<Math.ceil(count/2)?'冲突升级':'选择与转折';
  }
  function plannerShot(index,count,duration,roles,source,ending,variation){
    var phase=plannerPhase(index,count),first=roles[0]||'主人公',second=roles[1]||'关键人物';
    var dialogue=plannerDialogueSet(source,roles)[index%6]||{speaker:'',text:''};
    var actions=[
      first+'进入场景，停下脚步并观察眼前状况',
      second+'注意到异常，主动做出能改变局面的动作',
      first+'看向'+second+'，身体略微后退后重新站定',
      second+'拿出关键物件或信息，把选择摆到两人面前',
      first+'完成决定性的回应动作，两人的关系随之改变',
      first+'与'+second+'短暂对视，以克制动作收束故事'
    ];
    var expressions=['焦虑、警觉','关切、克制','惊讶、犹豫','认真、坚定','释然、感激','平静、温暖'];
    var cameras=['环境全景切人物中景，缓慢推近','双人中景，跟随关键动作轻微横移','正反打近景，停留人物表情','关键物件特写后拉回双人中景','人物近景，轻推至决定动作','稳定中景转远景，留出结尾余韵'];
    var transitions=['动作切入下一镜','沿视线方向切换','以对方反应承接','由物件特写转场','顺人物动作切换','淡出结束'];
    if(variation){actions[index%6]+='，补充一次清晰可见的反应';expressions[index%6]+='，情绪变化更明显';}
    var reading=plannerReadingSeconds(dialogue.text),remaining=Math.round((duration-reading)*100)/100;
    return {
      index:index+1,phase:phase,duration:duration,scene:/雨衣|便利店|下雨|雨天/.test(source)?'雨天，便利店门口':'故事主要场景',
      characters:roles.slice(0,2),action:actions[index%6],expression:expressions[index%6],
      speaker:dialogue.speaker,dialogue_kind:dialogue.text?'dialogue':'silence',dialogue:dialogue.text,
      reading_seconds:reading,remaining_seconds:remaining,camera:cameras[index%6],
      sound:/雨衣|便利店|下雨|雨天/.test(source)?'持续雨声、便利店开门提示音':'场景环境声，保持对白清晰',
      transition:transitions[index%6],continuity:index?'承接上一镜头的角色位置、服装和关键物件':'建立时间、空间、服装和关键物件基准',
      summary:index===0?source:index===count-1?ending:'围绕核心冲突推进第 '+(index+1)+' 个关键动作',
      locked:false,variation:Number(variation)||0
    };
  }
  function plannerQuality(preview){
    var blockers=[],warnings=[];
    (preview&&preview.shots||[]).forEach(function(shot){
      shot.reading_seconds=plannerReadingSeconds(shot.dialogue_kind==='silence'?'':shot.dialogue);
      shot.remaining_seconds=Math.round((Number(shot.duration)-shot.reading_seconds)*100)/100;
      if(shot.remaining_seconds<0)blockers.push({index:shot.index,message:'台词超出镜头 '+Math.abs(shot.remaining_seconds).toFixed(2)+' 秒'});
      else if(shot.dialogue&&shot.remaining_seconds<0.6)warnings.push({index:shot.index,message:'留给表情动作的时间不足 0.6 秒'});
      if(!shot.action)blockers.push({index:shot.index,message:'缺少可执行动作'});
      if(!shot.expression)blockers.push({index:shot.index,message:'缺少表情与情绪'});
    });
    return {blocking:blockers.length>0,blockers:blockers,warnings:warnings};
  }
  function buildPlannerPreview(payload,messages,selected){
    payload=payload||{};selected=selected||{};
    var title=text(payload.title||selected.title||'未命名短剧').trim();
    var synopsis=text(selected.premise||payload.synopsis||'').trim();
    var duration=Number(payload.target_duration)||30,shotCount=Number(payload.shot_count)||6;
    var notes=(messages||[]).map(compactIdea).filter(Boolean);
    var roles=plannerRoles(notes,selected),protagonist=roles[0];
    var conflict=(synopsis||notes.join('；')||'主人公必须完成一次重要选择').slice(0,180);
    var ending=(notes[2]||'结尾形成清晰的情绪落点').slice(0,80);
    var beats=[],shots=[],durations=plannerDurations(duration,shotCount);
    for(var i=0;i<shotCount;i++){
      var shot=plannerShot(i,shotCount,durations[i],roles,conflict,ending,0);shots.push(shot);
      beats.push({index:shot.index,phase:shot.phase,summary:shot.summary,duration:shot.duration});
    }
    var preview={
      title:title,logline:synopsis,protagonist:protagonist,conflict:conflict,ending:ending,
      ratio:text(payload.ratio)||'16:9',duration_seconds:duration,shot_count:shotCount,
      visual_style:text(payload.visual_style)||'电影感写实',characters:roles,beats:beats,shots:shots,
      selected_direction_id:text(selected.id)||'steady',notes:notes
    };
    preview.quality=plannerQuality(preview);return preview;
  }
  function plannerPromotionMessages(preview){
    preview=preview||{};
    var contract=plannerConfirmedContract(preview);
    var selection={steady:'方案一',conflict:'方案二',creative:'方案三'}[preview.selected_direction_id]||'方案一';
    var clean=function(value){return text(value).replace(/[“”"]/g,'').replace(/\s+/g,' ').trim();};
    var shotContract=contract.shots.map(function(shot){
      return '镜头'+shot.index+'['+shot.duration+'秒] 场景='+clean(shot.scene)+'；角色='+(shot.characters||[]).join('、')+'；动作='+clean(shot.action)+'；表情='+clean(shot.expression)+'；'+(shot.dialogue_kind==='silence'?'无台词':'说话人='+clean(shot.speaker)+'；台词='+clean(shot.dialogue))+'；镜头='+clean(shot.camera)+'；声音='+clean(shot.sound)+'；转场='+clean(shot.transition)+'；连续性='+clean(shot.continuity);
    }).join('\n');
    return [
      '核心设定：'+clean(preview.logline)+'；主角：'+clean(preview.protagonist)+'；冲突：'+clean(preview.conflict)+'；结局：'+clean(preview.ending)+'；补充要求：'+(preview.notes||[]).map(clean).join('；')+'。请推荐三个方向。',
      selection,
      '确认以下逐镜剧本并按原样生成正式结构化剧本。每句台词必须在对应镜头时长内说完，不得把核心设定或剧情摘要当成角色台词。\n'+shotContract
    ];
  }
  function plannerConfirmedContract(preview){
    preview=preview||{};
    function clean(value){return text(value).trim();}
    return {
      schema_version:'preproject-confirmed-shot-contract-v1',
      title:clean(preview.title),logline:clean(preview.logline),protagonist:clean(preview.protagonist),
      conflict:clean(preview.conflict),ending:clean(preview.ending),ratio:clean(preview.ratio),
      duration_seconds:Number(preview.duration_seconds)||0,shot_count:Number(preview.shot_count)||0,
      visual_style:clean(preview.visual_style),characters:(preview.characters||[]).map(clean),
      beats:(preview.beats||[]).map(function(beat){return {index:Number(beat.index),phase:clean(beat.phase),summary:clean(beat.summary),duration:Number(beat.duration)};}),
      shots:(preview.shots||[]).map(function(shot){return {
        index:Number(shot.index),phase:clean(shot.phase),duration:Number(shot.duration),scene:clean(shot.scene),
        characters:(shot.characters||[]).map(clean),action:clean(shot.action),expression:clean(shot.expression),
        speaker:clean(shot.speaker),dialogue_kind:clean(shot.dialogue_kind)||'silence',dialogue:clean(shot.dialogue),
        camera:clean(shot.camera),sound:clean(shot.sound),transition:clean(shot.transition),
        continuity:clean(shot.continuity),summary:clean(shot.summary),locked:!!shot.locked
      };})
    };
  }
  function stableContract(value){
    if(Array.isArray(value))return value.map(stableContract);
    if(value&&typeof value==='object')return Object.keys(value).sort().reduce(function(result,key){result[key]=stableContract(value[key]);return result;},{});
    return value;
  }
  function confirmedContractMatches(script,contract){
    return JSON.stringify(stableContract(script&&script.confirmed_contract||null))===JSON.stringify(stableContract(contract||null));
  }
  function continuePlannerContract(client,projectId,workspace,contract){
    function matching(current){return confirmedContractMatches(current&&current.current_script&&current.current_script.script,contract);}
    function lock(current){
      if(current.conversation.state==='script_locked')return Promise.resolve(current);
      return client.lock({project_id:projectId,conversation_revision:Number(current.conversation.revision),version_id:current.current_script.id},'preproject-'+projectId+'-lock');
    }
    if(workspace.conversation.state==='script_locked'){
      if(!matching(workspace))return Promise.reject(new Error('已锁定剧本与本次确认内容不一致，请进入项目核对。'));
      return Promise.resolve(workspace);
    }
    if(matching(workspace))return lock(workspace);
    return client.generate({project_id:projectId,conversation_revision:Number(workspace.conversation.revision),instruction:'持久化用户已确认的逐镜合同',confirmed_contract:contract},'preproject-'+projectId+'-generate').then(function(current){
      if(!matching(current))throw new Error('服务端保存的逐镜剧本与确认内容不一致，已阻止自动锁定，请重新确认。');
      return lock(current);
    });
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
  function newProjectKey(){
    var cryptoObject=runtimeRoot&&runtimeRoot.crypto;
    if(cryptoObject&&typeof cryptoObject.randomUUID==='function')return 'project-create-'+cryptoObject.randomUUID();
    return 'project-create-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
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
      create:function(payload,idempotencyKey){var options={method:'POST',body:payload};if(idempotencyKey)options.headers={'Idempotency-Key':idempotencyKey};return request('/api/gen/short-drama/projects',options);},
      promote:function(payload,idempotencyKey){return request('/api/gen/short-drama/projects/promote',{method:'POST',headers:{'Idempotency-Key':idempotencyKey},body:payload});},
      workspace:function(id){return request('/api/gen/short-drama/conversation?project_id='+encodeURIComponent(id));},
      message:function(payload,key){return request('/api/gen/short-drama/conversation/messages',{method:'POST',headers:{'Idempotency-Key':key},body:payload});},
      generate:function(payload,key){return request('/api/gen/short-drama/conversation/script/generate',{method:'POST',headers:{'Idempotency-Key':key},body:payload});},
      lock:function(payload,key){return request('/api/gen/short-drama/conversation/script/lock',{method:'POST',headers:{'Idempotency-Key':key},body:payload});},
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
    var createMode='idea',plannerPayload=null,selectedDirection=null,plannerPreview=null,pendingCreateKey='';
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
          doc.documentElement.classList.add('short-drama-immersive');
          if(doc.body)doc.body.classList.add('short-drama-immersive');
          doc.querySelector('.short-drama-center').classList.add('workspace-mode');
          runtimeRoot.HQShortDramaWorkspace.mount(doc,{projectId:selected,fetchImpl:options.fetchImpl});
        }else{
          doc.documentElement.classList.remove('short-drama-immersive');
          if(doc.body)doc.body.classList.remove('short-drama-immersive');
          if(selected)showProject(selected);
        }
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
    function renderPlanner(){
      var progressState=plannerProgress(ideaMessages,selectedDirection,plannerPreview);
      doc.getElementById('shortDramaPlannerStatus').textContent=progressState.label;
      doc.getElementById('shortDramaPlannerProgress').style.width=progressState.score+'%';
      var payload=plannerPayload||createPayload(form);
      doc.getElementById('shortDramaPlannerBrief').innerHTML='<dt>项目</dt><dd>'+escapeHtml(payload.title||'待填写')+'</dd><dt>核心想法</dt><dd>'+escapeHtml(payload.synopsis||'由助手一起探索')+'</dd><dt>规格</dt><dd>'+escapeHtml(payload.ratio||'16:9')+' · '+Number(payload.target_duration||30)+' 秒 · '+Number(payload.shot_count||6)+' 镜</dd><dt>方向</dt><dd>'+escapeHtml(selectedDirection&&selectedDirection.title||'待选择')+'</dd><dt>风格</dt><dd>'+escapeHtml(payload.visual_style||'电影感写实')+'</dd>';
      doc.getElementById('shortDramaGeneratePreview').disabled=!selectedDirection;
      var confirm=doc.getElementById('shortDramaConfirmScript');confirm.hidden=!plannerPreview;
      confirm.disabled=!!(plannerPreview&&plannerPreview.quality&&plannerPreview.quality.blocking);
    }
    function shotStatus(shot){
      if(Number(shot.remaining_seconds)<0)return {kind:'blocked',label:'超时 '+Math.abs(Number(shot.remaining_seconds)).toFixed(2)+' 秒'};
      if(shot.dialogue&&Number(shot.remaining_seconds)<0.6)return {kind:'warning',label:'表演时间不足'};
      return {kind:'pass',label:shot.dialogue?'时长正常':'静默表演'};
    }
    function renderPlannerShot(shot,preview){
      var status=shotStatus(shot),dialogue=shot.dialogue_kind==='silence'?'本镜头无台词，以表情和动作推进剧情。':shot.speaker+'：'+shot.dialogue;
      var characterOptions=(preview.characters||[]).map(function(item){return '<option value="'+escapeHtml(item)+'"'+(item===shot.speaker?' selected':'')+'>'+escapeHtml(item)+'</option>';}).join('');
      var editor=shot.editing?'<div class="short-drama-shot-editor"><label>场景<input name="scene" value="'+escapeHtml(shot.scene)+'"></label><label>动作<textarea name="action">'+escapeHtml(shot.action)+'</textarea></label><label>表情与情绪<input name="expression" value="'+escapeHtml(shot.expression)+'"></label><label>说话方式<select name="dialogue_kind"><option value="dialogue"'+(shot.dialogue_kind==='dialogue'?' selected':'')+'>画面内对白</option><option value="voiceover"'+(shot.dialogue_kind==='voiceover'?' selected':'')+'>画外音</option><option value="silence"'+(shot.dialogue_kind==='silence'?' selected':'')+'>无台词</option></select></label><label>说话角色<select name="speaker"><option value="">无</option>'+characterOptions+'</select></label><label>具体台词<input name="dialogue" value="'+escapeHtml(shot.dialogue)+'"></label><label>镜头设计<textarea name="camera">'+escapeHtml(shot.camera)+'</textarea></label><label>环境声音<input name="sound" value="'+escapeHtml(shot.sound)+'"></label><label>衔接方式<input name="transition" value="'+escapeHtml(shot.transition)+'"></label><label>连续性<input name="continuity" value="'+escapeHtml(shot.continuity)+'"></label><div class="short-drama-shot-editor-actions"><button type="button" data-planner-shot-action="cancel" data-shot-index="'+shot.index+'">取消</button><button class="short-drama-primary" type="button" data-planner-shot-action="save" data-shot-index="'+shot.index+'">保存本镜头</button></div></div>':'';
      return '<article class="short-drama-planner-shot '+status.kind+(shot.locked?' locked':'')+'" data-shot-index="'+shot.index+'"><header><div><b>#'+shot.index+'</b><strong>'+escapeHtml(shot.phase)+'</strong><span>'+shot.duration+' 秒 · '+escapeHtml((shot.characters||[]).join('、'))+'</span></div><em>'+escapeHtml(status.label)+'</em></header><p class="short-drama-planner-dialogue">'+escapeHtml(dialogue)+'</p><small>预计朗读 '+Number(shot.reading_seconds).toFixed(2)+' 秒 · 表情动作 '+Math.max(0,Number(shot.remaining_seconds)).toFixed(2)+' 秒</small><details open><summary>查看完整镜头执行信息</summary><dl><dt>场景</dt><dd>'+escapeHtml(shot.scene)+'</dd><dt>出场角色</dt><dd>'+escapeHtml((shot.characters||[]).join('、'))+'</dd><dt>动作</dt><dd>'+escapeHtml(shot.action)+'</dd><dt>表情</dt><dd>'+escapeHtml(shot.expression)+'</dd><dt>说话角色</dt><dd>'+escapeHtml(shot.speaker||'无')+'</dd><dt>说话方式</dt><dd>'+escapeHtml(shot.dialogue_kind==='voiceover'?'画外音':shot.dialogue_kind==='silence'?'无台词':'画面内对白')+'</dd><dt>镜头</dt><dd>'+escapeHtml(shot.camera)+'</dd><dt>环境声音</dt><dd>'+escapeHtml(shot.sound)+'</dd><dt>衔接</dt><dd>'+escapeHtml(shot.transition)+'</dd><dt>连续性</dt><dd>'+escapeHtml(shot.continuity)+'</dd></dl></details><div class="short-drama-shot-actions"><button type="button" data-planner-shot-action="edit" data-shot-index="'+shot.index+'"'+(shot.locked?' disabled':'')+'>编辑</button><button type="button" data-planner-shot-action="regenerate" data-shot-index="'+shot.index+'"'+(shot.locked?' disabled':'')+'>重生成本镜头</button><button type="button" data-planner-shot-action="mute" data-shot-index="'+shot.index+'"'+(shot.locked?' disabled':'')+'>改成无台词</button><button type="button" data-planner-shot-action="lock" data-shot-index="'+shot.index+'">'+(shot.locked?'解锁':'锁定')+'</button></div>'+editor+'</article>';
    }
    function renderScriptPreview(preview){
      var node=doc.getElementById('shortDramaScriptPreview');
      if(!preview){node.hidden=true;node.innerHTML='';return;}
      var quality=preview.quality||plannerQuality(preview),gate=quality.blocking?'<div class="short-drama-planner-gate blocked"><strong>还有 '+quality.blockers.length+' 个镜头不能确认</strong><span>'+quality.blockers.map(function(item){return '镜头 '+item.index+'：'+item.message;}).join('；')+'</span></div>':'<div class="short-drama-planner-gate pass"><strong>逐镜时长检查通过</strong><span>台词、表情动作和镜头时长均可执行。</span></div>';
      node.innerHTML='<article class="short-drama-planner-overview"><span class="short-drama-eyebrow">逐镜完整剧本</span><h3>'+escapeHtml(preview.title)+'</h3><p>'+escapeHtml(preview.logline)+'</p><dl><dt>主要角色</dt><dd>'+escapeHtml((preview.characters||[]).join('、'))+'</dd><dt>核心冲突</dt><dd>'+escapeHtml(preview.conflict)+'</dd><dt>结局</dt><dd>'+escapeHtml(preview.ending)+'</dd><dt>制作规格</dt><dd>'+escapeHtml(preview.ratio)+' · '+preview.duration_seconds+' 秒 · '+preview.shot_count+' 镜</dd></dl>'+gate+'<div class="short-drama-planner-shots">'+(preview.shots||[]).map(function(shot){return renderPlannerShot(shot,preview);}).join('')+'</div></article>';
      node.hidden=false;doc.getElementById('shortDramaPlannerCanvasTitle').textContent='完整剧本方案待确认';renderPlanner();
    }
    function plannerNotice(message,error){var node=doc.getElementById('shortDramaPlannerNotice');node.textContent=message||'';node.classList.toggle('error',!!error);}
    function setCreateHeading(eyebrow,title,lead){
      doc.getElementById('shortDramaCreateEyebrow').textContent=eyebrow;
      doc.getElementById('shortDramaCreateTitle').textContent=title;
      doc.getElementById('shortDramaCreateLead').textContent=lead;
    }
    function showCreateStep(step){
      startOptions.hidden=step!=='choice';inspiration.hidden=step!=='inspiration';form.hidden=step!=='idea';importSection.hidden=step!=='import';
      if(step==='choice') setCreateHeading('NEW PROJECT','你想怎样开始？','选择最符合当前状态的方式，后面的制作流程完全一致。');
      if(step==='idea') setCreateHeading(createMode==='inspiration'?'START WITH GUIDANCE':'CREATE WITH AN IDEA',createMode==='inspiration'?'先填写基本创作边界':'创建短剧设置',createMode==='inspiration'?'只需填写题材线索和制作规格，下一步由助手与你一起完成剧本。':'先填写已有想法和制作规格，下一步仍会经过助手讨论与剧本确认。');
      if(step==='inspiration') setCreateHeading('CREATIVE ADVISOR','前置剧本策划','先聊想法、选择结构化方案并确认完整剧本；确认后才创建正式项目。');
      if(step==='import') setCreateHeading('IMPORT A SCRIPT','导入已有剧本','上传文件或粘贴原稿，助手会先识别内容，再与你确认如何成片。');
    }
    function resetCreate(){
      form.reset();ideaMessages=[];chat.innerHTML='';recommendations.innerHTML='';recommendations.hidden=true;createMode='idea';plannerPayload=null;selectedDirection=null;plannerPreview=null;pendingCreateKey='';
      importText.value='';importFile.value='';importFilename='';importAnalysis=null;pendingImportKey='';importEditor.hidden=false;importForm.hidden=true;importForm.reset();
      doc.getElementById('shortDramaImportCount').textContent='0';doc.getElementById('shortDramaImportFileName').hidden=true;doc.getElementById('shortDramaImportError').hidden=true;
      doc.getElementById('shortDramaSelectedDirection').hidden=true;
      var opening=advisorStep([]);chatBubble('assistant',opening.message);renderQuickReplies(opening.quick);renderScriptPreview(null);plannerNotice('',false);renderPlanner();
      showCreateStep('choice');
    }
    function openCreate(){resetCreate();dialog.showModal();}
    function submitIdea(value){
      value=compactIdea(value);if(!value)return;
      if(plannerPreview){
        plannerPreview=null;renderScriptPreview(null);
        plannerNotice('创作要求已更新，请重新生成剧本方案后再确认。',false);
      }
      chatBubble('user',value);ideaMessages.push(value);ideaInput.value='';
      var reply=advisorStep(ideaMessages);chatBubble('assistant',reply.message);
      renderQuickReplies(reply.quick||[]);renderRecommendations(reply.recommendations||[]);
      renderPlanner();
    }
    function selectRecommendation(id){
      var selected=buildRecommendations(ideaMessages).find(function(item){return item.id===id;});if(!selected)return;
      selectedDirection=selected;plannerPreview=null;renderScriptPreview(null);
      doc.getElementById('shortDramaPlannerCanvasTitle').textContent='已选择 '+selected.title;
      chatBubble('user','采用 '+selected.label+'：'+selected.title);
      chatBubble('assistant','方向已记录。你可以继续补充要求，或生成完整剧本方案后进行人工确认。');
      renderPlanner();
    }
    function startPlanner(){
      plannerPayload=createPayload(form);ideaMessages=[];selectedDirection=null;plannerPreview=null;pendingCreateKey='';
      chat.innerHTML='';recommendations.innerHTML='';recommendations.hidden=true;renderScriptPreview(null);plannerNotice('',false);
      chatBubble('assistant',createMode==='inspiration'?'我会先从你给出的线索出发，再通过几个选择帮你找到故事方向。':'我已收到基本设定。接下来一起确认情绪、结局和故事方向，确认后才创建项目。');
      if(plannerPayload.synopsis){chatBubble('user',plannerPayload.synopsis);ideaMessages.push(plannerPayload.synopsis);}
      var reply=advisorStep(ideaMessages);chatBubble('assistant',reply.message);renderQuickReplies(reply.quick||[]);renderRecommendations(reply.recommendations||[]);
      showCreateStep('inspiration');renderPlanner();
    }
    function generatePlannerScript(){
      if(!selectedDirection)return;
      plannerPreview=buildPlannerPreview(plannerPayload,ideaMessages,selectedDirection);
      renderScriptPreview(plannerPreview);plannerNotice('逐镜完整剧本已生成。请检查每个镜头的角色、台词、表情动作、镜头和声音，再确认创建项目。',false);
    }
    function handlePlannerShotAction(event){
      var button=event.target.closest('[data-planner-shot-action]');if(!button||!plannerPreview)return;
      var index=Number(button.getAttribute('data-shot-index')),shot=(plannerPreview.shots||[]).find(function(item){return Number(item.index)===index;});
      if(!shot)return;var action=button.getAttribute('data-planner-shot-action');
      if(action==='edit'){shot.editing=true;renderScriptPreview(plannerPreview);return;}
      if(action==='cancel'){shot.editing=false;renderScriptPreview(plannerPreview);return;}
      if(action==='lock'){shot.locked=!shot.locked;shot.editing=false;renderScriptPreview(plannerPreview);return;}
      if(shot.locked)return;
      if(action==='mute'){
        shot.dialogue_kind='silence';shot.speaker='';shot.dialogue='';shot.editing=false;
      }else if(action==='regenerate'){
        var replacement=plannerShot(index-1,plannerPreview.shot_count,shot.duration,plannerPreview.characters||[],plannerPreview.conflict,plannerPreview.ending,Number(shot.variation||0)+1);
        plannerPreview.shots[index-1]=replacement;
      }else if(action==='save'){
        var card=button.closest('[data-shot-index]');if(!card)return;
        function field(name){var input=card.querySelector('[name="'+name+'"]');return text(input&&input.value).trim();}
        shot.scene=field('scene');shot.action=field('action');shot.expression=field('expression');shot.dialogue_kind=field('dialogue_kind')||'silence';
        shot.speaker=shot.dialogue_kind==='silence'?'':field('speaker');shot.dialogue=shot.dialogue_kind==='silence'?'':field('dialogue');
        shot.camera=field('camera');shot.sound=field('sound');shot.transition=field('transition');shot.continuity=field('continuity');shot.editing=false;
      }
      plannerPreview.quality=plannerQuality(plannerPreview);renderScriptPreview(plannerPreview);
      plannerNotice(plannerPreview.quality.blocking?'仍有镜头未通过时长或执行信息检查，请继续调整。':'当前逐镜剧本已通过时长检查，可以确认创建项目。',plannerPreview.quality.blocking);
    }
    function ensurePlanningMessage(workspace,message,index,projectId){
      var exists=(workspace.messages||[]).some(function(item){return item.role==='user'&&compactIdea(item.content)===compactIdea(message);});
      if(exists)return Promise.resolve(workspace);
      return client.message({project_id:projectId,conversation_revision:Number(workspace.conversation.revision),message:message},'preproject-'+projectId+'-message-'+index);
    }
    function promotePlannerProject(){
      if(!plannerPreview)return Promise.resolve();
      plannerPreview.quality=plannerQuality(plannerPreview);
      if(plannerPreview.quality.blocking){renderScriptPreview(plannerPreview);plannerNotice('请先处理所有超时台词或缺失的动作、表情，再确认创建项目。',true);return Promise.resolve();}
      var button=doc.getElementById('shortDramaConfirmScript');button.disabled=true;plannerNotice('正在建立正式项目并固化已确认剧本…',false);
      var contract=plannerConfirmedContract(plannerPreview);
      if(!pendingCreateKey)pendingCreateKey=newProjectKey();
      return client.promote({
        project:plannerPayload,
        planning_messages:plannerPromotionMessages(plannerPreview),
        confirmed_contract:contract
      },pendingCreateKey).then(function(result){
        var project=result&&result.project;
        if(!project||!project.id)throw new Error('服务端未返回已确认的短剧项目');
        plannerNotice('剧本已确认，正在进入正式项目。',false);
        if(runtimeRoot.location)runtimeRoot.location.href=projectUrl(project.id);
      }).catch(function(error){plannerNotice(error.message||'创建项目失败，可直接重试，系统不会重复发送已保存的策划内容。',true);button.disabled=false;});
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
      var mode=node.getAttribute('data-create-mode');createMode=mode;showCreateStep(mode==='import'?'import':'idea');
    });});
    doc.querySelectorAll('[data-action="back-create-settings"]').forEach(function(node){node.addEventListener('click',function(){showCreateStep('idea');});});
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
    doc.getElementById('shortDramaGeneratePreview').addEventListener('click',generatePlannerScript);
    doc.getElementById('shortDramaScriptPreview').addEventListener('click',handlePlannerShotAction);
    doc.getElementById('shortDramaConfirmScript').addEventListener('click',promotePlannerProject);
    doc.querySelector('[data-action="close-drawer"]').addEventListener('click',function(){selectedProjectId='';drawer.hidden=true;});
    deleteButton.addEventListener('click',deleteSelectedProject);
    doc.getElementById('shortDramaSearch').addEventListener('input',render);doc.getElementById('shortDramaStageFilter').addEventListener('change',render);
    grid.addEventListener('click',function(event){var card=event.target.closest('[data-project-id]');if(card)showProject(card.getAttribute('data-project-id'));});
    grid.addEventListener('keydown',function(event){var card=event.target.closest('[data-project-id]');if(card&&(event.key==='Enter'||event.key===' ')){event.preventDefault();showProject(card.getAttribute('data-project-id'));}});
    form.addEventListener('submit',function(event){
      event.preventDefault();startPlanner();
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
  return {STAGES:STAGES,LABELS:LABELS,normalizeProject:normalizeProject,progress:progress,filterProjects:filterProjects,metrics:metrics,deleteErrorMessage:deleteErrorMessage,createPayload:createPayload,compactIdea:compactIdea,buildRecommendations:buildRecommendations,advisorStep:advisorStep,plannerProgress:plannerProgress,plannerDurations:plannerDurations,plannerRoles:plannerRoles,plannerReadingSeconds:plannerReadingSeconds,plannerQuality:plannerQuality,buildPlannerPreview:buildPlannerPreview,plannerPromotionMessages:plannerPromotionMessages,plannerConfirmedContract:plannerConfirmedContract,confirmedContractMatches:confirmedContractMatches,continuePlannerContract:continuePlannerContract,importedTitle:importedTitle,analyzeImportedScript:analyzeImportedScript,importProjectPayload:importProjectPayload,newImportKey:newImportKey,newProjectKey:newProjectKey,readLimitedStream:readLimitedStream,extractPdfText:extractPdfText,extractDocxText:extractDocxText,readScriptFile:readScriptFile,createClient:createClient,projectUrl:projectUrl,cardHtml:cardHtml,mount:mount};
});
