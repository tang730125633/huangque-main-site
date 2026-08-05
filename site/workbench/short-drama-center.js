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
  var PLANNER_DRAFT_VERSION=4;
  var PLANNER_DRAFT_MAX_AGE=30*24*60*60*1000;
  var PLANNER_FIELDS=['topic','protagonist','conflict','emotion','ending','audience','style'];
  var PLANNER_FIELD_LABELS={topic:'故事主题',protagonist:'主角',conflict:'核心冲突',emotion:'目标情绪',ending:'结局',audience:'目标观众',style:'视觉风格'};
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
  function plannerUnderstanding(messages,payload,answers){
    messages=(messages||[]).map(compactIdea).filter(Boolean);payload=payload||{};answers=answers||{};
    function supplied(key){return Object.prototype.hasOwnProperty.call(answers,key);}
    var source=messages.join('；'),roles=plannerRoles(messages,null);
    var emotion=supplied('emotion')?text(answers.emotion):(source.match(/温暖治愈|温暖|治愈|紧张悬疑|紧张|悬疑|爽感反击|爽感|反击|笑中带泪|感动|压迫|轻松|悲伤/)||[])[0]||'';
    var ending=supplied('ending')?text(answers.ending):(source.match(/温暖圆满|圆满|合理反转|反转|克制留白|留白|人物成长|成长|悲剧|开放式/)||[])[0]||'';
    var conflict=supplied('conflict')?text(answers.conflict):(messages.find(function(item){return /冲突|必须|却|但是|无法|不能|被迫|困|误会|真相|选择/.test(item)&&item!==messages[0];})||'');
    var audience=supplied('audience')?text(answers.audience):(source.match(/女性|男性|年轻人|家庭观众|职场人|学生|大众|亲子/)||[])[0]||'';
    var protagonist=supplied('protagonist')?text(answers.protagonist):'';
    if(!protagonist&&roles[0]&&roles[0]!=='主人公')protagonist=roles[0];
    return {
      topic:text(supplied('topic')?answers.topic:(payload.synopsis||messages[0])).trim(),protagonist:protagonist,
      conflict:conflict,emotion:text(emotion).trim(),ending:text(ending).trim(),
      audience:audience,style:text(supplied('style')?answers.style:(payload.visual_style||'电影感写实')).trim(),
      ratio:text(payload.ratio||'16:9'),duration:Number(payload.target_duration)||30,shot_count:Number(payload.shot_count)||6
    };
  }
  function plannerCompleteness(understanding){
    understanding=understanding||{};
    var weights={topic:20,protagonist:15,conflict:20,emotion:15,ending:15,audience:5,style:10},score=0,missing=[];
    Object.keys(weights).forEach(function(key){if(text(understanding[key]).trim())score+=weights[key];else missing.push(key);});
    return {score:score,missing:missing,ready:score>=80};
  }
  function buildRecommendations(messages,understanding){
    var ideas=(messages||[]).map(compactIdea).filter(Boolean);
    understanding=understanding||plannerUnderstanding(messages,{}, {emotion:ideas[1],ending:ideas[2]});
    var topic=understanding.topic||ideas[0]||'普通人的一次重要选择';
    var tone=understanding.emotion||ideas[1]||'真实、有情绪张力';
    var ending=understanding.ending||ideas[2]||'结尾带来合理反转';
    var protagonist=understanding.protagonist||'主人公',conflict=understanding.conflict||'必须完成一次重要选择';
    return [
      {
        id:'steady',label:'方案 A · 情感共鸣',title:ideaTitle(topic,0),
        premise:'围绕“'+topic+'”，从一个看似平常的关系切入，让'+protagonist+'面对“'+conflict+'”，在'+tone+'的冲突中重新理解彼此，'+ending+'。',
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
  function plannerFlowState(messages,payload,answers,meta,selected,preview,dirtyFields){
    var understanding=plannerUnderstanding(messages,payload,answers),completeness=plannerCompleteness(understanding),metaState=meta||{};
    var conflicts=PLANNER_FIELDS.filter(function(key){return metaState[key]&&metaState[key].status==='conflicted';});
    var priority=['topic','protagonist','conflict','ending','emotion','audience','style'];
    var focus=conflicts[0]||priority.find(function(key){return completeness.missing.indexOf(key)>=0;})||'';
    var phase=focus?'collect':!selected?'directions':(!preview||dirtyFields&&dirtyFields.length)?'script':'review';
    var labels={collect:'补全故事核心',directions:'选择故事方向',script:preview?'更新受影响内容':'生成完整剧本',review:'审稿确认'};
    return {phase:phase,label:labels[phase],focus_field:focus,conflicts:conflicts,missing:completeness.missing,completeness:completeness,understanding:understanding};
  }
  function plannerQuestionDefinition(field){
    return {
      topic:{message:'先不用想完整故事。你最想创作哪一类内容，或者最近有什么画面、人物让你有感觉？',quick:['家庭情感','悬疑反转','校园成长']},
      protagonist:{message:'这个方向可以展开。故事最应该跟着谁走？请说清主角的身份或处境。',quick:['普通上班族','独居老人','青春期学生']},
      conflict:{message:'主角现在最想得到什么？又是什么人或事情拦住了他？',quick:['必须隐瞒真相','关系即将破裂','时间只剩一天']},
      emotion:{message:'你希望观众看完是什么感受？',quick:['温暖治愈','紧张悬疑','爽感反击']},
      ending:{message:'你偏好什么结局？',quick:['温暖圆满','合理反转','克制留白']},
      audience:{message:'最后确认一下，这个故事主要想给谁看？',quick:['大众观众','年轻人','家庭观众']}
    }[field]||null;
  }
  function plannerGuidanceReason(field,index,understanding){
    var reasons={
      topic:['人物关系清楚，容易建立情感共鸣','线索和反转鲜明，适合持续制造悬念','成长轨迹直观，年轻观众容易代入'],
      protagonist:['现实压力集中，人物目标容易讲清楚','生活反差明显，适合细腻的情感表达','成长矛盾直接，行动和变化都比较鲜明'],
      conflict:['秘密会持续推动剧情并制造悬念','关系危机会放大人物情绪和选择','时间压力能让每个镜头都有明确任务'],
      emotion:['适合用人物关系产生稳定共鸣','适合用未知信息保持观看动力','适合用明确目标和反击制造情绪释放'],
      ending:['情绪回报完整，适合温暖题材','能提升记忆点和传播讨论度','余味更长，适合克制的电影感表达'],
      audience:['理解门槛较低，覆盖面更广','节奏和议题更贴近年轻用户','人物关系更适合共同观看和讨论']
    };
    var context=understanding&&understanding.topic?('，也能延续“'+understanding.topic+'”的故事基础'):'';
    return ((reasons[field]||['方向清晰，便于继续细化','冲突更强，适合推动剧情','更有反差，适合形成记忆点'])[index]||'可以形成清楚的创作方向')+context;
  }
  function plannerGuidedQuestion(field,question,items,understanding,fillDefaults){
    var choices=[];
    (items||[]).forEach(function(item){var value=compactIdea(item);if(value&&choices.indexOf(value)<0&&choices.length<3)choices.push(value);});
    var fallback=plannerQuestionDefinition(field);
    if(fillDefaults!==false)(fallback&&fallback.quick||[]).forEach(function(item){if(choices.indexOf(item)<0&&choices.length<3)choices.push(item);});
    if(!choices.length)return {message:question,quick:[]};
    var lines=[question,'','我建议从以下 '+choices.length+' 个方向考虑：'];
    choices.forEach(function(item,index){lines.push(['①','②','③'][index]+' '+item+'：'+plannerGuidanceReason(field,index,understanding));});
    lines.push('','你更倾向哪个方向？也可以直接说说自己的想法。');
    return {message:lines.join('\n'),quick:choices};
  }
  function plannerChoiceIndex(value){
    var token=compactIdea(value).replace(/[\s，,。.!！?？:：]/g,'');
    var match=token.match(/^(?:我)?(?:选(?:择)?|要|采用)?(?:方向|方案)?(?:第)?([0-9]+|[一二三①②③])(?:个|项|种|号|方向|方案)?$/);
    if(!match)return 0;
    return {'一':1,'①':1,'二':2,'②':2,'三':3,'③':3}[match[1]]||Number(match[1])||0;
  }
  function plannerResolveChoice(value,context){
    var clean=compactIdea(value),index=plannerChoiceIndex(clean),items=context&&Array.isArray(context.items)?context.items:[];
    if(!index)return {matched:false,valid:true,value:clean,index:0,choice:''};
    if(!items.length||index>items.length)return {matched:true,valid:false,value:clean,index:index,choice:'',available:items.length};
    var choice=compactIdea(items[index-1]);
    return {matched:true,valid:true,index:index,choice:choice,value:'我选择方向 '+index+'：'+choice+'。',available:items.length};
  }
  function advisorStep(messages,payload,answers,meta){
    var flow=plannerFlowState(messages,payload,answers,meta,null,null,[]),understanding=flow.understanding,state=flow.completeness,field=flow.focus_field;
    var question=plannerQuestionDefinition(field);
    if(flow.conflicts.length){
      var conflictMeta=meta[field]||{},conflict=conflictMeta.conflict||{};
      var conflictChoices=[text(conflict.existing_value),text(conflict.proposed_value)].filter(Boolean);
      var conflictTurn=plannerGuidedQuestion(field,'关于'+PLANNER_FIELD_LABELS[field]+'，我现在有两个不同理解。你希望最终采用哪一个？',conflictChoices,understanding,false);
      return {field:field,message:conflictTurn.message,quick:conflictTurn.quick,understanding:understanding,completeness:state,flow:flow};
    }
    if(field&&question){var guided=plannerGuidedQuestion(field,question.message,question.quick,understanding);return {field:field,message:guided.message,quick:guided.quick,understanding:understanding,completeness:state};}
    return {message:'我已经理解了故事核心，并整理出三种不同力度的方向。请选择一个，也可以继续补充要求。',recommendations:buildRecommendations(messages,understanding),understanding:understanding,completeness:state};
  }
  function plannerLocalIntent(value){
    value=compactIdea(value);
    if(!value)return 'unknown';
    if(/^(撤销|撤回|取消上次|回到上一步|恢复刚才)/.test(value))return 'undo';
    if(/^(不要|取消|清除|删掉|去掉)/.test(value))return 'negate';
    if(/(?:改成|换成|调整为|还是用|应该是)/.test(value))return 'modify';
    if(/^(不知道|没想好|不确定|随便|都可以|你来定|你决定)$/.test(value))return 'unknown';
    if(/你觉得|你建议|你推荐|帮我(?:想|选|推荐)|有什么建议|哪个好|怎么办|如何/.test(value))return 'ask_recommendation';
    if(/[？?]$/.test(value))return 'question';
    return 'answer';
  }
  function plannerLocalField(value){
    value=compactIdea(value);
    if(/温暖|治愈|悬疑|紧张|压迫|爽感|反击|笑中带泪|悲伤|轻松/.test(value))return 'emotion';
    if(/结局|圆满|反转|留白|开放式|成长|悲剧/.test(value))return 'ending';
    if(/观众|年轻人|职场人|家庭|女性向|男性向|大众/.test(value))return 'audience';
    if(/风格|写实|电影感|动漫|国风|赛博|纪实/.test(value))return 'style';
    if(/主角|女主|男主|主人公/.test(value))return 'protagonist';
    if(/冲突|必须|阻止|隐瞒|来不及|只剩|无法|不能|却|但是/.test(value))return 'conflict';
    return '';
  }
  function plannerLocalFieldUpdates(value,expectedField,current){
    value=compactIdea(value);current=current||{};var updates=[],seen={};
    function add(field,fieldValue,confidence,evidence,status){fieldValue=compactIdea(fieldValue);if(!fieldValue||seen[field])return;seen[field]=true;updates.push({field:field,operation:'set',value:fieldValue,confidence:confidence,evidence:evidence||value,status:status||(confidence>=.8?'confirmed':'inferred')});}
    var topic=(value.match(/(?:拍|写|做)(?:一个|一部)?(.{2,32}?)(?:的故事|短剧)/)||[])[1];if(topic)add('topic',topic,.9,topic+'的故事');
    var protagonist=(value.match(/((?:刚|已经|正在|独自|独居)?[^，。；]{0,14}(?:女主|男主|主角|主人公)[^，。；]{0,14})/)||[])[1];
    if(protagonist)add('protagonist',protagonist.replace(/^我想(?:拍|写|做)(?:一个|一部)?/,''),.84,protagonist);
    var emotion=(value.match(/温暖治愈|温暖|治愈|紧张悬疑|紧张|悬疑|爽感反击|爽感|反击|笑中带泪|压迫|轻松|悲伤/)||[])[0];if(emotion)add('emotion',emotion,.94,emotion);if(emotion&&/最后|结尾|结局/.test(value))add('ending',emotion,.72,(value.match(/(?:最后|结尾|结局)[^，。；]{0,18}/)||[])[0]||emotion,'inferred');
    var ending=(value.match(/温暖圆满|圆满|合理反转|反转|克制留白|留白|开放式结局|人物成长|悲剧结局/)||[])[0];if(ending&&(/结局|最后|结尾/.test(value)||!emotion))add('ending',ending,.82,ending);
    var audience=(value.match(/年轻人|家庭观众|职场人|学生群体|女性观众|男性观众|大众观众/)||[])[0];if(audience)add('audience',audience,.94,audience);
    var style=(value.match(/电影感写实|现实主义电影感|电影感|写实|动漫风格|国风|赛博朋克|纪实/)||[])[0];if(style)add('style',style,.92,style);
    var conflict=(value.match(/([^，。；]{0,20}(?:必须|想要|希望|需要)[^，。；]{2,30}(?:但|却|可是|然而|被|无法|不能)[^，。；]{2,30})/)||[])[1];if(conflict)add('conflict',conflict,.88,conflict);
    if(!updates.length&&expectedField)add(expectedField,value,.72,value,'inferred');
    updates.forEach(function(update){if(current[update.field]&&current[update.field]!==update.value&&/也可以|或者|可能|不确定/.test(value))update.status='conflicted';});
    return updates;
  }
  function plannerLocalAdvice(value,expectedField,current){
    var intent=plannerLocalIntent(value),options={
      topic:['家庭情感','悬疑反转','校园成长','职场现实'],
      protagonist:['普通上班族','独居老人','青春期学生','新手妈妈'],
      conflict:['必须隐瞒真相','关系即将破裂','时间只剩一天','一次无法回避的选择'],
      emotion:['温暖治愈','紧张悬疑','爽感反击','笑中带泪'],
      ending:['温暖圆满','合理反转','克制留白','人物成长'],
      audience:['大众观众','年轻人','家庭观众','职场人']
    },labels={topic:'题材方向',protagonist:'主角身份',conflict:'核心冲突',emotion:'观看感受',ending:'结局',audience:'目标观众'};
    if(intent==='undo')return {intent:'undo',reply:'我会撤销你上一次对创作设定的修改。',recap:'已请求撤销上一次修改。',field_updates:[],extracted_fields:{},confidence:1,quick_replies:[],mode:'basic',degraded:true};
    if(intent==='negate'){
      var revised=(compactIdea(value).match(/(?:改成|换成|调整为|还是用)\s*(.+)$/)||[])[1]||'',field=plannerLocalField(revised||value)||expectedField,revisedFields={};
      if(revised&&field)revisedFields[field]=revised;
      return {intent:revised?'modify':'negate',reply:revised?'明白，我会取消原设定并使用你刚说的新设定。':'明白，我先取消这项设定，右侧摘要可以继续修改。',recap:revised?'已替换当前讨论的'+(labels[field]||'设定')+'。':'已取消当前讨论的'+(labels[field]||'设定')+'。',field_updates:field?[{field:field,operation:revised?'set':'clear',value:revised,confidence:.88,evidence:value,status:revised?'confirmed':'removed'}]:[],extracted_fields:revisedFields,confidence:.88,quick_replies:revised?[]:(options[field]||[]),next_action:'continue',focus_field:field,mode:'basic',degraded:true};
    }
    if(intent==='ask_recommendation'||intent==='question'||intent==='unknown'){
      var quick=options[expectedField]||[];
      return {intent:intent,reply:'可以，我先给你几个适合的'+(labels[expectedField]||'故事')+'方案。你可以直接选一个，也可以在此基础上修改。',recap:'当前已确认的设定保持不变。',field_updates:[],extracted_fields:{},confidence:1,quick_replies:quick,next_action:'ask',focus_field:expectedField,mode:'basic',degraded:true};
    }
    var updates=plannerLocalFieldUpdates(value,expectedField,current),fields={};updates.forEach(function(update){fields[update.field]=update.value;});
    return {intent:intent==='modify'?'modify':'answer',reply:updates.length>1?'明白，我已经一次记下你刚才说的多个设定。':'明白，我已经记下这个信息。',recap:'已更新'+updates.map(function(update){return labels[update.field]||PLANNER_FIELD_LABELS[update.field];}).join('、')+'。',field_updates:updates,extracted_fields:fields,confidence:updates.length?Math.min.apply(null,updates.map(function(update){return update.confidence;})):.5,quick_replies:[],next_action:'continue',focus_field:expectedField,mode:'basic',degraded:true};
  }
  function applyAdvisorResult(answers,result){
    var next=Object.assign({},answers||{}),intent=text(result&&result.intent).toLowerCase();
    if(['answer','modify','negate','confirm'].indexOf(intent)<0)return next;
    var updates=result&&result.field_updates;
    if(Array.isArray(updates)&&updates.length){
      updates.forEach(function(update){
        var field=text(update&&update.field),operation=text(update&&update.operation||'set').toLowerCase(),confidence=Number(update&&update.confidence);
        if(PLANNER_FIELDS.indexOf(field)<0||confidence<.65)return;
        if(operation==='clear')next[field]='';
        if(operation==='set'&&compactIdea(update.value))next[field]=compactIdea(update.value);
      });
      return next;
    }
    if(Number(result&&result.confidence)<.65)return next;
    Object.keys(result&&result.extracted_fields||{}).forEach(function(key){
      var value=compactIdea(result.extracted_fields[key]);
      if(PLANNER_FIELDS.indexOf(key)>=0&&value)next[key]=value;
    });
    return next;
  }
  function plannerMetaSnapshot(meta){
    var snapshot={};Object.keys(meta||{}).forEach(function(key){snapshot[key]=Object.assign({},meta[key]);});return snapshot;
  }
  function applyAdvisorMetadata(meta,result){
    var next=plannerMetaSnapshot(meta),conflicts={};
    (result&&result.conflicts||[]).forEach(function(item){if(item&&PLANNER_FIELDS.indexOf(item.field)>=0&&item.requires_confirmation)conflicts[item.field]=item;});
    (result&&result.field_updates||[]).forEach(function(update){
      var field=text(update&&update.field),confidence=Number(update&&update.confidence),operation=text(update&&update.operation||'set');if(PLANNER_FIELDS.indexOf(field)<0||confidence<.65)return;
      var status=text(update.status||'');if(['confirmed','inferred','suggested','conflicted','removed'].indexOf(status)<0)status=confidence>=.8?'confirmed':'inferred';if(conflicts[field])status='conflicted';if(operation==='clear')status='removed';
      next[field]={status:status,confidence:confidence,evidence:text(update.evidence).slice(0,200),conflict:conflicts[field]||null};
    });
    return next;
  }
  function plannerConversationAudit(transcript,feedback,meta,correctionCount){
    var questions={},repeats=0;
    (transcript||[]).forEach(function(item){
      if(item&&item.role==='assistant'&&/[？?]\s*$/.test(text(item.message))){var normalized=compactIdea(item.message).replace(/[？?]/g,'');questions[normalized]=(questions[normalized]||0)+1;}
    });
    Object.keys(questions).forEach(function(key){if(questions[key]>1)repeats+=questions[key]-1;});
    var negative=(feedback||[]).filter(function(item){return item&&item.rating==='wrong';}).length;
    var conflicts=Object.keys(meta||{}).filter(function(key){return meta[key]&&meta[key].status==='conflicted';}).length;
    var corrections=Math.max(0,Number(correctionCount)||0),score=Math.max(0,100-repeats*15-negative*20-conflicts*15-Math.max(0,corrections-3)*3);
    var parts=[];if(repeats)parts.push('重复追问 '+repeats+' 次');if(negative)parts.push('理解错误反馈 '+negative+' 次');if(conflicts)parts.push('待解决冲突 '+conflicts+' 项');if(corrections)parts.push('用户修正 '+corrections+' 次');
    return {score:score,repeated_questions:repeats,negative_feedback:negative,conflicts:conflicts,corrections:corrections,summary:parts.length?parts.join(' · '):'尚未发现重复追问或理解冲突'};
  }
  function plannerAnswerSnapshot(answers){
    var snapshot={};PLANNER_FIELDS.forEach(function(key){if(answers&&Object.prototype.hasOwnProperty.call(answers,key))snapshot[key]=compactIdea(answers[key]);});return snapshot;
  }
  function plannerChangedFields(before,after){
    return PLANNER_FIELDS.filter(function(key){return text(before&&before[key])!==text(after&&after[key]);});
  }
  function plannerRecap(before,after,result){
    var changed=plannerChangedFields(before,after),parts=[];
    changed.forEach(function(key){parts.push(after[key]?PLANNER_FIELD_LABELS[key]+'改为“'+after[key]+'”':'已取消'+PLANNER_FIELD_LABELS[key]);});
    if(!parts.length&&result&&result.recap)return text(result.recap);
    if(!parts.length)return '当前理解没有变化；已确认的设定继续保留。';
    var missing=plannerCompleteness(plannerUnderstanding([],{},after)).missing;
    return '我的理解：'+parts.join('；')+'。'+(missing.length?'还需要确认：'+missing.slice(0,3).map(function(key){return PLANNER_FIELD_LABELS[key];}).join('、')+'。':'关键信息已经齐全。');
  }
  function plannerAssistantTurn(parts){
    var messages=[];
    (parts||[]).forEach(function(part){
      var message=text(part).trim();
      if(message&&messages.indexOf(message)<0)messages.push(message);
    });
    return messages.join('\n\n');
  }
  function plannerProgress(messages,selected,preview,payload,answers){
    var interview=plannerCompleteness(plannerUnderstanding(messages,payload,answers)).score;
    var score=preview?100:Math.min(95,Math.round(interview*.75)+(selected?20:0));
    return {score:score,label:preview?'剧本待确认':selected?'方向待生成':'正在理解想法'};
  }
  function plannerAffectedLayers(fields){
    var layers=[];function add(name){if(layers.indexOf(name)<0)layers.push(name);}
    (fields||[]).forEach(function(field){
      if(['topic','protagonist','conflict','ending'].indexOf(field)>=0){add('story');add('scenes');add('shots');}
      else if(['emotion','audience'].indexOf(field)>=0){add('scenes');add('shots');}
      else if(field==='style')add('shots');
    });
    return layers;
  }
  function rebuildPlannerPreview(previous,fresh,layers){
    if(!previous||!layers||layers.indexOf('story')>=0)return fresh;
    if(layers.indexOf('scenes')>=0){
      fresh.story_plan=Object.assign({},previous.story_plan,{emotion:fresh.story_plan&&fresh.story_plan.emotion,audience:fresh.story_plan&&fresh.story_plan.audience});
      fresh.logline=previous.logline;fresh.conflict=previous.conflict;fresh.ending=previous.ending;fresh.characters=(previous.characters||[]).slice();
      return fresh;
    }
    if(layers.length===1&&layers[0]==='shots'){
      fresh.story_plan=previous.story_plan;fresh.scenes=previous.scenes;fresh.logline=previous.logline;fresh.conflict=previous.conflict;fresh.ending=previous.ending;fresh.characters=(previous.characters||[]).slice();
    }
    return fresh;
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
  function plannerStoryClauses(value){
    return text(value).replace(/\r/g,'').split(/[。！？!?；;\n]+/).map(function(item){return compactIdea(item).replace(/^(故事讲述|讲述|围绕)/,'');}).filter(function(item){return item.length>=3;});
  }
  function plannerShortPhrase(value,limit){
    var clean=compactIdea(value).replace(/^(主角|主人公|他|她)\s*(?:必须|想要|希望|需要)?/,'').replace(/^(必须|想要|希望|需要)/,'');
    return clean.slice(0,limit||18);
  }
  function plannerStoryPlan(payload,messages,selected,understanding){
    payload=payload||{};selected=selected||{};understanding=understanding||plannerUnderstanding(messages,payload,{});
    var source=[payload.synopsis].concat(messages||[]).filter(Boolean).join('；'),clauses=plannerStoryClauses(source);
    var premise=text(selected.premise||payload.synopsis||understanding.topic||clauses[0]||'主人公面对一次重要选择').trim();
    var rawConflict=compactIdea(understanding.conflict||clauses[1]||'现实阻力迫使主角立即行动');
    var goal=plannerShortPhrase(rawConflict||premise,28)||'完成一次不能回避的选择';
    var deadline=(rawConflict.match(/在(.{1,20})前/)||[])[1],obstacle=deadline?(deadline+'即将到来，主角所剩时间不多'):rawConflict;
    if(obstacle===goal||obstacle==='必须'+goal)obstacle='现实压力与人物犹豫同时阻止目标完成';
    var turn=compactIdea(clauses.length>1?clauses[Math.floor(clauses.length/2)]:'阻力背后的真相改变了主角原来的判断');
    var ending=compactIdea(understanding.ending||clauses[clauses.length-1]||'人物用行动完成选择，关系产生变化');
    var hook=compactIdea(clauses[0]||premise).slice(0,100);
    return {
      schema_version:'short-drama-story-plan-v1',premise:premise,theme:text(understanding.topic||premise).slice(0,120),
      audience:text(understanding.audience||'短视频观众'),emotion:text(understanding.emotion||'真实、有情绪张力'),
      dramatic_question:'主角能否在阻力升级前'+goal+'？',character_goal:goal,obstacle:obstacle,
      stakes:'如果失败，主角将失去当前最重要的关系或机会。',hook:hook,turning_point:turn,
      climax:'主角不再回避，以一个可见行动回应“'+plannerShortPhrase(obstacle,24)+'”。',resolution:ending,
      acts:[
        {act:1,name:'建立与钩子',purpose:'迅速交代人物处境，并让异常事件发生。',summary:hook},
        {act:2,name:'冲突与转折',purpose:'让阻力具体升级，同时揭开改变判断的信息。',summary:obstacle+'；转折：'+turn},
        {act:3,name:'选择与兑现',purpose:'让主角用行动作出选择，并兑现目标情绪。',summary:ending}
      ]
    };
  }
  function plannerSceneLocation(source,index){
    var locations=['便利店门口','医院走廊','学校教室','办公室','家中客厅','街道','车站','餐厅','天台','公园'];
    var found=locations.find(function(item){return text(source).indexOf(item)>=0;});
    if(found)return found;
    if(/雨|下雨/.test(source))return index===0?'雨中的街道':'避雨处';
    return ['故事发生地','冲突发生地','结局空间'][Math.min(2,index)];
  }
  function plannerScenePlan(plan,roles,shotCount){
    var sceneCount=Math.min(3,Math.max(1,Math.ceil(shotCount/3))),scenes=[],cursor=1;
    for(var index=0;index<sceneCount;index++){
      var remaining=shotCount-cursor+1,count=Math.ceil(remaining/(sceneCount-index)),act=plan.acts[Math.min(2,index)],end=cursor+count-1;
      scenes.push({index:index+1,phase:act.name,location:plannerSceneLocation(plan.premise+'；'+plan.obstacle,index),characters:roles.slice(0,2),objective:act.purpose,conflict:index===0?plan.dramatic_question:index===sceneCount-1?plan.climax:plan.obstacle,turn:index===0?plan.hook:index===sceneCount-1?plan.resolution:plan.turning_point,shot_start:cursor,shot_end:end});
      cursor=end+1;
    }
    return scenes;
  }
  function plannerSourceDialogues(source){
    var results=[],matched,re=/(?:^|\n)\s*([^\s：:，,。！？!?（）()]{1,12})\s*[：:]\s*([^\n]{1,80})/g;
    while((matched=re.exec(text(source)))&&results.length<12)results.push({speaker:matched[1],text:compactIdea(matched[2]).slice(0,80)});
    return results;
  }
  function plannerDialogueSet(source,roles,plan){
    source=text(source);plan=plan||plannerStoryPlan({synopsis:source},[],{},{});var first=roles[0]||'主人公',second=roles[1]||'关键人物',supplied=plannerSourceDialogues(source);
    if(supplied.length)return supplied.map(function(item,index){return {speaker:item.speaker&&roles.indexOf(item.speaker)>=0?item.speaker:(index%2?second:first),text:item.text};});
    var goal=plannerShortPhrase(plan.character_goal,18),obstacle=plannerShortPhrase(plan.obstacle,18),turn=plannerShortPhrase(plan.turning_point,18),resolution=plannerShortPhrase(plan.resolution,18);
    var resolutionLine=/和解|圆满/.test(plan.resolution)?'我们把话说清楚。':(/成长|留白|开放/.test(plan.resolution)?'':(resolution?'我选择'+resolution+'。':''));
    if(/雨衣/.test(source))return [{speaker:'',text:''},{speaker:second,text:'这件雨衣你先用。'},{speaker:first,text:'你把雨衣给我，那你呢？'},{speaker:second,text:turn||'我等雨小一点。'},{speaker:first,text:resolution||'我会记住这份善意。'},{speaker:'',text:''}];
    return [
      {speaker:'',text:''},{speaker:first,text:goal?'我得'+goal+'。':''},{speaker:second,text:obstacle?obstacle+'。':''},
      {speaker:second,text:turn?'其实，'+turn+'。':''},{speaker:first,text:resolutionLine},{speaker:'',text:''}
    ];
  }
  function plannerPhase(index,count){
    if(index===0)return '开场钩子';if(index===count-1)return '结局兑现';
    return index<Math.ceil(count/2)?'冲突升级':'选择与转折';
  }
  function plannerShot(index,count,duration,roles,source,ending,variation,plan,scenes){
    var phase=plannerPhase(index,count),first=roles[0]||'主人公',second=roles[1]||'关键人物';
    plan=plan||plannerStoryPlan({synopsis:source},[],{},{ending:ending});scenes=scenes||plannerScenePlan(plan,roles,count);
    var scene=scenes.find(function(item){return index+1>=item.shot_start&&index+1<=item.shot_end;})||scenes[0];
    var dialogueIndex=Math.min(5,Math.floor(index*6/Math.max(1,count))),dialogue=plannerDialogueSet(source,roles,plan)[dialogueIndex]||{speaker:'',text:''};
    if(dialogue.text)dialogue.text=compactIdea(dialogue.text).slice(0,Math.max(4,Math.floor((Number(duration)-.6)*3.2)));
    var actions=[
      first+'进入'+scene.location+'，通过一个具体动作暴露当前处境：'+plannerShortPhrase(plan.hook,38),
      second+'察觉主角的目标，立刻用行动制造或呈现阻力：'+plannerShortPhrase(scene.conflict,38),
      first+'尝试推进目标，却因“'+plannerShortPhrase(plan.obstacle,28)+'”被迫改变做法',
      second+'给出关键物件、证据或动作，让信息发生转折：'+plannerShortPhrase(plan.turning_point,38),
      first+'停止犹豫，完成决定性动作：'+plannerShortPhrase(plan.climax,42),
      first+'与'+second+'用最后一个动作回应结果：'+plannerShortPhrase(plan.resolution,38)
    ];
    var expressions=['焦虑、警觉','关切、克制','惊讶、犹豫','认真、坚定','释然、感激','平静、温暖'];
    var cameras=['环境全景切人物中景，缓慢推近','双人中景，跟随关键动作轻微横移','正反打近景，停留人物表情','关键物件特写后拉回双人中景','人物近景，轻推至决定动作','稳定中景转远景，留出结尾余韵'];
    var transitions=['动作切入下一镜','沿视线方向切换','以对方反应承接','由物件特写转场','顺人物动作切换','淡出结束'];
    if(variation){actions[index%6]+='，补充一次清晰可见的反应';expressions[index%6]+='，情绪变化更明显';}
    var reading=plannerReadingSeconds(dialogue.text),remaining=Math.round((duration-reading)*100)/100;
    return {
      index:index+1,phase:phase,duration:duration,scene:scene.location,scene_index:scene.index,purpose:scene.objective,
      characters:roles.slice(0,2),action:actions[index%6],expression:expressions[index%6],
      speaker:dialogue.speaker,dialogue_kind:dialogue.text?'dialogue':'silence',dialogue:dialogue.text,
      reading_seconds:reading,remaining_seconds:remaining,camera:cameras[index%6],
      sound:/雨衣|便利店|下雨|雨天/.test(source)?'持续雨声、便利店开门提示音':'场景环境声，保持对白清晰',
      transition:transitions[index%6],continuity:index?'承接上一镜头的角色位置、服装和关键物件':'建立时间、空间、服装和关键物件基准',
      summary:index===0?plan.hook:index===count-1?plan.resolution:(index<Math.ceil(count/2)?plan.obstacle:plan.turning_point),
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
  function plannerReview(preview){
    preview=preview||{};var issues=[],dialogues={},quality=plannerQuality(preview),generic=/事情怎么会这样|先听我说|我需要一个答案|不再回避|重新开始|别担心，我有办法/;
    quality.blockers.forEach(function(item){issues.push({severity:'blocking',scope:'shot',index:item.index,code:'execution_blocked',message:item.message,repairable:true});});
    quality.warnings.forEach(function(item){issues.push({severity:'warning',scope:'shot',index:item.index,code:'performance_tight',message:item.message,repairable:true});});
    if(!preview.story_plan||!preview.story_plan.dramatic_question)issues.push({severity:'blocking',scope:'story',code:'story_plan_missing',message:'缺少故事目标与核心悬念。',repairable:false});
    if(!(preview.scenes||[]).length)issues.push({severity:'blocking',scope:'story',code:'scene_plan_missing',message:'缺少分场结构。',repairable:false});
    (preview.scenes||[]).forEach(function(scene){if(!scene.objective||!scene.turn)issues.push({severity:'warning',scope:'scene',index:scene.index,code:'scene_purpose_missing',message:'场景缺少明确任务或转折。',repairable:true});});
    (preview.shots||[]).forEach(function(shot){
      var line=compactIdea(shot.dialogue);if(!line)return;
      if(generic.test(line))issues.push({severity:'warning',scope:'shot',index:shot.index,code:'generic_dialogue',message:'对白过于模板化，缺少当前故事信息。',repairable:true});
      if(dialogues[line])issues.push({severity:'warning',scope:'shot',index:shot.index,code:'duplicate_dialogue',message:'对白与镜头 '+dialogues[line]+' 重复。',repairable:true});else dialogues[line]=shot.index;
      if(line.length>Math.max(6,Math.floor(Number(shot.duration)*4)))issues.push({severity:'warning',scope:'shot',index:shot.index,code:'dialogue_dense',message:'对白密度偏高，可能挤压表演时间。',repairable:true});
    });
    if((preview.shots||[]).filter(function(shot){return shot.dialogue;}).length>(preview.shots||[]).length*.85)issues.push({severity:'warning',scope:'story',code:'dialogue_overuse',message:'对白镜头过多，建议让部分剧情通过动作和画面表达。',repairable:true});
    var blocking=issues.filter(function(item){return item.severity==='blocking';}),warnings=issues.filter(function(item){return item.severity==='warning';});
    return {schema_version:'short-drama-script-review-v1',score:Math.max(0,100-blocking.length*25-warnings.length*6),status:blocking.length?'blocked':warnings.length?'needs_revision':'passed',blocking:blocking,warnings:warnings,issues:issues,repairable_count:issues.filter(function(item){return item.repairable;}).length};
  }
  function repairPlannerPreview(preview){
    preview=preview||{};var issues=(preview.review||plannerReview(preview)).issues||[],byShot={};
    issues.forEach(function(item){if(item.scope==='shot'){if(!byShot[item.index])byShot[item.index]=[];byShot[item.index].push(item.code);}});
    (preview.shots||[]).forEach(function(shot){
      var codes=byShot[shot.index]||[];
      if(codes.indexOf('generic_dialogue')>=0||codes.indexOf('duplicate_dialogue')>=0){shot.dialogue_kind='silence';shot.dialogue='';shot.speaker='';shot.action+='，用可见反应替代解释性对白';}
      if(codes.indexOf('dialogue_dense')>=0||codes.indexOf('execution_blocked')>=0){var max=Math.max(4,Math.floor(Number(shot.duration)*3.2));shot.dialogue=compactIdea(shot.dialogue).slice(0,max);if(!shot.dialogue){shot.dialogue_kind='silence';shot.speaker='';}}
      if(!shot.action)shot.action='角色围绕“'+plannerShortPhrase(shot.summary,36)+'”完成一个清晰可见的动作';
      if(!shot.expression)shot.expression='先克制观察，再对新信息产生明确反应';
    });
    (preview.scenes||[]).forEach(function(scene){if(!scene.objective)scene.objective='推进“'+plannerShortPhrase(scene.conflict,40)+'”';if(!scene.turn)scene.turn=scene.conflict;});
    preview.quality=plannerQuality(preview);preview.review=plannerReview(preview);return preview;
  }
  function buildPlannerPreview(payload,messages,selected,understanding){
    payload=payload||{};selected=selected||{};
    var title=text(payload.title||selected.title||'未命名短剧').trim();
    var synopsis=text(selected.premise||payload.synopsis||'').trim();
    var duration=Number(payload.target_duration)||30,shotCount=Number(payload.shot_count)||6;
    var notes=(messages||[]).map(compactIdea).filter(Boolean);
    understanding=understanding||plannerUnderstanding(notes,payload,{});
    var roles=plannerRoles(notes.concat([understanding.protagonist]),selected),protagonist=understanding.protagonist||roles[0];
    if(protagonist&&roles.indexOf(protagonist)<0)roles.unshift(protagonist);
    roles=roles.slice(0,4);
    var conflict=(understanding.conflict||synopsis||notes.join('；')||'主人公必须完成一次重要选择').slice(0,180);
    var ending=(understanding.ending||'结尾形成清晰的情绪落点').slice(0,80);
    var plan=plannerStoryPlan(payload,notes,selected,understanding),scenes=plannerScenePlan(plan,roles,shotCount);
    var beats=[],shots=[],durations=plannerDurations(duration,shotCount);
    for(var i=0;i<shotCount;i++){
      var shot=plannerShot(i,shotCount,durations[i],roles,[synopsis,conflict].concat(notes).join('；'),ending,0,plan,scenes);shots.push(shot);
      beats.push({index:shot.index,phase:shot.phase,summary:shot.summary,duration:shot.duration});
    }
    var preview={
      title:title,logline:synopsis,protagonist:protagonist,conflict:conflict,ending:ending,
      ratio:text(payload.ratio)||'16:9',duration_seconds:duration,shot_count:shotCount,
      visual_style:text(payload.visual_style)||'电影感写实',target_emotion:text(understanding.emotion),target_audience:text(understanding.audience),characters:roles,beats:beats,shots:shots,
      selected_direction_id:text(selected.id)||'steady',notes:notes,story_plan:plan,scenes:scenes,
      creative_memory:{schema_version:'short-drama-creative-memory-v1',fields:PLANNER_FIELDS.reduce(function(result,key){result[key]=text(understanding[key]).trim();return result;},{})}
    };
    preview.quality=plannerQuality(preview);preview.review=plannerReview(preview);return preview;
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
      creative_memory:{schema_version:'short-drama-creative-memory-v1',fields:PLANNER_FIELDS.reduce(function(result,key){result[key]=clean(preview.creative_memory&&preview.creative_memory.fields&&preview.creative_memory.fields[key]);return result;},{})},
      story_plan:{schema_version:'short-drama-story-plan-v1',premise:clean(preview.story_plan&&preview.story_plan.premise),theme:clean(preview.story_plan&&preview.story_plan.theme),audience:clean(preview.story_plan&&preview.story_plan.audience),emotion:clean(preview.story_plan&&preview.story_plan.emotion),dramatic_question:clean(preview.story_plan&&preview.story_plan.dramatic_question),character_goal:clean(preview.story_plan&&preview.story_plan.character_goal),obstacle:clean(preview.story_plan&&preview.story_plan.obstacle),stakes:clean(preview.story_plan&&preview.story_plan.stakes),hook:clean(preview.story_plan&&preview.story_plan.hook),turning_point:clean(preview.story_plan&&preview.story_plan.turning_point),climax:clean(preview.story_plan&&preview.story_plan.climax),resolution:clean(preview.story_plan&&preview.story_plan.resolution),acts:(preview.story_plan&&preview.story_plan.acts||[]).map(function(act){return {act:Number(act.act),name:clean(act.name),purpose:clean(act.purpose),summary:clean(act.summary)};})},
      scenes:(preview.scenes||[]).map(function(scene){return {index:Number(scene.index),phase:clean(scene.phase),location:clean(scene.location),characters:(scene.characters||[]).map(clean),objective:clean(scene.objective),conflict:clean(scene.conflict),turn:clean(scene.turn),shot_start:Number(scene.shot_start),shot_end:Number(scene.shot_end)};}),
      script_review:{schema_version:'short-drama-script-review-v1',score:Number(preview.review&&preview.review.score)||0,status:clean(preview.review&&preview.review.status),issues:(preview.review&&preview.review.issues||[]).map(function(item){return {severity:clean(item.severity),scope:clean(item.scope),index:Number(item.index)||0,code:clean(item.code),message:clean(item.message),repairable:!!item.repairable};})},
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
  function plannerWordDocumentHtml(preview,understanding){
    preview=preview||{};understanding=understanding||{};
    function row(label,value){return '<tr><th>'+escapeHtml(label)+'</th><td>'+escapeHtml(value||'—')+'</td></tr>';}
    function shot(item){
      var dialogue=item.dialogue_kind==='silence'?'无台词':(item.speaker||'未指定')+'：'+(item.dialogue||'');
      return '<h3>镜头 '+Number(item.index)+' · '+escapeHtml(item.phase)+' · '+Number(item.duration)+' 秒</h3><table>'+row('场景',item.scene)+row('出场角色',(item.characters||[]).join('、'))+row('动作',item.action)+row('表情与情绪',item.expression)+row('台词',dialogue)+row('镜头设计',item.camera)+row('环境声音',item.sound)+row('衔接',item.transition)+'</table>';
    }
    var plan=preview.story_plan||{},sceneRows=(preview.scenes||[]).map(function(scene){return '<h3>场 '+scene.index+' · '+escapeHtml(scene.phase)+'</h3><table>'+row('地点',scene.location)+row('场景任务',scene.objective)+row('场内冲突',scene.conflict)+row('转折',scene.turn)+row('镜头范围',scene.shot_start+'—'+scene.shot_end)+'</table>';}).join('');
    var review=preview.review||plannerReview(preview),reviewText='评分 '+Number(review.score)+'；状态 '+text(review.status)+'；'+(review.issues||[]).map(function(item){return (item.index?'镜头 '+item.index+'：':'')+item.message;}).join('；');
    return '<!doctype html><html><head><meta charset="utf-8"><title>'+escapeHtml(preview.title||'短剧创作需求确认书')+'</title><style>body{font-family:"Microsoft YaHei",sans-serif;margin:36pt;color:#172033}h1{font-size:24pt}h2{margin-top:24pt;border-bottom:1px solid #aaa;padding-bottom:6pt}h3{margin-top:18pt;color:#8a6215}table{width:100%;border-collapse:collapse;margin:8pt 0 14pt}th,td{border:1px solid #ccd3dd;padding:7pt;text-align:left;vertical-align:top}th{width:90pt;background:#f2f4f7}</style></head><body><h1>短剧创作需求确认书</h1><p>版本：v2　生成时间：'+escapeHtml(new Date().toLocaleString('zh-CN'))+'</p><h2>创作理解</h2><table>'+row('项目名称',preview.title)+row('一句话故事',preview.logline)+row('主角',understanding.protagonist||preview.protagonist)+row('核心冲突',preview.conflict)+row('目标情绪',understanding.emotion)+row('目标观众',understanding.audience)+row('结局',preview.ending)+row('视觉风格',preview.visual_style)+row('制作规格',(preview.ratio||'')+' · '+Number(preview.duration_seconds||0)+' 秒 · '+Number(preview.shot_count||0)+' 镜')+'</table><h2>故事策划</h2><table>'+row('核心悬念',plan.dramatic_question)+row('人物目标',plan.character_goal)+row('主要阻力',plan.obstacle)+row('中段转折',plan.turning_point)+row('高潮选择',plan.climax)+row('结局兑现',plan.resolution)+'</table><h2>分场设计</h2>'+sceneRows+'<h2>剧本审稿</h2><p>'+escapeHtml(reviewText)+'</p><h2>逐镜执行稿</h2>'+(preview.shots||[]).map(shot).join('')+'<h2>确认说明</h2><p>本文件与页面中的在线确认稿来自同一份结构化快照。确认创建项目后，该版本作为后续角色、分镜、画面、配音和成片制作的依据。</p></body></html>';
  }
  function plannerWordFilename(preview){
    var name=text(preview&&preview.title||'未命名短剧').replace(/[\\/:*?"<>|]/g,'-').slice(0,40)||'未命名短剧';
    return '短剧创作需求确认书_'+name+'_v1.doc';
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
  function importedGlobalUnderstanding(source,lines,characters){
    source=text(source);lines=lines||[];characters=characters||[];
    var narrative=lines.filter(function(line){return !/^([^\s：:，,。！？!?（）()]{1,12})\s*[：:]/.test(line)&&!(/^(场景\s*[一二三四五六七八九十\d]*|内景|外景|第.{0,8}[场幕集]|INT\.|EXT\.)/i.test(line));});
    if(!narrative.length)narrative=lines;
    function node(ratio){var item=narrative[Math.min(narrative.length-1,Math.floor(narrative.length*ratio))]||'';return compactIdea(item).slice(0,180);}
    var conflict=narrative.find(function(item){return /冲突|但是|却|不能|必须|被迫|秘密|真相|误会|选择|失去|阻止/.test(item);})||node(.3);
    var relationship=[];
    characters.slice(0,8).forEach(function(name,index){characters.slice(index+1,8).forEach(function(other){var shared=lines.filter(function(line){return line.indexOf(name)>=0&&line.indexOf(other)>=0;}).length;if(shared)relationship.push({characters:[name,other],evidence_count:shared});});});
    return {
      schema_version:'short-drama-import-global-v1',premise:node(0),setup:node(.08),development:node(.3),turning_point:node(.55),climax:node(.78),ending:node(.96),central_conflict:compactIdea(conflict).slice(0,220),
      character_arcs:characters.slice(0,8).map(function(name){var related=lines.filter(function(line){return line.indexOf(name)>=0;});return {character:name,opening:compactIdea(related[0]||'待确认').slice(0,100),ending:compactIdea(related[related.length-1]||'待确认').slice(0,100),evidence_count:related.length};}),
      relationships:relationship.slice(0,12),coverage:{source_length:source.length,line_count:lines.length,analyzed_from_start:true,analyzed_from_end:true}
    };
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
    var globalStructure=importedGlobalUnderstanding(source,lines,characters);
    var summary=lines.slice(0,8).join(' ').replace(/\s+/g,' ').slice(0,260);
    if(summary.length<8)summary=source.slice(0,260);
    return {
      title:importedTitle(source,filename),source:source,filename:text(filename),
      character_count:characters.length,characters:characters.slice(0,20),scene_count:sceneCount||1,
      dialogue_count:dialogueCount,duration:duration,shot_count:shots,warnings:warnings,
      synopsis:summary,global_structure:globalStructure,summary:'已完整扫描 '+source.length.toLocaleString()+' 字，识别到 '+characters.length+' 个人物、'+(sceneCount||1)+' 个场景，并建立开场、发展、转折、高潮和结局的全局理解。确认后助手会先与你核对整体结构。'
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
  function plannerDraftStorageKey(username){
    username=text(username).trim();
    return username?'hq-short-drama-planner-draft-v3:'+username:'';
  }
  function plannerDraftMatchesUser(draft,username){
    return !!(draft&&(draft.version===3||draft.version===PLANNER_DRAFT_VERSION)&&text(draft.username).trim()&&text(draft.username).trim()===text(username).trim());
  }
  function plannerDraftActiveChoices(draft){
    draft=draft||{};var activeField=text(draft.active_field);
    return draft.active_choices&&Array.isArray(draft.active_choices.items)?{field:text(draft.active_choices.field),items:draft.active_choices.items.map(compactIdea).filter(Boolean).slice(0,3),updated_at:Number(draft.active_choices.updated_at)||0}:{field:activeField,items:[]};
  }
  function readPlannerDraftRecord(storage,key,username,now){
    try{if(!storage||!key)return null;var raw=storage.getItem(key),draft=raw?JSON.parse(raw):null;if(!plannerDraftMatchesUser(draft,username)){if(raw)storage.removeItem(key);return null;}now=Number(now)||Date.now();if(!draft.saved_at||now-draft.saved_at>PLANNER_DRAFT_MAX_AGE){storage.removeItem(key);return null;}return draft;}catch(error){return null;}
  }
  function writePlannerDraftRecord(storage,key,draft,username){
    try{if(!storage||!key)return false;storage.setItem(key,JSON.stringify(draft));var saved=JSON.parse(storage.getItem(key)||'null');return plannerDraftMatchesUser(saved,username)&&saved.pending_create_key===draft.pending_create_key;}catch(error){return false;}
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
      me:function(){return request('/api/auth/me');},
      list:function(){return request('/api/gen/short-drama/projects?page=1&page_size=50');},
      create:function(payload,idempotencyKey){var options={method:'POST',body:payload};if(idempotencyKey)options.headers={'Idempotency-Key':idempotencyKey};return request('/api/gen/short-drama/projects',options);},
      promote:function(payload,idempotencyKey){return request('/api/gen/short-drama/projects/promote',{method:'POST',headers:{'Idempotency-Key':idempotencyKey},body:payload});},
      workspace:function(id){return request('/api/gen/short-drama/conversation?project_id='+encodeURIComponent(id));},
      advisor:function(payload){return request('/api/gen/short-drama/advisor',{method:'POST',body:payload});},
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
    var advisorSubmit=ideaForm.querySelector('button[type="submit"]'),advisorSubmitLabel=advisorSubmit?advisorSubmit.textContent:'发送',advisorThinkingNode=null,advisorThinkingTimer=null;
    var recommendations=doc.getElementById('shortDramaRecommendations'),ideaMessages=[],selectedProjectId='',importFilename='',importAnalysis=null,pendingImportKey='';
    var createMode='idea',plannerPayload=null,selectedDirection=null,plannerPreview=null,pendingCreateKey='',plannerAnswers={},plannerMeta={},plannerDirtyFields=[],plannerHistory=[],plannerTranscript=[],plannerFeedback=[],plannerCorrectionCount=0,plannerPersistenceReady=false,activePlannerField='',activePlannerChoices={field:'',items:[]},advisorBusy=false,advisorDegraded=false,plannerPanel='auto',currentUsername='';
    var LEGACY_PLANNER_DRAFT_KEY='hq-short-drama-planner-draft-v3';
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
      var identity=typeof client.me==='function'?client.me():Promise.resolve({user:{username:options.username||''}});
      return identity.then(function(auth){currentUsername=text(auth&&auth.user&&auth.user.username).trim();if(!currentUsername){var error=new Error('无法确认当前登录账号');error.status=401;throw error;}return client.list();}).then(function(result){projects=(result&&result.items)||[];setNotice('',false);render();
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
    function chatBubble(role,message,options){
      options=options||{};var entry=options.entry||{id:'planner-message-'+Date.now()+'-'+Math.random().toString(16).slice(2),role:role,message:text(message),at:Date.now()};
      if(options.record!==false)plannerTranscript.push(entry);
      var node=doc.createElement('div');node.className='short-drama-chat-bubble '+role;node.setAttribute('data-planner-message-id',entry.id);
      node.innerHTML='<span>'+(role==='assistant'?'创作助手':'你')+'</span><p>'+escapeHtml(entry.message)+'</p>'+(role==='assistant'?'<div class="short-drama-message-feedback"><small>这次理解正确吗？</small><button type="button" data-advisor-feedback="correct" aria-label="理解正确">正确</button><button type="button" data-advisor-feedback="wrong" aria-label="理解错误">理解错了</button></div>':'');
      var existingFeedback=plannerFeedback.find(function(item){return item.message_id===entry.id;});if(existingFeedback){var feedbackNode=node.querySelector('.short-drama-message-feedback');feedbackNode.classList.add('recorded');feedbackNode.innerHTML='<small>'+(existingFeedback.rating==='correct'?'已记录：理解正确':'已记录：需要修正')+'</small>';}
      chat.appendChild(node);chat.scrollTop=chat.scrollHeight;
      savePlannerDraft();return entry;
    }
    function removeAdvisorThinkingIndicator(){
      if(advisorThinkingTimer!==null){runtimeRoot.clearTimeout(advisorThinkingTimer);advisorThinkingTimer=null;}
      if(advisorThinkingNode&&advisorThinkingNode.parentNode)advisorThinkingNode.parentNode.removeChild(advisorThinkingNode);
      advisorThinkingNode=null;
    }
    function showAdvisorThinkingIndicator(){
      removeAdvisorThinkingIndicator();
      var node=doc.createElement('div');
      node.className='short-drama-chat-bubble assistant thinking';
      node.setAttribute('role','status');node.setAttribute('aria-live','polite');node.setAttribute('aria-atomic','true');
      node.innerHTML='<span>创作助手</span><div class="short-drama-thinking-row"><p data-advisor-thinking-label>正在思考，请稍候</p><i class="short-drama-thinking-dots" aria-hidden="true"><b></b><b></b><b></b></i></div>';
      chat.appendChild(node);chat.scrollTop=chat.scrollHeight;advisorThinkingNode=node;
      advisorThinkingTimer=runtimeRoot.setTimeout(function(){
        if(!advisorThinkingNode)return;
        var label=advisorThinkingNode.querySelector('[data-advisor-thinking-label]');
        if(label)label.textContent='还在认真整理你的想法，请再稍候…';
        chat.scrollTop=chat.scrollHeight;
      },8000);
    }
    function setAdvisorBusyState(busy){
      advisorBusy=!!busy;ideaForm.classList.toggle('busy',advisorBusy);ideaInput.disabled=advisorBusy;
      chat.setAttribute('aria-busy',advisorBusy?'true':'false');
      if(advisorSubmit){advisorSubmit.disabled=advisorBusy;advisorSubmit.textContent=advisorBusy?'思考中…':advisorSubmitLabel;}
      Array.prototype.forEach.call(quickReplies.querySelectorAll('button'),function(button){button.disabled=advisorBusy;});
      if(advisorBusy)showAdvisorThinkingIndicator();else removeAdvisorThinkingIndicator();
    }
    function renderQuickReplies(items){
      var visible=[];(items||[]).forEach(function(item){var value=compactIdea(item);if(value&&visible.indexOf(value)<0&&visible.length<3)visible.push(value);});
      activePlannerChoices={field:activePlannerField||'',items:visible.slice(),updated_at:Date.now()};
      quickReplies.innerHTML=visible.map(function(item){
        return '<button type="button" data-idea-reply="'+escapeHtml(item)+'" title="填入输入框，修改后再发送">'+escapeHtml(item)+'</button>';
      }).join('');
      quickReplies.hidden=!visible.length;
    }
    function renderRecommendations(items){
      recommendations.innerHTML='<div class="short-drama-recommendation-lead"><strong>为你推荐 3 个方向</strong><span>选择后仍可修改</span></div>'+
        (items||[]).map(function(item){
          return '<article class="short-drama-recommendation-card"><span>'+escapeHtml(item.label)+'</span><h3>'+escapeHtml(item.title)+'</h3><p>'+escapeHtml(item.premise)+'</p><small>推荐理由：'+escapeHtml(item.reason)+'</small><button class="short-drama-primary" type="button" data-recommendation="'+escapeHtml(item.id)+'">采用这个方向</button></article>';
        }).join('');
      recommendations.hidden=!(items&&items.length);
      if(items&&items.length){plannerPanel='auto';doc.getElementById('shortDramaPlannerCanvasTitle').textContent='选择一个故事方向';}
    }
    function renderPlannerRecommendations(items){
      if(selectedDirection||plannerPreview){recommendations.hidden=true;return;}
      renderRecommendations(items);
    }
    function renderAdvisorMode(){
      var node=doc.getElementById('shortDramaAdvisorMode');
      node.hidden=!advisorDegraded;
      node.textContent=advisorDegraded?'智能理解暂不可用，当前为基础引导模式。复杂修改后请检查右侧“当前理解”，你也可以直接编辑。':'';
    }
    function plannerBriefInput(key,value,meta){
      meta=meta||{};var labels={confirmed:'已确认',inferred:'待确认',suggested:'AI 建议',conflicted:'有冲突',removed:'已删除'},status=meta.status||'',detail=meta.evidence?('依据：'+meta.evidence):'';
      if(meta.conflict)detail+=(detail?'；':'')+'当前为“'+text(meta.conflict.existing_value)+'”，新理解为“'+text(meta.conflict.proposed_value)+'”';
      return '<dt>'+escapeHtml(PLANNER_FIELD_LABELS[key])+'</dt><dd><div class="short-drama-memory-field"><input data-planner-field="'+key+'" value="'+escapeHtml(value||'')+'" placeholder="待确认" aria-label="编辑'+escapeHtml(PLANNER_FIELD_LABELS[key])+'">'+(status?'<span class="'+escapeHtml(status)+'" title="'+escapeHtml(detail)+'">'+escapeHtml(labels[status]||status)+'</span>':'')+'</div>'+(detail?'<small>'+escapeHtml(detail)+'</small>':'')+'</dd>';
    }
    function renderPlanner(){
      var payload=plannerPayload||createPayload(form),understanding=plannerUnderstanding(ideaMessages,payload,plannerAnswers),completeness=plannerCompleteness(understanding);
      var progressState=plannerProgress(ideaMessages,selectedDirection,plannerPreview,payload,plannerAnswers);
      var flowState=plannerFlowState(ideaMessages,payload,plannerAnswers,plannerMeta,selectedDirection,plannerPreview,plannerDirtyFields);
      var acknowledged=doc.getElementById('shortDramaPlannerAckInput').checked;
      var stage=plannerPreview?(acknowledged?'review':'script'):(!recommendations.hidden?'directions':'chat');
      var panel=plannerPanel==='chat'?'chat':stage==='chat'?'chat':'canvas';
      var gridNode=doc.querySelector('.short-drama-planner-grid');
      gridNode.setAttribute('data-planner-stage',stage);gridNode.setAttribute('data-planner-panel',panel);
      var order={chat:0,directions:1,script:2,review:3},current=order[stage];
      doc.querySelectorAll('[data-planner-step]').forEach(function(node){var value=order[node.getAttribute('data-planner-step')];node.classList.toggle('active',value===current);node.classList.toggle('complete',value<current);});
      var showCanvas=doc.getElementById('shortDramaShowCanvas');showCanvas.hidden=stage==='chat';showCanvas.textContent=plannerPreview?'查看完整剧本 →':'查看故事方向 →';
      doc.getElementById('shortDramaPlannerStatus').textContent=flowState.label;
      doc.getElementById('shortDramaPlannerProgress').style.width=progressState.score+'%';
      doc.getElementById('shortDramaPlannerScore').textContent='信息完整度 '+completeness.score+'%';
      doc.getElementById('shortDramaPlannerBrief').innerHTML='<dt>项目</dt><dd>'+escapeHtml(payload.title||'待填写')+'</dd>'+PLANNER_FIELDS.map(function(key){return plannerBriefInput(key,understanding[key],plannerMeta[key]);}).join('')+'<dt>规格</dt><dd>'+escapeHtml(payload.ratio||'16:9')+' · '+Number(payload.target_duration||30)+' 秒 · '+Number(payload.shot_count||6)+' 镜</dd><dt>方向</dt><dd>'+escapeHtml(selectedDirection&&selectedDirection.title||'待选择')+'</dd>';
      var conflictFields=PLANNER_FIELDS.filter(function(key){return plannerMeta[key]&&plannerMeta[key].status==='conflicted';});
      var dirtyLayers=plannerAffectedLayers(plannerDirtyFields);
      doc.getElementById('shortDramaPlannerMissing').innerHTML=(conflictFields.map(function(key){return '<span class="conflict">待确认 '+PLANNER_FIELD_LABELS[key]+'</span>';}).concat(completeness.missing.map(function(key){return '<span>待补 '+PLANNER_FIELD_LABELS[key]+'</span>';})).concat(dirtyLayers.map(function(layer){return '<span class="dirty">需更新 '+({story:'故事结构',scenes:'场景设计',shots:'镜头执行'}[layer])+'</span>';})).join(''))||'<span class="complete">关键信息已完整</span>';
      doc.getElementById('shortDramaPlannerUndo').disabled=plannerHistory.length===0;
      renderAdvisorMode();
      doc.getElementById('shortDramaCompleteBrief').hidden=completeness.missing.length===0;
      var generate=doc.getElementById('shortDramaGeneratePreview');generate.disabled=!selectedDirection||!completeness.ready||flowState.conflicts.length>0;generate.hidden=!!plannerPreview&&!plannerDirtyFields.length;
      generate.textContent=!selectedDirection?'请先选择故事方向':flowState.conflicts.length?'请先确认冲突设定':!completeness.ready?'还需补充 '+completeness.missing.length+' 项':plannerDirtyFields.length?'更新受影响的剧本内容':'生成完整剧本';
      var confirm=doc.getElementById('shortDramaConfirmScript');confirm.hidden=!plannerPreview;
      var ack=doc.getElementById('shortDramaPlannerAckInput');
      confirm.disabled=!!(plannerPreview&&((plannerPreview.quality&&plannerPreview.quality.blocking)||(plannerPreview.review&&plannerPreview.review.status==='blocked')))||!ack.checked||plannerDirtyFields.length>0;
      doc.getElementById('shortDramaDownloadWord').hidden=!plannerPreview||plannerDirtyFields.length>0;
      doc.getElementById('shortDramaPlannerAck').hidden=!plannerPreview||plannerDirtyFields.length>0;
      renderPlannerHistory();renderPlannerAudit();savePlannerDraft();
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
    function renderStoryArchitecture(preview){
      var plan=preview.story_plan||{},scenes=preview.scenes||[];
      return '<section class="short-drama-story-plan"><div class="short-drama-layer-title"><span>第一层</span><strong>故事策划</strong></div><dl><dt>核心悬念</dt><dd>'+escapeHtml(plan.dramatic_question||'—')+'</dd><dt>人物目标</dt><dd>'+escapeHtml(plan.character_goal||'—')+'</dd><dt>主要阻力</dt><dd>'+escapeHtml(plan.obstacle||'—')+'</dd><dt>中段转折</dt><dd>'+escapeHtml(plan.turning_point||'—')+'</dd><dt>高潮选择</dt><dd>'+escapeHtml(plan.climax||'—')+'</dd></dl></section>'+
        '<section class="short-drama-scene-plan"><div class="short-drama-layer-title"><span>第二层</span><strong>分场设计</strong></div><div>'+(scenes.map(function(scene){return '<article><b>场 '+scene.index+' · '+escapeHtml(scene.phase)+'</b><span>'+escapeHtml(scene.location)+' · 镜头 '+scene.shot_start+'—'+scene.shot_end+'</span><p>'+escapeHtml(scene.objective)+'</p><small>场内转折：'+escapeHtml(scene.turn)+'</small></article>';}).join('')||'<p>暂无分场</p>')+'</div></section>';
    }
    function renderScriptReview(preview){
      var review=preview.review||plannerReview(preview),label={passed:'审稿通过',needs_revision:'建议修订',blocked:'暂不能确认'}[review.status]||'待审稿';
      return '<section class="short-drama-script-review '+escapeHtml(review.status)+'"><div><span>剧本审稿 · '+Number(review.score)+' 分</span><strong>'+label+'</strong><small>检查故事结构、场景任务、对白自然度和逐镜可执行性。</small></div>'+(review.issues.length?'<ul>'+review.issues.slice(0,8).map(function(item){return '<li class="'+escapeHtml(item.severity)+'">'+(item.index?'镜头 '+item.index+'：':'')+escapeHtml(item.message)+'</li>';}).join('')+'</ul>':'<p>结构、对白和镜头执行检查均已通过。</p>')+(review.repairable_count?'<button class="short-drama-secondary" type="button" data-planner-review-action="repair">自动修复 '+review.repairable_count+' 项安全问题</button>':'')+'</section>';
    }
    function renderScriptPreview(preview){
      var node=doc.getElementById('shortDramaScriptPreview');
      if(!preview){node.hidden=true;node.innerHTML='';return;}
      recommendations.hidden=true;
      var quality=preview.quality||plannerQuality(preview),gate=quality.blocking?'<div class="short-drama-planner-gate blocked"><strong>还有 '+quality.blockers.length+' 个镜头不能确认</strong><span>'+quality.blockers.map(function(item){return '镜头 '+item.index+'：'+item.message;}).join('；')+'</span></div>':'<div class="short-drama-planner-gate pass"><strong>逐镜时长检查通过</strong><span>台词、表情动作和镜头时长均可执行。</span></div>';
      node.innerHTML='<article class="short-drama-planner-overview"><span class="short-drama-eyebrow">分阶段生成的完整剧本</span><h3>'+escapeHtml(preview.title)+'</h3><p>'+escapeHtml(preview.logline)+'</p><dl><dt>主要角色</dt><dd>'+escapeHtml((preview.characters||[]).join('、'))+'</dd><dt>核心冲突</dt><dd>'+escapeHtml(preview.conflict)+'</dd><dt>结局</dt><dd>'+escapeHtml(preview.ending)+'</dd><dt>制作规格</dt><dd>'+escapeHtml(preview.ratio)+' · '+preview.duration_seconds+' 秒 · '+preview.shot_count+' 镜</dd></dl>'+renderStoryArchitecture(preview)+renderScriptReview(preview)+gate+'<div class="short-drama-layer-title"><span>第三层</span><strong>逐镜执行稿</strong></div><div class="short-drama-planner-shots">'+(preview.shots||[]).map(function(shot){return renderPlannerShot(shot,preview);}).join('')+'</div></article>';
      node.hidden=false;doc.getElementById('shortDramaPlannerCanvasTitle').textContent='完整剧本方案待确认';renderPlanner();
    }
    function plannerNotice(message,error){var node=doc.getElementById('shortDramaPlannerNotice');node.textContent=message||'';node.classList.toggle('error',!!error);}
    function setCreateHeading(eyebrow,title,lead){
      doc.getElementById('shortDramaCreateEyebrow').textContent=eyebrow;
      doc.getElementById('shortDramaCreateTitle').textContent=title;
      doc.getElementById('shortDramaCreateLead').textContent=lead;
    }
    function plannerStorage(){try{return runtimeRoot.localStorage||null;}catch(error){return null;}}
    function plannerDraftKey(){return plannerDraftStorageKey(currentUsername);}
    function readPlannerDraft(){
      try{var storage=plannerStorage(),key=plannerDraftKey();if(!storage||!key)return null;storage.removeItem(LEGACY_PLANNER_DRAFT_KEY);return readPlannerDraftRecord(storage,key,currentUsername,Date.now());}catch(error){return null;}
    }
    function savePlannerDraft(required){
      if(!plannerPersistenceReady||!plannerPayload)return !required;
      return writePlannerDraftRecord(plannerStorage(),plannerDraftKey(),{version:PLANNER_DRAFT_VERSION,username:currentUsername,saved_at:Date.now(),create_mode:createMode,payload:plannerPayload,answers:plannerAnswers,meta:plannerMeta,dirty_fields:plannerDirtyFields,history:plannerHistory,transcript:plannerTranscript,feedback:plannerFeedback,correction_count:plannerCorrectionCount,selected_direction:selectedDirection,preview:plannerPreview,active_field:activePlannerField,active_choices:activePlannerChoices,advisor_degraded:advisorDegraded,panel:plannerPanel,pending_create_key:pendingCreateKey},currentUsername);
    }
    function clearPlannerDraft(){try{var storage=plannerStorage(),key=plannerDraftKey();if(storage&&key)storage.removeItem(key);}catch(error){}}
    function plannerHistoryEntry(answers,meta,dirtyFields,messages,label,changedFields){return {answers:answers,meta:meta,dirtyFields:(dirtyFields||[]).slice(),messages:(messages||[]).slice(),label:label||'创作设定修改',changedFields:(changedFields||[]).slice(),at:Date.now()};}
    function renderPlannerHistory(){
      var list=doc.getElementById('shortDramaPlannerHistoryList'),details=doc.getElementById('shortDramaPlannerHistory');doc.getElementById('shortDramaPlannerHistoryCount').textContent=plannerHistory.length+' 条';details.hidden=!plannerHistory.length;
      list.innerHTML=plannerHistory.map(function(entry,index){return '<article><div><strong>'+escapeHtml(entry.label||'创作设定修改')+'</strong><small>'+escapeHtml(entry.at?new Date(entry.at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'较早记录')+'</small></div><button type="button" data-planner-history-index="'+index+'">恢复到修改前</button></article>';}).reverse().join('');
    }
    function renderPlannerAudit(){var audit=plannerConversationAudit(plannerTranscript,plannerFeedback,plannerMeta,plannerCorrectionCount);doc.getElementById('shortDramaPlannerAuditScore').textContent=audit.score;doc.getElementById('shortDramaPlannerAuditSummary').textContent=audit.summary;}
    function restorePlannerHistory(index){
      index=Number(index);var entry=plannerHistory[index];if(!entry)return;
      var before=plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers)),nextAnswers=plannerAnswerSnapshot(entry.answers||{}),changed=plannerChangedFields(before,plannerUnderstanding(entry.messages||[],plannerPayload,nextAnswers));
      plannerAnswers=nextAnswers;plannerMeta=plannerMetaSnapshot(entry.meta||{});plannerDirtyFields=(entry.dirtyFields||[]).slice();ideaMessages=(entry.messages||[]).slice();plannerHistory=plannerHistory.slice(0,index);plannerCorrectionCount++;
      if(plannerPreview){plannerAffectedLayers(changed);changed.forEach(function(field){if(plannerDirtyFields.indexOf(field)<0)plannerDirtyFields.push(field);});doc.getElementById('shortDramaPlannerAckInput').checked=false;}
      chatBubble('user','恢复到“'+text(entry.label||'上一次修改')+'”之前');chatBubble('assistant','已恢复当时的创作设定。现有剧本继续保留，受影响部分已标记为需要更新。');
      var reply=advisorStep(ideaMessages,plannerPayload,plannerAnswers,plannerMeta);activePlannerField=reply.field||'';renderQuickReplies(reply.quick||[]);renderPlannerRecommendations(reply.recommendations||[]);renderPlanner();
    }
    function restorePlannerDraft(draft){
      plannerPersistenceReady=false;createMode=draft.create_mode||'inspiration';plannerPayload=draft.payload||{};plannerAnswers=draft.answers||{};plannerMeta=draft.meta||{};plannerDirtyFields=draft.dirty_fields||[];plannerHistory=draft.history||[];plannerTranscript=draft.transcript||[];plannerFeedback=draft.feedback||[];plannerCorrectionCount=Number(draft.correction_count)||0;selectedDirection=draft.selected_direction||null;plannerPreview=draft.preview||null;activePlannerField=draft.active_field||'';activePlannerChoices=plannerDraftActiveChoices(draft);advisorDegraded=!!draft.advisor_degraded;plannerPanel=draft.panel||'auto';pendingCreateKey=draft.pending_create_key||'';
      chat.innerHTML='';plannerTranscript.forEach(function(entry){chatBubble(entry.role,entry.message,{record:false,entry:entry});});doc.getElementById('shortDramaPlannerAckInput').checked=false;
      if(activePlannerChoices.items.length){
        renderQuickReplies(activePlannerChoices.items);
      }else{
        var restoredReply=advisorStep(ideaMessages,plannerPayload,plannerAnswers,plannerMeta);
        activePlannerField=restoredReply.field||activePlannerField;
        renderQuickReplies(restoredReply.quick||[]);
      }
      recommendations.innerHTML='';recommendations.hidden=true;if(!plannerPreview){var flow=plannerFlowState(ideaMessages,plannerPayload,plannerAnswers,plannerMeta,selectedDirection,null,plannerDirtyFields);if(flow.phase!=='collect')renderRecommendations(buildRecommendations(ideaMessages,flow.understanding));}
      renderScriptPreview(plannerPreview);showCreateStep('inspiration');plannerPersistenceReady=true;renderPlanner();plannerNotice('已恢复上次未完成的剧本草稿。你可以继续对话、修改或确认。',false);
    }
    function showCreateStep(step){
      startOptions.hidden=step!=='choice';inspiration.hidden=step!=='inspiration';form.hidden=step!=='idea';importSection.hidden=step!=='import';
      if(step==='choice') setCreateHeading('NEW PROJECT','你想怎样开始？','选择最符合当前状态的方式，后面的制作流程完全一致。');
      if(step==='idea') setCreateHeading(createMode==='inspiration'?'START WITH GUIDANCE':'CREATE WITH AN IDEA',createMode==='inspiration'?'先填写基本创作边界':'创建短剧设置',createMode==='inspiration'?'只需填写题材线索和制作规格，下一步由助手与你一起完成剧本。':'先填写已有想法和制作规格，下一步仍会经过助手讨论与剧本确认。');
      if(step==='inspiration') setCreateHeading('SCRIPT CO-CREATION','剧本共创室','先和创作助手聊清想法，再选择方向、查看完整剧本并完成审稿确认。');
      if(step==='import') setCreateHeading('IMPORT A SCRIPT','导入已有剧本','上传文件或粘贴原稿，助手会先识别内容，再与你确认如何成片。');
    }
    function resetCreate(){
      plannerPersistenceReady=false;form.reset();ideaMessages=[];chat.innerHTML='';recommendations.innerHTML='';recommendations.hidden=true;createMode='idea';plannerPayload=null;selectedDirection=null;plannerPreview=null;pendingCreateKey='';plannerAnswers={};plannerMeta={};plannerDirtyFields=[];plannerHistory=[];plannerTranscript=[];plannerFeedback=[];plannerCorrectionCount=0;activePlannerField='';activePlannerChoices={field:'',items:[]};advisorDegraded=false;plannerPanel='auto';
      importText.value='';importFile.value='';importFilename='';importAnalysis=null;pendingImportKey='';importEditor.hidden=false;importForm.hidden=true;importForm.reset();
      doc.getElementById('shortDramaImportCount').textContent='0';doc.getElementById('shortDramaImportFileName').hidden=true;doc.getElementById('shortDramaImportError').hidden=true;
      doc.getElementById('shortDramaImportGlobal').hidden=true;
      doc.getElementById('shortDramaSelectedDirection').hidden=true;
      var opening=advisorStep([],{},plannerAnswers,plannerMeta);activePlannerField=opening.field||'';chatBubble('assistant',opening.message);renderQuickReplies(opening.quick);renderScriptPreview(null);plannerNotice('',false);doc.getElementById('shortDramaPlannerAckInput').checked=false;renderPlanner();
      showCreateStep('choice');
    }
    function openCreate(){if(!currentUsername){setNotice('正在确认登录账号，请稍后重试。',true);return;}var draft=readPlannerDraft();if(draft)restorePlannerDraft(draft);else resetCreate();dialog.showModal();}
    function submitIdea(value){
      value=compactIdea(value);if(!value||advisorBusy)return Promise.resolve(false);
      var choiceContext={field:activePlannerChoices.field||activePlannerField||'',items:(activePlannerChoices.items||[]).slice(0,3)};
      var resolvedChoice=plannerResolveChoice(value,choiceContext);
      if(resolvedChoice.matched&&!resolvedChoice.valid){
        chatBubble('user',value);ideaInput.value='';
        chatBubble('assistant',choiceContext.items.length?'当前只有 '+choiceContext.items.length+' 个推荐方向，请输入 1-'+choiceContext.items.length+'，或直接说说你的想法。':'刚才的推荐方向已经失效，请根据当前问题重新选择。');
        ideaInput.focus();return Promise.resolve(false);
      }
      if(resolvedChoice.matched)value=resolvedChoice.value;
      chatBubble('user',value);ideaInput.value='';
      var expectedField=activePlannerField,understanding=plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers);
      setAdvisorBusyState(true);
      return client.advisor({messages:ideaMessages.slice(-20),understanding:understanding,field_states:plannerMeta,expected_field:expectedField,recommendation_context:{field:choiceContext.field,options:choiceContext.items,selected_index:resolvedChoice.matched?resolvedChoice.index:0,selected_value:resolvedChoice.matched?resolvedChoice.choice:''},user_message:value})
        .catch(function(){var fallback=plannerLocalAdvice(value,expectedField,understanding);fallback.degraded=true;fallback.mode='basic';return fallback;})
        .then(function(result){
          removeAdvisorThinkingIndicator();
          var priorAnswers=plannerAnswerSnapshot(plannerAnswers),priorMeta=plannerMetaSnapshot(plannerMeta),priorDirty=plannerDirtyFields.slice(),priorMessages=ideaMessages.slice(),before=plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers)),intent=text(result&&result.intent).toLowerCase();
          advisorDegraded=!!(result&&result.degraded)||text(result&&result.mode)==='basic';
          if(intent==='undo'){
            if(plannerHistory.length){var restored=plannerHistory.pop();plannerAnswers=restored.answers;plannerMeta=restored.meta||{};plannerDirtyFields=restored.dirtyFields||[];ideaMessages=restored.messages;}
            else result.recap='还没有可以撤销的修改，当前理解保持不变。';
          }else{
            var updated=applyAdvisorResult(plannerAnswers,result);
            var effectiveUpdated=plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,updated));
            var changed=plannerChangedFields(before,effectiveUpdated);if(changed.length){plannerHistory.push(plannerHistoryEntry(priorAnswers,priorMeta,priorDirty,priorMessages,(intent==='modify'||intent==='negate'?'用户修正：':'补充设定：')+changed.map(function(key){return PLANNER_FIELD_LABELS[key];}).join('、'),changed));if(intent==='modify'||intent==='negate')plannerCorrectionCount++;plannerAnswers=updated;plannerMeta=applyAdvisorMetadata(plannerMeta,result);markPlannerChanges(changed);}
          }
          var after=plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers));
          var assistantParts=[result.reply||plannerLocalAdvice(value,expectedField,understanding).reply,plannerRecap(before,after,result)];
          if(['question','ask_recommendation','unknown'].indexOf(intent)>=0){
            var focusField=text(result.focus_field||expectedField),focusQuestion=plannerQuestionDefinition(focusField),rawChoices=result.quick_replies||plannerLocalAdvice(value,focusField,understanding).quick_replies||[];
            var questionTurn=plannerGuidedQuestion(focusField,focusQuestion?focusQuestion.message:'你希望接下来采用哪个方向？',rawChoices,after);
            activePlannerField=focusField;assistantParts.push(questionTurn.message);renderQuickReplies(questionTurn.quick);
          }else{
            if(intent!=='undo')ideaMessages.push(value);
            var reply=advisorStep(ideaMessages,plannerPayload,plannerAnswers,plannerMeta);activePlannerField=reply.field||'';
            assistantParts.push(reply.message);renderQuickReplies(reply.quick||[]);renderPlannerRecommendations(reply.recommendations||[]);
          }
          chatBubble('assistant',plannerAssistantTurn(assistantParts));
          renderPlanner();return true;
        }).finally(function(){setAdvisorBusyState(false);ideaInput.focus();});
    }
    function markPlannerChanges(fields){
      if(!plannerPreview)return;
      (fields||[]).forEach(function(field){if(plannerDirtyFields.indexOf(field)<0)plannerDirtyFields.push(field);});
      doc.getElementById('shortDramaPlannerAckInput').checked=false;
      var labels=plannerAffectedLayers(plannerDirtyFields).map(function(layer){return {story:'故事结构',scenes:'场景设计',shots:'镜头执行'}[layer];});
      plannerNotice('设定已更新。当前剧本仍保留供对照，需要重新更新：'+labels.join('、')+'。',false);
    }
    function updatePlannerField(field,value){
      if(PLANNER_FIELDS.indexOf(field)<0)return;
      var priorAnswers=plannerAnswerSnapshot(plannerAnswers),before=plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers)),next=Object.assign({},plannerAnswers),clean=compactIdea(value);
      next[field]=clean;
      var after=plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,next));
      if(!plannerChangedFields(before,after).length)return;
      plannerHistory.push(plannerHistoryEntry(priorAnswers,plannerMetaSnapshot(plannerMeta),plannerDirtyFields,ideaMessages,'直接修改：'+PLANNER_FIELD_LABELS[field],[field]));plannerCorrectionCount++;plannerAnswers=next;plannerMeta[field]={status:'confirmed',confidence:1,evidence:'你在创作记忆中直接修改'};markPlannerChanges([field]);
      chatBubble('user','将'+PLANNER_FIELD_LABELS[field]+(clean?'改为“'+clean+'”':'清空'));
      chatBubble('assistant',plannerRecap(before,after,{recap:''}));
      var reply=advisorStep(ideaMessages,plannerPayload,plannerAnswers,plannerMeta);activePlannerField=reply.field||'';
      renderQuickReplies(reply.quick||[]);renderPlannerRecommendations(reply.recommendations||[]);renderPlanner();
    }
    function undoPlannerChange(){
      if(!plannerHistory.length)return;
      var before=plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers)),restored=plannerHistory.pop();plannerAnswers=restored.answers;plannerMeta=restored.meta||{};plannerDirtyFields=restored.dirtyFields||[];ideaMessages=restored.messages;if(!plannerDirtyFields.length)plannerNotice('',false);
      var after=plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers));
      chatBubble('user','撤销上次修改');chatBubble('assistant',plannerRecap(before,after,{recap:'已撤销上次修改。'}));
      var reply=advisorStep(ideaMessages,plannerPayload,plannerAnswers,plannerMeta);activePlannerField=reply.field||'';
      renderQuickReplies(reply.quick||[]);renderPlannerRecommendations(reply.recommendations||[]);renderPlanner();
    }
    function selectRecommendation(id){
      var understanding=plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers);
      var selected=buildRecommendations(ideaMessages,understanding).find(function(item){return item.id===id;});if(!selected)return;
      selectedDirection=selected;plannerPreview=null;plannerPanel='auto';renderScriptPreview(null);
      if(!plannerPayload)plannerPayload=createPayload(form);
      if(!text(plannerPayload.title).trim())plannerPayload.title=selected.title;
      if(!text(plannerPayload.synopsis).trim())plannerPayload.synopsis=selected.premise;
      doc.getElementById('shortDramaPlannerAckInput').checked=false;
      doc.getElementById('shortDramaPlannerCanvasTitle').textContent='已选择 '+selected.title;
      chatBubble('user','采用 '+selected.label+'：'+selected.title);
      chatBubble('assistant','方向已记录。你可以继续补充要求，或生成完整剧本方案后进行人工确认。');
      renderPlanner();
    }
    function startPlanner(){
      plannerPayload=createPayload(form);ideaMessages=[];selectedDirection=null;plannerPreview=null;pendingCreateKey='';plannerAnswers={};plannerMeta={};plannerDirtyFields=[];plannerHistory=[];activePlannerField='';advisorDegraded=false;plannerPanel='auto';
      chat.innerHTML='';recommendations.innerHTML='';recommendations.hidden=true;renderScriptPreview(null);plannerNotice('',false);
      chatBubble('assistant',createMode==='inspiration'?'我会先从你给出的线索出发，再通过几个选择帮你找到故事方向。':'我已收到基本设定。接下来一起确认情绪、结局和故事方向，确认后才创建项目。');
      if(plannerPayload.synopsis){chatBubble('user',plannerPayload.synopsis);ideaMessages.push(plannerPayload.synopsis);plannerAnswers.topic=plannerPayload.synopsis;plannerMeta.topic={status:'confirmed',confidence:1,evidence:plannerPayload.synopsis};}
      var reply=advisorStep(ideaMessages,plannerPayload,plannerAnswers,plannerMeta);activePlannerField=reply.field||'';chatBubble('assistant',reply.message);renderQuickReplies(reply.quick||[]);renderPlannerRecommendations(reply.recommendations||[]);
      showCreateStep('inspiration');plannerPersistenceReady=true;renderPlanner();
    }
    function generatePlannerScript(){
      if(!selectedDirection)return;
      var previous=plannerPreview,layers=plannerAffectedLayers(plannerDirtyFields),fresh=buildPlannerPreview(plannerPayload,ideaMessages,selectedDirection,plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers));
      plannerPreview=rebuildPlannerPreview(previous,fresh,layers);plannerDirtyFields=[];plannerPanel='auto';
      doc.getElementById('shortDramaPlannerAckInput').checked=false;
      renderScriptPreview(plannerPreview);plannerNotice(previous?'受影响的剧本内容已更新，未受影响的结构已保留。请重新检查后确认。':'逐镜完整剧本已生成。请检查每个镜头的角色、台词、表情动作、镜头和声音，再确认创建项目。',false);
    }
    function completePlannerBrief(){
      var priorAnswers=plannerAnswerSnapshot(plannerAnswers),current=plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers);
      var defaults={topic:'普通人在一次意外中重新理解重要关系',protagonist:'一名处于人生转折点的普通人',conflict:'主角必须在时间压力下完成一次无法回避的选择',emotion:'温暖治愈',ending:'人物成长并形成清晰情绪落点',audience:'大众观众',style:'电影感写实'};
      Object.keys(defaults).forEach(function(key){if(!text(current[key]).trim())plannerAnswers[key]=defaults[key];});
      var completedFields=plannerChangedFields(current,plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers));if(completedFields.length){plannerHistory.push(plannerHistoryEntry(priorAnswers,plannerMetaSnapshot(plannerMeta),plannerDirtyFields,ideaMessages,'AI 补齐：'+completedFields.map(function(key){return PLANNER_FIELD_LABELS[key];}).join('、'),completedFields));Object.keys(defaults).forEach(function(key){if(plannerAnswers[key]===defaults[key])plannerMeta[key]={status:'suggested',confidence:.6,evidence:'AI 根据现有故事信息补齐'};});markPlannerChanges(completedFields);}
      chatBubble('assistant','我已根据现有信息补齐缺失项，并在右侧标出完整理解。你仍可继续输入要求覆盖这些建议。');
      var reply=advisorStep(ideaMessages,plannerPayload,plannerAnswers,plannerMeta);activePlannerField=reply.field||'';renderQuickReplies(reply.quick||[]);renderPlannerRecommendations(reply.recommendations||buildRecommendations(ideaMessages,plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers)));renderPlanner();
    }
    function downloadPlannerWord(){
      if(!plannerPreview)return;
      var html=plannerWordDocumentHtml(plannerPreview,plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers));
      var blob=new Blob(['\ufeff',html],{type:'application/msword;charset=utf-8'}),url=URL.createObjectURL(blob),anchor=doc.createElement('a');
      anchor.href=url;anchor.download=plannerWordFilename(plannerPreview);doc.body.appendChild(anchor);anchor.click();anchor.remove();setTimeout(function(){URL.revokeObjectURL(url);},1000);
      plannerNotice('Word 确认稿已下载。请核对后勾选确认，再创建项目。',false);
    }
    function handlePlannerShotAction(event){
      var reviewAction=event.target.closest('[data-planner-review-action]');
      if(reviewAction&&plannerPreview){plannerPreview=repairPlannerPreview(plannerPreview);renderScriptPreview(plannerPreview);plannerNotice(plannerPreview.review.status==='passed'?'自动审稿与安全修复完成，当前剧本可以继续确认。':'已完成安全修复；仍有需要人工判断的问题，请检查审稿结果。',plannerPreview.review.status==='blocked');return;}
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
        var replacement=plannerShot(index-1,plannerPreview.shot_count,shot.duration,plannerPreview.characters||[],[plannerPreview.logline,plannerPreview.conflict].concat(plannerPreview.notes||[]).join('；'),plannerPreview.ending,Number(shot.variation||0)+1,plannerPreview.story_plan,plannerPreview.scenes);
        plannerPreview.shots[index-1]=replacement;
      }else if(action==='save'){
        var card=button.closest('[data-shot-index]');if(!card)return;
        function field(name){var input=card.querySelector('[name="'+name+'"]');return text(input&&input.value).trim();}
        shot.scene=field('scene');shot.action=field('action');shot.expression=field('expression');shot.dialogue_kind=field('dialogue_kind')||'silence';
        shot.speaker=shot.dialogue_kind==='silence'?'':field('speaker');shot.dialogue=shot.dialogue_kind==='silence'?'':field('dialogue');
        shot.camera=field('camera');shot.sound=field('sound');shot.transition=field('transition');shot.continuity=field('continuity');shot.editing=false;
      }
      plannerPreview.quality=plannerQuality(plannerPreview);plannerPreview.review=plannerReview(plannerPreview);renderScriptPreview(plannerPreview);
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
      if(!savePlannerDraft(true)){plannerNotice('无法安全保存创建恢复点，请检查浏览器存储权限后重试。项目尚未创建。',true);button.disabled=false;return Promise.resolve();}
      return client.promote({
        project:plannerPayload,
        planning_messages:plannerPromotionMessages(plannerPreview),
        confirmed_contract:contract
      },pendingCreateKey).then(function(result){
        var project=result&&result.project;
        if(!project||!project.id)throw new Error('服务端未返回已确认的短剧项目');
        clearPlannerDraft();plannerNotice('剧本已确认，正在进入正式项目。',false);
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
        var structure=importAnalysis.global_structure||{},globalNode=doc.getElementById('shortDramaImportGlobal');
        globalNode.innerHTML='<strong>长剧本全局理解</strong><div class="short-drama-import-global-grid"><div><b>开场设定</b><span>'+escapeHtml(structure.setup||'待确认')+'</span></div><div><b>故事发展</b><span>'+escapeHtml(structure.development||'待确认')+'</span></div><div><b>关键转折</b><span>'+escapeHtml(structure.turning_point||'待确认')+'</span></div><div><b>高潮选择</b><span>'+escapeHtml(structure.climax||'待确认')+'</span></div><div><b>结局落点</b><span>'+escapeHtml(structure.ending||'待确认')+'</span></div><div><b>核心冲突</b><span>'+escapeHtml(structure.central_conflict||'待确认')+'</span></div></div>';
        globalNode.hidden=false;
        var warning=doc.getElementById('shortDramaImportWarnings');warning.innerHTML=importAnalysis.warnings.map(function(item){return '<p>• '+escapeHtml(item)+'</p>';}).join('');warning.hidden=!importAnalysis.warnings.length;
        importForm.elements.title.value=importAnalysis.title;importForm.elements.target_duration.value=String(importAnalysis.duration);importForm.elements.shot_count.value=String(importAnalysis.shot_count);
        importEditor.hidden=true;importForm.hidden=false;
      }catch(error){doc.getElementById('shortDramaImportGlobal').hidden=true;showImportError(error.message||'剧本识别失败，请检查内容。');}
    }
    doc.getElementById('shortDramaCreate').addEventListener('click',openCreate);
    doc.querySelectorAll('[data-action="open-create"]').forEach(function(node){node.addEventListener('click',openCreate);});
    doc.querySelectorAll('[data-action="close-create"]').forEach(function(node){node.addEventListener('click',function(){dialog.close();});});
    doc.querySelectorAll('[data-action="back-create-choice"]').forEach(function(node){node.addEventListener('click',function(){showCreateStep('choice');});});
    doc.querySelectorAll('[data-create-mode]').forEach(function(node){node.addEventListener('click',function(){
      var mode=node.getAttribute('data-create-mode');createMode=mode;
      if(mode==='inspiration'){startPlanner();return;}
      showCreateStep(mode==='import'?'import':'idea');
    });});
    doc.querySelectorAll('[data-action="back-create-settings"]').forEach(function(node){node.addEventListener('click',function(){showCreateStep('idea');});});
    doc.getElementById('shortDramaImportChoose').addEventListener('click',function(){importFile.click();});
    importFile.addEventListener('change',function(){loadImportFile(importFile.files&&importFile.files[0]);});
    doc.getElementById('shortDramaRemoveImportFile').addEventListener('click',function(){
      importFile.value='';importFilename='';importAnalysis=null;pendingImportKey='';importText.value='';updateImportCount();
      doc.getElementById('shortDramaImportFileText').textContent='';doc.getElementById('shortDramaImportFileName').hidden=true;doc.getElementById('shortDramaImportGlobal').hidden=true;importText.focus();
    });
    importText.addEventListener('input',updateImportCount);
    ['dragenter','dragover'].forEach(function(name){importDrop.addEventListener(name,function(event){event.preventDefault();importDrop.classList.add('dragging');});});
    ['dragleave','drop'].forEach(function(name){importDrop.addEventListener(name,function(event){event.preventDefault();importDrop.classList.remove('dragging');});});
    importDrop.addEventListener('drop',function(event){loadImportFile(event.dataTransfer&&event.dataTransfer.files&&event.dataTransfer.files[0]);});
    doc.getElementById('shortDramaAnalyzeImport').addEventListener('click',analyzeImport);
    doc.getElementById('shortDramaEditImport').addEventListener('click',function(){importForm.hidden=true;importEditor.hidden=false;});
    ideaForm.addEventListener('submit',function(event){event.preventDefault();submitIdea(ideaInput.value);});
    quickReplies.addEventListener('click',function(event){
      var node=event.target.closest('[data-idea-reply]');if(!node||advisorBusy)return;
      ideaInput.value=node.getAttribute('data-idea-reply')||'';ideaInput.focus();
      ideaInput.setSelectionRange(ideaInput.value.length,ideaInput.value.length);
    });
    doc.getElementById('shortDramaPlannerBrief').addEventListener('change',function(event){var input=event.target.closest('[data-planner-field]');if(input)updatePlannerField(input.getAttribute('data-planner-field'),input.value);});
    doc.getElementById('shortDramaPlannerUndo').addEventListener('click',undoPlannerChange);
    doc.getElementById('shortDramaPlannerMemoryToggle').addEventListener('click',function(){
      var gridNode=doc.querySelector('.short-drama-planner-grid');
      var button=doc.getElementById('shortDramaPlannerMemoryToggle');
      var collapsed=!gridNode.classList.contains('memory-collapsed');
      gridNode.classList.toggle('memory-collapsed',collapsed);
      button.setAttribute('aria-expanded',collapsed?'false':'true');
      button.setAttribute('aria-label',collapsed?'展开创作记忆':'收起创作记忆');
      button.textContent=collapsed?'展开':'收起';
    });
    doc.getElementById('shortDramaPlannerHistoryList').addEventListener('click',function(event){var button=event.target.closest('[data-planner-history-index]');if(button)restorePlannerHistory(button.getAttribute('data-planner-history-index'));});
    doc.getElementById('shortDramaRestartPlanner').addEventListener('click',function(){if(!confirmDelete('这会清除当前未完成的对话、创作记忆和剧本草稿。确认重新开始？'))return;clearPlannerDraft();resetCreate();createMode='inspiration';startPlanner();});
    chat.addEventListener('click',function(event){
      var button=event.target.closest('[data-advisor-feedback]');if(!button)return;var bubble=button.closest('[data-planner-message-id]'),messageId=bubble&&bubble.getAttribute('data-planner-message-id');if(!messageId||plannerFeedback.some(function(item){return item.message_id===messageId;}))return;
      var entry=plannerTranscript.find(function(item){return item.id===messageId;}),userEntry=plannerTranscript.slice(0,plannerTranscript.indexOf(entry)).reverse().find(function(item){return item.role==='user';});
      plannerFeedback.push({message_id:messageId,rating:button.getAttribute('data-advisor-feedback'),assistant_message:text(entry&&entry.message),user_message:text(userEntry&&userEntry.message),stage:plannerFlowState(ideaMessages,plannerPayload,plannerAnswers,plannerMeta,selectedDirection,plannerPreview,plannerDirtyFields).phase,understanding:plannerAnswerSnapshot(plannerUnderstanding(ideaMessages,plannerPayload,plannerAnswers)),at:Date.now()});
      var feedbackNode=button.parentElement;feedbackNode.classList.add('recorded');feedbackNode.innerHTML='<small>'+(button.getAttribute('data-advisor-feedback')==='correct'?'已记录：理解正确':'已记录：需要修正')+'</small>';renderPlanner();
    });
    doc.getElementById('shortDramaShowChat').addEventListener('click',function(){plannerPanel='chat';renderPlanner();ideaInput.focus();});
    doc.getElementById('shortDramaShowCanvas').addEventListener('click',function(){plannerPanel='auto';renderPlanner();});
    recommendations.addEventListener('click',function(event){var node=event.target.closest('[data-recommendation]');if(node)selectRecommendation(node.getAttribute('data-recommendation'));});
    doc.getElementById('shortDramaCompleteBrief').addEventListener('click',completePlannerBrief);
    doc.getElementById('shortDramaGeneratePreview').addEventListener('click',generatePlannerScript);
    doc.getElementById('shortDramaDownloadWord').addEventListener('click',downloadPlannerWord);
    doc.getElementById('shortDramaPlannerAckInput').addEventListener('change',renderPlanner);
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
  return {STAGES:STAGES,LABELS:LABELS,normalizeProject:normalizeProject,progress:progress,filterProjects:filterProjects,metrics:metrics,deleteErrorMessage:deleteErrorMessage,createPayload:createPayload,compactIdea:compactIdea,plannerChoiceIndex:plannerChoiceIndex,plannerResolveChoice:plannerResolveChoice,plannerUnderstanding:plannerUnderstanding,plannerCompleteness:plannerCompleteness,plannerFlowState:plannerFlowState,buildRecommendations:buildRecommendations,advisorStep:advisorStep,plannerLocalIntent:plannerLocalIntent,plannerLocalFieldUpdates:plannerLocalFieldUpdates,plannerLocalAdvice:plannerLocalAdvice,applyAdvisorResult:applyAdvisorResult,plannerMetaSnapshot:plannerMetaSnapshot,applyAdvisorMetadata:applyAdvisorMetadata,plannerConversationAudit:plannerConversationAudit,plannerAnswerSnapshot:plannerAnswerSnapshot,plannerChangedFields:plannerChangedFields,plannerRecap:plannerRecap,plannerProgress:plannerProgress,plannerAffectedLayers:plannerAffectedLayers,rebuildPlannerPreview:rebuildPlannerPreview,plannerDurations:plannerDurations,plannerRoles:plannerRoles,plannerReadingSeconds:plannerReadingSeconds,plannerStoryPlan:plannerStoryPlan,plannerScenePlan:plannerScenePlan,plannerDialogueSet:plannerDialogueSet,plannerQuality:plannerQuality,plannerReview:plannerReview,repairPlannerPreview:repairPlannerPreview,buildPlannerPreview:buildPlannerPreview,plannerPromotionMessages:plannerPromotionMessages,plannerConfirmedContract:plannerConfirmedContract,plannerWordDocumentHtml:plannerWordDocumentHtml,plannerWordFilename:plannerWordFilename,confirmedContractMatches:confirmedContractMatches,continuePlannerContract:continuePlannerContract,importedTitle:importedTitle,importedGlobalUnderstanding:importedGlobalUnderstanding,analyzeImportedScript:analyzeImportedScript,importProjectPayload:importProjectPayload,newImportKey:newImportKey,newProjectKey:newProjectKey,plannerDraftStorageKey:plannerDraftStorageKey,plannerDraftMatchesUser:plannerDraftMatchesUser,plannerDraftActiveChoices:plannerDraftActiveChoices,readPlannerDraftRecord:readPlannerDraftRecord,writePlannerDraftRecord:writePlannerDraftRecord,readLimitedStream:readLimitedStream,extractPdfText:extractPdfText,extractDocxText:extractDocxText,readScriptFile:readScriptFile,createClient:createClient,projectUrl:projectUrl,cardHtml:cardHtml,mount:mount};
});
