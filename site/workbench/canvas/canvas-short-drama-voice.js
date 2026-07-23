(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.shortDramaVoice=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var VOICE_PATH='/api/gen/short-drama/voice';
  var VOICES_PATH='/api/gen/audio/voices';

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
    return {
      id:line.id,sort_order:number(line.sort_order,index),
      line_type:line.line_type==='narration'?'narration':'dialogue',
      character_key:text(line.character_key),
      character_name:text(line.character_name||line.character_key),
      source_text:text(line.source_text),speech_text:text(line.speech_text),
      subtitle_text:text(line.subtitle_text),
      subtitle_visible:line.subtitle_visible!==false,
      voice_key:voiceKey,
      voice_name:voiceMap[voiceKey]?voiceMap[voiceKey].display_name:(voiceKey||'未选择音色'),
      speed:number(line.speed,1),pitch:number(line.pitch,0),volume:number(line.volume,0),
      current_version:line.current_version,start_ms:line.start_ms,end_ms:line.end_ms
    };
  }
  function normalizeState(input,voices,options){
    input=input&&typeof input==='object'?input:{};
    options=options&&typeof options==='object'?options:{};
    var catalog=voiceItems(voices),voiceMap={};
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
      destroyed:!!options.destroyed
    };
  }
  function selectedShot(state){
    return state.shots.find(function(shot){ return shot.id===state.selectedShotId; })||null;
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
        escapeHtml(item.status)+'</small></button>';
    }).join('');
    var lines=shot?shot.lines.map(function(line){
      return '<article class="nc-sdv-line"><header><strong>'+
        escapeHtml(line.character_name)+'</strong><span>'+escapeHtml(line.voice_name)+
        '</span></header><label>发音文本<textarea disabled>'+
        escapeHtml(line.speech_text)+'</textarea></label><label>字幕文本<textarea disabled>'+
        escapeHtml(line.subtitle_text)+'</textarea></label><div class="nc-sdv-params">'+
        '<span>语速 '+line.speed+'</span><span>音调 '+line.pitch+
        '</span><span>音量 '+line.volume+'</span></div>'+
        '<button type="button" data-action="generate-line" disabled>生成配音</button></article>';
    }).join(''):'';
    return '<div class="nc-short-drama-voice" data-busy="'+state.busy+'">'+
      '<aside class="nc-sdv-rail"><header><span>配音字幕</span><h2>镜头列表</h2></header>'+
      rail+'</aside><main class="nc-sdv-editor"><header><span>逐句资产</span>'+
      '<h2>台词与字幕</h2></header>'+(shot&&shot.lines.length?lines:
      '<section class="nc-sdv-empty">当前镜头没有台词，将作为静音镜头。</section>')+
      '<section class="nc-sdv-timeline">字幕时间轴将在配音生成后显示。</section></main>'+
      '<aside class="nc-sdv-inspector"><header><span>只读基础阶段</span>'+
      '<h2>配音控制台</h2></header><dl><div><dt>项目预算</dt><dd>'+
      state.point_budget+' 点</dd></div><div><dt>累计已用</dt><dd>'+
      state.spent_points+' 点</dd></div></dl>'+
      '<button type="button" data-action="generate-shot" disabled>生成当前镜头</button>'+
      '<button type="button" data-action="save-timeline" disabled>保存字幕时间轴</button>'+
      '<p>本批次仅开放数据核对和音色映射，付费生成将在下一批次验收。</p>'+
      (state.error?'<div role="alert">'+escapeHtml(state.error)+'</div>':'')+
      '</aside></div>';
  }
  function createWorkspace(options){
    options=options||{};
    if(!options.client||typeof options.client.json!=='function') throw new Error('voice workspace requires a JSON client');
    if(!options.projectId) throw new Error('voice workspace requires projectId');
    var destroyed=false,snapshot=null,voices=[],host=options.host||null;
    var ui={busy:true,error:'',selectedShotId:options.selectedShotId};
    function render(){
      var html=renderWorkspace(snapshot||{}, {
        voices:voices,busy:ui.busy,error:ui.error,
        selectedShotId:ui.selectedShotId,destroyed:destroyed
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
    function onClick(event){
      var node=event&&event.target;
      while(node&&node!==host){
        if(node.getAttribute&&node.getAttribute('data-shot-id')!=null){
          selectShot(node.getAttribute('data-shot-id'));return;
        }
        node=node.parentNode;
      }
    }
    if(host&&typeof host.addEventListener==='function') host.addEventListener('click',onClick);
    function reload(){
      if(destroyed) return Promise.resolve(null);
      ui.busy=true;ui.error='';render();
      return Promise.all([
        options.client.json(VOICE_PATH+'?project_id='+encodeURIComponent(options.projectId)),
        options.client.json(VOICES_PATH)
      ]).then(function(results){
        if(destroyed) return null;
        snapshot=results[0];voices=voiceItems(results[1]);ui.busy=false;render();
        return snapshot;
      }).catch(function(error){
        if(destroyed) return null;
        ui.busy=false;ui.error=text(error&&error.message||error);render();
        return null;
      });
    }
    var ready=reload();
    return {
      projectId:options.projectId,ready:ready,render:render,reload:reload,
      selectShot:selectShot,
      getState:function(){ return clone(normalizeState(snapshot||{},voices,ui)); },
      destroy:function(){
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('click',onClick);
        destroyed=true;host=null;snapshot=null;voices=[];
      }
    };
  }
  return {
    normalizeState:normalizeState,
    renderWorkspace:renderWorkspace,
    createWorkspace:createWorkspace
  };
});
