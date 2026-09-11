(function(root){
  const signature=values=>JSON.stringify(Object.keys(values).sort().map(k=>[k,String(values[k])]));
  function rebuild(selected,previous,defaultId,points){
    let variants=[{}];
    for(const [key,choices] of Object.entries(selected)){
      if(!choices.length)throw Error('每项参数至少选择一个值');
      variants=variants.flatMap(row=>choices.map(v=>({...row,[key]:v}))).filter(v=>!(v.background==='transparent'&&v.output_format==='jpeg'));
      if(variants.length>128)throw Error('组合超过 128 个，请减少选项');
    }
    if(!variants.length)throw Error('没有有效组合：透明背景不能使用 JPEG');
    const old=new Map(previous.map(r=>[signature(r.values),r])),used=new Set(previous.map(r=>r.id));let nextId=1;
    const rows=variants.map(values=>{const match=old.get(signature(values));if(match)return {...match,values};while(used.has('option-'+nextId))nextId++;const id='option-'+nextId++;used.add(id);return {id,values,points}});
    const retained=rows.filter(r=>old.has(signature(r.values))).length;
    return {rows,default:rows.some(r=>r.id===defaultId)?defaultId:rows[0].id,added:rows.length-retained,removed:previous.length-retained,retained};
  }
  function issues(spec){
    const result=[];
    if(!spec.combinations.length)result.push('请先生成至少一个有效组合');
    for(const f of spec.fields){
      if(!f.label.trim())result.push('参数展示名称不能为空');
      if(!f.visible&&new Set(spec.combinations.map(r=>String(r.values[f.key]))).size>1)result.push(f.label+'设为固定值后只能保留一个选项');
    }
    if(spec.combinations.some(r=>!Number.isInteger(r.points)||r.points<1||r.points>100000))result.push('每组点数须为 1–100000 的整数');
    if(!spec.combinations.some(r=>r.id===spec.default))result.push('请选择默认组合');
    if(!Number.isInteger(spec.reference_min)||!Number.isInteger(spec.reference_max)||spec.reference_min<0||spec.reference_max<spec.reference_min)result.push('参考图数量范围无效');
    if(spec.mask&&spec.reference_max<1)result.push('开启局部修图前请把参考图上限设为至少 1 张');
    return [...new Set(result)];
  }
  root.ChannelParameterEditorLogic={rebuild,issues};
  if(typeof window==='undefined')return;
  window.initChannelParameterEditor=function(env){
    const {el,api,toast,workspace}=env,{esc,mount,label}=window.ChannelParameterControls;
    let state,channel,rows=[],fieldOrder=[],previewChoice=null,dirty=false,busy=false,pending=false,tab='options',filter='';
    const post=(action,body)=>api('/api/admin/channel-manager/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const host=()=>el('cmEditor'),q=selector=>host().querySelector(selector);
    const profileNames={gpt_image:'GPT Image 1 系列',gpt_image_2:'GPT Image 2',dalle3:'DALL·E 3',compatible:'兼容图片协议',minimax_h3:'MiniMax H3',xai_video:'Grok 视频'};
    const refs=()=>env.parameterMappings?.(channel.id)||[];
    function collect(){return {profile:state.capabilities.profile,fields:fieldOrder.map(key=>({key,label:q('[data-label="'+key+'"]').value,visible:q('[data-visible="'+key+'"]').checked})),
      combinations:rows.map(r=>({...r,points:Number(q('[data-points="'+r.id+'"]').value)})),default:q('[name=cpDefault]:checked')?.value,
      reference_min:Number(q('#cpRefMin').value),reference_max:Number(q('#cpRefMax').value),mask:q('#cpMask')?.checked||false};}
    function remember(){const spec=collect();rows=spec.combinations;state.localDefault=spec.default;return spec}
    function showTab(name){tab=name;host().querySelectorAll('[data-cp-page]').forEach(n=>n.hidden=n.dataset.cpPage!==name);host().querySelectorAll('[data-cp-tab]').forEach(n=>n.setAttribute('aria-selected',String(n.dataset.cpTab===name)));}
    function changed(){dirty=true;q('#cpError').textContent='';sync();preview()}
    function sync(){
      const spec=collect(),errors=issues(spec);q('.cp-editor-body').inert=busy;q('.cp-tabs').inert=busy;
      if(spec.reference_min<state.capabilities.reference_min||spec.reference_max>state.capabilities.reference_max)errors.push('参考图数量超出当前模型支持范围');
      host().querySelectorAll('[data-points]').forEach(n=>n.setAttribute('aria-invalid',String(!Number.isInteger(Number(n.value))||Number(n.value)<1||Number(n.value)>100000)));
      host().querySelectorAll('[data-label]').forEach(n=>n.setAttribute('aria-invalid',String(!n.value.trim())));
      ['cpRefMin','cpRefMax'].forEach(id=>{const n=q('#'+id),value=Number(n.value);n.setAttribute('aria-invalid',String(!Number.isInteger(value)||value<Number(n.min)||value>Number(n.max)||spec.reference_min>spec.reference_max))});
      const status=pending?'选项已变更，请应用到组合':dirty?'有未保存修改':state.draft?'草稿已保存，尚未发布':'正在查看已发布配置';
      q('#cpStatus').textContent=status;q('#cpStatus').classList.toggle('is-dirty',dirty);
      q('#cpSave').disabled=busy||pending||errors.length>0;
      q('#cpPublish').disabled=busy||dirty||!state.draft||state.draft.base_version!==state.version;
      q('#cpPublish').title=dirty?'请先保存草稿':state.draft&&state.draft.base_version!==state.version?'连接版本已变化，请先重新保存草稿':'';
      q('#cpSelectionNote').textContent=pending?'选项已调整，应用后更新右侧预览与定价。':'组合与选项已同步；未变化组合保留原价格。';
      q('#cpSelectionNote').classList.toggle('is-dirty',pending);
      q('#cpValidation').textContent=errors.join('；');q('#cpValidation').hidden=!errors.length;
      q('#cpComboCount').textContent=rows.length+' 个组合';
      q('#cpPreviewNote').textContent=pending?'预览仍使用应用前组合':'与用户页面使用同一套参数控件';
    }
    function combinations(){
      q('#cpRows').innerHTML='<table><thead><tr><th>默认</th>'+fieldOrder.map(k=>'<th>'+esc(q('[data-label="'+k+'"]').value)+'</th>').join('')+'<th>消耗点数</th><th></th></tr></thead><tbody>'+rows.map(r=>'<tr data-combo-row="'+esc(r.id)+'"><td><input aria-label="默认组合 '+esc(r.id)+'" type="radio" name="cpDefault" value="'+esc(r.id)+'" '+(r.id===state.localDefault?'checked':'')+'></td>'+fieldOrder.map(k=>'<td>'+esc(label(k,r.values[k]))+'</td>').join('')+'<td><input aria-label="组合 '+esc(r.id)+' 点数" type="number" min="1" max="100000" data-points="'+esc(r.id)+'" value="'+Number(r.points)+'"></td><td><button class="cp-text-button" data-remove="'+esc(r.id)+'" aria-label="删除组合 '+esc(r.id)+'">删除</button></td></tr>').join('')+'</tbody></table>';
      filterRows();preview();
    }
    function filterRows(){let count=0;rows.forEach(r=>{const match=Object.entries(r.values).map(([k,v])=>label(k,v)).join(' ').toLowerCase().includes(filter.toLowerCase());q('[data-combo-row="'+r.id+'"]').hidden=!match;if(match)count++});q('#cpFilteredCount').textContent='显示 '+count+' / '+rows.length+' 组';}
    function preview(){
      const spec=collect();
      mount(q('#cpPreview'),spec,c=>{previewChoice=c.id;q('#cpRequest').textContent='尚未校验实际请求。\n'+JSON.stringify({model:channel.model,...c.values,points:c.points},null,2)},previewChoice);
      q('#cpPreviewSummary').textContent=(spec.reference_max?'参考图 '+spec.reference_min+'–'+spec.reference_max+' 张':'文字输入')+' · 每次 1 个产物'+(spec.mask?' · 支持局部修图（蒙版）':'');
      q('#cpRequest').parentElement.open=false;
    }
    function option(key,v,index,checked){
      let visual='',main=label(key,v),sub='';
      if(key==='size'||key==='ratio'){
        const parts=String(v).split(key==='size'?'x':':').map(Number),ratio=parts[0]/parts[1];
        if(Number.isFinite(ratio)&&ratio>0)visual='<span class="cp-ratio-shape" style="width:'+Math.round(28*Math.min(ratio,1))+'px;height:'+Math.round(28/Math.max(ratio,1))+'px"></span>';
        main=(ratio===1?'方形':ratio>1?'横向':'竖向')+' · '+(state.sizes?.[v]||v);sub=key==='size'?String(v).replace('x',' × '):'';
      }else if(key==='background')visual='<span class="cp-background-sample '+(v==='transparent'?'transparent':'')+'"></span>';
      return '<label class="cp-option '+(key==='size'?'cp-size-option':'')+'" title="'+esc(v)+'"><input type="checkbox" data-choice="'+key+'" data-index="'+index+'" '+(checked?'checked':'')+'>'+visual+'<span><strong>'+esc(main)+'</strong>'+(sub?'<small>'+esc(sub)+'</small>':'')+'</span><span class="cp-option-tick" aria-hidden="true">✓</span></label>';
    }
    function render(reset=false){
      const cap=state.capabilities,spec=reset?null:(state.draft?.parameters||state.published);
      rows=JSON.parse(JSON.stringify(spec?.combinations||[]));state.localDefault=spec?.default;previewChoice=state.localDefault;
      fieldOrder=spec?.fields.map(f=>f.key)||Object.keys(cap.fields);pending=false;filter='';
      const checked=(key,v)=>spec?rows.some(r=>String(r.values[key])===String(v)):v===(key==='quality'&&cap.fields[key].includes('medium')?'medium':cap.fields[key][0]);
      host().innerHTML='<section class="cp-panel cp-editor"><header class="cp-editor-header"><div><span class="cp-eyebrow">模型参数 / 前端发布</span><h3>'+esc(channel.name)+'</h3><p>实际模型 <b>'+esc(channel.model)+'</b><span class="cp-header-dot">·</span>配置 v'+state.version+'</p></div><span class="cp-version">'+(state.published?'已有发布配置':'尚未发布')+'</span></header>'+
        '<nav class="cp-tabs" role="tablist" aria-label="参数设置分区"><button role="tab" id="cpTabOptions" aria-controls="cpPageOptions" data-cp-tab="options">可选参数</button><button role="tab" id="cpTabPricing" aria-controls="cpPagePricing" data-cp-tab="pricing">组合与定价 <span id="cpComboCount"></span></button><button role="tab" id="cpTabHistory" aria-controls="cpPageHistory" data-cp-tab="history">发布记录</button></nav>'+
        '<div class="cp-editor-body"><main class="cp-editor-main"><section id="cpPageOptions" role="tabpanel" aria-labelledby="cpTabOptions" data-cp-page="options"><div class="cp-section-intro"><h4>用户可以选择哪些参数</h4><p>勾选开放选项，或将参数设为固定值。</p></div><div id="cpGroups">'+fieldOrder.map(key=>{
          const f=spec?.fields.find(f=>f.key===key),visible=f?.visible!==false;
          return '<section class="cp-parameter-card" data-parameter-group="'+key+'"><div class="cp-card-heading"><h4>'+esc(f?.label||state.labels[key])+'</h4><label class="cp-switch"><input type="checkbox" data-visible="'+key+'" '+(visible?'checked':'')+'><span class="cp-switch-track"></span><span data-mode-label="'+key+'">'+(visible?'用户可选':'固定值')+'</span></label></div><div class="cp-options">'+cap.fields[key].map((v,i)=>option(key,v,i,checked(key,v))).join('')+'</div><details class="cp-more"><summary>更多设置</summary><div class="cp-more-fields"><label>前端显示名称<input data-label="'+key+'" value="'+esc(f?.label||state.labels[key])+'" maxlength="40"></label><button data-move-field="'+key+'" data-direction="-1" aria-label="'+esc(state.labels[key])+'上移">↑ 上移</button><button data-move-field="'+key+'" data-direction="1" aria-label="'+esc(state.labels[key])+'下移">↓ 下移</button></div></details></section>';
        }).join('')+'</div><section class="cp-parameter-card cp-reference-card"><div class="cp-card-heading"><h4>参考图数量</h4><span class="cp-muted">'+(cap.reference_max?'按模型能力限制':'当前协议仅支持文字输入')+'</span></div><div class="cp-reference-fields"><label>最少<input id="cpRefMin" type="number" min="'+cap.reference_min+'" max="'+cap.reference_max+'" value="'+(spec?.reference_min??cap.reference_min)+'" '+(!cap.reference_max?'disabled':'')+'></label><span>—</span><label>最多<input id="cpRefMax" type="number" min="'+cap.reference_min+'" max="'+cap.reference_max+'" value="'+(spec?.reference_max??cap.reference_max)+'" '+(!cap.reference_max?'disabled':'')+'></label><span>张</span></div></section>'+
        (cap.mask?'<section class="cp-parameter-card cp-reference-card"><div class="cp-card-heading"><h4>局部修图（蒙版）</h4><label class="cp-switch"><input type="checkbox" id="cpMask" '+(spec?.mask?'checked':'')+'><span class="cp-switch-track"></span><span data-mode-label="mask">'+(spec?.mask?'已开启':'已关闭')+'</span></label></div><p class="cp-muted">开启后用户可上传与参考图同尺寸的黑白蒙版，对参考图局部重绘；需要 1 张参考图。</p></section>':'')+
        '<div class="cp-apply-card"><div><strong>更新有效组合</strong><p id="cpSelectionNote"></p></div><div class="cp-apply-controls"><label>新增组合点数<input id="cpBasePoints" type="number" min="1" max="100000" value="20"></label><button id="cpBuild">应用选项</button></div></div>'+
        '<details class="cp-advanced"><summary>高级设置 · 参数协议</summary><label>适配协议<select id="cpProfile">'+cap.profiles.map(p=>'<option value="'+esc(p)+'" '+(p===cap.profile?'selected':'')+'>'+esc(profileNames[p]||p)+'</option>').join('')+'</select></label><p class="cp-note">协议须与实际模型匹配；参数配置不代表供应商已验证。</p></details></section>'+
        '<section id="cpPagePricing" role="tabpanel" aria-labelledby="cpTabPricing" data-cp-page="pricing" hidden><div class="cp-section-intro"><h4>每个组合，明确计价</h4><p>点数为一次任务的总价。视频按选定时长计总价。</p></div><div class="cp-pricing-tools"><input id="cpFilter" type="search" aria-label="筛选参数组合" placeholder="筛选尺寸、画质、格式…"><label>批量点数<input id="cpBatchPoints" type="number" min="1" max="100000" value="20"></label><button id="cpBatch">应用到筛选结果</button></div><p class="cp-muted" id="cpFilteredCount"></p><div id="cpRows" class="cp-table"></div></section>'+
        '<section id="cpPageHistory" role="tabpanel" aria-labelledby="cpTabHistory" data-cp-page="history" hidden><div class="cp-section-intro"><h4>发布与回滚</h4><p>恢复历史参数会创建新版本，已受理任务保留原配置。</p></div><div class="cp-history-list">'+(state.history.length?state.history.map(h=>'<div class="cp-history-item"><span class="cp-history-icon">↺</span><div><strong>参数版本 v'+Number(h.version)+'</strong><p>恢复此版本的参数选项、默认值与定价</p></div><button data-parameter-rollback="'+Number(h.version)+'">恢复此版本</button></div>').join(''):'<div class="cp-empty">尚无已发布记录<br><small>保存草稿后即可发布第一版配置</small></div>')+'</div></section></main>'+
        '<aside class="cp-editor-preview"><div class="cp-preview-heading"><span class="cp-eyebrow">LIVE PREVIEW</span><h4>用户端预览</h4><p id="cpPreviewNote"></p></div><div class="cp-preview-device"><div class="cp-preview-title">'+esc(refs().find(m=>m.enabled)?.label||'生成参数')+'</div><div class="cp-preview" id="cpPreview"></div><p id="cpPreviewSummary" class="cp-note"></p><div class="cp-preview-submit" aria-hidden="true">开始生成</div></div><p class="cp-preview-hint">预览不会发起任务或消耗点数</p><details class="cp-advanced"><summary>高级调试 · 实际请求</summary><button id="cpPreviewButton">校验并预览请求</button><pre id="cpRequest"></pre></details></aside></div>'+
        '<footer class="cp-editor-footer"><div class="cp-footer-status"><span id="cpStatus" role="status"></span><p id="cpValidation" role="alert" hidden></p><p id="cpError" role="alert"></p></div><div class="cp-footer-actions"><button id="cpClose">关闭</button><button id="cpSave">保存草稿</button><button class="primary" id="cpPublish">发布配置</button></div></footer></section>';
      host().querySelectorAll('.cp-history-item').forEach((node,i)=>{const h=state.history[i];node.querySelector('p').textContent=[h.created?new Date(h.created*1000).toLocaleString():'时间未记录',h.actor||'操作人未记录',(h.parameters?.combinations.length||0)+' 个组合'].join(' · ')});
      combinations();dirty=reset;sync();showTab(tab);
      host().oninput=e=>{
        if(e.target.closest('#cpPreview'))return;
        if(e.target.id==='cpFilter'){filter=e.target.value;filterRows();return}
        if(e.target.matches('[data-points],[data-label],#cpRefMin,#cpRefMax')){
          if(e.target.dataset.label)q('[data-parameter-group="'+e.target.dataset.label+'"] h4').textContent=e.target.value;
          changed();
        }
      };
      host().onchange=async e=>{
        const target=e.target;if(target.closest('#cpPreview'))return;
        if(target.id==='cpProfile'){
          if((dirty||pending)&&!confirm('切换协议会清除未保存编辑，继续？')){target.value=state.capabilities.profile;return}
          await open(channel,target.value);return;
        }
        if(target.id==='cpMask'){q('[data-mode-label="mask"]').textContent=target.checked?'已开启':'已关闭';changed();return}
        const key=target.dataset.choice||target.dataset.visible;
        if(key){
          const choices=[...host().querySelectorAll('[data-choice="'+key+'"]')],mode=q('[data-visible="'+key+'"]');
          if(!mode.checked){const keep=target.dataset.choice&&target.checked?target:choices.find(n=>n.checked)||choices[0];choices.forEach(n=>n.checked=n===keep)}
          q('[data-mode-label="'+key+'"]').textContent=mode.checked?'用户可选':'固定值';pending=true;changed();
        }else if(target.name==='cpDefault'){state.localDefault=target.value;previewChoice=target.value;changed()}
      };
      host().onclick=click;
      q('.cp-tabs').onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const tabs=[...host().querySelectorAll('[data-cp-tab]')],current=tabs.indexOf(document.activeElement),i=e.key==='Home'?0:e.key==='End'?2:(current+(e.key==='ArrowRight'?1:2))%3;showTab(tabs[i].dataset.cpTab);tabs[i].focus()};
    }
    function selection(){return Object.fromEntries(Object.entries(state.capabilities.fields).map(([key,allowed])=>[key,[...host().querySelectorAll('[data-choice="'+key+'"]:checked')].map(x=>allowed[Number(x.dataset.index)])]));}
    async function click(e){
      const b=e.target.closest('button');if(!b||busy||b.closest('#cpPreview'))return;e.preventDefault();
      try{
        if(b.dataset.cpTab){showTab(b.dataset.cpTab);return}
        if(b.id==='cpClose'){workspace.close();return}
        if(b.id==='cpPreviewButton'){
          if(pending)throw Error('请先应用选项，再预览实际请求');
          const result=await post('parameter-preview',{id:channel.id,version:state.version,parameters:collect(),combination:previewChoice});
          if(!q('#cpRequest'))return;q('#cpRequest').textContent=JSON.stringify(result,null,2);q('#cpRequest').parentElement.open=true;toast(result.validation);return;
        }
        if(b.dataset.moveField){
          const index=fieldOrder.indexOf(b.dataset.moveField),next=index+Number(b.dataset.direction);if(next<0||next>=fieldOrder.length)return;
          remember();[fieldOrder[next],fieldOrder[index]]=[fieldOrder[index],fieldOrder[next]];fieldOrder.forEach(k=>q('#cpGroups').append(q('[data-parameter-group="'+k+'"]')));combinations();changed();return;
        }
        if(b.dataset.remove){remember();rows=rows.filter(r=>r.id!==b.dataset.remove);if(state.localDefault===b.dataset.remove)state.localDefault=rows[0]?.id;combinations();changed();return}
        if(b.id==='cpBuild'){
          const points=Number(q('#cpBasePoints').value);if(!Number.isInteger(points)||points<1||points>100000)throw Error('新增组合点数须为 1–100000 的整数');
          const current=collect(),next=rebuild(selection(),current.combinations,current.default,points);
          if(rows.length&&!confirm('更新后共 '+next.rows.length+' 组：新增 '+next.added+' 组，移除 '+next.removed+' 组。\n'+next.retained+' 组保留原价格，新增组合每组 '+points+' 点。确认应用？'))return;
          rows=next.rows;state.localDefault=next.default;pending=false;combinations();changed();toast('已更新 '+rows.length+' 个有效组合');return;
        }
        if(b.id==='cpBatch'){
          const value=Number(q('#cpBatchPoints').value);if(!Number.isInteger(value)||value<1||value>100000)throw Error('批量点数须为 1–100000 的整数');
          const targets=[...host().querySelectorAll('[data-combo-row]')].filter(n=>!n.hidden);if(!targets.length)throw Error('当前筛选没有组合');
          if(!confirm('将当前筛选的 '+targets.length+' 个组合设为每组 '+value+' 点？'))return;targets.forEach(n=>n.querySelector('[data-points]').value=value);changed();return;
        }
        if(!['cpSave','cpPublish'].includes(b.id)&&!b.dataset.parameterRollback)return;
        const action=b.id==='cpSave'?'draft':b.id==='cpPublish'?'publish':'rollback';
        if(action==='draft'){if(pending)throw Error('请先应用选项');const errors=issues(collect());if(errors.length)throw Error(errors.join('；'))}
        if(action!=='draft'){
          const draft=state.draft?.parameters,published=state.published;
          const targetSpec=action==='rollback'?state.history.find(h=>h.version===Number(b.dataset.parameterRollback))?.parameters:draft;
          const changes=targetSpec?.fields.map(f=>{
            const old=published?.fields.find(x=>x.key===f.key),values=s=>[...new Set((s?.combinations||[]).map(r=>label(f.key,r.values[f.key])))].join(' / ');
            return values(published)!==values(targetSpec)||old?.visible!==f.visible||old?.label!==f.label?f.label+'：'+values(targetSpec)+(f.visible?'（用户可选）':'（固定值）'):null;
          }).filter(Boolean)||[];
          if(targetSpec?.mask!==published?.mask)changes.push('局部修图（蒙版）：'+(targetSpec?.mask?'开启':'关闭'));
          const summary=(action==='rollback'?'恢复参数版本 v'+b.dataset.parameterRollback+(dirty?'，未保存修改将被丢弃':''):'发布已保存草稿')+'\n组合数量：'+(published?.combinations.length||0)+' → '+(targetSpec?.combinations.length||0)+' 组\n'+(targetSpec?.combinations.length?'点数范围：'+Math.min(...targetSpec.combinations.map(r=>r.points))+'–'+Math.max(...targetSpec.combinations.map(r=>r.points))+' 点\n':'')+changes.join('\n');
          if(!confirm(summary+'\n关联功能：'+(refs().map(m=>(m.label||m.front)+(m.enabled?'':'（未启用）')).join('、')||'暂无映射')+'\n仅影响新请求，旧任务保持原配置。参数配置不代表真实生成测试通过。确认？'))return;
        }
        busy=true;sync();
        state=await post('parameters',{id:channel.id,version:state.version,draft_revision:state.draft?.revision||0,action,parameters:action==='draft'?collect():undefined,target_version:Number(b.dataset.parameterRollback)||undefined,confirmed:action!=='draft'});
        if(!q('.cp-editor'))return;render();toast(action==='draft'?'草稿已保存，尚未影响用户':'参数已发布，用户页面将自动刷新选项');
      }catch(error){if(q('#cpError'))q('#cpError').textContent=error.message}
      finally{busy=false;if(q('#cpPublish'))sync()}
    }
    async function open(c,profile){
      channel=c;tab='options';workspace.editor('参数设置 · '+c.name);el('cmMappingEditor').hidden=true;host().hidden=false;host().textContent='正在读取参数配置…';
      try{state=await post('parameter-state',{id:c.id,profile});render(!!profile);workspace.setCloseGuard(()=>{if(busy){toast('正在保存，请稍候');return false}return !dirty||confirm('有未保存修改，确认关闭？')})}catch(error){host().textContent=error.message}
    }
    return {open};
  };
})(typeof window==='undefined'?globalThis:window);
