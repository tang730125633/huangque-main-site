/* Included inside the admin module through its explicit init API. */
(function(){
  window.initChannelManager=function(env){
    const {api,esc,el,toast}=env;
    let data={items:[],mappings:[],runs:[],adapters:{}}, editing=null, loading=false, runFilter=null, secretTimer=null, replacementPaused=[];
    const secretField=()=>'<div class="cm-secret-field" style="display:grid;gap:6px"><label>API 密钥（留空保留旧值）<input class="field" name="secret" type="password" autocomplete="new-password" placeholder="不填则保留已保存密钥"></label><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><button type="button" class="mini" data-cm-secret="view">显示 5 秒</button><button type="button" class="mini" data-cm-secret="copy">复制</button><span class="muted" style="font-size:11.5px">查看明文会留审计记录</span></div><p id="cmSecretReveal" class="muted" hidden style="margin:0;word-break:break-all;font-family:ui-monospace,monospace;user-select:all"></p></div>';
    function selectReveal(node){
      try{const range=document.createRange();range.selectNodeContents(node);const sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);return true}catch(e){return false}
    }
    function copyToClipboard(text){
      const legacy=()=>{const node=document.createElement('textarea');node.value=text;node.setAttribute('readonly','readonly');node.style.position='fixed';node.style.top='-1000px';document.body.appendChild(node);node.select();let ok=false;try{ok=document.execCommand('copy')}catch(e){ok=false}node.remove();return ok};
      const modern=navigator.clipboard&&window.isSecureContext?navigator.clipboard.writeText(text):Promise.reject(new Error('剪贴板不可用'));
      return modern.then(()=>true,()=>legacy());
    }
    function revealSecret(mode){
      if(!editing||!editing.id){toast('请先保存渠道，才能查看已保存的密钥');return}
      if(!confirm('查看密钥明文会留下审计记录（操作人 / 渠道 / 时间），确认查看？'))return;
      api('/api/admin/channel-manager/secret-reveal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:editing.id})}).then(d=>{
        const box=el('cmSecretReveal');if(!box)return;
        const secret=String(d.secret||''),seconds=Number(d.expires_in)||5;
        box.hidden=false;box.textContent=secret||'（该渠道未配置密钥）';
        clearTimeout(secretTimer);secretTimer=setTimeout(()=>{box.hidden=true;box.textContent=''},seconds*1000);
        if(mode!=='copy'){toast('已显示，'+seconds+' 秒后隐藏');return}
        if(!secret){toast('该渠道未配置密钥');return}
        selectReveal(box);
        copyToClipboard(secret).then(ok=>{
          toast(ok?'密钥已复制，'+seconds+' 秒后隐藏':'未能自动复制；明文已选中，请按 Ctrl+C');
        });
      }).catch(e=>toast(e.message));
    }
    function editChannelById(id){
      const channel=data.items.find(c=>c.id===id);
      if(!channel){toast('没有找到对应的托管渠道，已取消编辑');return false}
      edit(channel);return true;
    }
    /* 服务器托管线路替换：新渠道只能是候选，绝不能顶掉已生效的主渠道。
       managed 映射把新渠道追加到候选末尾；legacy/shadow 先写影子候选；
       paused 的功能一律不动 —— 暂停是管理员的明确决定，不能因为换 Key 被静默恢复。 */
    async function applyReplacement(saved,replacement){
      const paused=[];
      for(const operationId of replacement.operations||[]){
        const current=routeMappings().find(m=>m.operation_id===operationId)||{};
        const state=String(current.state||'legacy');
        if(state==='paused'){paused.push(operationId);continue}
        const existing=mappingChannels(current).filter(id=>id!==saved.id),managed=state==='managed';
        await post('operation-mapping',{operation_id:operationId,state:managed?'managed':'shadow',channels:managed?[...existing,saved.id]:[saved.id,...existing],expected_revision:Number(current.revision||0)});
      }
      await post('test',{id:saved.id,kind:'connection'});
      await post('test',{id:saved.id,kind:'auth'});
      return paused;
    }
    const workspace=window.initChannelWorkspace({...env,mapping:editMap,refresh:load,lifecycle,editChannel:editChannelById,newChannel:template=>edit(template||{})});
    const mappingChannels=m=>Array.isArray(m?.channels)?m.channels:[m?.channel,m?.backup].filter(Boolean);
    function routeMappings(){return [...(data.mappings||[]),...(data.operation_mappings||[])]}
    const parameterEditor=window.initChannelParameterEditor({...env,workspace,parameterMappings:id=>routeMappings().filter(m=>mappingChannels(m).includes(id))});
    window.closeChannelWorkspace=workspace.close;
    const labels={passed:'通过',failed:'失败',unknown:'结果未知',terminated:'已终止',queued:'等待中',running:'执行中',blocked:'条件未满足',captured:'已记录'};
    const kinds={connection:'连接检测',auth:'鉴权检测',full:'完整生成测试',task:'用户任务',shadow:'影子观察'};
    const date=n=>n?new Date(n*1000).toLocaleString():'—';
    const field=(label,name,value,type='text')=>'<label>'+esc(label)+'<input class="field" name="'+name+'" type="'+type+'" value="'+esc(value??'')+'" '+(type==='password'?'autocomplete="new-password"':type==='number'?'step="any"':'')+'></label>';
    const check=(label,name,value)=>'<label><input name="'+name+'" type="checkbox" '+(value?'checked':'')+'> '+esc(label)+'</label>';
    function options(items,selected){return items.map(([id,name])=>'<option value="'+esc(id)+'" '+(id===selected?'selected':'')+'>'+esc(name)+'</option>').join('')}
    function select(label,name,items,selected){return '<label>'+esc(label)+'<select class="field" name="'+name+'">'+options(items,selected)+'</select></label>'}
    function values(form){const out=Object.fromEntries(new FormData(form));form.querySelectorAll('input[type=checkbox]').forEach(x=>out[x.name]=x.checked);return out}
    async function post(action,body){return api('/api/admin/channel-manager/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
    function table(head,rows){return '<div style="overflow:auto"><table><thead><tr>'+head.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>'}
    function lifecycle(c,action){
      if(!c)return;
      const names={enable:'启用',disable:'停用',delete:'移入回收站',restore:'恢复'};
      const refs=[...(data.mappings||[]),...(data.operation_mappings||[])]
        .filter(m=>mappingChannels(m).includes(c.id)).map(m=>m.label||m.operation_id||m.front);
      const impact={enable:'允许后续新任务接单。启用不代表渠道测试通过。',disable:'阻止后续新任务和新测试。已受理任务继续使用原配置，不会被终止。',delete:'必须先停用，并解除全部渠道优先级引用。执行中或结果未知的调用会阻止删除。历史配置和任务记录保留。',restore:'恢复后保持停用。请检查配置及映射，再手动启用。'};
      if(c.source==='legacy')impact.disable='阻止下列范围的新任务接单。已有任务继续执行；鉴权探针及其他接口保持原有行为。';
      const dialog=document.createElement('dialog');dialog.className='cm-lifecycle-dialog';
      dialog.innerHTML='<form><h3>'+names[action]+' · '+esc(c.name)+'</h3><p>'+esc(impact[action])+'</p><p>'+esc(c.source==='legacy'?'控制范围：'+c.scope+'。其他同步接口不受此开关控制。':'关联功能：'+(refs.join('、')||'无'))+'</p><label>操作原因<textarea class="field" name="reason" required minlength="2" maxlength="200" placeholder="请填写原因，便于后续追踪"></textarea></label><p class="cm-operation-error" role="alert"></p><div class="actions"><button type="button">取消</button><button class="primary" type="submit">确认'+names[action]+'</button></div></form>';
      document.body.append(dialog);dialog.querySelector('button[type=button]').onclick=()=>dialog.close();dialog.onclose=()=>dialog.remove();
      const form=dialog.querySelector('form');let busy=false;
      dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault()});
      form.onsubmit=async e=>{e.preventDefault();if(busy)return;if(form.elements.reason.value.trim().length<2){dialog.querySelector('[role=alert]').textContent='请填写至少 2 字操作原因';return}busy=true;const buttons=[...form.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
        try{await post(c.source==='legacy'?'legacy-lifecycle':'lifecycle',{id:c.id||c.key,version:c.version,action,reason:form.elements.reason.value.trim()});dialog.close();workspace.close();toast('已'+names[action]);await load()}
        catch(error){dialog.querySelector('[role=alert]').textContent=error.message+'；若版本已变化，请取消并刷新列表后重试。'}
        finally{busy=false;buttons.forEach(b=>b.disabled=false)}
      };dialog.showModal();form.elements.reason.focus();
    }
    function render(){
      workspace.render(data);
      const stateName={legacy:'旧线路',shadow:'影子校验',managed:'统一托管',paused:'已暂停'};
      const operationRows=(data.operations||[]).map(op=>{const m=op.mapping||{};return '<tr><td>'+esc(op.page_name+' / '+op.feature_name)+'</td><td><strong>'+esc(op.name)+'</strong><br><code>'+esc(op.operation_id)+'</code></td><td>'+esc(data.items.find(c=>c.id===m.channel)?.name||'—')+' / '+esc(data.items.find(c=>c.id===m.backup)?.name||'未设置')+'</td><td>'+esc(stateName[m.state]||'未发布（旧线路）')+(m.revision?' · r'+m.revision:'')+'</td><td><button data-operation="'+esc(op.operation_id)+'">配置 / 切换</button></td></tr>'});
      const legacy=(data.mappings||[]).length?'<details><summary>兼容期旧 kind/front 映射（'+data.mappings.length+'）</summary>'+table(['选择器','说明','主渠道','状态','操作'],data.mappings.map((m,i)=>'<tr><td>'+esc(m.kind+' / '+m.front)+'</td><td>'+esc(m.label)+'</td><td>'+esc(data.items.find(c=>c.id===m.channel)?.name||m.channel)+'</td><td>'+(m.enabled?'启用':'停用')+'</td><td><button data-legacy-unmap="'+i+'">删除旧映射</button></td></tr>'))+'</details>':'';
      el('cmMappings').innerHTML=operationRows.length?table(['页面 / 功能','稳定操作标识','主渠道 / 备用','控制状态','操作'],operationRows)+legacy:'<p class="muted">暂无可接入统一渠道的功能。</p>';
      renderRuns(runFilter);
      el('cmAudit').innerHTML=(data.legacy_audit_error?'<p class="muted">'+esc(data.legacy_audit_error)+'</p>':'')+[...(data.events||[]),...(data.legacy_events||[])].sort((a,b)=>b.created-a.created).map(x=>'<div class="task-proof-line">'+esc(date(x.created)+' · '+x.actor+' · '+x.action+' · '+x.target)+'</div>').join('')||'暂无记录';
      const n=data.notifications||{},counts=n.delivery||{};
      // Do not overwrite a notification form while the operator is editing it.
      if(!el('cmNotifications').querySelector('form'))el('cmNotifications').innerHTML='<form id="cmNoticeForm" class="cm-form">'+check('启用故障与恢复通知','enabled',n.enabled)+field('接收端 Webhook（留空保留，保存后隐藏）','endpoint','','password')+'<p>已发送 '+Number(counts.sent||0)+' · 待发 '+Number(counts.pending||0)+' · 失败 '+Number(counts.failed||0)+'。HTTP 接受不等于收件人已阅读。</p><button class="primary">保存通知设置</button></form>';
      el('cmNotifications').querySelector('p').textContent='已发送 '+Number(counts.sent||0)+' · 待发 '+Number(counts.pending||0)+' · 失败 '+Number(counts.failed||0)+'。HTTP 接受不等于收件人已阅读。';
    }
    function renderRuns(cid){
      runFilter=cid||null;
      const rows=data.runs.filter(r=>!cid||r.channel===cid);
      el('cmRuns').innerHTML=(cid?'<button id="cmAllRuns">显示全部渠道</button>':'')+table(['时间 / 类型','渠道 / 版本','结果 / 耗时','说明','任务 / 供应商工单'],rows.map(r=>'<tr><td>'+esc(date(r.started))+'<br>'+esc(kinds[r.kind])+'</td><td>'+esc(data.items.find(c=>c.id===r.channel)?.name||r.channel)+' / v'+r.version+'</td><td>'+esc(labels[r.state])+' / '+(r.duration==null?'—':Number(r.duration).toFixed(1)+'秒')+'</td><td>'+esc(r.detail)+'</td><td>'+esc(r.job_id||'测试 '+r.id.slice(0,8))+'<br>'+esc(r.provider_id||'尚未采集')+'<br>'+esc(r.operation_id||'未归档 operation')+(r.mapping_revision?' · r'+r.mapping_revision:'')+(r.invocation_source?' · '+esc(r.invocation_source):'')+'</td></tr>'));
    }
    async function load(){if(loading)return;loading=true;try{data=await api('/api/admin/channel-manager');render();el('cmStatus').textContent='更新于 '+new Date().toLocaleTimeString()}catch(e){el('cmStatus').textContent=e.message}finally{loading=false}}
    function edit(c={}){
      const replacement=!!c._replacement;
      editing=c;clearTimeout(secretTimer);workspace.editor(c.id?'配置渠道 · '+c.name:replacement?'直接修改 API Key / Base URL':'新增渠道');el('cmMappingEditor').hidden=true;el('cmEditor').hidden=false;
      el('cmEditor').innerHTML='<form id="cmForm" class="cm-form"><h3>'+(c.id?'编辑渠道 · v'+c.version:replacement?'替换服务器托管线路':'新增渠道')+'</h3>'+(replacement?'<p class="cm-replacement-intro">当前服务器环境变量不会被覆盖。保存后会建立一条可管理的新线路、加入当前模型的影子候选并自动发起连接与鉴权检测；完整生成验证通过后，才能切为生产主渠道。已暂停的功能保持暂停，不会被自动恢复。</p>':'')+'<nav class="module-subnav"><button type="button" data-edit-pane="connection" class="active">连接配置</button><button type="button" data-edit-pane="material">测试素材</button><button type="button" data-edit-pane="monitor">巡检与预算</button></nav><div data-cm-edit-pane="connection"><div class="cm-fields">'+field('渠道名称','name',c.name)+field('供应商名称','supplier',c.supplier)+select('接入方式','connection_type',[['unknown','未标注'],['official','官方直连'],['relay','中转 API']],c.connection_type||'unknown')+select('协议适配器','adapter',Object.entries(data.adapters).map(([k,v])=>[k,v.name]),c.adapter)+field('API 基础地址（含协议版本路径）','base_url',c.base_url)+field('实际模型 ID','model',c.model)+secretField()+field('网络代理地址（留空直连）','proxy',c.proxy)+field('请求超时 / 秒','timeout',c.timeout||120,'number')+field('最大并发','concurrency',c.concurrency||2,'number')+field('等待队列上限','queue_limit',c.queue_limit??20,'number')+field('每分钟调用上限','rpm',c.rpm||30,'number')+'</div>'+check('启用渠道，允许新任务接单','enabled',c.enabled)+'</div><div data-cm-edit-pane="material" hidden><h4>测试素材</h4><div class="cm-fields">'+field('测试提示词','prompt',c.fixture?.prompt||'生成一张纯色背景的产品展示图')+field('视频时长 / 秒','duration',c.fixture?.duration||5,'number')+select('画面比例','ratio',['9:16','16:9','1:1'].map(x=>[x,x]),c.fixture?.ratio||'9:16')+'<label>参考图（视频协议支持；留空保留已有 '+Number(c.material_count||0)+' 张）<input name="materials" type="file" accept="image/png,image/jpeg" multiple></label>'+check('清除已有参考图','clear_materials',false)+'</div></div><div data-cm-edit-pane="monitor" hidden><h4>定时巡检与预算</h4><div class="cm-fields">'+field('轻量巡检间隔 / 秒','poll_seconds',c.poll_seconds||900,'number')+field('每日完整测试时间 / 北京时间小时','daily_hour',c.daily_hour??9,'number')+field('每日完整测试最多次数','daily_limit',c.daily_limit??1,'number')+field('单次预留费用 / 元','test_cost',c.test_cost??0,'number')+field('每日预留费用上限 / 元','daily_budget',c.daily_budget??0,'number')+'</div><p class="muted">费用由管理员按供应商价格预估，不是实扣账单。失败和结果未知也占用预算。完整测试核验产物，不代表用户已接收。</p>'+check('启用定时连接检测','monitor',c.monitor)+check('启用每日完整生成测试','daily_test',c.daily_test)+'</div><div class="actions"><button class="primary">'+(replacement?'保存新 Key 并开始检测':'保存为新版本')+'</button><button type="button" id="cmCancel">取消</button></div></form>'+(c.id?'<details><summary>历史版本回滚</summary><div class="actions">'+(c.history||[]).filter(h=>h.version!==c.version).map(h=>'<button data-rollback="'+h.version+'">恢复 v'+h.version+' · '+esc(date(h.created))+'</button>').join('')+'</div><p class="muted">回滚创建新版本，仅影响新任务。历史任务继续使用当时配置。</p></details>':'');
      el('cmEditor').scrollIntoView({behavior:'smooth',block:'start'});
    }
    function editMap(selected={}){
      workspace.editor('配置功能映射');el('cmEditor').hidden=true;el('cmMappingEditor').hidden=false;
      const operations=data.operations||[],op=operations.find(x=>x.operation_id===(selected.operation_id||''))||operations[0];
      if(!op){toast('暂无可配置功能');return}
      const m=selected.revision?selected:(op.mapping||{}),choices=window.ChannelCatalog.compatible(data,op.channel_kind),state=m.state||'legacy';
      el('cmMappingEditor').innerHTML='<form id="cmMapForm" class="cm-form"><h3>稳定功能 → 实际模型</h3><p class="muted">映射按 operation_id 发布不可变版本。托管状态要求候选渠道已启用，并有最近 24 小时完整生成测试；生图仅在确认未受理时按优先级安全切换，不会退回旧线路。</p><div class="cm-fields">'+select('稳定功能','operation_id',operations.map(x=>[x.operation_id,x.page_name+' / '+x.name]),op.operation_id)+select('控制状态','state',[['legacy','旧线路'],['shadow','影子校验（不改实际路由）'],['managed','统一托管'],['paused','暂停（明确拒绝）']],state)+select('主渠道','channel',[['','未设置'],...choices.map(c=>[c.id,c.name+' · '+c.model+(c.enabled?'':'（已停用）')])],m.channel)+select('备用渠道（生图安全候补）','backup',[['','未设置'],...choices.map(c=>[c.id,c.name])],m.backup)+'</div><input type="hidden" name="expected_revision" value="'+esc(m.revision||0)+'"><input type="hidden" name="channels_json" value="'+esc(JSON.stringify(mappingChannels(m)))+'"><p id="cmMappingImpact" class="cm-flow"></p><button class="primary">发布新映射版本</button></form>'+((m.history||[]).length>1?'<details><summary>历史映射版本</summary><div class="actions">'+m.history.filter(h=>h.revision!==m.revision).map(h=>'<button data-operation-rollback="'+h.revision+'" data-operation-id="'+esc(op.operation_id)+'" data-expected-revision="'+m.revision+'">恢复 r'+h.revision+' · '+esc(h.state)+' · '+esc(date(h.created))+'</button>').join('')+'</div><p class="muted">恢复会发布新修订，不改写历史任务。</p></details>':'');
      const f=el('cmMapForm');
      const impact=()=>{const c=data.items.find(c=>c.id===f.elements.channel.value);el('cmMappingImpact').textContent=op.operation_id+' → '+f.elements.state.options[f.elements.state.selectedIndex].text+' → '+(c?c.name+' / '+c.model:'旧线路或暂停状态无需渠道')+'；仅影响发布后的新任务'};
      f.oninput=impact;f.onchange=e=>{if(e.target.name==='operation_id'){const next=operations.find(x=>x.operation_id===e.target.value);editMap(next?.mapping||{operation_id:e.target.value})}else impact()};impact();
    }
    document.querySelector('[data-module="managedChannels"]').addEventListener('click',async e=>{
      const b=e.target.closest('button');if(!b)return;
      try{
        if(b.dataset.cmSecret){revealSecret(b.dataset.cmSecret);return}
        if(b.dataset.editPane){el('cmEditor').querySelectorAll('[data-cm-edit-pane]').forEach(n=>n.hidden=n.dataset.cmEditPane!==b.dataset.editPane);el('cmEditor').querySelectorAll('[data-edit-pane]').forEach(n=>n.classList.toggle('active',n===b));return}
        if(b.id==='cmReload')return load();if(b.id==='cmNew')return edit();if(b.id==='cmCancel'){workspace.close();return}
        if(b.id==='cmMapNew')return editMap();if(b.id==='cmAllRuns')return renderRuns();
        if(b.dataset.edit)return editChannelById(b.dataset.edit);
        if(b.dataset.parameters)return parameterEditor.open(data.items.find(c=>c.id===b.dataset.parameters));
        if(b.dataset.addModel){toast('先保存独立模型连接配置，再设置参数和功能映射；原内置线路保留。');return edit({name:b.dataset.supplier+' · 模型配置',supplier:b.dataset.supplier,adapter:b.dataset.addModel,enabled:false})}
        if(b.dataset.legacyUnmap!=null){const m=data.mappings[Number(b.dataset.legacyUnmap)];if(!confirm('删除兼容期旧映射 '+m.kind+' / '+m.front+'？\n未发布 operation 映射的请求将恢复原有默认线路；已有任务不受影响。'))return;b.disabled=true;await post('unmap',{selector:m.kind+':'+m.front,expected:m});await load();return}
        if(b.dataset.operationRollback){if(!confirm('把 '+b.dataset.operationId+' 恢复为 r'+b.dataset.operationRollback+' 并发布新修订？'))return;b.disabled=true;await post('operation-mapping-rollback',{operation_id:b.dataset.operationId,target_revision:Number(b.dataset.operationRollback),expected_revision:Number(b.dataset.expectedRevision)});workspace.close();await load();return}
        if(b.dataset.operation)return editMap((data.operations||[]).find(x=>x.operation_id===b.dataset.operation)?.mapping||{operation_id:b.dataset.operation});
        if(b.dataset.runs){renderRuns(b.dataset.runs);el('cmRuns').scrollIntoView({behavior:'smooth'});return}
        if(b.dataset.test){if(b.dataset.test==='full'){const c=data.items.find(c=>c.id===b.dataset.id);if(!confirm('发起 '+c.name+' 的完整生成测试？\n素材：'+(c.fixture?.prompt||'未准备提示词')+'，参考图 '+Number(c.material_count||0)+' 张。\n预留费用 '+Number(c.test_cost||0)+' 元；可能产生供应商费用，未知结果也占用预算。'))return}b.disabled=true;await post('test',{id:b.dataset.id,kind:b.dataset.test});toast('测试已排队，可在最近记录查看');await load()}
        if(b.dataset.rollback){if(!confirm('恢复为 v'+b.dataset.rollback+' 的配置并创建新版本？仅影响新任务。\n关联映射：'+routeMappings().filter(m=>mappingChannels(m).includes(editing.id)).map(m=>m.label||m.operation_id||m.front).join('、')))return;b.disabled=true;await post('rollback',{id:editing.id,target_version:Number(b.dataset.rollback),enabled:!!editing.enabled});workspace.close();await load()}
      }catch(err){toast(err.message)}finally{b.disabled=false}
    });
    document.querySelector('[data-module="managedChannels"]').addEventListener('submit',async e=>{
      if(!['cmForm','cmMapForm','cmNoticeForm'].includes(e.target.id))return;
      e.preventDefault();const f=e.target,b=f.querySelector('button[type=submit],button.primary');if(b)b.disabled=true;
      try{
        const v=values(f);
        if(f.id==='cmForm'){
          const files=Array.from(f.elements.materials.files);if(files.length>5||files.reduce((n,x)=>n+x.size,0)>8*1024*1024)throw Error('最多5张参考图，总大小不超过8MB');
          const refs=await Promise.all(files.map(file=>new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file)})));
          v.id=editing.id;v.version=editing.version;v.fixture={prompt:v.prompt,duration:Number(v.duration),ratio:v.ratio};
          if(refs.length||v.clear_materials)v.fixture.reference_images=refs;
          delete v.materials;const affected=routeMappings().filter(m=>mappingChannels(m).includes(editing.id)).map(m=>m.label||m.operation_id||m.front);if(!confirm('保存渠道 '+v.name+' 为新版本？\n实际模型：'+(editing.model||'未配置')+' → '+v.model+'\n接单：'+(v.enabled?'启用':'停用')+'\n关联功能：'+(affected.join('、')||'尚未配置映射')+'\n已有任务保留原版本，仅新任务受影响。'))return;const replacement=editing._replacement,saved=await post('save',v);replacementPaused=replacement?await applyReplacement(saved,replacement):[];workspace.close();
        }else if(f.id==='cmMapForm'){const name=id=>data.items.find(c=>c.id===id)?.name||'未配置',tail=JSON.parse(v.channels_json||'[]').slice(2);v.channels=[v.channel,v.backup,...tail].filter((id,index,list)=>id&&list.indexOf(id)===index);delete v.channels_json;if(!confirm('确认发布 '+v.operation_id+' 的新路由版本？\n控制状态：'+v.state+'\n主渠道：'+name(v.channel)+'\n'+el('cmMappingImpact').textContent))return;v.expected_revision=Number(v.expected_revision||0);await post('operation-mapping',v);workspace.close()}
        else if(f.id==='cmNoticeForm'){await post('notifications',v);el('cmNotifications').innerHTML=''}
        toast(editing?._replacement?(replacementPaused.length?'新 Key 已保存并加入候选；'+replacementPaused.length+' 个已暂停的功能保持不变，未加入候选':'新 Key 已保存并加入影子候选；连接与鉴权检测已开始'):'已保存');await load();
      }catch(err){toast(err.message)}finally{if(b)b.disabled=false}
    });
    setInterval(()=>{if(env.active()&&el('cmDrawer').hidden&&!document.querySelector('.cm-lifecycle-dialog[open]')&&!document.querySelector('#cmNoticeForm:focus-within'))load()},15000);
    return load;
  };
})();
