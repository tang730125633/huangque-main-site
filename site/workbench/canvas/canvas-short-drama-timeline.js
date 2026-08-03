(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){
    root.HQCanvas=root.HQCanvas||{};
    root.HQCanvas.shortDramaTimeline=api;
  }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

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
  function equal(left,right){ return JSON.stringify(left)===JSON.stringify(right); }
  function normalize(input){
    input=input&&typeof input==='object'?clone(input):{};
    input.capabilities=input.capabilities&&typeof input.capabilities==='object'?
      input.capabilities:{};
    input.current_version=input.current_version&&typeof input.current_version==='object'?
      input.current_version:null;
    input.characters=Array.isArray(input.characters)?input.characters:[];
    input.versions=Array.isArray(input.versions)?input.versions:[];
    input.blockers=Array.isArray(input.blockers)?input.blockers:[];
    input.timeline_revision=number(input.timeline_revision,0);
    input.project_revision=number(input.project_revision,0);
    return input;
  }
  function createDraft(snapshot){
    snapshot=normalize(snapshot);
    var current=snapshot.current_version;
    if(!current) return null;
    return {
      versionId:text(current.id),
      timelineRevision:snapshot.timeline_revision,
      projectRevision:snapshot.project_revision,
      segments:clone(current.segments||[])
    };
  }
  function updateDraft(draft,segmentId,field,value){
    if(!draft) throw new Error('请先重建主时间轴草稿');
    var segment=draft.segments.find(function(item){ return text(item.id)===text(segmentId); });
    if(!segment) throw new Error('说话区间不存在，请刷新后重试');
    if(['start_ms','end_ms'].indexOf(field)>=0) segment[field]=Math.round(number(value,0));
    else if(field==='character_key') segment.character_key=text(value);
    else if(field==='speaking_mode'){
      segment.speaking_mode=text(value);
      if(segment.speaking_mode!=='visible') segment.face_target=null;
      else segment.face_target={type:'character',value:text(segment.character_key)};
    }else if(field==='face_target'){
      segment.face_target=value?{type:'character',value:text(value)}:null;
    }else throw new Error('不支持的主时间轴字段');
    if(field==='character_key'&&segment.speaking_mode==='visible'){
      segment.face_target={type:'character',value:text(value)};
    }
    return draft;
  }
  function changes(snapshot,draft){
    snapshot=normalize(snapshot);
    if(!draft||!snapshot.current_version) return [];
    var original={};
    (snapshot.current_version.segments||[]).forEach(function(item){
      original[text(item.id)]=item;
    });
    return draft.segments.filter(function(item){
      var source=original[text(item.id)];
      return !source||[
        'start_ms','end_ms','character_key','speaking_mode','face_target'
      ].some(function(key){ return !equal(item[key],source[key]); });
    }).map(function(item){
      return {
        id:text(item.id),start_ms:Math.round(number(item.start_ms,0)),
        end_ms:Math.round(number(item.end_ms,0)),
        character_key:text(item.character_key),
        speaking_mode:text(item.speaking_mode),
        face_target:item.face_target?clone(item.face_target):null
      };
    });
  }
  function statusLabel(status){
    return ({
      legacy:'\u5386\u53f2\u517c\u5bb9',
      draft:'\u5f85\u68c0\u67e5',
      blocked:'\u5f85\u8865\u5145',
      ready:'\u5df2\u786e\u8ba4',
      stale:'\u5df2\u8fc7\u671f'
    })[text(status)]||text(status);
  }
  function blockerText(blockers,snapshot){
    snapshot=normalize(snapshot);
    var current=snapshot.current_version||{};
    var segments=Array.isArray(current.segments)?current.segments:[];
    var names={};
    snapshot.characters.forEach(function(item){
      names[text(item.character_key)]=text(item.name||item.character_key);
    });
    return (blockers||[]).map(function(item){
      var segment=segments.find(function(candidate){
        return (
          item.segment_id&&text(candidate.id)===text(item.segment_id)
        )||(
          item.line_id&&text(candidate.line_id)===text(item.line_id)
        );
      });
      var location=[];
      if(item.line_id) location.push(text(item.line_id));
      if(segment&&names[text(segment.character_key)]){
        location.push(names[text(segment.character_key)]);
      }
      return (location.length?location.join(' / ')+'\uff1a':'')+
        text(item.message||item.code);
    }).join('\uff1b');
  }
  function optionsForCharacters(characters,current){
    return characters.map(function(item){
      var key=text(item.character_key);
      return '<option value="'+escapeHtml(key)+'"'+
        (key===text(current)?' selected':'')+'>'+
        escapeHtml(item.name||key)+'</option>';
    }).join('');
  }
  function renderPanel(input,draft,options){
    var snapshot=normalize(input),current=snapshot.current_version;
    options=options||{};
    var busy=!!options.busy,canEdit=options.canEdit!==false&&!options.conflictFrozen;
    var editable=!!(current&&snapshot.capabilities.save&&canEdit&&!busy);
    var shown=draft&&current&&draft.versionId===text(current.id)?
      draft.segments:(current&&current.segments||[]);
    var characterOptions=snapshot.characters||[];
    var segments=shown.map(function(segment){
      var visible=text(segment.speaking_mode)==='visible';
      return '<article class="nc-sdt-segment" data-timeline-segment="'+
        escapeHtml(segment.id)+'"><header><strong>'+
        escapeHtml(segment.line_id)+'</strong><span>'+
        escapeHtml(segment.shot_id)+'</span></header><div class="nc-sdt-grid">'+
        '<label>开始(ms)<input type="number" step="50" data-master-field="start_ms" '+
        'data-master-segment-id="'+escapeHtml(segment.id)+'" value="'+
        number(segment.start_ms,0)+'"'+(editable?'':' disabled')+'></label>'+
        '<label>结束(ms)<input type="number" step="50" data-master-field="end_ms" '+
        'data-master-segment-id="'+escapeHtml(segment.id)+'" value="'+
        number(segment.end_ms,0)+'"'+(editable?'':' disabled')+'></label>'+
        '<label>角色<select data-master-field="character_key" data-master-segment-id="'+
        escapeHtml(segment.id)+'"'+(editable?'':' disabled')+'>'+
        optionsForCharacters(characterOptions,segment.character_key)+'</select></label>'+
        '<label>说话模式<select data-master-field="speaking_mode" data-master-segment-id="'+
        escapeHtml(segment.id)+'"'+(editable?'':' disabled')+'>'+
        ['visible','offscreen','narration'].map(function(mode){
          var labels={visible:'画面内',offscreen:'画外音',narration:'旁白'};
          return '<option value="'+mode+'"'+
            (mode===text(segment.speaking_mode)?' selected':'')+'>'+labels[mode]+'</option>';
        }).join('')+'</select></label>'+
        '<label>可见角色<select data-master-field="face_target" data-master-segment-id="'+
        escapeHtml(segment.id)+'"'+(editable&&visible?'':' disabled')+'>'+
        '<option value="">未绑定</option>'+
        optionsForCharacters(
          characterOptions,
          segment.face_target&&segment.face_target.value
        )+'</select></label></div></article>';
    }).join('');
    var dirty=changes(snapshot,draft).length>0;
    var status=current?text(current.effective_status||current.status):'legacy';
    var history=snapshot.versions.slice(0,8).map(function(version){
      return '<li><strong>V'+number(version.version,0)+'</strong> '+
        escapeHtml(statusLabel(version.effective_status||version.status))+' · '+
        escapeHtml(text(version.timeline_hash).slice(0,10))+'</li>';
    }).join('');
    return '<section class="nc-sdt-panel"><header><div><span>PR-C 主时间轴</span>'+
      '<h3>说话人与权威时间基准</h3></div><strong class="nc-sdt-status" data-status="'+
      escapeHtml(status)+'">'+escapeHtml(statusLabel(status))+'</strong></header>'+
      '<p class="nc-sdt-note">配音、字幕、角色和可见说话区间统一保存为不可变版本；画外音和旁白不会进入口型输入。</p>'+
      (!current?'<p class="nc-sdt-empty">尚未建立主时间轴。锁定配音和字幕对齐后可生成草稿。</p>':
        '<div class="nc-sdt-segments">'+(segments||
          '<p class="nc-sdt-empty">当前版本没有说话区间。</p>')+'</div>')+
      ((current&&current.blockers||[]).length?
        '<p class="nc-sdt-error">'+escapeHtml(blockerText(
          current.blockers,snapshot
        ))+'</p>':'')+
      (snapshot.blockers.length?
        '<p class="nc-sdt-warning">'+escapeHtml(blockerText(
          snapshot.blockers,snapshot
        ))+'</p>':'')+
      '<div class="nc-sdt-actions">'+
      '<button type="button" data-action="rebuild-master-timeline"'+
      (snapshot.capabilities.rebuild&&canEdit&&!busy?'':' disabled')+
      '>重建主时间轴</button>'+
      '<button type="button" data-action="save-master-timeline"'+
      (editable&&dirty?'':' disabled')+'>保存区间修改</button>'+
      '<button type="button" data-action="confirm-master-timeline"'+
      (snapshot.capabilities.confirm&&canEdit&&!busy&&!dirty?'':' disabled')+
      '>'+(snapshot.capabilities.confirm_speaker_migration?
        '确认角色映射迁移':'确认主时间轴')+'</button></div>'+
      (options.conflictFrozen?
        '<p class="nc-sdt-error">检测到版本冲突，已冻结保存。请刷新后重新应用局部修改。</p>':'')+
      '<details class="nc-sdt-history"><summary>版本历史（'+
      snapshot.versions.length+'）</summary><ol>'+history+'</ol></details></section>';
  }
  return {
    normalize:normalize,
    createDraft:createDraft,
    updateDraft:updateDraft,
    changes:changes,
    statusLabel:statusLabel,
    blockerText:blockerText,
    renderPanel:renderPanel
  };
});
