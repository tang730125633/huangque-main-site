(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQDirectorAgent=api; if(root.document) api.bootstrap(root.document,root); }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var STORAGE_KEY='hq_director_agent_unified_v1';
  var LEGACY_STORAGE_KEYS=['hq_director_agent_v1','hq_director_agent_digital_human_v1'];
  var ROUTES={
    script:'/workbench/script.html',digital_human:'/workbench/digital-human-oneclick.html',
    ip12:'/workbench/ip12.html',assets:'/workbench/assets.html',audio:'/workbench/audio.html',
    video:'/workbench/video.html',canvas:'/workbench/canvas.html'
  };
  var FOCUS={
    topic:'scTopic',selling_points:'scSell',generate_script:'scGen',breakdown_url:'bdUrl',
    analyze_breakdown:'bdGen',generate_video:'scGenVideo',generate_audio:'scGenAudio',export_script:'scExport',
    photo_upload:'photoDrop',voice_source:'voiceSource',voice_upload:'voiceUploadDrop',
    customer_materials:'customerMaterialsPicker',full_audio_upload:'driveAudioDrop',
    photo_authorization:'consent',analyze_plan:'analyze',generate_photo_video:'start',
    video_upload:'dhDrop',precision_authorization:'dhConsent',analyze_voice:'dhAnalyze',
    generate_precision_video:'dhStart'
  };

  function digest(value){
    var text=JSON.stringify(value),hash=2166136261;
    for(var i=0;i<text.length;i++){ hash^=text.charCodeAt(i); hash=Math.imul(hash,16777619); }
    return ('00000000'+(hash>>>0).toString(16)).slice(-8);
  }
  function normalizedUsername(value){
    value=String(value||'').trim();
    return value&&value.length<=160?value:'';
  }
  function accountStorageKey(username){
    username=normalizedUsername(username);
    return username?STORAGE_KEY+':'+encodeURIComponent(username):'';
  }
  function emptyState(){
    return {messages:[],open:false,pending_request:null,production_offer:null,pending_production:null};
  }
  function discardOwnerlessState(storage){
    if(!storage||typeof storage.removeItem!=='function') return;
    [STORAGE_KEY].concat(LEGACY_STORAGE_KEYS).forEach(function(key){
      try{storage.removeItem(key);}catch(error){}
    });
  }
  function text(node){ return String(node&&node.value!=null?node.value:node&&node.textContent||'').trim(); }
  function activeText(doc,selector){ var node=doc.querySelector(selector+' .on'); return text(node); }
  function isVisible(node){
    if(!node) return false;
    if(node.style&&node.style.display==='none') return false;
    return !(node.hidden||node.getAttribute&&node.getAttribute('aria-hidden')==='true');
  }
  function countScenes(doc,selector){
    return Array.prototype.filter.call(doc.querySelectorAll(selector+' .sc-card'),function(node){
      return !(node.getAttribute&&node.getAttribute('data-placeholder')==='1');
    }).length;
  }
  function createScriptPageContext(doc){
    var breakdown=doc.getElementById('panelBreakdown');
    var activeMode=doc.querySelector('#scModeTabs [data-mode].on');
    var requestedMode=String(activeMode&&activeMode.getAttribute('data-mode')||'');
    var mode=/^(write|script_to_video|breakdown)$/.test(requestedMode)
      ?requestedMode:(isVisible(breakdown)?'breakdown':'write');
    var activeBreakdownTool=doc.querySelector('#bdToolTabs [data-bd-tool].on');
    var requestedBreakdownTool=String(activeBreakdownTool&&activeBreakdownTool.getAttribute('data-bd-tool')||'');
    var breakdownTool=/^(scenes|reverse_prompt)$/.test(requestedBreakdownTool)?requestedBreakdownTool:'scenes';
    var hasReversePrompt=breakdownTool==='reverse_prompt'&&!!text(doc.getElementById('bdReversePromptText'));
    var sceneCount=countScenes(doc,'#scScenes');
    var breakdownCount=breakdownTool==='scenes'?countScenes(doc,'#scScenes'):0;
    var meta=doc.getElementById('scMeta');
    var busy=['scGen','bdGen','scGenVideo','scGenAudio','bdImageReverse','bdVideoReverse']
      .some(function(id){var node=doc.getElementById(id);return !!(node&&node.disabled);});
    return {
      page:'script',path:'/workbench/script.html',mode:mode,
      topic:text(doc.getElementById('scTopic')).slice(0,1000),
      selling_points:text(doc.getElementById('scSell')).slice(0,2000),
      style:activeText(doc,'#segStyle').slice(0,40),
      duration:activeText(doc,'#segDur').slice(0,20),
      platform:activeText(doc,'#platRow').slice(0,40),
      has_script:mode!=='breakdown'&&isVisible(meta)&&sceneCount>0,scene_count:mode!=='breakdown'?sceneCount:0,
      has_breakdown:mode==='breakdown'&&(breakdownCount>0||hasReversePrompt),
      breakdown_scene_count:mode==='breakdown'?breakdownCount:0,
      breakdown_url:text(doc.getElementById('bdUrl')).slice(0,2000),
      breakdown_tool:breakdownTool,has_reverse_prompt:mode==='breakdown'&&hasReversePrompt,
      active_job_status:busy?'running':'idle'
    };
  }
  function hasClass(node,name){
    return !!(node&&node.classList&&typeof node.classList.contains==='function'&&node.classList.contains(name));
  }
  function hasFile(node){ return !!(node&&node.files&&node.files.length); }
  function digitalHumanMode(doc){
    var active=doc.querySelector('[data-dh-mode].on');
    var requested=String(active&&active.getAttribute('data-dh-mode')||'');
    if(requested==='photo'||requested==='video') return requested;
    return isVisible(doc.getElementById('dhVideoMode'))?'video':'photo';
  }
  function digitalHumanJobStatus(doc,hasResult){
    if(doc.querySelector('.step.failed')) return 'failed';
    if(hasResult) return 'completed';
    if(doc.querySelector('.step.running')) return 'running';
    return 'idle';
  }
  function materialCount(doc){
    var match=text(doc.getElementById('customerMaterialCount')).match(/^\d+/);
    var count=match?Number(match[0]):0;
    return Math.max(0,Math.min(6,count));
  }
  function createDigitalHumanPageContext(doc){
    var mode=digitalHumanMode(doc);
    var narration=doc.querySelector('input[name="narrationMode"]:checked');
    var narrationMode=String(narration&&narration.value||'text');
    if(narrationMode!=='audio') narrationMode='text';
    var scriptNode=doc.getElementById(mode==='video'?'dhScript':'script');
    var scriptText=text(scriptNode).slice(0,6000);
    var result=doc.getElementById(mode==='video'?'dhPrecisionResult':'result');
    var hasResult=hasClass(result,'show');
    var photoName=doc.getElementById('photoName'),videoName=doc.getElementById('dhVideoName');
    var voiceSource=doc.getElementById('voiceSource'),voiceSourceValue=text(voiceSource);
    var videoVoicePreview=doc.getElementById('dhVoicePreview');
    var activeTemplate=doc.querySelector('.precision-template.on');
    return {
      page:'digital_human_oneclick',path:'/workbench/digital-human-oneclick.html',mode:mode,
      guide_contract:String(doc.body&&doc.body.getAttribute&&doc.body.getAttribute('data-director-guide-contract')||''),
      narration_mode:narrationMode,script_text:scriptText,script_length:scriptText.length,
      has_portrait:hasFile(doc.getElementById('photo'))||hasClass(photoName,'file-ready'),
      has_video_source:hasFile(doc.getElementById('dhVideoFile'))||hasClass(videoName,'file-ready')||!!doc.querySelector('.precision-source.on'),
      has_voice_source:mode==='video'
        ?!!(videoVoicePreview&&videoVoicePreview.disabled===false)
        :!!((voiceSourceValue&&voiceSourceValue!=='__clone__')||hasFile(doc.getElementById('voice'))),
      has_drive_audio:hasFile(doc.getElementById('driveAudio')),
      customer_material_count:materialCount(doc),
      consent_confirmed:!!(doc.getElementById(mode==='video'?'dhConsent':'consent')||{}).checked,
      precision_template:String(activeTemplate&&activeTemplate.getAttribute('data-template')||''),
      has_result:hasResult,active_job_status:digitalHumanJobStatus(doc,hasResult)
    };
  }
  function createPageContext(doc){
    if(doc&&doc.getElementById('dhPhotoMode')) return createDigitalHumanPageContext(doc);
    return createScriptPageContext(doc);
  }
  function createPageSnapshot(doc){
    var context=createPageContext(doc);
    return {page_context:context,page_revision:digest(context)};
  }
  function sessionId(storage){
    var stored='';
    try{ stored=storage.getItem('hq_director_agent_session')||''; }catch(error){}
    if(/^[A-Za-z0-9_-]{8,80}$/.test(stored)) return stored;
    stored='director_'+Date.now().toString(36)+Math.random().toString(36).slice(2,12);
    try{ storage.setItem('hq_director_agent_session',stored); }catch(error){}
    return stored;
  }
  function buildPayload(prompt,doc,state,storage){
    var snapshot=createPageSnapshot(doc);
    return {
      prompt:String(prompt||'').trim().slice(0,6000),session_id:sessionId(storage),
      page_revision:snapshot.page_revision,page_context:snapshot.page_context,
      history:(state.messages||[]).filter(function(item){return item.role==='user'||item.role==='assistant';})
        .slice(-10).map(function(item){return {role:item.role,content:String(item.content||'').slice(0,2000)};}),
      source_page:snapshot.page_context.page,
      provider:'openai_responses',quoted_cost:0
    };
  }
  function validatePlan(plan,doc){
    if(!plan||!Array.isArray(plan.actions)||plan.actions.length>6) throw new Error('编导助手方案无效，请重新询问');
    if(plan.page_revision!==createPageSnapshot(doc).page_revision) throw new Error('页面内容已变化，请重新让编导助手判断');
    return true;
  }
  function dispatchValue(node,value){
    node.value=String(value||'');
    if(typeof node.dispatchEvent==='function'){
      var EventCtor=node.ownerDocument&&node.ownerDocument.defaultView&&node.ownerDocument.defaultView.Event;
      if(EventCtor){ node.dispatchEvent(new EventCtor('input',{bubbles:true})); node.dispatchEvent(new EventCtor('change',{bubbles:true})); }
    }
    if(typeof node.focus==='function') node.focus();
  }
  function choose(doc,selector,value){
    var wanted=String(value||'').replace(/\s+/g,'').toLowerCase(),found=null;
    if(!wanted) throw new Error('页面选项不能为空');
    var nodes=Array.prototype.slice.call(doc.querySelectorAll(selector));
    function parts(node){
      var current=text(node).replace(/\s+/g,'').toLowerCase();
      var dataValue=String(node.getAttribute&&(node.getAttribute('data-mode')||node.getAttribute('data-bd-tool')||node.getAttribute('data-dh-mode')||node.getAttribute('data-template')||node.getAttribute('value'))||node.value||'').toLowerCase();
      return {node:node,current:current,dataValue:dataValue};
    }
    for(var i=0;i<nodes.length;i++){
      var exact=parts(nodes[i]);
      if(exact.dataValue===wanted||exact.current===wanted){ found=exact.node; break; }
    }
    for(var j=0;!found&&j<nodes.length;j++){
      var partial=parts(nodes[j]);
      if(partial.current&&(partial.current.indexOf(wanted)>=0||wanted.indexOf(partial.current)>=0)){
        found=partial.node; break;
      }
    }
    if(!found) throw new Error('页面上没有找到“'+value+'”选项');
    if(typeof found.click==='function') found.click();
    return found;
  }
  function mediaKind(file){
    var type=String(file&&file.type||'').toLowerCase(),name=String(file&&file.name||'').toLowerCase();
    if(/^image\/(jpeg|png|webp)$/.test(type)||/\.(jpe?g|png|webp)$/.test(name)) return 'image';
    if(/^video\/(mp4|quicktime|webm)$/.test(type)||/\.(mp4|mov|webm)$/.test(name)) return 'video';
    return '';
  }
  function transferFiles(win,target,files){
    if(!target) throw new Error('当前页面没有可用的上传位置');
    if(!win||typeof win.DataTransfer!=='function') throw new Error('当前浏览器不支持从对话框传递文件，请使用页面原上传按钮');
    var transfer=new win.DataTransfer();
    files.forEach(function(file){transfer.items.add(file);});
    target.files=transfer.files;
    var EventCtor=win.Event||(target.ownerDocument&&target.ownerDocument.defaultView&&target.ownerDocument.defaultView.Event);
    if(EventCtor&&typeof target.dispatchEvent==='function') target.dispatchEvent(new EventCtor('change',{bubbles:true}));
  }
  function attachFilesToPage(fileList,doc,win){
    var files=Array.prototype.slice.call(fileList||[]);
    if(!files.length) throw new Error('请选择图片或视频');
    var kinds={};
    files.forEach(function(file){
      var kind=mediaKind(file); if(!kind) throw new Error('仅支持 JPG、PNG、WEBP、MP4、MOV 或 WEBM'); kinds[kind]=true;
    });
    if(Object.keys(kinds).length!==1) throw new Error('图片和视频请分开上传');
    var kind=Object.keys(kinds)[0],context=createPageContext(doc),page=context.page;
    if(kind==='video'){
      if(files.length!==1) throw new Error('每次只能上传一个视频');
      if(page==='digital_human_oneclick'){
        if(!/^(video\/mp4)$/i.test(String(files[0].type||''))&&!/\.mp4$/i.test(String(files[0].name||''))) throw new Error('数字人真人视频仅支持 MP4');
        if(Number(files[0].size||0)>100*1024*1024) throw new Error('数字人真人视频不能超过 100MB');
        if(context.mode!=='video') choose(doc,'[data-dh-mode]','video');
        transferFiles(win,doc.getElementById('dhVideoFile'),files);
        return '已把真人视频交给当前数字人页面上传。上传完成后，我会继续检查文案、模板和授权。';
      }
      if(Number(files[0].size||0)>200*1024*1024) throw new Error('拆解视频不能超过 200MB');
      if(context.mode!=='breakdown') choose(doc,'#scModeTabs [data-mode]','breakdown');
      if(context.breakdown_tool!=='reverse_prompt') choose(doc,'#bdToolTabs [data-bd-tool]','reverse_prompt');
      transferFiles(win,doc.getElementById('bdLocalVideo'),files);
      return '已把视频放入提示词反推位置；点击页面反推按钮后才会扣点。';
    }
    if(page==='script'){
      if(context.mode==='breakdown'&&context.breakdown_tool==='reverse_prompt'){
        if(files.length!==1) throw new Error('图片反推每次只能上传一张图片');
        if(Number(files[0].size||0)>20*1024*1024) throw new Error('图片反推文件不能超过 20MB');
        transferFiles(win,doc.getElementById('bdLocalImage'),files);
        return '已把图片放入当前图片反推位置。点击页面“反推图片”后才会扣点。';
      }
      if(files.length>4) throw new Error('编导参考图最多上传 4 张');
      files.forEach(function(file){if(Number(file.size||0)>5*1024*1024) throw new Error('编导参考图单张不能超过 5MB');});
      transferFiles(win,doc.getElementById('scRefImage'),files);
      return '已把 '+files.length+' 张图片放入编导参考图，生成脚本时会一起使用。';
    }
    if(files.length>7) throw new Error('数字人人物照片加成片图片最多选择 7 张');
    files.forEach(function(file){if(Number(file.size||0)>20*1024*1024) throw new Error('数字人图片单张不能超过 20MB');});
    if(context.mode!=='photo') choose(doc,'[data-dh-mode]','photo');
    var portrait=doc.getElementById('photo'),hasPortrait=hasFile(portrait)||hasClass(doc.getElementById('photoName'),'file-ready');
    var offset=0;
    if(!hasPortrait){transferFiles(win,portrait,[files[0]]);offset=1;}
    var materials=files.slice(offset);
    if(materials.length>6) throw new Error('顾客成片图片最多上传 6 张');
    if(materials.length) transferFiles(win,doc.getElementById('customerMaterials'),materials);
    return offset
      ?'已把第一张图片放入人物照片'+(materials.length?'，其余 '+materials.length+' 张放入顾客成片素材。':'。')
      :'人物照片已存在，已把 '+materials.length+' 张图片放入顾客成片素材。';
  }
  function applyAction(action,doc,win){
    if(!action||!action.type) throw new Error('编导助手动作无效');
    if(action.type==='fill_field'){
      var mode=doc.getElementById('dhPhotoMode')?digitalHumanMode(doc):'';
      var fields={topic:'scTopic',selling_points:'scSell',breakdown_url:'bdUrl',
        digital_human_script:mode==='video'?'dhScript':'script'};
      var field=doc.getElementById(fields[action.field]);
      if(!field) throw new Error('页面字段不存在');
      dispatchValue(field,action.value);
      var fillLabel=String(action.label||'页面字段');
      return fillLabel.indexOf('填入')===0?'已'+fillLabel:'已填入'+fillLabel;
    }
    if(action.type==='choose_option'){
      var selectors={style:'#segStyle .sc-opt',duration:'#segDur .sc-opt',platform:'#platRow .sc-chip',
        breakdown_tool:'#bdToolTabs [data-bd-tool]',narration_mode:'input[name="narrationMode"]',
        precision_template:'.precision-template'};
      if(!selectors[action.field]) throw new Error('页面选项无效');
      choose(doc,selectors[action.field],action.value); return '已选择 '+action.value;
    }
    if(action.type==='switch_mode'){
      var selector=(action.mode==='photo'||action.mode==='video')?'[data-dh-mode]':'#scModeTabs [data-mode]';
      choose(doc,selector,action.mode); return '已切换页面模式';
    }
    if(action.type==='focus'){
      var node=doc.getElementById(FOCUS[action.target]);
      if(!node) throw new Error('页面目标不存在');
      if(typeof node.scrollIntoView==='function') node.scrollIntoView({behavior:'smooth',block:'center'});
      if(typeof node.focus==='function') node.focus();
      node.classList&&node.classList.add('hq-agent-focus');
      setTimeout(function(){node.classList&&node.classList.remove('hq-agent-focus');},1800);
      return '已定位到页面操作';
    }
    if(action.type==='navigate'){
      if(!ROUTES[action.target]) throw new Error('站内目标无效');
      if(win&&win.location) win.location.href=ROUTES[action.target];
      return '正在前往下一步';
    }
    throw new Error('不允许执行这个动作');
  }
  function validPendingRequest(value){
    if(!value||typeof value!=='object'||Array.isArray(value)) return null;
    var key=String(value.key||''),body=value.body,jobId=value.job_id;
    if(!/^director-agent-[A-Za-z0-9_-]{8,100}$/.test(key)) return null;
    if(!body||typeof body!=='object'||Array.isArray(body)) return null;
    try{ if(JSON.stringify(body).length>48000) return null; }catch(error){ return null; }
    if(jobId!==null&&jobId!==undefined&&!/^[A-Za-z0-9_-]{1,80}$/.test(String(jobId))) return null;
    return {
      key:key,body:body,
      summary:value.summary&&typeof value.summary==='object'?value.summary:{},
      job_id:jobId===null||jobId===undefined?null:String(jobId),
      created_at:Number(value.created_at)||Date.now()
    };
  }
  function createPendingRequest(body,key,prompt,now){
    var copy=JSON.parse(JSON.stringify(body||{}));
    return validPendingRequest({
      key:key,body:copy,job_id:null,created_at:Number(now)||Date.now(),
      summary:{
        prompt:String(prompt||'').slice(0,2000),
        page_revision:String(copy.page_revision||'').slice(0,32),
        mode:String(copy.page_context&&copy.page_context.mode||'').slice(0,24)
      }
    });
  }
  function validProductionOffer(value,allowLegacyRevision){
    if(!value||typeof value!=='object'||Array.isArray(value)) return null;
    var offerId=String(value.offer_id||''),input=value.input,summary=value.summary;
    if(value.requires_confirmation!==true) return null;
    if(!input||typeof input!=='object'||Array.isArray(input)||String(input.request_id||'')!==offerId) return null;
    if(!summary||typeof summary!=='object'||Array.isArray(summary)) return null;
    var cost=Number(value.expected_cost);
    if(!Number.isInteger(cost)||cost<1||cost>10000) return null;
    var planDigest=String(value.plan_digest||''),quoteToken=String(value.quote_token||'');
    var pageRevision=String(value.page_revision||'');
    var expiresAt=Number(value.expires_at);
    if(!/^director-production-[A-Za-z0-9_-]{16,64}$/.test(offerId)||value.kind!=='script') return null;
    if(!/^[a-f0-9]{64}$/.test(planDigest)||!/^[A-Za-z0-9._-]{20,4096}$/.test(quoteToken)||(!allowLegacyRevision&&!/^[a-f0-9]{8,32}$/.test(pageRevision))||(allowLegacyRevision&&pageRevision&&!/^[a-f0-9]{8,32}$/.test(pageRevision))||!Number.isInteger(expiresAt)||expiresAt<=0) return null;
    var clean={offer_id:offerId,kind:'script',expected_cost:cost,requires_confirmation:true,
      plan_digest:planDigest,quote_token:quoteToken,expires_at:expiresAt,page_revision:pageRevision,
      input:{request_id:offerId,topic:String(input.topic||'').slice(0,1000),
        selling_points:String(input.selling_points||'').slice(0,2000),style:String(input.style||''),
        duration:String(input.duration||''),platform:String(input.platform||'')},
      summary:{topic:String(summary.topic||'').slice(0,1000),style:String(summary.style||''),
        duration:String(summary.duration||''),platform:String(summary.platform||'')}};
    if(!clean.input.topic) return null;
    return clean;
  }
  function validPendingProduction(value){
    if(!value||typeof value!=='object'||Array.isArray(value)) return null;
    var jobId=value.job_id,offer=validProductionOffer(value.offer,true);
    if(!offer) return null;
    if(jobId!==null&&jobId!==undefined&&!/^\d{1,20}$/.test(String(jobId))) return null;
    return {offer:offer,job_id:jobId===null||jobId===undefined?null:String(jobId),
      created_at:Number(value.created_at)||Date.now()};
  }
  function autoResumeKind(state){
    if(state&&state.pending_production&&state.pending_production.job_id) return 'production';
    if(state&&state.pending_request&&state.pending_request.job_id) return 'request';
    return '';
  }
  function retainProductionOfferAfterError(error){
    var code=String(error&&error.data&&error.data.code||'');
    return Number(error&&error.status)===402||code==='insufficient_points';
  }
  function readState(storage,username){
    username=normalizedUsername(username);
    var key=accountStorageKey(username);
    if(!key) return emptyState();
    try{
      var value=JSON.parse(storage.getItem(key)||'null');
      if(value&&value.owner===username&&Array.isArray(value.messages)) return {
        messages:value.messages.slice(-20),open:!!value.open,
        pending_request:validPendingRequest(value.pending_request),
        production_offer:validProductionOffer(value.production_offer),
        pending_production:validPendingProduction(value.pending_production),
        updated_at:Number(value.updated_at)||0
      };
    }catch(error){}
    return emptyState();
  }
  function saveState(storage,state,username){
    username=normalizedUsername(username);
    var key=accountStorageKey(username);
    if(!key) return false;
    try{ storage.setItem(key,JSON.stringify({
      owner:username,messages:state.messages.slice(-20),open:state.open,
      pending_request:validPendingRequest(state.pending_request),
      production_offer:validProductionOffer(state.production_offer),
      pending_production:validPendingProduction(state.pending_production),
      updated_at:Date.now()
    })); return true; }catch(error){return false;}
  }
  function readUnifiedState(storage,username){
    username=normalizedUsername(username);
    if(!username) return emptyState();
    discardOwnerlessState(storage);
    return readState(storage,username);
  }
  function jsonFetch(win,url,options){
    options=options||{}; var headers=options.headers||{};
    headers['Content-Type']='application/json';
    return win.fetch(url,{method:options.method||'GET',credentials:'same-origin',cache:'no-store',headers:headers,
      body:options.body===undefined?undefined:JSON.stringify(options.body)}).then(function(response){
      return response.text().then(function(raw){
        var data={}; try{data=raw?JSON.parse(raw):{};}catch(error){}
        if(!response.ok){
          var requestError=new Error(data.detail||('请求失败（'+response.status+'）'));
          requestError.status=response.status;
          requestError.data=data;
          throw requestError;
        }
        return data;
      });
    });
  }

  function bootstrap(doc,win,mounter){
    if(!doc||(!doc.getElementById('scTopic')&&!doc.getElementById('dhPhotoMode'))||!win||typeof win.fetch!=='function') return Promise.resolve(null);
    return jsonFetch(win,'/api/gen/health').then(function(health){
      if(!health||health.director_agent_enabled!==true) return null;
      return jsonFetch(win,'/api/auth/me').then(function(account){
        var username=normalizedUsername(account&&account.user&&account.user.username);
        if(!username) return null;
        return (mounter||mount)(doc,win,username);
      });
    }).catch(function(){return null;});
  }
  function pollJob(win,jobId,onProgress){
    var started=Date.now(),transientFailures=0;
    return new Promise(function(resolve,reject){
      function timedOut(){ return Date.now()-started>300000; }
      function tick(){
        if(timedOut()){ var timeoutError=new Error('编导助手响应超时，请稍后重试'); timeoutError.terminal=false; reject(timeoutError); return; }
        jsonFetch(win,'/api/gen/job/'+encodeURIComponent(jobId)).then(function(job){
          transientFailures=0;
          if(job.status==='done'){
            var result=job.result; if(typeof result==='string') result=JSON.parse(result); resolve(result); return;
          }
          if(job.status==='error'||job.status==='failed'){ var jobError=new Error(job.error||'编导助手处理失败'); jobError.terminal=true; reject(jobError); return; }
          if(timedOut()){ reject(new Error('编导助手响应超时，请稍后重试')); return; }
          if(onProgress) onProgress(Math.floor((Date.now()-started)/1000));
          setTimeout(tick,1400);
        }).catch(function(error){
          transientFailures+=1;
          if(timedOut()){ error.terminal=false; reject(error); return; }
          if(error.status&&error.status<500){ error.terminal=true; reject(error); return; }
          if(onProgress) onProgress(Math.floor((Date.now()-started)/1000));
          setTimeout(tick,Math.min(5000,1400*transientFailures));
        });
      }
      tick();
    });
  }
  function resumeRequest(win,record,onRecord,onProgress){
    record=validPendingRequest(record);
    if(!record) return Promise.reject(new Error('未找到可恢复的编导助手请求'));
    function accepted(){
      if(record.job_id) return Promise.resolve(record);
      return jsonFetch(win,'/api/gen/director_agent',{
        method:'POST',body:record.body,headers:{'Idempotency-Key':record.key}
      }).then(function(data){
        if(!data.job_id) throw new Error(data.detail||'编导助手任务提交失败');
        record.job_id=String(data.job_id);
        if(onRecord) onRecord(record);
        return record;
      }).catch(function(error){
        var code=error.data&&error.data.code;
        var retryable=!error.status||error.status>=500||code==='idempotency_in_progress';
        error.terminal=!retryable;
        error.uncertain=retryable;
        throw error;
      });
    }
    return accepted().then(function(){
      return pollJob(win,record.job_id,function(seconds){
        if(onProgress) onProgress(seconds,'polling');
      });
    });
  }

  function resumeProduction(win,record,onRecord,onProgress){
    record=validPendingProduction(record);
    if(!record) return Promise.reject(new Error('未找到可恢复的编导生产单'));
    function accepted(){
      if(record.job_id) return Promise.resolve(record);
      var offer=record.offer;
      var endpoint='/api/gen/director_agent/produce';
      return jsonFetch(win,endpoint,{
        method:'POST',body:{offer_id:offer.offer_id,input:offer.input,expected_cost:offer.expected_cost,
          plan_digest:offer.plan_digest,quote_token:offer.quote_token},
        headers:{'Idempotency-Key':offer.offer_id}
      }).then(function(data){
        if(!data.job_id) throw new Error(data.detail||'编导生产任务提交失败');
        record.job_id=String(data.job_id); if(onRecord) onRecord(record); return record;
      }).catch(function(error){
        var code=error.data&&error.data.code;
        if(code==='production_price_changed'){
          error.priceChanged=Number(error.data.current_cost)||0; error.terminal=true; throw error;
        }
        var retryable=!error.status||error.status>=500||code==='idempotency_in_progress'||code==='reconcile_pending'||code==='director_cli_submit_retryable'||code==='director_production_retryable'||code==='director_reverse_retryable';
        error.terminal=!retryable; error.uncertain=retryable; throw error;
      });
    }
    return accepted().then(function(){
      return pollJob(win,record.job_id,function(seconds){if(onProgress) onProgress(seconds,'polling');});
    });
  }

  function formatScriptResult(result){
    var scenes=result&&Array.isArray(result.scenes)?result.scenes:[];
    if(!scenes.length) return '生产已完成：\n'+String(result&&result.text||'脚本已生成').slice(0,12000);
    var lines=['脚本已生产完成 · '+String(result.platform||'')+' · '+String(result.dur||'')];
    scenes.slice(0,20).forEach(function(scene,index){
      lines.push('\n镜头 '+(index+1)+(scene.dur?' · '+String(scene.dur):''));
      if(scene.scene) lines.push('画面：'+String(scene.scene));
      if(scene.line) lines.push('口播：'+String(scene.line));
    });
    return lines.join('\n').slice(0,16000);
  }

  function addStyles(doc){
    if(doc.getElementById('hqDirectorAgentStyle')) return;
    var style=doc.createElement('style'); style.id='hqDirectorAgentStyle';
    style.textContent=''
      +'.hq-da-launch{position:fixed;right:24px;bottom:24px;z-index:8800;border:0;border-radius:999px;padding:12px 17px;background:linear-gradient(135deg,#f4cd72,#e7b24c);color:#241604;font:700 14px/1.2 inherit;box-shadow:0 16px 42px rgba(0,0,0,.38);cursor:pointer}'
      +'.hq-da-panel{position:fixed;right:24px;bottom:82px;z-index:8801;width:min(390px,calc(100vw - 28px));height:min(620px,calc(100vh - 112px));display:none;flex-direction:column;border:1px solid rgba(231,178,76,.25);border-radius:18px;background:#0b111c;color:#eaf1fa;box-shadow:0 24px 70px rgba(0,0,0,.55);overflow:hidden}'
      +'.hq-da-panel.on{display:flex}.hq-da-head{display:flex;align-items:center;justify-content:space-between;padding:15px 16px;border-bottom:1px solid rgba(148,164,187,.13);background:linear-gradient(135deg,rgba(231,178,76,.12),rgba(11,17,28,.96))}'
      +'.hq-da-head b{font-size:15px}.hq-da-head span{display:block;margin-top:3px;color:#94a4bb;font-size:11px}.hq-da-close{border:0;background:transparent;color:#94a4bb;font-size:22px;cursor:pointer}'
      +'.hq-da-messages{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:10px}.hq-da-msg{max-width:88%;padding:10px 12px;border-radius:13px;font-size:13px;line-height:1.65;white-space:pre-wrap}.hq-da-msg.user{align-self:flex-end;background:#e7b24c;color:#211502}.hq-da-msg.assistant{align-self:flex-start;background:#141e2e;border:1px solid rgba(148,164,187,.12)}.hq-da-msg.error{align-self:flex-start;background:rgba(244,112,138,.12);color:#ffc1ce}'
      +'.hq-da-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:7px}.hq-da-action,.hq-da-quick{border:1px solid rgba(231,178,76,.32);border-radius:999px;background:rgba(231,178,76,.08);color:#f4cd72;padding:7px 10px;font:600 11.5px/1 inherit;cursor:pointer}.hq-da-action[disabled]{opacity:.45;cursor:not-allowed}'
      +'.hq-da-offer{align-self:flex-start;max-width:90%;padding:11px 12px;border:1px solid rgba(45,212,191,.3);border-radius:13px;background:rgba(45,212,191,.07);font-size:12px;line-height:1.65}.hq-da-confirm{margin-top:8px;border:0;border-radius:10px;background:#2dd4bf;color:#05201c;padding:8px 12px;font-weight:800;cursor:pointer}.hq-da-confirm[disabled]{opacity:.5;cursor:not-allowed}'
      +'.hq-da-status{min-height:18px;padding:0 14px;color:#94a4bb;font-size:11px}.hq-da-recovery{display:none;padding:0 14px 10px}.hq-da-recovery.on{display:block}.hq-da-retry{border:1px solid rgba(244,112,138,.35);border-radius:999px;background:rgba(244,112,138,.08);color:#ffc1ce;padding:7px 11px;font:700 11px/1 inherit;cursor:pointer}.hq-da-compose{display:flex;gap:8px;padding:12px 14px 14px;border-top:1px solid rgba(148,164,187,.13)}.hq-da-input{flex:1;min-width:0;resize:none;border:1px solid rgba(148,164,187,.18);border-radius:12px;background:#070b13;color:#eaf1fa;padding:10px;font:13px/1.5 inherit;outline:none}.hq-da-attach{width:42px;flex:0 0 42px;border:1px solid rgba(148,164,187,.2);border-radius:12px;background:#141e2e;color:#f4cd72;font-size:20px;cursor:pointer}.hq-da-send{border:0;border-radius:12px;background:#e7b24c;color:#241604;padding:0 14px;font-weight:700;cursor:pointer}.hq-da-send[disabled],.hq-da-attach[disabled]{opacity:.5;cursor:not-allowed}'
      +'.hq-agent-focus{outline:3px solid rgba(244,205,114,.78)!important;outline-offset:3px!important;box-shadow:0 0 0 7px rgba(231,178,76,.16)!important}@media(max-width:640px){.hq-da-launch{right:14px;bottom:14px}.hq-da-launch.digital-human{bottom:94px}.hq-da-panel{right:14px;bottom:70px;height:calc(100vh - 88px)}}';
    doc.head.appendChild(style);
  }
  function mount(doc,win,username){
    username=normalizedUsername(username);
    if(!username||(!doc.getElementById('scTopic')&&!doc.getElementById('dhPhotoMode'))||doc.getElementById('hqDirectorAgent')) return null;
    var page=createPageContext(doc).page,isDigitalHuman=page==='digital_human_oneclick';
    addStyles(doc); var storage=win.sessionStorage;
    var state=readUnifiedState(storage,username),pending=false,currentPlan=null;
    function persist(){saveState(storage,state,username);}
    var assistantName='黄雀编导 Agent';
    var pageName=isDigitalHuman?'数字人一键生成':'文案编导';
    var launch=doc.createElement('button'); launch.type='button'; launch.className='hq-da-launch'+(isDigitalHuman?' digital-human':''); launch.id='hqDirectorAgent'; launch.textContent='✦ '+assistantName; launch.setAttribute('aria-expanded',state.open?'true':'false');
    var panel=doc.createElement('section'); panel.className='hq-da-panel'+(state.open?' on':''); panel.setAttribute('aria-label',assistantName);
    var head=doc.createElement('div'); head.className='hq-da-head';
    var title=doc.createElement('div'); title.innerHTML='<b>'+assistantName+'</b><span>贯通脚本和数字人 · 当前：'+pageName+'</span>';
    var close=doc.createElement('button'); close.type='button'; close.className='hq-da-close'; close.textContent='×'; close.setAttribute('aria-label','关闭'); head.appendChild(title); head.appendChild(close);
    var messages=doc.createElement('div'); messages.className='hq-da-messages';
    var status=doc.createElement('div'); status.className='hq-da-status';
    var recovery=doc.createElement('div'); recovery.className='hq-da-recovery';
    var retry=doc.createElement('button'); retry.type='button'; retry.className='hq-da-retry'; recovery.appendChild(retry);
    var compose=doc.createElement('div'); compose.className='hq-da-compose';
    var attach=doc.createElement('button'); attach.type='button'; attach.className='hq-da-attach'; attach.textContent='＋'; attach.setAttribute('aria-label','上传图片或视频'); attach.title='上传图片或视频';
    var attachmentInput=doc.createElement('input'); attachmentInput.type='file'; attachmentInput.accept='image/jpeg,image/png,image/webp,video/mp4,video/quicktime,video/webm,.jpg,.jpeg,.png,.webp,.mp4,.mov,.webm'; attachmentInput.multiple=true; attachmentInput.hidden=true;
    var input=doc.createElement('textarea'); input.className='hq-da-input'; input.rows=2; input.maxLength=6000; input.placeholder=isDigitalHuman?'把文案发给我，或问我下一步怎么做':'例如：我第一次用，下一步该做什么？';
    var send=doc.createElement('button'); send.type='button'; send.className='hq-da-send'; send.textContent='发送'; compose.appendChild(attach); compose.appendChild(attachmentInput); compose.appendChild(input); compose.appendChild(send);
    panel.appendChild(head); panel.appendChild(messages); panel.appendChild(status); panel.appendChild(recovery); panel.appendChild(compose); doc.body.appendChild(launch); doc.body.appendChild(panel);
    function setOpen(open){state.open=!!open; panel.classList.toggle('on',state.open); launch.setAttribute('aria-expanded',state.open?'true':'false'); persist(); if(state.open) input.focus();}
    function addMessage(role,content){state.messages.push({role:role,content:String(content||'')}); state.messages=state.messages.slice(-20); persist(); render();}
    function clearRecovery(){recovery.classList.remove('on');retry.onclick=null;}
    function showRecovery(kind){
      retry.textContent=kind==='production'?'重试原生产单':'重试原请求';
      retry.onclick=function(){clearRecovery();if(kind==='production')runProduction(state.pending_production,false);else runPending(state.pending_request,false);};
      recovery.classList.add('on');
    }
    function actionButton(action){
      var button=doc.createElement('button'); button.type='button'; button.className='hq-da-action'; button.textContent=action.label||'应用建议';
      button.onclick=function(){
        try{validatePlan(currentPlan,doc); var result=applyAction(action,doc,win); button.disabled=true; status.textContent=result+(page==='script'?'。若要生成，我会在对话中给你确认单。':'。授权、上传或生成仍需你确认。');}
        catch(error){status.textContent=error.message||'应用建议失败';}
      }; return button;
    }
    function productionCard(offer){
      var card=doc.createElement('div'); card.className='hq-da-offer';
      var summary=offer.summary||{};
      var copy=doc.createElement('div');
      copy.textContent='生产确认\n选题：'+String(summary.topic||'')+
        '\n规格：'+[summary.platform,summary.style,summary.duration].filter(Boolean).join(' · ')+
        '\n费用：'+offer.expected_cost+' 点（确认时由 CLI 再校验）';
      var button=doc.createElement('button'); button.type='button'; button.className='hq-da-confirm';
      button.textContent='确认生产并扣 '+offer.expected_cost+' 点'; button.disabled=pending;
      button.onclick=function(){confirmProduction(offer);};
      card.appendChild(copy); card.appendChild(button); return card;
    }
    function render(){
      messages.textContent='';
      if(!state.messages.length){
        var welcome=doc.createElement('div'); welcome.className='hq-da-msg assistant'; welcome.textContent='你好，我是黄雀编导 Agent，会在文案编导和数字人页面持续陪你生产。你可以把内容直接发给我，我会结合当前页面填充、切换或带你到下一步；涉及扣点、上传、授权、生成、删除和发布时，会保留必要的顾客确认。'; messages.appendChild(welcome);
        var quick=doc.createElement('div'); quick.className='hq-da-actions';
        (isDigitalHuman
          ?['我第一次用，带我走一遍','帮我看看还缺什么','照片模式和真人视频模式怎么选']
          :['帮我生成一份分镜脚本','我第一次用，带我走一遍','生成脚本后怎么做视频']
        ).forEach(function(label){var b=doc.createElement('button');b.type='button';b.className='hq-da-quick';b.textContent=label;b.onclick=function(){submit(label);};quick.appendChild(b);});
        messages.appendChild(quick);
      }
      state.messages.forEach(function(message,index){
        var box=doc.createElement('div'); box.className='hq-da-msg '+message.role; box.textContent=message.content; messages.appendChild(box);
        if(message.role==='assistant'&&index===state.messages.length-1&&currentPlan&&currentPlan.actions.length){
          var actions=doc.createElement('div'); actions.className='hq-da-actions'; currentPlan.actions.forEach(function(action){actions.appendChild(actionButton(action));}); messages.appendChild(actions);
        }
        if(message.role==='assistant'&&index===state.messages.length-1&&state.production_offer){
          messages.appendChild(productionCard(state.production_offer));
        }
      });
      messages.scrollTop=messages.scrollHeight; send.disabled=pending; attach.disabled=pending; input.disabled=pending;
    }
    function handleResult(result){
      state.pending_request=null; persist();
      currentPlan=result&&result.plan||null;
      state.production_offer=validProductionOffer(result&&result.production_offer);
      addMessage('assistant',result&&result.content||'我已经看完当前页面。');
      if(currentPlan&&currentPlan.actions.length){
        try{
          validatePlan(currentPlan,doc);
          var applied=currentPlan.actions.map(function(action){return applyAction(action,doc,win);});
          status.textContent=applied.join('；')+(state.production_offer?'。生产单已在对话中等待你确认。':'。');
          currentPlan=null;
          render();
        }catch(error){
          status.textContent=error.message||'自动操作失败，请重新告诉我你的要求';
        }
      }else{
        status.textContent='';
      }
    }
    function runProduction(record,resumed){
      record=validPendingProduction(record); if(!record) return;
      clearRecovery();
      state.pending_production=record; state.production_offer=record.offer; persist();
      pending=true; status.textContent=resumed?'正在恢复上次确认的生产任务…':'CLI 正在报价并提交生产…'; render();
      resumeProduction(win,record,function(updated){state.pending_production=validPendingProduction(updated);persist();},function(seconds,phase){
        status.textContent=phase==='submitting'?'CLI 正在确认原提交结果…':'脚本生产中，已用 '+seconds+' 秒…';
      }).then(function(result){
        state.pending_production=null; state.production_offer=null; persist();
        addMessage('assistant',formatScriptResult(result));
        status.textContent='生产结果已回传到对话。';
        if(win.HQ&&typeof win.HQ.refreshPoints==='function') win.HQ.refreshPoints();
      }).catch(function(error){
        if(error.priceChanged>0){
          state.pending_production=null; state.production_offer=null;
          persist(); addMessage('assistant','生成价格已更新为 '+error.priceChanged+' 点，没有扣点，原确认已失效。请核对后重新回复：确认生成。');
        }else{
          if(error.terminal){
            state.pending_production=null;
            if(!retainProductionOfferAfterError(error)) state.production_offer=null;
          }
          persist(); addMessage('error',error.message||'编导生产失败，请稍后重试');
          status.textContent=state.pending_production?'原生产单已保留，不会自动重提；请检查后手动重试。':'';
          if(state.pending_production) showRecovery('production');
        }
      }).finally(function(){pending=false;render();});
    }
    function confirmProduction(offer){
      if(pending||page!=='script') return;
      offer=validProductionOffer(offer); if(!offer){addMessage('error','生产单已失效，请重新告诉我你的需求');return;}
      if(offer.page_revision!==createPageSnapshot(doc).page_revision){
        state.production_offer=null; persist();
        addMessage('error','页面参数已经变化，原生产单已失效。请让我重新整理方案后再回复：确认生成。');
        return;
      }
      runProduction({offer:offer,job_id:null,created_at:Date.now()},false);
    }
    function runPending(record,resumed){
      record=validPendingRequest(record);
      if(!record) return;
      clearRecovery();
      state.pending_request=record; persist();
      pending=true;
      status.textContent=resumed?'正在恢复上次未完成的请求…':'正在结合当前页面判断…';
      render();
      resumeRequest(win,record,function(updated){
        state.pending_request=validPendingRequest(updated);
        persist();
      },function(seconds,phase){
        status.textContent=phase==='submitting'
          ?'正在确认上次提交结果…'
          :'黄雀编导 Agent 思考中，已用 '+seconds+' 秒…';
      }).then(handleResult).catch(function(error){
        if(error.terminal) state.pending_request=null;
        persist();
        addMessage('error',error.message||'黄雀编导 Agent 请求失败，请稍后重试');
        status.textContent=state.pending_request
          ?'原请求已保留，不会自动重提；请检查后手动重试。':'';
        if(state.pending_request) showRecovery('request');
      }).finally(function(){pending=false;render();});
    }
    function submit(value){
      value=String(value||input.value||'').trim(); if(!value||pending) return;
      clearRecovery();
      var body=buildPayload(value,doc,state,storage);
      var key='director-agent-'+Date.now().toString(36)+Math.random().toString(36).slice(2,10);
      var record=createPendingRequest(body,key,value);
      if(!record){ addMessage('error','黄雀编导 Agent 请求摘要保存失败，请重试'); return; }
      input.value=''; currentPlan=null; state.production_offer=null; addMessage('user',value);
      state.pending_request=record; persist();
      runPending(record,false);
    }
    attach.onclick=function(){
      attachmentInput.accept='image/jpeg,image/png,image/webp,video/mp4,video/quicktime,video/webm,.jpg,.jpeg,.png,.webp,.mp4,.mov,.webm';
      attachmentInput.multiple=true;
      attachmentInput.click();
    };
    attachmentInput.onchange=function(){
      var files=Array.prototype.slice.call(attachmentInput.files||[]);
      try{addMessage('assistant',attachFilesToPage(files,doc,win));}
      catch(error){addMessage('error',error.message||'图片或视频上传失败');}
      attachmentInput.value='';
    };
    launch.onclick=function(){setOpen(!state.open);}; close.onclick=function(){setOpen(false);}; send.onclick=function(){submit();};
    input.addEventListener('keydown',function(event){if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();submit();}});
    render();
    var resumeKind=autoResumeKind(state);
    if(state.pending_production){
      if(resumeKind==='production') runProduction(state.pending_production,true);
      else{status.textContent='发现未确认受理的原生产单，不会自动重提。';showRecovery('production');}
    }else if(state.pending_request){
      if(resumeKind==='request') runPending(state.pending_request,true);
      else{status.textContent='发现未确认受理的原请求，不会自动重提。';showRecovery('request');}
    }
    return {
      state:state,submit:submit,setOpen:setOpen,confirmProduction:confirmProduction,
      resume:function(){if(state.pending_production)runProduction(state.pending_production,true);else runPending(state.pending_request,true);}
    };
  }
  return {digest:digest,createPageContext:createPageContext,createPageSnapshot:createPageSnapshot,
    buildPayload:buildPayload,validatePlan:validatePlan,applyAction:applyAction,
    attachFilesToPage:attachFilesToPage,pollJob:pollJob,
    validPendingRequest:validPendingRequest,createPendingRequest:createPendingRequest,
    validProductionOffer:validProductionOffer,validPendingProduction:validPendingProduction,autoResumeKind:autoResumeKind,
    retainProductionOfferAfterError:retainProductionOfferAfterError,
    accountStorageKey:accountStorageKey,readState:readState,saveState:saveState,readUnifiedState:readUnifiedState,resumeRequest:resumeRequest,resumeProduction:resumeProduction,
    formatScriptResult:formatScriptResult,
    bootstrap:bootstrap,mount:mount,routes:ROUTES};
});
