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
  var SAVE_TIMELINE_PATH='/api/gen/short-drama/save-voice-timeline';
  var SET_LOCK_PATH='/api/gen/short-drama/set-voice-shot-lock';
  var ALIGNMENT_JOBS_PATH='/api/gen/short-drama/subtitle-alignment/jobs';
  var ALIGNMENT_TIMELINE_PATH='/api/gen/short-drama/subtitle-alignment/timeline';
  var ALIGNMENT_LOCK_PATH='/api/gen/short-drama/subtitle-alignment/lock';
  var CONFIRM_PATH='/api/gen/short-drama/confirm';
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
      error:text(line.job.error),refunded:number(line.job.refunded,0),
      idempotency_key:text(line.job.idempotency_key)
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
      suggested_start_ms:line.suggested_start_ms,
      suggested_end_ms:line.suggested_end_ms,
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
        timeline_revision:number(shot.timeline_revision,1),
        lockable:shot.lockable===true,
        lock_blockers:(Array.isArray(shot.lock_blockers)?shot.lock_blockers:[]).map(clone),
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
      operationError:text(options.operationError),
      timelineDirty:!!options.timelineDirty,
      timelinePlaying:!!options.timelinePlaying,
      timelineCursorMs:number(options.timelineCursorMs,0),
      playingLineId:text(options.playingLineId),
      conflictFrozen:!!options.conflictFrozen,
      unlocked_shot_count:number(input.unlocked_shot_count,shots.filter(function(shot){
        return !shot.locked;
      }).length),
      handoff_blocked:input.handoff_blocked!==false,
      handoff_blockers:(Array.isArray(input.handoff_blockers)?
        input.handoff_blockers:[]).map(clone),
      alignment:input.alignment&&typeof input.alignment==='object'?
        clone(input.alignment):null,
      alignmentDraft:options.alignmentDraft&&typeof options.alignmentDraft==='object'?
        clone(options.alignmentDraft):null
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
  function formatMs(value){
    return value==null?'--':(number(value,0)/1000).toFixed(2)+'s';
  }
  function blockerText(items){
    var seen=Object.create(null);
    return (items||[]).map(function(item){
      return text(item.message||item.code);
    }).filter(function(message){
      if(!message||seen[message]) return false;
      seen[message]=true;
      return true;
    }).join('；');
  }
  function timelineBlocker(code,message,shot,line,details){
    return Object.assign({
      code:code,message:message,shot_id:shot&&shot.id,line_id:line&&line.id
    },details||{});
  }
  function recommendedSpeed(line,duration,available){
    if(!line||duration<=0||available<=0) return null;
    var current=number(line.speed,1);
    if(current<0.5||current>2) return null;
    var result=Math.ceil((current*duration/available*1.03)*20)/20;
    return result>=0.5&&result<=2?Number(result.toFixed(2)):null;
  }
  function analyzeShotTiming(shot){
    var result={blockers:[],byLine:Object.create(null)};
    if(!shot) return result;
    var limit=Math.max(0,number(shot.duration,0)*1000);
    var audio=[],subtitles=[];
    (shot.lines||[]).slice().sort(function(left,right){
      return number(left.sort_order,0)-number(right.sort_order,0)||
        text(left.id).localeCompare(text(right.id));
    }).forEach(function(line){
      var version=currentVersion(line),timing={
        shotDurationMs:limit,audioDurationMs:number(version&&version.duration_ms,0),
        audioEndMs:null,subtitleEndMs:null,audioOverflowMs:0,
        subtitleOverflowMs:0,overflowMs:0,recommendedSpeed:null,
        settingsPending:false
      };
      result.byLine[line.id]=timing;
      if(!version||version.status==='failed'){
        result.blockers.push(timelineBlocker(
          'missing_current_version','存在尚未生成成功配音的台词',shot,line
        ));return;
      }
      if(version.status==='metadata_pending'||timing.audioDurationMs<=0){
        result.blockers.push(timelineBlocker(
          'metadata_pending','音频时长仍在解析，请稍后刷新',shot,line
        ));return;
      }
      var settings=version.settings||{};
      timing.settingsPending=number(settings.speed,1)!==number(line.speed,1)||
        number(settings.pitch,0)!==number(line.pitch,0)||
        number(settings.volume,0)!==number(line.volume,0)||
        text(version.voice_key)!==text(line.voice_key);
      if(timing.settingsPending){
        result.blockers.push(timelineBlocker(
          'stale_current_version',
          '当前配音版本与音色参数不一致，请重新生成配音',shot,line
        ));
      }
      if(line.start_ms==null||line.end_ms==null){
        result.blockers.push(timelineBlocker(
          'timeline_missing','字幕时间轴尚未保存',shot,line
        ));return;
      }
      var start=number(line.start_ms,-1),end=number(line.end_ms,-1);
      if(start<0||end<=start){
        result.blockers.push(timelineBlocker(
          'timeline_invalid','字幕时间值不完整或顺序无效',shot,line
        ));return;
      }
      timing.audioEndMs=start+timing.audioDurationMs;
      timing.subtitleEndMs=end;
      timing.audioOverflowMs=Math.max(0,timing.audioEndMs-limit);
      timing.subtitleOverflowMs=Math.max(0,end-limit);
      timing.overflowMs=Math.max(timing.audioOverflowMs,timing.subtitleOverflowMs);
      if(timing.overflowMs>0){
        if(timing.audioOverflowMs>0){
          timing.recommendedSpeed=recommendedSpeed(
            line,timing.audioDurationMs,limit-start
          );
        }
        result.blockers.push(timelineBlocker(
          'duration_overflow','配音或字幕超过镜头时长',shot,line,{
            overflow_ms:timing.overflowMs,
            audio_end_ms:timing.audioEndMs,
            subtitle_end_ms:timing.subtitleEndMs,
            audio_overflow_ms:timing.audioOverflowMs,
            subtitle_overflow_ms:timing.subtitleOverflowMs,
            recommended_speed:timing.recommendedSpeed
          }
        ));
      }
      audio.push([start,timing.audioEndMs,line]);
      if(line.subtitle_visible!==false){
        if(!text(line.subtitle_text).trim()){
          result.blockers.push(timelineBlocker(
            'timeline_invalid','可见字幕文本不能为空',shot,line
          ));
        }
        subtitles.push([start,end,line]);
      }
    });
    function overlaps(intervals,code,message){
      intervals.sort(function(left,right){ return left[0]-right[0]||left[1]-right[1]; });
      for(var index=1;index<intervals.length;index+=1){
        if(intervals[index][0]<intervals[index-1][1]){
          result.blockers.push(timelineBlocker(
            code,message,shot,intervals[index][2]
          ));
        }
      }
    }
    overlaps(audio,'audio_overlap','配音播放时间发生重叠');
    overlaps(subtitles,'subtitle_overlap','可见字幕时间区间发生重叠');
    return result;
  }
  function analyzeAlignmentDraft(version,draft){
    var result={dirty:false,blockers:[],byLine:Object.create(null),lines:[]};
    if(!version||!Array.isArray(version.timeline)) return result;
    var draftLines=draft&&draft.versionId===version.id&&
      Array.isArray(draft.lines)?draft.lines:[];
    var drafts=Object.create(null);
    draftLines.forEach(function(line){ drafts[text(line.line_id)]=line; });
    version.timeline.forEach(function(source){
      var edited=drafts[text(source.line_id)]||source;
      var line={
        line_id:text(source.line_id),
        subtitle_start_ms:Math.round(number(edited.subtitle_start_ms,0)),
        subtitle_end_ms:Math.round(number(edited.subtitle_end_ms,0))
      };
      result.lines.push(line);
      result.dirty=result.dirty||
        line.subtitle_start_ms!==number(source.subtitle_start_ms,0)||
        line.subtitle_end_ms!==number(source.subtitle_end_ms,0);
      var lineBlockers=[];
      if(line.subtitle_start_ms<number(source.audio_start_ms,0)||
          line.subtitle_end_ms>number(source.audio_end_ms,0)||
          line.subtitle_end_ms<=line.subtitle_start_ms){
        lineBlockers.push({
          code:'timeline_boundary_invalid',
          message:'字幕边界必须位于只读音频区间内'
        });
      }
      result.byLine[line.line_id]=lineBlockers;
      result.blockers=result.blockers.concat(lineBlockers);
    });
    result.lines.slice().sort(function(left,right){
      return left.subtitle_start_ms-right.subtitle_start_ms||
        left.subtitle_end_ms-right.subtitle_end_ms;
    }).forEach(function(line,index,ordered){
      if(index&&line.subtitle_start_ms<ordered[index-1].subtitle_end_ms){
        var blocker={code:'subtitle_overlap',message:'字幕时间区间发生重叠'};
        result.byLine[line.line_id].push(blocker);
        result.blockers.push(blocker);
      }
    });
    return result;
  }
  function renderWorkspace(input,options){
    options=options||{};
    var state=normalizeState(input,options.voices,options);
    var shot=selectedShot(state);
    var timelineWritable=!!(shot&&state.canEdit&&state.stage==='voice_review'&&
      !shot.locked&&!state.operationBusy&&!state.conflictFrozen);
    var timingAnalysis=analyzeShotTiming(shot);
    var alignment=state.alignment||{};
    var alignmentCurrent=alignment.current_version||null;
    var alignmentReadiness=alignment.readiness||{ready:false,blockers:[]};
    var alignmentHandoff=alignment.handoff||{
      required:!!alignmentCurrent,
      ready:!alignmentCurrent||
        alignmentCurrent.effective_status==='locked',
      blockers:[]
    };
    var alignmentActions=alignment.actions||{};
    var alignmentQuality=alignmentCurrent&&alignmentCurrent.quality||{};
    var alignmentReview=alignmentCurrent&&alignmentCurrent.review||null;
    var alignmentDraftAnalysis=analyzeAlignmentDraft(
      alignmentCurrent,state.alignmentDraft
    );
    var alignmentEditable=!!(
      alignmentCurrent&&alignmentActions.save&&state.canEdit&&
      state.stage==='voice_review'&&!state.operationBusy&&!state.conflictFrozen
    );
    var alignmentLines=alignmentCurrent?
      (alignmentCurrent.timeline||[]).map(function(source){
        var edited=alignmentDraftAnalysis.lines.find(function(item){
          return item.line_id===text(source.line_id);
        })||source;
        var voiceLine=null;
        state.shots.some(function(item){
          voiceLine=(item.lines||[]).find(function(line){
            return text(line.id)===text(source.line_id);
          })||null;
          return !!voiceLine;
        });
        var voiceVersion=voiceLine&&currentVersion(voiceLine);
        var voiceUrl=audioUrl(voiceVersion);
        var lineBlockers=alignmentDraftAnalysis.byLine[text(source.line_id)]||[];
        return '<article class="nc-sdv-alignment-line'+
          (lineBlockers.length?' has-error':'')+'"><strong>'+
          escapeHtml(source.text||source.line_id)+'</strong>'+
          '<small>只读音频 '+formatMs(source.audio_start_ms)+' - '+
          formatMs(source.audio_end_ms)+'</small>'+
          '<div class="nc-sdv-alignment-boundaries"><label>字幕开始(ms)'+
          '<input type="number" step="50" data-alignment-field="subtitle_start_ms" '+
          'data-alignment-line-id="'+escapeHtml(source.line_id)+'" value="'+
          number(edited.subtitle_start_ms,0)+'"'+
          (alignmentEditable?'':' disabled')+'></label>'+
          '<label>字幕结束(ms)<input type="number" step="50" '+
          'data-alignment-field="subtitle_end_ms" data-alignment-line-id="'+
          escapeHtml(source.line_id)+'" value="'+number(edited.subtitle_end_ms,0)+'"'+
          (alignmentEditable?'':' disabled')+'></label></div>'+
          '<div class="nc-sdv-alignment-line-actions">'+
          '<button type="button" data-action="nudge-alignment" '+
          'data-alignment-line-id="'+escapeHtml(source.line_id)+
          '" data-alignment-field="subtitle_start_ms" data-delta="-50"'+
          (alignmentEditable?'':' disabled')+'>开始 -50ms</button>'+
          '<button type="button" data-action="nudge-alignment" '+
          'data-alignment-line-id="'+escapeHtml(source.line_id)+
          '" data-alignment-field="subtitle_end_ms" data-delta="50"'+
          (alignmentEditable?'':' disabled')+'>结束 +50ms</button>'+
          '<button type="button" data-action="reset-alignment-line" '+
          'data-alignment-line-id="'+escapeHtml(source.line_id)+'"'+
          (alignmentEditable?'':' disabled')+'>恢复估算值</button>'+
          (voiceUrl?'<button type="button" data-action="preview-alignment" '+
            'data-audio-url="'+escapeHtml(voiceUrl)+'">试听本句</button>':'')+
          '</div>'+
          (lineBlockers.length?'<p class="nc-sdv-error">'+
            escapeHtml(blockerText(lineBlockers))+'</p>':'')+
          '</article>';
      }).join(''):'';
    var alignmentIsSilent=!!(
      alignmentCurrent&&Array.isArray(alignmentCurrent.timeline)&&
      !alignmentCurrent.timeline.length
    );
    var alignmentProviderMode=alignmentQuality.provider_mode||'';
    var alignmentIsEstimated=alignmentProviderMode==='estimated_fallback'||
      alignmentProviderMode==='mixed';
    var alignmentNote=alignmentIsEstimated?
      '真实音频对齐未完整覆盖；当前含估算时间，必须逐句试听并人工确认后才能锁定。':
      '时间来自真实音频 ASR word timestamps；音频边界只读，锁定前仍须人工确认。';
    var alignmentPanel='<section class="nc-sdv-alignment"><header><strong>'+
      '第 4 阶段 · 字幕强制对齐</strong><span>'+
      escapeHtml(alignmentCurrent?
        ('V'+alignmentCurrent.version+' · '+(alignmentCurrent.effective_status||alignmentCurrent.status)):
        '尚未生成')+'</span></header>'+
      '<dl><div><dt>Provider</dt><dd>'+
      escapeHtml(alignment.provider&&alignment.provider.name||'--')+
      '</dd></div><div><dt>覆盖率</dt><dd>'+
      (alignmentQuality.coverage==null?'--':
        (number(alignmentQuality.coverage,0)*100).toFixed(1)+'%')+
      '</dd></div><div><dt>平均置信度</dt><dd>'+
      (alignmentQuality.mean_confidence==null?'--':
        number(alignmentQuality.mean_confidence,0).toFixed(2))+
      '</dd></div></dl>'+
      '<p class="nc-sdv-alignment-note">'+escapeHtml(alignmentNote)+'</p>'+
      (alignmentReview?'<p class="nc-sdv-alignment-review">审核方式：'+
        escapeHtml(alignmentReview.action==='confirm_unchanged'?
          '原样确认':'调整后确认')+' · 审核人：'+
        escapeHtml(alignmentReview.reviewed_by||'--')+' · 审核时间：'+
        escapeHtml(alignmentReview.reviewed_at?
          new Date(number(alignmentReview.reviewed_at,0)*1000).toLocaleString():'--')+
        '</p>':'')+
      (alignmentLines?'<div class="nc-sdv-alignment-editor">'+alignmentLines+'</div>':
        alignmentIsSilent?
          '<p class="nc-sdv-alignment-empty">当前项目没有对白，无需调整字幕边界。'+
          '请确认无对白结果后锁定。</p>':'')+
      '<button type="button" data-action="generate-alignment"'+
      (alignmentActions.generate&&!state.operationBusy?'':' disabled')+
      '>生成字级对齐</button>'+
      '<button type="button" data-action="review-alignment"'+
      (alignmentEditable&&!alignmentDraftAnalysis.blockers.length?'':' disabled')+
      ' data-review-action="'+
      (alignmentDraftAnalysis.dirty?'save_adjustments':'confirm_unchanged')+'">'+
      (alignmentDraftAnalysis.dirty?
        '保存调整并确认':alignmentIsSilent?
          '确认当前无对白结果':'确认当前估算结果正确')+'</button>'+
      '<button type="button" data-action="lock-alignment"'+
      (alignmentActions.lock&&!state.operationBusy?'':' disabled')+
      '>锁定对齐版本</button>'+
      ((alignmentReadiness.blockers||[]).length?
        '<p class="nc-sdv-error">'+escapeHtml(blockerText(alignmentReadiness.blockers))+'</p>':'')+
      (alignmentQuality.blockers&&alignmentQuality.blockers.length?
        '<p class="nc-sdv-error">'+escapeHtml(blockerText(alignmentQuality.blockers))+'</p>':'')+
      '</section>';
    var visibleBlockers=state.timelineDirty?
      timingAnalysis.blockers:(shot&&shot.lock_blockers||[]);
    var blockedLines=Object.create(null);
    visibleBlockers.forEach(function(item){
      if(item.line_id) blockedLines[item.line_id]=true;
    });
    var rail=state.shots.map(function(item){
      return '<button type="button" class="nc-sdv-shot'+
        (item.id===state.selectedShotId?' is-selected':'')+
        (item.locked?' is-locked':'')+
        '" data-shot-id="'+escapeHtml(item.id)+'"><strong>'+escapeHtml(item.shot_key)+
        '</strong><small>'+item.duration+' 秒 · '+item.lines.length+' 句 · '+
        shotStatusLabel(item.status)+(item.locked?' · 已锁定':'')+'</small></button>';
    }).join('');
    var lines=shot?shot.lines.map(function(line){
      var active=currentVersion(line),busy=line.job&&
        ['pending','running','metadata_pending'].indexOf(line.job.status)>=0;
      var timing=timingAnalysis.byLine[line.id]||{};
      var voiceWritable=state.canEdit&&state.stage==='voice_review'&&!shot.locked&&
        !state.operationBusy&&!state.conflictFrozen;
      var disabled=voiceWritable?'':' disabled';
      var timelineDisabled=timelineWritable?'':' disabled';
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
            (voiceWritable?'':' disabled')+'>设为当前</button>':'')+
          '</li>';
      }).join('');
      var recommendation='';
      if(timing.settingsPending){
        recommendation='<p class="nc-sdv-timing is-warning">'+
          '配音参数已修改，请重新生成后再保存时间轴。</p>';
      }else if(timing.audioOverflowMs>0){
        recommendation='<div class="nc-sdv-timing is-error"><strong>音频超出镜头 '+
          formatMs(timing.audioOverflowMs)+'</strong><span>音频结束于 '+
          formatMs(timing.audioEndMs)+'，镜头结束于 '+formatMs(timing.shotDurationMs)+
          '。</span>'+(timing.recommendedSpeed?
            '<button type="button" data-action="apply-recommended-speed" data-line-id="'+
            escapeHtml(line.id)+'" data-speed="'+timing.recommendedSpeed+'">采用推荐语速 '+
            timing.recommendedSpeed.toFixed(2)+'</button>':
            '<span>当前台词无法仅靠允许范围内的语速压缩，请缩短文案或增加镜头时长。</span>')+
          (timing.subtitleOverflowMs>0?'<span>字幕结束时间也超过镜头，请同时调整字幕。</span>':'')+
          '</div>';
      }else if(timing.subtitleOverflowMs>0){
        recommendation='<div class="nc-sdv-timing is-error"><strong>字幕超出镜头 '+
          formatMs(timing.subtitleOverflowMs)+'</strong><span>字幕结束于 '+
          formatMs(timing.subtitleEndMs)+'，镜头结束于 '+formatMs(timing.shotDurationMs)+
          '。请调整字幕结束时间。</span></div>';
      }else if(timing.audioEndMs!=null){
        recommendation='<p class="nc-sdv-timing is-valid">时长校验通过 · 音频结束于 '+
          formatMs(timing.audioEndMs)+'</p>';
      }
      return '<article class="nc-sdv-line'+
        (blockedLines[line.id]?' has-conflict':'')+
        (state.playingLineId===line.id?' is-playing':'')+
        '"><header><strong>'+
        escapeHtml(line.character_name)+'</strong>'+
        (line.line_type==='narration'?'<span class="nc-sdv-line-type">旁白/叙述</span>':'')+
        '<span class="nc-sdv-status">'+escapeHtml(lineStatus(line))+
        '</span></header><label>发音文本<textarea disabled>'+
        escapeHtml(line.speech_text)+'</textarea></label><label>字幕文本<textarea '+
        'data-field="subtitle_text" data-line-id="'+escapeHtml(line.id)+'"'+
        timelineDisabled+'>'+escapeHtml(line.subtitle_text)+'</textarea></label>'+
        '<div class="nc-sdv-subtitle-controls"><label><input type="checkbox" '+
        'data-field="subtitle_visible" data-line-id="'+escapeHtml(line.id)+'"'+
        (line.subtitle_visible?' checked':'')+timelineDisabled+'> 显示字幕</label>'+
        '<label>开始(ms)<input type="number" min="0" step="50" '+
        'data-field="start_ms" data-line-id="'+escapeHtml(line.id)+'" value="'+
        (line.start_ms==null?'':line.start_ms)+'"'+timelineDisabled+'></label>'+
        '<label>结束(ms)<input type="number" min="1" step="50" '+
        'data-field="end_ms" data-line-id="'+escapeHtml(line.id)+'" value="'+
        (line.end_ms==null?'':line.end_ms)+'"'+timelineDisabled+'></label></div>'+
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
        escapeHtml(line.id)+'"'+(busy||!voiceWritable?' disabled':'')+'>'+
        (line.job&&line.job.status==='failed'?'重新生成':'生成配音')+'</button></div>'+
        (active&&audioUrl(active)?'<audio controls preload="none" src="'+escapeHtml(audioUrl(active))+
          '" data-current-audio="'+escapeHtml(line.id)+'"></audio>':'')+
        '<p class="nc-sdv-time-note">字幕 '+formatMs(line.start_ms)+' - '+
        formatMs(line.end_ms)+(active&&active.duration_ms?
          ' · 音频 '+formatMs(active.duration_ms):'')+'</p>'+recommendation+
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
      var durationMs=shot.duration*1000;
      var blocks=shot.lines.map(function(line){
        if(line.start_ms==null||line.end_ms==null) return '';
        var left=Math.max(0,Math.min(100,line.start_ms/durationMs*100));
        var width=Math.max(1,Math.min(100-left,(line.end_ms-line.start_ms)/durationMs*100));
        return '<button type="button" class="nc-sdv-timeline-block'+
          (blockedLines[line.id]?' has-conflict':'')+
          (state.playingLineId===line.id?' is-playing':'')+
          '" style="left:'+left+'%;width:'+width+'%" data-line-id="'+
          escapeHtml(line.id)+'" title="'+escapeHtml(line.subtitle_text)+'">'+
          '<i data-resize-edge="start" aria-hidden="true"></i>'+
          escapeHtml(line.character_name)+
          '<i data-resize-edge="end" aria-hidden="true"></i></button>';
      }).join('');
      var cursor=Math.max(0,Math.min(100,state.timelineCursorMs/durationMs*100));
      editorBody=lines+'<section class="nc-sdv-timeline"><header><strong>镜头时间轴</strong>'+
        '<span>'+formatMs(state.timelineCursorMs)+' / '+formatMs(durationMs)+'</span></header>'+
        '<div class="nc-sdv-timeline-track">'+blocks+
        '<i class="nc-sdv-playhead" style="left:'+cursor+'%"></i></div>'+
        '<div class="nc-sdv-timeline-actions"><button type="button" data-action="play-shot">'+
        (state.timelinePlaying?'暂停':'连续试听')+'</button>'+
        '<button type="button" data-action="replay-shot">重播</button>'+
        '<button type="button" data-action="restore-auto-timeline"'+
        (timelineWritable?'':' disabled')+'>恢复自动排布</button></div></section>';
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
      '<aside class="nc-sdv-inspector"><header><span>C-2 字幕验收</span>'+
      '<h2>验收控制台</h2></header><dl><div><dt>项目预算</dt><dd>'+
      state.point_budget+' 点</dd></div><div><dt>累计已用</dt><dd>'+
      state.spent_points+' 点</dd></div><div><dt>处理中</dt><dd>'+
      state.reserved_points+' 点</dd></div>'+
      '<div><dt>未锁定镜头</dt><dd>'+state.unlocked_shot_count+'</dd></div></dl>'+
      '<button type="button" data-action="generate-shot"'+
      (shot&&state.canEdit&&state.stage==='voice_review'&&!shot.locked&&!state.operationBusy?'':' disabled')+
      '>生成当前镜头未完成台词</button>'+
      '<button type="button" data-action="generate-all"'+
      (state.canEdit&&state.stage==='voice_review'&&!state.operationBusy?'':' disabled')+
      '>生成全剧未完成台词</button>'+
      '<button type="button" data-action="save-timeline"'+
      (timelineWritable&&state.timelineDirty&&!timingAnalysis.blockers.length?'':' disabled')+
      '>保存字幕时间轴</button>'+
      '<button type="button" data-action="set-shot-lock" data-lock="'+
      (shot&&shot.locked?'false':'true')+'"'+
      (shot&&state.canEdit&&state.stage==='voice_review'&&!state.operationBusy&&
        (shot.locked||shot.lockable)&&!state.timelineDirty&&!state.conflictFrozen?'':' disabled')+
      '>'+(shot&&shot.locked?'解锁当前镜头':'锁定当前镜头')+'</button>'+
      '<button type="button" data-action="confirm-voice-stage"'+
      (state.canEdit&&state.stage==='voice_review'&&!state.handoff_blocked&&
        alignmentHandoff.ready===true&&
        !state.operationBusy&&!state.timelineDirty&&!state.conflictFrozen?'':' disabled')+
      '>进入视频生成阶段</button>'+
      (visibleBlockers.length?'<div class="nc-sdv-blockers"><strong>'+
        (state.timelineDirty?'当前修改尚未保存':'当前镜头阻塞')+'</strong><p>'+
        escapeHtml(blockerText(visibleBlockers))+'</p></div>':'')+
      (state.handoff_blockers.length?'<div class="nc-sdv-blockers"><strong>阶段推进阻塞</strong><p>'+
        escapeHtml(blockerText(state.handoff_blockers))+'</p></div>':'')+
      (state.conflictFrozen?'<p class="nc-sdv-error">检测到版本冲突，请刷新工作区后继续。</p>':'')+
      alignmentPanel+
      (state.operationError?'<p class="nc-sdv-error" role="alert">'+escapeHtml(state.operationError)+'</p>':'')+
      '<p>时间轴保存、锁定和阶段推进均不扣点；服务端校验结果为准。</p>'+
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
    var timelineDrafts=Object.create(null),conflictFrozen=false;
    var alignmentDraft=null;
    var timelinePlaying=false,timelineCursorMs=0,playingLineId='';
    var timelinePlayers=[],timelineTimers=[],timelineClockTimer=null;
    var timelineDrag=null;
    var storage=options.storage;
    if(!storage&&typeof globalThis!=='undefined'){
      try{ storage=globalThis.localStorage; }catch(_storageError){ storage=null; }
    }
    var pendingStorageKey='hq-short-drama-voice-pending:'+text(options.projectId);
    var pendingSubmissions=Object.create(null);
    function loadPendingSubmissions(){
      if(!storage||typeof storage.getItem!=='function') return;
      try{
        var parsed=JSON.parse(storage.getItem(pendingStorageKey)||'{}');
        Object.keys(parsed||{}).forEach(function(lineId){
          var record=parsed[lineId];
          if(record&&record.line_id===lineId&&
              record.payload&&record.payload.project_id===options.projectId&&
              text(record.idempotency_key)){
            pendingSubmissions[lineId]={
              line_id:lineId,payload:clone(record.payload),
              idempotency_key:text(record.idempotency_key)
            };
          }
        });
      }catch(_pendingError){ pendingSubmissions=Object.create(null); }
    }
    function persistPendingSubmissions(){
      if(!storage||typeof storage.setItem!=='function') return;
      try{
        if(Object.keys(pendingSubmissions).length){
          storage.setItem(pendingStorageKey,JSON.stringify(pendingSubmissions));
        }else if(typeof storage.removeItem==='function'){
          storage.removeItem(pendingStorageKey);
        }
      }catch(_pendingError){}
    }
    function clearPendingSubmission(lineId){
      delete pendingSubmissions[lineId];
      persistPendingSubmissions();
    }
    loadPendingSubmissions();
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
      var html=renderWorkspace(viewSnapshot(), {
        voices:voices,busy:destroyed?false:ui.busy,error:ui.error,
        selectedShotId:ui.selectedShotId,destroyed:destroyed,canEdit:options.canEdit,
        operationBusy:generationBusy,operationError:ui.operationError,
        timelineDirty:!!timelineDrafts[ui.selectedShotId],
        timelinePlaying:timelinePlaying,timelineCursorMs:timelineCursorMs,
        playingLineId:playingLineId,conflictFrozen:conflictFrozen,
        alignmentDraft:alignmentDraft
      });
      if(host&&!destroyed) host.innerHTML=html;
      return html;
    }
    function viewSnapshot(){
      var current=clone(snapshot||{});
      (current.shots||[]).forEach(function(shot){
        var draft=timelineDrafts[shot.id];
        if(!draft) return;
        (shot.lines||[]).forEach(function(line){
          if(draft[line.id]) Object.assign(line,clone(draft[line.id]));
        });
      });
      return current;
    }
    function confirmDiscard(){
      if(typeof options.confirmDiscard==='function'){
        return options.confirmDiscard(ui.selectedShotId);
      }
      var ask=typeof globalThis!=='undefined'&&globalThis.confirm;
      return typeof ask==='function'?ask('当前镜头有未保存的字幕时间轴，放弃修改吗？'):true;
    }
    function selectShot(shotId){
      if(destroyed||!snapshot||!Array.isArray(snapshot.shots)) return false;
      var exists=snapshot.shots.some(function(shot){ return shot.id===shotId; });
      if(!exists) return false;
      if(shotId!==ui.selectedShotId&&timelineDrafts[ui.selectedShotId]){
        if(!confirmDiscard()) return false;
        delete timelineDrafts[ui.selectedShotId];
      }
      stopShotPlayback();
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
    function currentShotRaw(){
      return (snapshot&&snapshot.shots||[]).find(function(shot){
        return shot.id===ui.selectedShotId;
      })||null;
    }
    function draftForShot(shot){
      if(!shot) return null;
      if(!timelineDrafts[shot.id]){
        var draft=Object.create(null);
        (shot.lines||[]).forEach(function(line){
          draft[line.id]={
            subtitle_text:text(line.subtitle_text),
            subtitle_visible:line.subtitle_visible!==false,
            start_ms:line.start_ms,
            end_ms:line.end_ms
          };
        });
        timelineDrafts[shot.id]=draft;
      }
      return timelineDrafts[shot.id];
    }
    function updateTimelineLine(lineId,patch){
      requireWritable();
      var shot=currentShotRaw(),line=shot&&(shot.lines||[]).find(function(item){
        return item.id===lineId;
      });
      if(!line||shot.locked||snapshot.stage!=='voice_review'||conflictFrozen){
        throw new Error('当前镜头不能修改字幕时间轴');
      }
      patch=patch||{};
      var allowed=['subtitle_text','subtitle_visible','start_ms','end_ms'];
      if(Object.keys(patch).some(function(key){ return allowed.indexOf(key)<0; })){
        throw new Error('字幕时间轴字段无效');
      }
      var draft=draftForShot(shot)[lineId];
      if(Object.prototype.hasOwnProperty.call(patch,'subtitle_text')){
        draft.subtitle_text=text(patch.subtitle_text);
      }
      if(Object.prototype.hasOwnProperty.call(patch,'subtitle_visible')){
        draft.subtitle_visible=!!patch.subtitle_visible;
      }
      ['start_ms','end_ms'].forEach(function(key){
        if(Object.prototype.hasOwnProperty.call(patch,key)){
          draft[key]=patch[key]==null?null:Math.round(number(patch[key],0)/50)*50;
        }
      });
      render();
      return clone(draft);
    }
    function restoreAutoTimeline(){
      requireWritable();
      var shot=currentShotRaw();
      if(!shot||shot.locked||snapshot.stage!=='voice_review'||conflictFrozen){
        throw new Error('当前镜头不能恢复自动时间轴');
      }
      var draft=draftForShot(shot);
      var ordered=(shot.lines||[]).slice().sort(function(left,right){
        return number(left.sort_order,0)-number(right.sort_order,0)||
          text(left.id).localeCompare(text(right.id));
      });
      var durations=ordered.map(function(line){
        var version=currentVersion(normalizeLine(line,0,Object.create(null)));
        return number(version&&version.duration_ms,0);
      });
      var cursor=0;
      ordered.forEach(function(line,index){
        var duration=durations[index];
        if(duration<=0) return;
        draft[line.id].start_ms=cursor;
        draft[line.id].end_ms=cursor+duration;
        cursor+=duration+150;
      });
      render();
      return clone(draft);
    }
    function applyRecommendedSpeed(lineId,speed){
      requireWritable();
      var line=findLine(lineId),value=Math.ceil(number(speed,0)*20)/20;
      if(!line||value<0.5||value>2){
        throw new Error('推荐语速无效，请缩短文案或增加镜头时长');
      }
      line.speed=Number(value.toFixed(2));
      render();
      return line.speed;
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
    function shotForLine(lineId){
      return (snapshot&&snapshot.shots||[]).find(function(shot){
        return (shot.lines||[]).some(function(line){ return line.id===lineId; });
      })||null;
    }
    function requireVoiceWritable(lines){
      requireWritable();
      if(!snapshot||snapshot.stage!=='voice_review'){
        throw new Error('当前短剧阶段不能生成或切换配音');
      }
      if(ui.conflictFrozen) throw new Error('数据版本已冲突，请刷新后重试');
      (lines||[]).forEach(function(line){
        var shot=line&&shotForLine(line.id);
        if(shot&&shot.locked) throw new Error('已锁定镜头不能修改配音');
      });
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
    function ambiguousSubmissionError(error){
      var status=number(error&&error.status,0);
      if(!error||error.operation_terminal===true||
          error.data&&error.data.operation_terminal===true) return false;
      return error.code==='timeout'||error.code==='network_error'||status===0||
        status===408||status===429||status>=500;
    }
    function submitPendingSubmission(record,retryOnce){
      var requestOptions={
        method:'POST',body:clone(record.payload),
        headers:{'Idempotency-Key':record.idempotency_key}
      };
      return callJson(GENERATE_PATH,requestOptions).catch(function(error){
        if(retryOnce&&ambiguousSubmissionError(error)){
          return callJson(GENERATE_PATH,requestOptions);
        }
        throw error;
      }).then(function(result){
        clearPendingSubmission(record.line_id);
        return result;
      },function(error){
        if(!ambiguousSubmissionError(error)){
          clearPendingSubmission(record.line_id);
        }
        throw error;
      });
    }
    function submitQuoteItem(item,line){
      var record={
        line_id:line.id,
        payload:{
        project_id:snapshot.project_id,revision:number(snapshot.revision,0),
        line_id:line.id,voice_key:text(line.voice_key),
        speed:number(line.speed,1),pitch:number(line.pitch,0),volume:number(line.volume,0),
        quote_token:item.quote_token
        },
        idempotency_key:requestKey(line.id)
      };
      pendingSubmissions[line.id]=record;
      persistPendingSubmissions();
      return submitPendingSubmission(record,true);
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
      lines=(lines||[]).filter(Boolean);
      try{ requireVoiceWritable(lines); }catch(error){ return Promise.reject(error); }
      if(generationBusy) return Promise.reject(new Error('配音请求正在处理中，请勿重复提交'));
      if(!lines.length) return Promise.reject(new Error('没有需要生成的台词'));
      var items=lines.map(editableItem);
      generationBusy=true;ui.operationError='';render();
      var pending=lines.map(function(line){
        return pendingSubmissions[line.id]||null;
      }).filter(Boolean);
      if(pending.length){
        return mapConcurrent(pending,3,function(record){
          return submitPendingSubmission(record,false);
        }).then(function(results){
          return reload(true).then(function(){
            return {cancelled:false,quote:null,results:results,recovered:true};
          });
        }).finally(function(){
          generationBusy=false;render();
        });
      }
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
    function timelineBody(shot){
      var draft=timelineDrafts[shot.id]||draftForShot(shot);
      return {
        project_id:snapshot.project_id,revision:number(snapshot.revision,0),
        shot_id:shot.id,timeline_revision:number(shot.timeline_revision,1),
        items:(shot.lines||[]).map(function(line){
          var item=draft[line.id];
          return {
            line_id:line.id,subtitle_text:text(item.subtitle_text),
            subtitle_visible:item.subtitle_visible!==false,
            start_ms:number(item.start_ms,0),end_ms:number(item.end_ms,0)
          };
        })
      };
    }
    function timelineMatches(body){
      var shot=(snapshot&&snapshot.shots||[]).find(function(item){
        return item.id===body.shot_id;
      });
      if(!shot) return false;
      return body.items.every(function(expected){
        var line=(shot.lines||[]).find(function(item){
          return item.id===expected.line_id;
        });
        return !!line&&text(line.subtitle_text)===text(expected.subtitle_text)&&
          (line.subtitle_visible!==false)===(expected.subtitle_visible!==false)&&
          number(line.start_ms,-1)===expected.start_ms&&
          number(line.end_ms,-1)===expected.end_ms;
      });
    }
    function revisionConflict(error){
      return !!(error&&(error.code==='revision_conflict'||
        error.data&&error.data.code==='revision_conflict'));
    }
    function ambiguousFreeWrite(error){
      var status=number(error&&error.status,0);
      return !!(error&&(error.code==='timeout'||error.code==='network_error'||
        status===0||status===408||status>=500));
    }
    function acceptSnapshot(result,shotId,notify){
      if(result&&Array.isArray(result.shots)) snapshot=result;
      if(shotId) delete timelineDrafts[shotId];
      conflictFrozen=false;ui.operationError='';render();schedulePoll();
      if(notify&&typeof options.onChange==='function'){
        return Promise.resolve(options.onChange({
          project_id:snapshot.project_id,
          revision:number(snapshot.revision,0),
          stage:snapshot.stage,
          spent_points:number(snapshot.spent_points,0),
          point_budget:number(snapshot.point_budget,0),
          reserved_points:number(snapshot.reserved_points,0)
        })).then(function(){ return snapshot; });
      }
      return snapshot;
    }
    function mutationFailure(error){
      if(revisionConflict(error)) conflictFrozen=true;
      ui.operationError=text(error&&error.message||error);
      render();
      throw error;
    }
    function saveTimeline(){
      try{ requireWritable(); }catch(error){ return Promise.reject(error); }
      var shot=currentShotRaw();
      if(!shot||!timelineDrafts[shot.id]) {
        return Promise.reject(new Error('当前镜头没有待保存的字幕时间轴'));
      }
      if(generationBusy) return Promise.reject(new Error('操作正在处理中'));
      var body=timelineBody(shot);
      var draftShot=(viewSnapshot().shots||[]).find(function(item){
        return item.id===shot.id;
      });
      var blockers=analyzeShotTiming(draftShot).blockers;
      if(blockers.length){
        return Promise.reject(new Error(blockers[0].message));
      }
      generationBusy=true;ui.operationError='';render();
      return callJson(SAVE_TIMELINE_PATH,{method:'POST',body:body}).then(function(result){
        return acceptSnapshot(result,shot.id,true);
      }).catch(function(error){
        if(!ambiguousFreeWrite(error)) return mutationFailure(error);
        return reload(true).then(function(){
          if(timelineMatches(body)) return acceptSnapshot(snapshot,shot.id,true);
          var latest=currentShotRaw();
          if(!latest) throw error;
          body.revision=number(snapshot.revision,0);
          body.timeline_revision=number(latest.timeline_revision,1);
          return callJson(SAVE_TIMELINE_PATH,{method:'POST',body:body}).then(function(result){
            return acceptSnapshot(result,shot.id,true);
          });
        }).catch(mutationFailure);
      }).finally(function(){ generationBusy=false;render(); });
    }
    function setShotLock(lock){
      try{ requireWritable(); }catch(error){ return Promise.reject(error); }
      var shot=currentShotRaw();
      if(!shot) return Promise.reject(new Error('当前镜头不存在'));
      if(timelineDrafts[shot.id]) {
        return Promise.reject(new Error('请先保存或放弃字幕时间轴修改'));
      }
      var body={
        project_id:snapshot.project_id,revision:number(snapshot.revision,0),
        shot_id:shot.id,timeline_revision:number(shot.timeline_revision,1),
        lock:!!lock
      };
      generationBusy=true;ui.operationError='';render();
      return callJson(SET_LOCK_PATH,{method:'POST',body:body}).then(function(result){
        return acceptSnapshot(result,null,true);
      }).catch(function(error){
        if(!ambiguousFreeWrite(error)) return mutationFailure(error);
        return reload(true).then(function(){
          var latest=currentShotRaw();
          if(latest&&latest.locked===body.lock) return acceptSnapshot(snapshot,null,true);
          if(!latest) throw error;
          body.revision=number(snapshot.revision,0);
          body.timeline_revision=number(latest.timeline_revision,1);
          return callJson(SET_LOCK_PATH,{method:'POST',body:body}).then(function(result){
            return acceptSnapshot(result,null,true);
          });
        }).catch(mutationFailure);
      }).finally(function(){ generationBusy=false;render(); });
    }
    function confirmVoiceStage(){
      try{ requireWritable(); }catch(error){ return Promise.reject(error); }
      if(Object.keys(timelineDrafts).length){
        return Promise.reject(new Error('请先保存或放弃字幕时间轴修改'));
      }
      var body={
        project_id:snapshot.project_id,revision:number(snapshot.revision,0),
        stage:'voice_review'
      };
      generationBusy=true;ui.operationError='';render();
      return callJson(CONFIRM_PATH,{method:'POST',body:body}).then(function(result){
        return acceptSnapshot(result,null,true);
      }).catch(function(error){
        if(!ambiguousFreeWrite(error)) return mutationFailure(error);
        return reload(true).then(function(){
          if(snapshot&&snapshot.stage==='video_review') return acceptSnapshot(snapshot,null,true);
          body.revision=number(snapshot&&snapshot.revision,0);
          return callJson(CONFIRM_PATH,{method:'POST',body:body}).then(function(result){
            return acceptSnapshot(result,null,true);
          });
        }).catch(mutationFailure);
      }).finally(function(){ generationBusy=false;render(); });
    }
    function alignmentMutation(path,body,headers){
      try{ requireWritable(); }catch(error){ return Promise.reject(error); }
      if(generationBusy) return Promise.reject(new Error('操作正在处理中'));
      generationBusy=true;ui.operationError='';render();
      return callJson(path,{
        method:'POST',body:body,headers:headers||{}
      }).then(function(){
        return reload(true);
      }).catch(mutationFailure).finally(function(){
        generationBusy=false;render();
      });
    }
    function generateAlignment(){
      return alignmentMutation(ALIGNMENT_JOBS_PATH,{
        project_id:snapshot.project_id,
        revision:number(snapshot.revision,0)
      },{'Idempotency-Key':requestKey('alignment')});
    }
    function currentAlignmentVersion(){
      var alignment=snapshot&&snapshot.alignment;
      return alignment&&alignment.current_version||null;
    }
    function ensureAlignmentDraft(){
      requireWritable();
      var version=currentAlignmentVersion();
      if(!version||snapshot.stage!=='voice_review'||
          !snapshot.alignment.actions||!snapshot.alignment.actions.save||
          conflictFrozen){
        throw new Error('当前字幕对齐版本不能人工校对');
      }
      if(!alignmentDraft||alignmentDraft.versionId!==version.id||
          alignmentDraft.revision!==number(version.revision,0)){
        alignmentDraft={
          versionId:version.id,
          revision:number(version.revision,0),
          lines:(version.timeline||[]).map(function(line){
            return {
              line_id:text(line.line_id),
              subtitle_start_ms:number(line.subtitle_start_ms,0),
              subtitle_end_ms:number(line.subtitle_end_ms,0)
            };
          })
        };
      }
      return alignmentDraft;
    }
    function updateAlignmentLine(lineId,patch){
      var draft=ensureAlignmentDraft(),version=currentAlignmentVersion();
      var target=draft.lines.find(function(line){
        return line.line_id===text(lineId);
      });
      var source=(version.timeline||[]).find(function(line){
        return text(line.line_id)===text(lineId);
      });
      if(!target||!source) throw new Error('字幕校对条目不存在');
      patch=patch||{};
      ['subtitle_start_ms','subtitle_end_ms'].forEach(function(field){
        if(Object.prototype.hasOwnProperty.call(patch,field)){
          target[field]=Math.round(number(patch[field],target[field]));
        }
      });
      render();
      return clone(target);
    }
    function resetAlignmentLine(lineId){
      var version=currentAlignmentVersion();
      var source=version&&(version.timeline||[]).find(function(line){
        return text(line.line_id)===text(lineId);
      });
      if(!source) throw new Error('字幕校对条目不存在');
      return updateAlignmentLine(lineId,{
        subtitle_start_ms:number(source.subtitle_start_ms,0),
        subtitle_end_ms:number(source.subtitle_end_ms,0)
      });
    }
    function reviewAlignment(){
      var alignment=snapshot&&snapshot.alignment;
      var version=alignment&&alignment.current_version;
      if(!version) return Promise.reject(new Error('没有可校对的字幕对齐版本'));
      var draft;
      try{ draft=ensureAlignmentDraft(); }
      catch(error){ return Promise.reject(error); }
      var analysis=analyzeAlignmentDraft(version,draft);
      if(analysis.blockers.length){
        return Promise.reject(new Error(analysis.blockers[0].message));
      }
      return alignmentMutation(ALIGNMENT_TIMELINE_PATH,{
        project_id:snapshot.project_id,
        version_id:version.id,
        revision:number(version.revision,0),
        review_action:analysis.dirty?
          'save_adjustments':'confirm_unchanged',
        lines:analysis.lines
      }).then(function(result){
        alignmentDraft=null;
        render();
        return result;
      });
    }
    function lockAlignment(){
      var alignment=snapshot&&snapshot.alignment;
      var version=alignment&&alignment.current_version;
      if(!version) return Promise.reject(new Error('没有可锁定的字幕对齐版本'));
      return alignmentMutation(ALIGNMENT_LOCK_PATH,{
        project_id:snapshot.project_id,
        version_id:version.id,
        revision:number(version.revision,0)
      });
    }
    function selectVersion(lineId,version){
      var line=findLine(lineId);
      try{ requireVoiceWritable(line?[line]:[]); }catch(error){ return Promise.reject(error); }
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
    function clearTimelineTimers(){
      timelineTimers.forEach(function(timer){
        if(typeof clearTimeout==='function') clearTimeout(timer);
      });
      timelineTimers=[];
      if(timelineClockTimer!=null&&typeof clearInterval==='function'){
        clearInterval(timelineClockTimer);
      }
      timelineClockTimer=null;
    }
    function stopShotPlayback(){
      clearTimelineTimers();
      timelinePlayers.forEach(function(player){
        if(player&&typeof player.pause==='function') player.pause();
      });
      timelinePlayers=[];timelinePlaying=false;playingLineId='';
      timelineCursorMs=0;
    }
    function pauseShotPlayback(){
      clearTimelineTimers();
      timelinePlayers.forEach(function(player){
        if(player&&typeof player.pause==='function') player.pause();
      });
      timelinePlaying=false;playingLineId='';render();
      return false;
    }
    function updatePlayingLine(shot){
      playingLineId='';
      (shot.lines||[]).some(function(line){
        var version=currentVersion(normalizeLine(line,0,Object.create(null)));
        var duration=number(version&&version.duration_ms,0);
        if(line.start_ms!=null&&timelineCursorMs>=line.start_ms&&
            timelineCursorMs<line.start_ms+duration){
          playingLineId=line.id;return true;
        }
        return false;
      });
    }
    function playShot(){
      if(timelinePlaying) return pauseShotPlayback();
      var shot=currentShotRaw();
      if(!shot||!(shot.lines||[]).length) return false;
      stopAudio();stopShotPlayback();
      var factory=options.audioFactory||
        (typeof Audio==='function'?function(source){ return new Audio(source); }:null);
      if(!factory) return false;
      var started=(options.now||Date.now)();
      timelinePlaying=true;
      (shot.lines||[]).forEach(function(line){
        var version=currentVersion(normalizeLine(line,0,Object.create(null)));
        var url=audioUrl(version);
        if(!url||line.start_ms==null) return;
        var player=factory(url);
        timelinePlayers.push(player);
        var delay=Math.max(0,line.start_ms-timelineCursorMs);
        var timer=setTimeout(function(){
          if(!timelinePlaying||destroyed) return;
          if(typeof player.play==='function'){
            var result=player.play();
            if(result&&typeof result.catch==='function') result.catch(function(){});
          }
        },delay);
        timelineTimers.push(timer);
      });
      updatePlayingLine(shot);render();
      timelineClockTimer=setInterval(function(){
        if(!timelinePlaying||destroyed) return;
        timelineCursorMs=Math.min(
          shot.duration*1000,timelineCursorMs+((options.now||Date.now)()-started)
        );
        started=(options.now||Date.now)();
        updatePlayingLine(shot);
        if(timelineCursorMs>=shot.duration*1000){
          pauseShotPlayback();
        }else{
          render();
        }
      },100);
      return true;
    }
    function replayShot(){
      stopShotPlayback();
      timelineCursorMs=0;
      return playShot();
    }
    function stopNativeAudio(except){
      if(!host||typeof host.querySelectorAll!=='function') return;
      Array.prototype.forEach.call(host.querySelectorAll('audio'),function(audio){
        if(audio!==except&&typeof audio.pause==='function') audio.pause();
      });
    }
    function preview(url){
      stopShotPlayback();stopAudio();stopNativeAudio(null);
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
          else if(action==='save-timeline') task=saveTimeline();
          else if(action==='set-shot-lock') task=setShotLock(
            node.getAttribute('data-lock')==='true'
          );
          else if(action==='confirm-voice-stage') task=confirmVoiceStage();
          else if(action==='generate-alignment') task=generateAlignment();
          else if(action==='review-alignment') task=reviewAlignment();
          else if(action==='lock-alignment') task=lockAlignment();
          else if(action==='nudge-alignment'){
            try{
              var alignmentLineId=node.getAttribute('data-alignment-line-id');
              var alignmentField=node.getAttribute('data-alignment-field');
              var alignmentDelta=number(node.getAttribute('data-delta'),0);
              var draft=ensureAlignmentDraft();
              var alignmentLine=draft.lines.find(function(item){
                return item.line_id===text(alignmentLineId);
              });
              var alignmentPatch={};
              alignmentPatch[alignmentField]=
                number(alignmentLine&&alignmentLine[alignmentField],0)+alignmentDelta;
              updateAlignmentLine(alignmentLineId,alignmentPatch);
            }catch(error){ ui.operationError=error.message;render(); }
          }
          else if(action==='reset-alignment-line'){
            try{
              resetAlignmentLine(node.getAttribute('data-alignment-line-id'));
            }catch(error){ ui.operationError=error.message;render(); }
          }
          else if(action==='preview-alignment'){
            preview(node.getAttribute('data-audio-url'));
          }
          else if(action==='restore-auto-timeline'){
            try{ restoreAutoTimeline(); }catch(error){ ui.operationError=error.message;render(); }
          }
          else if(action==='apply-recommended-speed'){
            try{
              applyRecommendedSpeed(lineId,node.getAttribute('data-speed'));
            }catch(error){ ui.operationError=error.message;render(); }
          }
          else if(action==='play-shot') playShot();
          else if(action==='replay-shot') replayShot();
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
      var alignmentField=node.getAttribute('data-alignment-field');
      if(alignmentField){
        var alignmentPatch={};
        alignmentPatch[alignmentField]=number(node.value,0);
        try{
          updateAlignmentLine(
            node.getAttribute('data-alignment-line-id'),alignmentPatch
          );
        }catch(error){ ui.operationError=text(error.message||error);render(); }
        return;
      }
      var field=node.getAttribute('data-field'),line=findLine(node.getAttribute('data-line-id'));
      if(!line) return;
      if(['subtitle_text','subtitle_visible','start_ms','end_ms'].indexOf(field)>=0){
        var patch={};
        patch[field]=field==='subtitle_visible'?!!node.checked:
          (field==='subtitle_text'?text(node.value):number(node.value,0));
        try{ updateTimelineLine(line.id,patch); }
        catch(error){ ui.operationError=text(error.message||error);render(); }
        return;
      }
      if(['voice_key','speed','pitch','volume'].indexOf(field)<0) return;
      line[field]=field==='voice_key'?text(node.value):number(node.value,line[field]);
    }
    function onPointerDown(event){
      var node=event&&event.target,edge='';
      if(node&&node.getAttribute) edge=text(node.getAttribute('data-resize-edge'));
      while(node&&node!==host&&
          !(text(node.className).indexOf('nc-sdv-timeline-block')>=0)){
        node=node.parentNode;
      }
      if(!node||node===host||!node.getAttribute) return;
      var shot=currentShotRaw(),lineId=node.getAttribute('data-line-id');
      var line=viewSnapshot().shots.find(function(item){
        return shot&&item.id===shot.id;
      });
      line=line&&(line.lines||[]).find(function(item){ return item.id===lineId; });
      var track=node.parentNode,rect=track&&track.getBoundingClientRect&&
        track.getBoundingClientRect();
      if(!shot||!line||shot.locked||snapshot.stage!=='voice_review'||
          !rect||!rect.width) return;
      timelineDrag={
        lineId:lineId,edge:edge||'move',clientX:number(event.clientX,0),
        start_ms:number(line.start_ms,0),end_ms:number(line.end_ms,0),
        width:rect.width,duration:shot.duration*1000
      };
      if(event.preventDefault) event.preventDefault();
    }
    function onPointerMove(event){
      if(!timelineDrag) return;
      var drag=timelineDrag;
      var delta=Math.round(
        ((number(event.clientX,drag.clientX)-drag.clientX)/drag.width*drag.duration)/50
      )*50;
      var start=drag.start_ms,end=drag.end_ms;
      if(drag.edge==='start'){
        start=Math.max(0,Math.min(end-50,start+delta));
      }else if(drag.edge==='end'){
        end=Math.min(drag.duration,Math.max(start+50,end+delta));
      }else{
        var length=end-start;
        start=Math.max(0,Math.min(drag.duration-length,start+delta));
        end=start+length;
      }
      updateTimelineLine(drag.lineId,{start_ms:start,end_ms:end});
      if(event.preventDefault) event.preventDefault();
    }
    function onPointerUp(){ timelineDrag=null; }
    if(host&&typeof host.addEventListener==='function') host.addEventListener('click',onClick);
    if(host&&typeof host.addEventListener==='function') host.addEventListener('change',onChange);
    if(host&&typeof host.addEventListener==='function') host.addEventListener('pointerdown',onPointerDown);
    if(host&&typeof host.addEventListener==='function') host.addEventListener('pointermove',onPointerMove);
    if(host&&typeof host.addEventListener==='function') host.addEventListener('pointerup',onPointerUp);
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
        if(!silent&&conflictFrozen){
          timelineDrafts=Object.create(null);
          conflictFrozen=false;
        }
        snapshot=results[0];voices=voiceItems(results[1]);
        var reloadedAlignment=snapshot.alignment&&snapshot.alignment.current_version;
        if(alignmentDraft&&(
          !reloadedAlignment||alignmentDraft.versionId!==reloadedAlignment.id||
          alignmentDraft.revision!==number(reloadedAlignment.revision,0)
        )){
          alignmentDraft=null;
        }
        if(!(snapshot.shots||[]).some(function(shot){
          return shot.id===ui.selectedShotId;
        })){
          ui.selectedShotId=snapshot.shots&&snapshot.shots[0]&&snapshot.shots[0].id;
        }
        allLines().forEach(function(line){
          var pending=pendingSubmissions[line.id];
          if(pending&&line.job&&
              line.job.idempotency_key===pending.idempotency_key){
            clearPendingSubmission(line.id);
          }
        });
        ui.busy=false;render();schedulePoll();
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
      updateTimelineLine:updateTimelineLine,restoreAutoTimeline:restoreAutoTimeline,
      applyRecommendedSpeed:applyRecommendedSpeed,
      updateAlignmentLine:updateAlignmentLine,
      resetAlignmentLine:resetAlignmentLine,
      saveTimeline:saveTimeline,setShotLock:setShotLock,
      generateAlignment:generateAlignment,reviewAlignment:reviewAlignment,
      lockAlignment:lockAlignment,
      confirmVoiceStage:confirmVoiceStage,playShot:playShot,
      pauseShot:pauseShotPlayback,replayShot:replayShot,
      getState:function(){
        return clone(normalizeState(viewSnapshot(),voices,{
          busy:destroyed?false:ui.busy,error:ui.error,
          selectedShotId:ui.selectedShotId,destroyed:destroyed,canEdit:options.canEdit,
          operationBusy:generationBusy,operationError:ui.operationError,
          timelineDirty:!!timelineDrafts[ui.selectedShotId],
          timelinePlaying:timelinePlaying,timelineCursorMs:timelineCursorMs,
          playingLineId:playingLineId,conflictFrozen:conflictFrozen,
          alignmentDraft:alignmentDraft
        }));
      },
      destroy:function(){
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('click',onClick);
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('change',onChange);
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('pointerdown',onPointerDown);
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('pointermove',onPointerMove);
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('pointerup',onPointerUp);
        if(host&&typeof host.removeEventListener==='function') host.removeEventListener('play',onNativePlay,true);
        clearPoll();stopShotPlayback();stopAudio();stopNativeAudio(null);
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
