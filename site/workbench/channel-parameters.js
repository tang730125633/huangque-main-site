(function(){
  const host=document.getElementById('publishedChannelParameters');if(!host)return;
  const {esc,mount}=window.ChannelParameterControls,kind=host.dataset.kind;
  const legacy=document.querySelector(kind==='image'?'.banana-workspace':'#videoWorkspace');if(legacy)legacy.setAttribute('data-channel-legacy','');
  let showLegacy=false,billingEnabled=false;
  // 两个工作台都以各自的原生界面为首屏：图片页的托管线路已内嵌为「乐创 · Image 2」引擎卡，
  // 视频页提交托管渠道时再由 redirect 接管。平台面板不再默认顶掉工作台，
  // 只在“命中托管映射提交”或“存在待确认提交”时出现。
  let managedActive=false;
  let items=[],current=null,controls=null,busy=false,owner='',pending=null,pollTimer=null,ready=false,fetching=false;
  // 当前选择的功能身份：功能条目取 operation_id，兼容条目取 legacy:<kind>:<front>。
  // 用身份而不是 front 查找——同一个 front 可能对应多个业务功能。
  let currentKey='';
  let sourceDraft=null;
  let layoutApplied=false;
  host.className='cp-panel';
  const request=async(url,options={})=>{const r=await fetch(url,{credentials:'same-origin',cache:'no-store',...options});const d=await r.json();if(!r.ok){const e=Error(d.detail||'请求失败');e.status=r.status;throw e}return d};
  const storageKey=()=> 'hq_parameter_request:'+owner+':'+kind;
  function savePending(){sessionStorage.setItem(storageKey(),JSON.stringify(pending))}
  const note=text=>{const n=host.querySelector('#cpUserNote');if(n)n.textContent=text};
  function applyWorkbenchLayout(layout,entries){
    const applyDefault=!layoutApplied;layoutApplied=true;
    if(!layout||typeof layout!=='object')return;
    try{
      const q=new URLSearchParams(location.search);
      const canUse=entry=>!!entry&&entry.visible!==false&&entry.defaultable!==false;
      if(kind==='image'){
        const row=document.getElementById('engineRow');if(!row)return;
        const hasCatalog=Array.isArray(entries?.image),catalog=hasCatalog?entries.image:[];
        catalog.forEach(entry=>{
          const item=row.querySelector('[data-engine="'+entry.key+'"]');if(!item)return;
          const visible=entry.visible!==false;item.style.display=visible?'':'none';item.setAttribute('aria-hidden',visible?'false':'true');
        });
        (layout.image?.order||[]).forEach(key=>{const el=row.querySelector('[data-engine="'+key+'"]');if(el)row.appendChild(el);});
        const def=layout.image?.default;
        const api=window.HQBananaWorkbench,current=api?.getEngine?.();
        const requestedRaw=q.get('engine'),requested=['nb2','pro'].includes(requestedRaw)?'banana':requestedRaw;
        const requestedEntry=catalog.find(entry=>entry.key===requested);
        const currentEntry=catalog.find(entry=>entry.key===current);
        let selected='';
        if(applyDefault)selected=q.has('engine')?(hasCatalog?(canUse(requestedEntry)?requested:def):''):def;
        else if(hasCatalog&&current&&!canUse(currentEntry))selected=def;
        if(selected&&selected!==current&&typeof api?.selectEngine==='function')api.selectEngine(selected);
      }else{
        const tabs=document.querySelector('.function-tabs');if(!tabs)return;
        const hasCatalog=Array.isArray(entries?.video),catalog=hasCatalog?entries.video:[];
        catalog.forEach(entry=>{
          const item=tabs.querySelector('[data-function="'+entry.key+'"]');if(!item)return;
          const visible=entry.visible!==false;item.hidden=!visible;item.setAttribute('aria-hidden',visible?'false':'true');
        });
        (layout.video?.order||[]).forEach(key=>{const el=tabs.querySelector('[data-function="'+key+'"]');if(el)tabs.appendChild(el);});
        const def=layout.video?.default;
        const hasDeepLink=['function','prefill','prompt','task','action'].some(k=>q.has(k));
        const api=window.HQVideoWorkbench,current=api?.getFunction?.(),currentEntry=catalog.find(entry=>entry.key===current);
        const shouldFallback=hasCatalog&&current&&!canUse(currentEntry);
        if(def&&(shouldFallback||(applyDefault&&!hasDeepLink))&&def!==current&&typeof api?.updateFunction==='function')api.updateFunction(def);
      }
    }catch(error){}
  }
  // ── 条目身份 ────────────────────────────────────────────────────────────
  // 同一个 front 可能对应多个业务功能（纳米香蕉 2 的文生图与参考图都提交
  // model=nb2；grok 对应文生视频与参考图生视频）。所以身份用 operation_id，
  // 不能用 front 查找，否则两个功能会串用彼此的配置。
  const keyOf=e=>e?(e.operation_id||('legacy:'+(e.kind||'')+':'+(e.front||''))):'';
  const isCompat=e=>!!(e&&e.legacy_compat);
  // 兼容条目只服务明确的旧入口，不参与任何自动选择
  const autoSelectable=()=>items.filter(i=>!isCompat(i));

  // ── 「选择失效」是持续状态 ────────────────────────────────────────────
  // 刷新一次就又替你选中第一项，等于把用户的处境抹掉。这里把它存起来，
  // 直到用户自己明确重新选择为止。
  const invalidStorageKey=()=>'hq_parameter_invalid:'+owner+':'+kind;
  const invalidKey=()=>{try{return sessionStorage.getItem(invalidStorageKey())||''}catch(error){return ''}};
  const markInvalid=k=>{try{sessionStorage.setItem(invalidStorageKey(),k||'1')}catch(error){}};
  const clearInvalid=()=>{try{sessionStorage.removeItem(invalidStorageKey())}catch(error){}};

  // 原选择失效时用的界面：保留提示词与可兼容的输入，给出明确的重新选择入口。
  // 不清空整个表单、不自动替用户选一个，也不自动提交。
  function renderChooser(invalidated){
    const prompt=host.querySelector('#cpPrompt')?.value||'';
    const options=items.map(i=>'<option value="'+esc(keyOf(i))+'">'+esc(i.label)
      +(isCompat(i)?'（旧线路）':'')+'</option>').join('');
    return '<div class="cp-actions"><h2>模型与生成参数</h2></div>'
      +'<div id="cpManagedBody"><p class="cp-note">'+(invalidated
        ?'原模型已不在当前渠道支持范围，请选择当前功能支持的配置。提示词已保留；'
         +'未重新选择前不会生成，也不会自动提交。'
        :'请选择当前功能支持的配置。')+'</p>'
      +'<div class="cp-fields"><label>模型<select id="cpModel">'+options+'</select></label></div>'
      +'<textarea id="cpPrompt" maxlength="7000" aria-label="生成提示词">'+esc(prompt)+'</textarea>'
      +'<p id="cpRefHint" class="cp-note">选择后即可看到该功能当前渠道支持的参数与点数。</p></div>';
  }
  function bindChooser(){
    const sel=host.querySelector('#cpModel');
    if(sel)sel.onchange=e=>{
      current=items.find(i=>keyOf(i)===e.target.value)||null;
      currentKey=current?keyOf(current):'';
      controls=null;clearInvalid();render();
    };
  }

  function render(){
    if(!managedActive){if(legacy)legacy.hidden=false;host.hidden=true;host.innerHTML='';return}
    const previous=current,previousKey=currentKey;
    const matched=items.find(i=>keyOf(i)===previousKey);
    // 三种情况分开：
    //   * 首次进入（没有原选择、也没有失效记录）—— 用第一个【非兼容】条目；
    //   * 原选择失效 —— 记为持续状态，保持未选中，等用户明确重选；
    //     【不】自动换成第一项，也【不】因为排列靠前或查找失败就落到兼容条目；
    //   * 已有选择 —— 按 operation_id 找回同一条。
    const invalidated=!!(previousKey&&!matched);
    if(invalidated)markInvalid(previousKey);
    else if(matched)clearInvalid();
    const firstVisit=!previousKey&&!invalidKey();
    const droppedFront=invalidated?previous:null;
    current=firstVisit?(autoSelectable()[0]||null):(matched||null);
    currentKey=current?keyOf(current):'';
    if(!current&&!pending){
      if(legacy)legacy.hidden=false;
      host.hidden=false;
      // 不提前 return 清空整个表单：保留提示词、可兼容素材和重新选择入口
      host.innerHTML=renderChooser(invalidated);
      bindChooser();
      return;
    }
    host.hidden=false;
    const prompt=host.querySelector('#cpPrompt')?.value||'',oldChoice=controls?.value();
    if(legacy)legacy.hidden=!showLegacy;
    host.innerHTML='<div class="cp-actions"><h2>模型与生成参数</h2>'+(kind==='image'?'<button id="cpInpaintEntry">涂抹局部修图（黄雀引擎 2）</button>':'')+'<button id="cpLegacyToggle">'+(showLegacy?'返回平台配置模型':'其他模型与工具')+'</button></div><div id="cpManagedBody" '+(showLegacy?'hidden':'')+'><p class="cp-note">选择模型和参数，确认本次点数后生成。参数由平台统一维护。</p><div class="cp-fields"><label>模型<select id="cpModel">'+items.map(i=>'<option value="'+esc(keyOf(i))+'" '+(keyOf(i)===keyOf(current)?'selected':'')+'>'+esc(i.label)+(isCompat(i)?'（旧线路）':'')+'</option>').join('')+'</select></label></div><div id="cpUserControls"></div><textarea id="cpPrompt" maxlength="7000" placeholder="描述你希望生成的内容" aria-label="生成提示词">'+esc(prompt)+'</textarea><label id="cpUploadLabel">参考图片<input id="cpUpload" type="file" accept="image/png,image/jpeg" multiple></label><p id="cpRefHint" class="cp-note"></p>'+(current&&current.mask_enabled&&current.reference_max>=1?'<label id="cpMaskLabel" class="cp-mask-label">蒙版图片（可选 · 局部修图）<input id="cpMaskUpload" type="file" accept="image/png,image/jpeg"><small>上传与参考图同尺寸的蒙版，白色区域将被重绘；需要 1 张参考图</small></label>':'')+'<div class="cp-actions"><button class="primary" id="cpGenerate">确认点数并生成</button><button id="cpRetry" hidden>使用原编号重试提交</button><a href="assets.html">查看我的作品与任务</a></div><p id="cpUserNote" role="status"></p><div id="cpUserResult"></div></div>';
    if(current){
      const preserved=current.combinations.find(c=>JSON.stringify(c.values)===JSON.stringify(oldChoice?.values));
      controls=mount(host.querySelector('#cpUserControls'),current,null,preserved?.id,{billingEnabled});
      host.querySelector('#cpUploadLabel').hidden=current.reference_max===0;
      host.querySelector('#cpRefHint').textContent=current.reference_max?'参考图 '+current.reference_min+'～'+current.reference_max+' 张，总大小不超过8MB。'+(current.mask_enabled?'可选上传蒙版进行局部修改。':''):'当前模型为文生图模式，无需参考图。';
      if(droppedFront)note('原模型「'+droppedFront.label
        +'」已不在当前渠道支持范围，请选择当前功能支持的配置。'
        +'未重新选择前不会生成，也不会自动提交。');
      else if(previous&&current&&previous.revision!==current.revision){
        // 对比用的是【控件实际返回的选择】在旧条目里的那条组合，
        // 以及新条目里真正保留（按 values 命中）或重选（按 id 命中）的那条。
        // 不拿 current.default 代替当前选择——那会在组合对不上时给出错误的点数结论。
        const list=it=>it?.combinations||[];
        const was=list(previous).find(x=>x.id===oldChoice?.id)
          ||list(previous).find(x=>JSON.stringify(x.values)===JSON.stringify(oldChoice?.values))
          ||null;
        const now=list(current).find(x=>x.id===oldChoice?.id)
          ||list(current).find(x=>JSON.stringify(x.values)===JSON.stringify(oldChoice?.values))
          ||list(current).find(x=>x.id===current.default)&&null
          ||null;
        if(!was||!now){
          note('生成配置已更新，需要重新选择并确认费用。提示词已保留；如需参考图，请重新选择。');
        }else if(was.points!==now.points){
          note('本次点数已变化（'+was.points+' → '+now.points+' 点），请核对参数及点数后重新提交。提示词已保留；如需参考图，请重新选择。');
        }else{
          note('模型参数已更新（点数未变），请核对参数后提交。提示词已保留；如需参考图，请重新选择。');
        }
      }
    }
    host.querySelector('#cpLegacyToggle').onclick=()=>{showLegacy=!showLegacy;host.querySelector('#cpManagedBody').hidden=showLegacy;if(legacy)legacy.hidden=!showLegacy;host.querySelector('#cpLegacyToggle').textContent=showLegacy?'返回平台配置模型':'其他模型与工具'};
    const inpaint=host.querySelector('#cpInpaintEntry');
    if(inpaint)inpaint.onclick=()=>{
      showLegacy=true;host.querySelector('#cpManagedBody').hidden=true;if(legacy)legacy.hidden=false;
      host.querySelector('#cpLegacyToggle').textContent='返回平台配置模型';
      window.HQBananaWorkbench?.selectEngine?.('gpt');
      note('已切到黄雀引擎 2 的涂抹局部修图：先上传 1 张参考图，再涂抹要修改的区域。');
      legacy?.scrollIntoView?.({behavior:'smooth',block:'start'});
    };
    host.querySelector('#cpModel').onchange=e=>{current=items.find(i=>keyOf(i)===e.target.value)||null;currentKey=current?keyOf(current):'';controls=null;clearInvalid();render()};
    host.querySelector('#cpGenerate').onclick=submit;
    host.querySelector('#cpRetry').onclick=()=>send();
    sync();
    if(pending)note(pending.job_id?'任务 #'+pending.job_id+' 已提交，正在查询结果。':'上一笔提交结果待确认，可用原编号重试；不会创建新的提交编号。');
  }
  function sync(){
    if(!host.querySelector('#cpGenerate'))return;
    controls?.setBillingEnabled(billingEnabled);
    host.querySelector('#cpGenerate').textContent=billingEnabled?'确认点数并生成':'开始生成';
    host.querySelector('#cpManagedBody > .cp-note').textContent=billingEnabled?'选择模型和参数，确认本次点数后生成。参数由平台统一维护。':'选择模型和参数后生成。内测期间免费。';
    host.querySelector('#cpGenerate').disabled=busy||!!pending||!current;
    host.querySelector('#cpRetry').hidden=!pending||!!pending.job_id;
    host.querySelector('#cpRetry').disabled=busy;
  }
  async function identity(){
    const d=await request('/api/auth/me');if(!d.user?.username)throw Error('请先登录');
    billingEnabled=d.user.points_billing_enabled===true;sync();
    return d.user.username;
  }
  async function submit(){
    if(busy||pending||!current)return;
    try{
      busy=true;sync();
      const user=await identity();if(owner&&owner!==user)throw Error('登录账户已变化，请刷新页面');owner=user;
      const prompt=host.querySelector('#cpPrompt').value.trim();if(!prompt)throw Error('请输入提示词');
      const files=[...host.querySelector('#cpUpload').files];
      const inherited=files.length?[]:(sourceDraft?.reference_images|| (sourceDraft?.image?[sourceDraft.image]:[]));
      if((files.length+inherited.length)<current.reference_min||(files.length+inherited.length)>current.reference_max||files.reduce((n,f)=>n+f.size,0)>8*1024*1024)throw Error('参考图数量或大小不符合要求');
      const maskInput=host.querySelector('#cpMaskUpload'),maskFile=maskInput&&maskInput.files&&maskInput.files[0]||null;
      if(maskFile&&files.length!==1)throw Error('局部修图需要恰好 1 张参考图');
      if(maskFile&&maskFile.size>10*1024*1024)throw Error('蒙版图片不能超过 10MB');
      const choice=controls.value();if(!confirm(billingEnabled?'生成 1 个产物，本次共 '+choice.points+' 点。确认提交？':'生成 1 个产物，内测期间免费。确认提交？'))return;
      const readFile=file=>new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file)});
      const refs=files.length?await Promise.all(files.map(readFile)):inherited;
      const payload={prompt,count:1,parameter_selection:{revision:current.revision,combination:choice.id}};
      // 请求字段按后端下发的识别条件填写 —— 前后端用同一个功能身份，
      // 不再让前端自己拼 provider/model 去猜是哪个功能。
      const match=current.match||{};
      Object.keys(match).forEach(function(k){
        if(k==='kind'||k==='reference_count'||k==='mask_present')return;
        if(match[k]===null||match[k]===undefined||match[k]==='')return;
        payload[k]=match[k];
      });
      if(!payload.model&&!payload.channel){
        // 明确的旧入口（兼容条目）：沿用它自己的旧字段
        if(kind==='image'){payload.model=current.front;payload.provider='openai'}else payload.channel=current.front;
      }
      if(refs.length)payload.reference_images=refs;
      if(maskFile)payload.mask=await readFile(maskFile);
      else if(sourceDraft?.mask)payload.mask=sourceDraft.mask;
      if(sourceDraft?.source_inspiration_id)payload.source_inspiration_id=sourceDraft.source_inspiration_id;
      pending={key:crypto.randomUUID(),payload,owner};
      try{savePending()}catch(error){pending=null;throw Error('无法保存防重复提交信息，请释放浏览器会话存储后重试')}
    }catch(error){note(error.status===401?'请先登录后生成。':error.message);return}
    finally{busy=false;sync()}
    if(pending)await send();
  }
  async function send(){
    if(busy||!pending||pending.job_id)return;
    busy=true;sync();
    try{
      if(await identity()!==pending.owner)throw Error('登录账户已变化，请切回原账户核对任务');
      const d=await request('/api/gen/'+(kind==='image'?'image':'xiaole_video'),{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':pending.key},body:JSON.stringify(pending.payload)});
      if(!d.job_id)throw Error('提交结果未确认，请用原编号重试');
      pending.job_id=d.job_id;savePending();poll();
    }catch(error){
      if([400,402,413,422].includes(error.status)){pending=null;savePending()}
      note(error.message);
    }finally{busy=false;sync()}
  }
  async function poll(){
    if(!pending?.job_id)return;
    try{
      const d=await request('/api/gen/job/'+pending.job_id);
      if(['done','failed','error','cancelled'].includes(d.status)){
        note(d.status==='done'?'生成完成，可在作品中查看。':d.error||'任务未完成，请在任务记录查看处理结果。');
        const result=d.result||{},url=result.url||(result.urls||[])[0];
        if(d.status==='done'&&url){const u=new URL(url,location.href);if(['http:','https:'].includes(u.protocol)){const media=document.createElement(kind==='image'?'img':'video');media.src=u.href;if(kind!=='image')media.controls=true;media.alt='生成结果';host.querySelector('#cpUserResult').replaceChildren(media)}}
        pending=null;savePending();sync();window.HQ?.refreshPoints?.();return;
      }
      note('任务 #'+pending.job_id+' · '+(d.status||'处理中'));
    }catch(error){note('暂时无法查询，任务记录已保留：'+error.message)}
    pollTimer=setTimeout(poll,5000);
  }
  async function load(){
    if(fetching||busy)return;fetching=true;
    try{
      const d=await request('/api/gen/channel-parameters'),next=d.items.filter(i=>i.kind===kind);
      applyWorkbenchLayout(d.layout,d.layout_entries);
      const changed=JSON.stringify(items)!==JSON.stringify(next);items=next;ready=true;
      if(changed||!host.innerHTML)render();
    }catch(error){if(host.innerHTML)note('参数暂时无法刷新；提交时会再次校验。')}
    finally{fetching=false}
  }
  function matchesInput(entry,input){
    const refs=input.reference_images||input.images||(input.image?[input.image]:[]);
    const actual={...input,kind,provider:input.provider||(kind==='image'?'openai':''),
      operation:input.operation||(input.channel==='grok'?'generate':''),
      reference_count:refs.length,mask_present:!!input.mask};
    return Object.entries(entry.match||{}).every(([key,value])=>
      value==='>0'?actual[key]>0:actual[key]===value);
  }
  window.PublishedChannelParameters={redirect:(k,input)=>{
    if(k!==kind)return false;
    const choices=autoSelectable().filter(i=>typeof input==='string'
      ?keyOf(i)===input:!!i.match&&matchesInput(i,input));
    if(choices.length!==1)return false;
    const found=choices[0];
    sourceDraft=typeof input==='object'?{...input}:null;
    managedActive=true;showLegacy=false;current=found;currentKey=keyOf(found);controls=null;clearInvalid();render();
    if(sourceDraft?.prompt)host.querySelector('#cpPrompt').value=sourceDraft.prompt;
    host.scrollIntoView({behavior:'smooth',block:'start'});
    note('已载入该功能当前渠道的参数，请确认后提交。'+(sourceDraft?.reference_images?.length||sourceDraft?.image?'已保留参考图。':''));
    return true;
  }};
  (async()=>{try{owner=await identity();pending=JSON.parse(sessionStorage.getItem(storageKey())||'null')}catch(e){}await load();if(pending){managedActive=true;render();sync();if(pending.job_id)poll()}})();
  window.addEventListener('hq:auth-changed',event=>{billingEnabled=event.detail?.verified===true&&event.detail?.user?.points_billing_enabled===true;sync()});
  setInterval(()=>{if(!document.hidden)load()},15000);
})();
