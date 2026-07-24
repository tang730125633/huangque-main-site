(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.shortDramaVoice=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var VOICE_PATH='/api/gen/short-drama/voice';
  var VOICES_PATH='/api/gen/audio/voices';
  var QUOTE_PATH='/api/gen/short-drama/voice-quote';
  var GENERATE_PATH='/api/gen/short-drama/generate-voice';
  var SELECT_VERSION_PATH='/api/gen/short-drama/select-voice-version';
  var POLL_INTERVAL=1800;

  function text(value){ return String(value==null?'':value); }
  function number(value,fallback){
    var result=Number(value);
    return isFinite(result)?result:(fallback==null?0:fallback);
  }
  function clone(value){
    if(Array.isArray(value)) return value.map(clone);
    if(value&&typeof value==='object'){
      var copy={};
      Object.keys(value).forEach(function(key){ copy[key]=clone(value[key]); });
      return copy;
    }
    return value;
  }
  function escapeHtml(value){
    return text(value).replace(/[&<>"']/g,function(character){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character];
    });
  }
  function voiceItems(input){
    var items=Array.isArray(input)?input:(input&&Array.isArray(input.items)?input.items:[]);
    return items.map(function(item){
      return {
        voice_key:text(item.voice_key),
        display_name:text(item.display_name||item.voice_key||'未命名音色'),
        preview_url:text(item.preview_url)
      };
    });
  }
  function normalizeLine(line,index,voiceMap){
    line=line&&typeof line==='object'?line:{};
    var voiceKey=text(line.voice_key);
    var versions=(Array.isArray(line.versions)?line.versions:[]).map(function(version){
      version=version&&typeof version==='object'?version:{};
      return {
        version:number(version.version,0),status:text(version.status),
        audio_url:text(version.audio_url),audio_file:text(version.audio_file),
        duration_ms:number(version.duration_ms,0),cost:number(version.cost,0),
        voice_key:text(version.voice_key),input_hash:text(version.input_hash),
        error:text(version.error),created_at:number(version.created_at,0),
        settings:version.settings&&typeof version.settings==='object'?clone(version.settings):{}
      };
    });
    var job=line.job&&typeof line.job==='object'?{
      job_id:number(line.job.job_id,0),status:text(line.job.status),
      error:text(line.job.error),refunded:number(line.job.refunded,0)
    }:null;
    return {
      id:line.id,sort_order:number(line.sort_order,index),
      line_type:line.character_key==='narrator'?'narration':'dialogue',
      character_key:text(line.character_key),
      character_name:text(line.character_name||line.character_key),
      source_text:text(line.source_text),speech_text:text(line.speech_text),
      subtitle_text:text(line.subtitle_text),
      subtitle_visible:line.subtitle_visible!==false,
      voice_key:voiceKey,
      voice_name:voiceMap[voiceKey]?voiceMap[voiceKey].display_name:(voiceKey||'未选择音色'),
      speed:number(line.speed,1),pitch:number(line.pitch,0),volume:number(line.volume,0),
      current_version:line.current_version,start_ms:line.start_ms,end_ms:line.end_ms,
      input_hash:text(line.input_hash),versions:versions,job:job
    };
  }
  function normalizeState(input,voices,options){
    input=input&&typeof input==='object'?input:{};
    options=options&&typeof options==='object'?options:{};
    var catalog=voiceItems(voices),voiceMap=Object.create(null);
    catalog.forEach(function(item){ voiceMap[item.voice_key]=item; });
    var shots=(Array.isArray(input.shots)?input.shots:[]).map(function(shot,index){
      shot=shot&&typeof shot==='object'?shot:{};
      return {
        id:shot.id,shot_key:text(shot.shot_key||('镜头 '+(index+1))),
        sort_order:number(shot.sort_order,index),duration:number(shot.duration,0),
        locked:!!shot.locked,status:text(shot.status||'pending'),
        lines:(Array.isArray(shot.lines)?shot.lines:[]).map(function(line,lineIndex){
          return normalizeLine(line,lineIndex,voiceMap);
        })
      };
    }).sort(function(left,right){ return left.sort_order-right.sort_order; });
    var selected=options.selectedShotId||input.selectedShotId;
    if(!shots.some(function(shot){ return shot.id===selected; })) selected=shots[0]&&shots[0].id;
    return {
      project_id:input.project_id,revision:number(input.revision,0),
      stage:text(input.stage||'voice_review'),ratio:text(input.ratio||'9:16'),
      point_budget:number(input.point_budget,0),spent_points:number(input.spent_points,0),
      reserved_points:number(input.reserved_points,0),shots:shots,voices:catalog,
      selectedShotId:selected,busy:!!options.busy,error:text(options.error),
      destroyed:!!options.destroyed,canEdit:options.canEdit!==false,
      operationBusy:!!options.operationBusy,
      operationError:text(options.operationError)
    };
  }
  function selectedShot(state){
    return state.shots.find(function(shot){ return shot.id===state.selectedShotId; })||null;
  }
  function shotStatusLabel(status){
    switch(status){
      case 'pending': return '待配音';
      case 'silent': return '静音';
      case 'ready': return '待核对';
      case 'done': return '已完成';
      case 'failed': return '失败';
      default: return '状态未知';
    }
  }
  function lineStatus(line){
    var status=line.job&&line.job.status;
    if(status==='pending') return '等待生成';
    if(status==='running') return '正在生成';
    if(status==='metadata_pending') return '音频已生成，正在解析时长';
    if(status==='failed') return line.job.refunded===1?'生成失败 · 已退款':'生成失败 · 退款处理中';
    if(line.current_version) return '配音已完成';
    return '未生成';
  }
  function currentVersion(line){
    return line.versions.find(function(item){
      return item.version===number(line.current_version,0);
    })||null;
  }
  function optionHtml(voices,selected){
    var known=voices.some(function(item){ return item.voice_key===selected; });
    var items=voices.map(function(item){
      return '<option value="'+escapeHtml(item.voice_key)+'"'+
        (item.voice_key===selected?' selected':'')+'>'+escapeHtml(item.display_name)+'</option>';
    }).join('');
    if(selected&&!known){
      items='<option value="'+escapeHtml(selected)+'" selected>'+
        escapeHtml(selected)+'</option>'+items;
    }
    return '<option value="">请选择音色</option>'+items;
  }
  function audioUrl(version){
    if(!version) return '';
    if(version.audio_url) return version.audio_url;
    return version.audio_file?'/api/gen/file/'+version.audio_file.replace(/^\/+/,''):'';
  }
  function renderWorkspace(input,options){
    options=options||{};
    var state=normalizeState(input,options.voices,options);
    var shot=selectedShot(state);
    var rail=state.shots.map(function(item){
      return '<button type="button" class="nc-sdv-shot'+
        (item.id===state.selectedShotId?' is-selected':'')+
        '" data-shot-id="'+escapeHtml(item.id)+'"><strong>'+escapeHtml(item.shot_key)+
        '</strong><small>'+item.duration+' 秒 · '+item.lines.length+' 句 · '+
        shotStatusLabel(item.status)+'</small></button>';
    }).join('');
    var lines=shot?shot.lines.map(function(line){
      var active=currentVersion(line),busy=line.job&&
        ['pending','running','metadata_pending'].indexOf(line.job.status)>=0;
      var disabled=state.canEdit&&!state.operationBusy?'':' disabled';
      var history=line.versions.map(function(version){
        var url=audioUrl(version);
        return '<li><span>V'+version.version+' · '+escapeHtml(version.status)+
          (version.duration_ms?' · '+(version.duration_ms/1000).toFixed(1)+' 秒':'')+
          ' · '+version.cost+' 点</span>'+
          (url?'<button type="button" data-action="preview-version" data-line-id="'+
            escapeHtml(line.id)+'" data-audio-url="'+escapeHtml(url)+'">试听</button>':'')+
          (version.status==='done'&&version.input_hash===line.input_hash?
            '<button type="button" data-action="select-version" data-line-id="'+
            escapeHtml(line.id)+'" data-version="'+version.version+'"'+
            (state.canEdit?'':' disabled')+'>设为当前</button>':'')+
          '</li>';
      }).join('');
      return '<article class="nc-sdv-line"><header><strong>'+
        escapeHtml(line.character_name)+'</strong>'+
        (line.line_type==='narration'?'<span class="nc-sdv-line-type">旁白/叙述</span>':'')+
        '<span class="nc-sdv-status">'+escapeHtml(lineStatus(line))+
        '</span></header><label>发音文本<textarea disabled>'+
        escapeHtml(line.speech_text)+'</textarea></label><label>字幕文本<textarea disabled>'+
        escapeHtml(line.subtitle_text)+'</textarea></label>'+
        '<div class="nc-sdv-params"><label>音色<select data-field="voice_key" data-line-id="'+
        escapeHtml(line.id)+'"'+disabled+'>'+optionHtml(state.voices,line.voice_key)+'</select></label>'+
        '<label>语速<input data-field="speed" data-line-id="'+escapeHtml(line.id)+
        '" type="number" min="0.5" max="2" step="0.1" value="'+line.speed+'"'+disabled+'></label>'+
        '<label>音调<input data-field="pitch" data-line-id="'+escapeHtml(line.id)+
        '" type="number" min="-12" max="12" step="1" value="'+line.pitch+'"'+disabled+'></label>'+
        '<label>音量<input data-field="volume" data-line-id="'+escapeHtml(line.id)+
        '" type="number" min="-50" max="100" step="1" value="'+line.volume+'"'+disabled+'></label></div>'+
        '<div class="nc-sdv-actions"><button type="button" data-action="preview-voice" data-line-id="'+
        escapeHtml(line.id)+'">试听音色</button><button type="button" data-action="generate-line" data-line-id="'+
        escapeHtml(line.id)+'"'+(busy||!state.canEdit||state.operationBusy?' disabled':'')+'>'+
        (line.job&&line.job.status==='failed'?'重新生成':'生成配音')+'</button></div>'+
        (active&&audioUrl(active)?'<audio controls preload="none" src="'+escapeHtml(audioUrl(active))+
          '" data-current-audio="'+escapeHtml(line.id)+'"></audio>':'')+
        (line.job&&line.job.status==='failed'?'<p class="nc-sdv-error">'+
          escapeHtml(line.job.error||'配音生成失败')+'</p>':'')+
        (history?'<details class="nc-sdv-history"><summary>历史版本（'+line.versions.length+
          '）</summary><ul>'+history+'</ul></details>':'')+'</article>';
    }).join(''):'';
    var editorBody;
    if(state.error){
      editorBody='<section class="nc-sdv-empty" data-state="error" role="alert">'+
        '<strong>配音数据加载失败</strong><p>'+escapeHtml(state.error)+'</p></section>';
    }else if(state.busy){
      editorBody='<section class="nc-sdv-empty" data-state="loading">正在加载配音数据…</section>';
    }else if(!shot){
      editorBody='<section class="nc-sdv-empty" data-state="empty">暂无镜头，请先完成分镜。</section>';
    }else if(shot.lines.length){
      editorBody=lines+'<section class="nc-sdv-timeline">字幕时间轴将在配音生成后显示。</section>';
    }else if(shot.status==='silent'){
      editorBody='<section class="nc-sdv-empty" data-state="silent">当前镜头为静音镜头，没有台词。</section>'+
        '<section class="nc-sdv-timeline">静音镜头无需生成配音。</section>';
    }else{
      editorBody='<section class="nc-sdv-empty" data-state="pending">当前镜头台词尚未就绪。</section>';
    }
    return '<div class="nc-short-drama-voice" data-busy="'+state.busy+'">'+
      '<aside class="nc-sdv-rail"><header><span>配音字幕</span><h2>镜头列表</h2></header>'+
      rail+'</aside><main class="nc-sdv-editor"><header><span>逐句资产</span>'+
      '<h2>台词与字幕</h2></header>'+editorBody+'</main>'+
      '<aside class="nc-sdv-inspector"><header><span>C-1 配音生成</span>'+
      '<h2>配音控制台</h2></header><dl><div><dt>项目预算</dt><dd>'+
      state.point_budget+' 点</dd></div><div><dt>累计已用</dt><dd>'+
      state.spent_points+' 点</dd></div><div><dt>处理中</dt><dd>'+
      state.reserved_points+' 点</dd></div></dl>'+
      '<button type="button" data-action="generate-shot"'+(state.canEdit&&!state.operationBusy?'':' disabled')+'>生成当前镜头未完成台词</button>'+
      '<button type="button" data-action="generate-all"'+(state.canEdit&&!state.operationBusy?'':' disabled')+'>生成全剧未完成台词</button>'+
      '<button type="button" data-action="save-timeline" disabled>保存字幕时间轴</button>'+
      (state.operationError?'<p class="nc-sdv-error" role="alert">'+escapeHtml(state.operationError)+'</p>':'')+
      '<p>询价免费；确认后逐句扣点。失败任务会自动退款，重试需要重新询价。</p>'+
      '</aside></div>';
  }
  function createWorkspace(options){
    options=options||{};
    if(!options.client||typeof options.client.json!=='function') throw new Error('voice workspace requires a JSON client');
    if(!options.projectId) throw new Error('voice workspace requires projectId');
    var client=options.client;
    var destroyed=false,snapshot=null,voices=[],host=options.host||null,requestGeneration=0;
    var pollTimer=null,activeAudio=null,generationBusy=false;
    var ui={busy:true,error:'',operationError:'',selectedShotId:options.selectedShotId};
    function callJson(path,requestOptions){
      return Promise.resolve().then(function(){
        if(destroyed) return null;
        var scoped=requestOptions?Object.assign({},requestOptions):{};
        if(options.boardId){
          scoped.headers=Object.assign({},scoped.headers||{}, {
            'X-Canvas-Board-Id':String(options.boardId)
          });
        }
        return client.json(path,scoped);
      });
    }
    function render(){
      var html=renderWorkspace(snapshot||{}, {
        voices:voices,busy:destroyed?false:ui.busy,error:ui.error,
        selectedShotId:ui.selectedShotId,destroyed:destroyed,canEdit:options.canEdit,
        operationBusy:generationBusy,operationError:ui.operationError
      });
      if(host&&!destroyed) host.innerHTML=html;
      return html;
    }
    function selectShot(shotId){
      if(destroyed||!snapshot||!Array.isArray(snapshot.shots)) return false;
      var exists=snapshot.shots.some(function(shot){ return shot.id===shotId; });
      if(!exists) return false;
      ui.selectedShotId=shotId;render();return true;
    }
    function allLines(){
      var result=[];
      (snapshot&&snapshot.shots||[]).forEach(function(shot){
        (shot.lines||[]).forEach(function(line){ result.push(line); });
      });
      return result;
    }
    function findLine(lineId){
      return allLines().find(function(line){ return line.id===lineId; })||null;
    }
    function editableItem(line){
      return {
        line_id:line.id,voice_key:text(line.voice_key),
        speed:number(line.speed,1),pitch:number(line.pitch,0),volume:number(line.volume,0)
      };
    }
    function unfinished(line){
      var status=line&&line.job&&line.job.status;
      if(['pending','running','metadata_pending'].indexOf(status)>=0) return false;
      var version=currentVersion(normalizeLine(line,0,Object.create(null)));
      if(!version) return true;
      var settings=version.settings||{};
      return version.voice_key!==text(line.voice_key)||
        number(settings.speed,1)!==number(line.speed,1)||
        number(settings.pitch,0)!==number(line.pitch,0)||
        number(settings.volume,0)!==number(line.volume,0);
    }
    function requestKey(lineId){
      return 'sdv-'+text(lineId).replace(/[^A-Za-z0-9._:-]/g,'').slice(0,24)+'-'+
        Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10);
    }
    function requireWritable(){
      if(options.canEdit===false) throw new Error('当前为只读权限，不能生成或切换配音');
    }
    function confirmQuote(quote,items){
      if(!quote||!Array.isArray(quote.items)||number(quote.total_cost,-1)<0){
        throw new Error('配音询价结果无效');
      }
      if(quote.can_submit===false){
        throw new Error(
          number(quote.points_left,0)<number(quote.total_cost,0)?
            '账户点数不足，请充值后再生成配音':
            '短剧项目预算不足，请调整预算后再生成配音'
        );
      }
      if(typeof options.confirm==='function'){
        return Promise.resolve(options.confirm(
          quote.total_cost,
          Object.assign({},quote,{
            kind:'voice',line_count:items.length,items:quote.items
          }),
          items
        ));
      }
      var globalConfirm=typeof globalThis!=='undefined'&&globalThis.confirm;
      return Promise.resolve(typeof globalConfirm==='function'?
        globalConfirm('生成 '+items.length+' 条配音将消耗 '+quote.total_cost+' 点，确认提交吗？'):true);
    }
    function submitQuoteItem(item,line){
      var payload={
        project_id:snapshot.project_id,revision:number(snapshot.revision,0),
        line_id:line.id,voice_key:text(line.voice_key),
        speed:number(line.speed,1),pitch:number(line.pitch,0),volume:number(line.volume,0),
        quote_token:item.quote_token
      };
      var requestOptions={
        method:'POST',body:payload,headers:{'Idempotency-Key':requestKey(line.id)}
      };
      return callJson(GENERATE_PATH,requestOptions).catch(function(error){
        if(error&&error.code==='timeout') return callJson(GENERATE_PATH,requestOptions);
        throw error;
      });
    }
    function mapConcurrent(entries,limit,worker){
      var cursor=0,results=[];
      function run(){
        var index=cursor++;
        if(index>=entries.length) return Promise.resolve();
        return Promise.resolve(worker(entries[index],index)).then(function(value){
          results[index]={ok:true,value:value};
        },function(error){
          results[index]={ok:false,error:error};
        }).then(run);
      }
      var workers=[];
      for(var i=0;i<Math.min(limit,entries.length);i+=1) workers.push(run());
      return Promise.all(workers).then(function(){ return results; });
    }
    function generateLines(lines){
      try{ requireWritable(); }catch(error){ return Promise.reject(error); }
      if(generationBusy) return Promise.reject(new Error('配音请求正在处理中，请勿重复提交'));
      lines=(lines||[]).filter(Boolean);
      if(!lines.length) return Promise.reject(new Error('没有需要生成的台词'));
      var items=lines.map(editableItem);
      generationBusy=true;ui.operationError='';render();
      return callJson(QUOTE_PATH,{
        method:'POST',body:{
          project_id:snapshot.project_id,revision:number(snapshot.revision,0),items:items
        }
      }).then(function(quote){
        return confirmQuote(quote,items).then(function(confirmed){
          if(!confirmed) return {cancelled:true,quote:quote,results:[]};
          var byLine=Object.create(null);
          quote.items.forEach(function(item){ byLine[item.line_id]=item; });
          return mapConcurrent(lines,3,function(line){
            if(!byLine[line.id]) throw new Error('询价结果缺少台词 '+line.id);
            return submitQuoteItem(byLine[line.id],line);
          }).then(function(results){
            return reload(true).then(function(){
              return {cancelled:false,quote:quote,results:results};
            });
          });
        });
      }).catch(function(error){
        ui.operationError=text(error&&error.message||error);throw error;
      }).finally(function(){
        generationBusy=false;render();
      });
    }
    function generateLine(lineId){ return generateLines([findLine(lineId)]); }
    function generateShot(){
      var shot=selectedShot(normalizeState(snapshot||{},voices,{selectedShotId:ui.selectedShotId}));
      return generateLines(shot?(snapshot.shots.find(function(item){ return item.id===shot.id; }).lines||[]).filter(unfinished):[]);
    }
    function generateAll(){ return generateLines(allLines().filter(unfinished)); }
    function selectVersion(lineId,version){
      try{ requireWritable(); }catch(error){ return Promise.reject(error); }
      return callJson(SELECT_VERSION_PATH,{
        method:'POST',body:{
          project_id:snapshot.project_id,revision:number(snapshot.revision,0),
          line_id:lineId,version:number(version,0)
        }
      }).then(function(result){
        return reload().then(function(){
          if(typeof options.onChange==='function') return Promise.resolve(options.onChange({
            project_id:snapshot.project_id,revision:result.revision,stage:snapshot.stage
          })).then(function(){ return result; });
          return result;
        });
      });
    }
    function stopAudio(){
      if(activeAudio&&typeof activeAudio.pause==='function') activeAudio.pause();
      activeAudio=null;
    }
    function stopNativeAudio(except){
      if(!host||typeof host.querySelectorAll!=='function') return;
      Array.prototype.forEach.call(host.querySelectorAll('audio'),function(audio){
        if(audio!==except&&typeof audio.pause==='function') audio.pause();
      });
    }
    function preview(url){
      stopAudio();stopNativeAudio(null);
      if(!url) return false;
      var factory=options.audioFactory||
        (typeof Audio==='function'?function(source){ return new Audio(source); }:null);
      if(!factory) return false;
      activeAudio=factory(url);
      if(activeAudio&&typeof activeAudio.play==='function'){
        var playing=activeAudio.play();
        if(playing&&typeof playing.catch==='function') playing.catch(function(){});
      }
      return true;
    }
    function previewVoice(lineId){
      var line=findLine(lineId),voice=line&&voices.find(function(item){
        return item.voice_key===line.voice_key;
      });
      return preview(voice&&voice.preview_url);
    }
    function onClick(event){
      var node=event&&event.target;
      while(node&&node!==host){
        if(node.getAttribute&&node.getAttribute('data-action')){
          var action=node.getAttribute('data-action');
          var lineId=node.getAttribute('data-line-id');
          var task=null;
          if(action==='generate-line') task=generateLine(lineId);
          else if(action==='generate-shot') task=generateShot();
          else if(action==='generate-all') task=generateAll();
          else if(action==='select-version') task=selectVersion(lineId,node.getAttribute('data-version'));
          else if(action==='preview-version') preview(node.getAttribute('data-audio-url'));
          else if(action==='preview-voice') previewVoice(lineId);
          if(task&&typeof task.catch==='function') task.catch(function(){});
          return;
        }
        if(node.getAttribute&&node.getAttribute('data-shot-id')!=null){
          selectShot(node.getAttribute('data-shot-id'));return;
        }
        node=node.parentNode;
      }
    }
    function onChange(event){
      var node=event&&event.target;
      if(!node||!node.getAttribute) return;
      var field=node.getAttribute('data-field'),line=findLine(node.getAttribute('data-line-id'));
      if(!line||['voice_key','speed','pitch','volume'].indexOf(field)<0) return;
      line[field]=field==='voice_key'?text(node.value):number(node.value,line[field]);
    }
    if(host&&typeof host.addEventListener==='function') host.addEventListener('click',onClick);
    if(host&&typeof host.addEventListener==='function') host.addEventListener('change',onChange);
    function onNativePlay(event){ stopAudio();stopNativeAudio(event&&event.target); }
    if(host&&typeof host.addEventListener==='function') host.addEventListener('play',onNativePlay,true);
    function clearPoll(){
      if(pollTimer!=null&&typeof clearTimeout==='function') clearTimeout(pollTimer);
      pollTimer=null;
    }
    function schedulePoll(){
      clearPoll();
      if(destroyed) return;
      var active=allLines().some(function(line){
        return line.job&&['pending','running','metadata_pending'].indexOf(line.job.status)>=0;
      });
      if(active&&typeof setTimeout==='function'){
        pollTimer=setTimeout(function(){
          pollTimer=null;reload(true);
        },number(options.pollInterval,POLL_INTERVAL));
      }
    }
    function reload(silent){
      if(destroyed) return Promise.resolve(null);
      var generation=++requestGeneration;
      if(!silent){ ui.busy=true;ui.error='';render(); }
      return Promise.all([
        callJson(VOICE_PATH+'?project_id='+encodeURIComponent(options.projectId)),
        callJson(VOICES_PATH)
      ]).then(function(results){
        if(destroyed||generation!==requestGeneration) return null;
        snapshot=results[0];voices=voiceItems(results[1]);ui.busy=false;render();schedulePoll();
        return snapshot;
      }).catch(function(error){
        if(destroyed||generation!==requestGeneration) return null;
        ui.busy=false;
        if(silent) ui.operationError=text(error&&error.message||error);
        else ui.error=text(error&&error.message||error);
        render();
        return null;
      });
    }
    var ready=reload();
    return {
      projectId:options.projectId,ready:ready,render:render,reload:reload,
      selectShot:selectShot,generateLine:generateLine,generateShot:generateShot,
      generateAll:generateAll,selectVersion:selectVersion,preview:preview,
      getState:function(){
        return clone(normalizeState(snapshot||{},voices,{
          busy:destroyed?false:ui.busy,error:ui.error,
          selectedShotId:ui.selectedShotId,destroyed:destroyed,canEdit:options.canEdit,
          operationBusy:generationBusy,operationError:ui.operationError
        }));
      },
      destroy:function(){
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('click',onClick);
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('change',onChange);
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('play',onNativePlay,true);
        clearPoll();stopAudio();stopNativeAudio(null);
        destroyed=true;requestGeneration+=1;ui.busy=false;ui.error='';ui.operationError='';host=null;snapshot=null;voices=[];
      }
    };
  }
  return {
    normalizeState:normalizeState,
    renderWorkspace:renderWorkspace,
    createWorkspace:createWorkspace
  };
});
