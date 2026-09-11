(function(){
  const host=document.getElementById('publishedChannelParameters');if(!host)return;
  const {esc,mount}=window.ChannelParameterControls,kind=host.dataset.kind;
  const legacy=document.querySelector(kind==='image'?'.banana-workspace':'#videoWorkspace');if(legacy)legacy.setAttribute('data-channel-legacy','');
  let showLegacy=false,billingEnabled=false;
  // 视频页托管多个功能（数字人口播、剧情、换装等），平台配置面板只在用户提交托管渠道时接管，
  // 否则会在打开页面时把整个工作台替换掉。图片页本身只有作图一件事，首屏即用托管面板。
  let managedActive=kind==='image';
  let items=[],current=null,controls=null,busy=false,owner='',pending=null,pollTimer=null,ready=false,fetching=false;
  let layoutApplied=false;
  host.className='cp-panel';
  const request=async(url,options={})=>{const r=await fetch(url,{credentials:'same-origin',cache:'no-store',...options});const d=await r.json();if(!r.ok){const e=Error(d.detail||'请求失败');e.status=r.status;throw e}return d};
  const storageKey=()=> 'hq_parameter_request:'+owner+':'+kind;
  function savePending(){sessionStorage.setItem(storageKey(),JSON.stringify(pending))}
  const note=text=>{const n=host.querySelector('#cpUserNote');if(n)n.textContent=text};
  function applyWorkbenchLayout(layout){
    layoutApplied=true;
    if(!layout||typeof layout!=='object')return;
    try{
      const q=new URLSearchParams(location.search);
      if(kind==='image'){
        const row=document.getElementById('engineRow');if(!row)return;
        (layout.image?.order||[]).forEach(key=>{const el=row.querySelector('[data-engine="'+key+'"]');if(el)row.appendChild(el);});
        const def=layout.image?.default;
        if(def&&!q.has('engine')&&typeof window.HQBananaWorkbench?.selectEngine==='function')window.HQBananaWorkbench.selectEngine(def);
      }else{
        const tabs=document.querySelector('.function-tabs');if(!tabs)return;
        (layout.video?.order||[]).forEach(key=>{const el=tabs.querySelector('[data-function="'+key+'"]');if(el)tabs.appendChild(el);});
        const def=layout.video?.default;
        const hasDeepLink=['function','prefill','prompt','task','action'].some(k=>q.has(k));
        if(def&&!hasDeepLink&&typeof window.HQVideoWorkbench?.updateFunction==='function')window.HQVideoWorkbench.updateFunction(def);
      }
    }catch(error){}
  }
  function render(){
    if(!managedActive){if(legacy)legacy.hidden=false;host.hidden=true;host.innerHTML='';return}
    const previous=current;current=items.find(i=>i.front===previous?.front)||items[0];
    if(!current&&!pending){if(legacy)legacy.hidden=false;host.hidden=!previous;host.textContent=previous?'当前模型已停用或映射已变更，请稍后刷新。':'';return}
    host.hidden=false;
    const prompt=host.querySelector('#cpPrompt')?.value||'',oldChoice=controls?.value();
    if(legacy)legacy.hidden=!showLegacy;
    host.innerHTML='<div class="cp-actions"><h2>模型与生成参数</h2><button id="cpLegacyToggle">'+(showLegacy?'返回平台配置模型':'其他模型与工具')+'</button></div><div id="cpManagedBody" '+(showLegacy?'hidden':'')+'><p class="cp-note">选择模型和参数，确认本次点数后生成。参数由平台统一维护。</p><div class="cp-fields"><label>模型<select id="cpModel">'+items.map(i=>'<option value="'+esc(i.front)+'" '+(i.front===current?.front?'selected':'')+'>'+esc(i.label)+'</option>').join('')+'</select></label></div><div id="cpUserControls"></div><textarea id="cpPrompt" maxlength="7000" placeholder="描述你希望生成的内容" aria-label="生成提示词">'+esc(prompt)+'</textarea><label id="cpUploadLabel">参考图片<input id="cpUpload" type="file" accept="image/png,image/jpeg" multiple></label><p id="cpRefHint" class="cp-note"></p>'+(current&&current.mask_enabled&&current.reference_max>=1?'<label id="cpMaskLabel" class="cp-mask-label">蒙版图片（可选 · 局部修图）<input id="cpMaskUpload" type="file" accept="image/png,image/jpeg"><small>上传与参考图同尺寸的蒙版，白色区域将被重绘；需要 1 张参考图</small></label>':'')+'<div class="cp-actions"><button class="primary" id="cpGenerate">确认点数并生成</button><button id="cpRetry" hidden>使用原编号重试提交</button><a href="assets.html">查看我的作品与任务</a></div><p id="cpUserNote" role="status"></p><div id="cpUserResult"></div></div>';
    if(current){
      const preserved=current.combinations.find(c=>JSON.stringify(c.values)===JSON.stringify(oldChoice?.values));
      controls=mount(host.querySelector('#cpUserControls'),current,null,preserved?.id,{billingEnabled});
      host.querySelector('#cpUploadLabel').hidden=current.reference_max===0;
      host.querySelector('#cpRefHint').textContent=current.reference_max?'参考图 '+current.reference_min+'～'+current.reference_max+' 张，总大小不超过8MB。'+(current.mask_enabled?'可选上传蒙版进行局部修改。':''):'当前模型为文生图模式，无需参考图。';
      if(previous&&previous.revision!==current.revision)note('模型参数或点数已更新，请核对后提交。提示词已保留；如需参考图，请重新选择。');
    }
    host.querySelector('#cpLegacyToggle').onclick=()=>{showLegacy=!showLegacy;host.querySelector('#cpManagedBody').hidden=showLegacy;if(legacy)legacy.hidden=!showLegacy;host.querySelector('#cpLegacyToggle').textContent=showLegacy?'返回平台配置模型':'其他模型与工具'};
    host.querySelector('#cpModel').onchange=e=>{current=items.find(i=>i.front===e.target.value);controls=null;render()};
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
      if(files.length<current.reference_min||files.length>current.reference_max||files.reduce((n,f)=>n+f.size,0)>8*1024*1024)throw Error('参考图数量或大小不符合要求');
      const maskInput=host.querySelector('#cpMaskUpload'),maskFile=maskInput&&maskInput.files&&maskInput.files[0]||null;
      if(maskFile&&files.length!==1)throw Error('局部修图需要恰好 1 张参考图');
      if(maskFile&&maskFile.size>10*1024*1024)throw Error('蒙版图片不能超过 10MB');
      const choice=controls.value();if(!confirm(billingEnabled?'生成 1 个产物，本次共 '+choice.points+' 点。确认提交？':'生成 1 个产物，内测期间免费。确认提交？'))return;
      const readFile=file=>new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file)});
      const refs=await Promise.all(files.map(readFile));
      const payload={prompt,count:1,parameter_selection:{revision:current.revision,combination:choice.id}};
      if(kind==='image'){payload.model=current.front;payload.provider='openai'}else payload.channel=current.front;
      if(refs.length)payload.reference_images=refs;
      if(maskFile)payload.mask=await readFile(maskFile);
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
      if(!layoutApplied)applyWorkbenchLayout(d.layout);
      const changed=JSON.stringify(items)!==JSON.stringify(next);items=next;ready=true;
      if(changed||!host.innerHTML)render();
    }catch(error){if(host.innerHTML)note('参数暂时无法刷新；提交时会再次校验。')}
    finally{fetching=false}
  }
  window.PublishedChannelParameters={redirect:(k,front)=>{
    if(k!==kind)return false;const found=items.find(i=>i.front===front);if(!found)return false;
    managedActive=true;showLegacy=false;current=found;render();host.scrollIntoView({behavior:'smooth',block:'start'});note('该模型已启用新的参数配置，请在此选择参数并提交。');return true;
  }};
  (async()=>{try{owner=await identity();pending=JSON.parse(sessionStorage.getItem(storageKey())||'null')}catch(e){}await load();if(pending){managedActive=true;render();sync();if(pending.job_id)poll()}})();
  window.addEventListener('hq:auth-changed',event=>{billingEnabled=event.detail?.verified===true&&event.detail?.user?.points_billing_enabled===true;sync()});
  setInterval(()=>{if(!document.hidden)load()},15000);
})();
