/* Included inside the admin module through its explicit init API. */
(function(){
  window.initChannelManager=function(env){
    const {api,esc,el,toast}=env;
    let data={items:[],mappings:[],runs:[],adapters:{}}, editing=null, loading=false, runFilter=null;
    const workspace=window.initChannelWorkspace({...env,mapping:editMap,refresh:load,lifecycle});
    const parameterEditor=window.initChannelParameterEditor({...env,workspace,parameterMappings:id=>data.mappings.filter(m=>m.channel===id||m.backup===id)});
    window.closeChannelWorkspace=workspace.close;
    const labels={passed:'通过',failed:'失败',unknown:'结果未知',queued:'等待中',running:'执行中',blocked:'条件未满足'};
    const kinds={connection:'连接检测',auth:'鉴权检测',full:'完整生成测试',task:'用户任务'};
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
      const refs=data.mappings.filter(m=>m.channel===c.id||m.backup===c.id).map(m=>m.label||m.front);
      const impact={enable:'允许后续新任务接单。启用不代表渠道测试通过。',disable:'阻止后续新任务和新测试。已受理任务继续使用原配置，不会被终止。',delete:'必须先停用，并解除全部主渠道 / 备用映射引用。执行中或结果未知的调用会阻止删除。历史配置和任务记录保留。',restore:'恢复后保持停用。请检查配置及映射，再手动启用。'};
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
      el('cmMappings').innerHTML=data.mappings.length?table(['功能 / 前台标识','后台说明','主渠道 / 备用','状态','操作'],data.mappings.map((m,i)=>'<tr><td>'+esc(m.kind+' / '+m.front)+'</td><td>'+esc(m.label)+'</td><td>'+esc(data.items.find(c=>c.id===m.channel)?.name||m.channel)+' / '+esc(data.items.find(c=>c.id===m.backup)?.name||'未设置')+'</td><td>'+(m.enabled?'已生效':'未启用')+'</td><td><button data-map="'+i+'">修改 / 切换</button><button data-unmap="'+i+'">删除映射</button></td></tr>')):'<p class="muted">未配置映射，继续使用原有调用方式。</p>';
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
      el('cmRuns').innerHTML=(cid?'<button id="cmAllRuns">显示全部渠道</button>':'')+table(['时间 / 类型','渠道 / 版本','结果 / 耗时','说明','任务 / 供应商工单'],rows.map(r=>'<tr><td>'+esc(date(r.started))+'<br>'+esc(kinds[r.kind])+'</td><td>'+esc(data.items.find(c=>c.id===r.channel)?.name||r.channel)+' / v'+r.version+'</td><td>'+esc(labels[r.state])+' / '+(r.duration==null?'—':Number(r.duration).toFixed(1)+'秒')+'</td><td>'+esc(r.detail)+'</td><td>'+esc(r.job_id||'测试 '+r.id.slice(0,8))+'<br>'+esc(r.provider_id||'尚未采集')+'</td></tr>'));
    }
    async function load(){if(loading)return;loading=true;try{data=await api('/api/admin/channel-manager');render();el('cmStatus').textContent='更新于 '+new Date().toLocaleTimeString()}catch(e){el('cmStatus').textContent=e.message}finally{loading=false}}
    function edit(c={}){
      editing=c;workspace.editor(c.id?'配置渠道 · '+c.name:'新增渠道');el('cmMappingEditor').hidden=true;el('cmEditor').hidden=false;
      el('cmEditor').innerHTML='<form id="cmForm" class="cm-form"><h3>'+(c.id?'编辑渠道 · v'+c.version:'新增渠道')+'</h3><nav class="module-subnav"><button type="button" data-edit-pane="connection" class="active">连接配置</button><button type="button" data-edit-pane="material">测试素材</button><button type="button" data-edit-pane="monitor">巡检与预算</button></nav><div data-cm-edit-pane="connection"><div class="cm-fields">'+field('渠道名称','name',c.name)+field('供应商名称','supplier',c.supplier)+select('接入方式','connection_type',[['unknown','未标注'],['official','官方直连'],['relay','中转 API']],c.connection_type||'unknown')+select('协议适配器','adapter',Object.entries(data.adapters).map(([k,v])=>[k,v.name]),c.adapter)+field('API 基础地址（含协议版本路径）','base_url',c.base_url)+field('实际模型 ID','model',c.model)+field('API 密钥（留空保留旧值）','secret','','password')+field('网络代理地址（留空直连）','proxy',c.proxy)+field('请求超时 / 秒','timeout',c.timeout||120,'number')+field('最大并发','concurrency',c.concurrency||2,'number')+field('等待队列上限','queue_limit',c.queue_limit??20,'number')+field('每分钟调用上限','rpm',c.rpm||30,'number')+'</div>'+check('启用渠道，允许新任务接单','enabled',c.enabled)+'</div><div data-cm-edit-pane="material" hidden><h4>测试素材</h4><div class="cm-fields">'+field('测试提示词','prompt',c.fixture?.prompt||'生成一张纯色背景的产品展示图')+field('视频时长 / 秒','duration',c.fixture?.duration||5,'number')+select('画面比例','ratio',['9:16','16:9','1:1'].map(x=>[x,x]),c.fixture?.ratio||'9:16')+'<label>参考图（视频协议支持；留空保留已有 '+Number(c.material_count||0)+' 张）<input name="materials" type="file" accept="image/png,image/jpeg" multiple></label>'+check('清除已有参考图','clear_materials',false)+'</div></div><div data-cm-edit-pane="monitor" hidden><h4>定时巡检与预算</h4><div class="cm-fields">'+field('轻量巡检间隔 / 秒','poll_seconds',c.poll_seconds||900,'number')+field('每日完整测试时间 / 北京时间小时','daily_hour',c.daily_hour??9,'number')+field('每日完整测试最多次数','daily_limit',c.daily_limit??1,'number')+field('单次预留费用 / 元','test_cost',c.test_cost??0,'number')+field('每日预留费用上限 / 元','daily_budget',c.daily_budget??0,'number')+'</div><p class="muted">费用由管理员按供应商价格预估，不是实扣账单。失败和结果未知也占用预算。完整测试核验产物，不代表用户已接收。</p>'+check('启用定时连接检测','monitor',c.monitor)+check('启用每日完整生成测试','daily_test',c.daily_test)+'</div><div class="actions"><button class="primary">保存为新版本</button><button type="button" id="cmCancel">取消</button></div></form>'+(c.id?'<details><summary>历史版本回滚</summary><div class="actions">'+(c.history||[]).filter(h=>h.version!==c.version).map(h=>'<button data-rollback="'+h.version+'">恢复 v'+h.version+' · '+esc(date(h.created))+'</button>').join('')+'</div><p class="muted">回滚创建新版本，仅影响新任务。历史任务继续使用当时配置。</p></details>':'');
      el('cmEditor').scrollIntoView({behavior:'smooth',block:'start'});
    }
    function editMap(m={}){
      workspace.editor('配置功能映射');el('cmEditor').hidden=true;el('cmMappingEditor').hidden=false;
      const kind=m.kind||'image',choices=window.ChannelCatalog.compatible(data,kind);
      el('cmMappingEditor').innerHTML='<form id="cmMapForm" class="cm-form"><h3>前台功能 → 实际模型</h3><p class="muted">只列出兼容协议的渠道。启用前请确认渠道测试结果；保存时显示变更影响。修改请求标识将新增映射，不会自动删除旧映射。</p><div class="cm-fields">'+select('功能类型','kind',[['image','图片生成'],['xiaole_video','视频生成']],kind)+field('前台请求标识','front',m.front)+field('后台展示说明','label',m.label)+select('主渠道','channel',choices.map(c=>[c.id,c.name+' · '+c.model+(c.enabled?'':'（已停用）')]),m.channel)+select('备用渠道（手动切换）','backup',[['','未设置'],...choices.map(c=>[c.id,c.name])],m.backup)+'</div>'+check('启用该映射','enabled',m.enabled)+'<p id="cmMappingImpact" class="cm-flow"></p><button class="primary">预览影响并保存</button></form>';
      const f=el('cmMapForm');
      const impact=()=>{const c=data.items.find(c=>c.id===f.elements.channel.value);el('cmMappingImpact').textContent=(f.elements.label.value||f.elements.front.value||'前台功能')+' → '+(c?c.name+' → '+c.model:'未选择兼容渠道')+'；'+(f.elements.enabled.checked?'保存后影响此请求标识的新任务':'当前不启用')};
      f.oninput=impact;f.onchange=e=>{if(e.target.name==='kind')editMap({...values(f),channel:'',backup:''});else impact()};impact();
    }
    document.querySelector('[data-module="managedChannels"]').addEventListener('click',async e=>{
      const b=e.target.closest('button');if(!b)return;
      try{
        if(b.dataset.editPane){el('cmEditor').querySelectorAll('[data-cm-edit-pane]').forEach(n=>n.hidden=n.dataset.cmEditPane!==b.dataset.editPane);el('cmEditor').querySelectorAll('[data-edit-pane]').forEach(n=>n.classList.toggle('active',n===b));return}
        if(b.id==='cmReload')return load();if(b.id==='cmNew')return edit();if(b.id==='cmCancel'){workspace.close();return}
        if(b.id==='cmMapNew')return editMap();if(b.id==='cmAllRuns')return renderRuns();
        if(b.dataset.edit)return edit(data.items.find(c=>c.id===b.dataset.edit));
        if(b.dataset.parameters)return parameterEditor.open(data.items.find(c=>c.id===b.dataset.parameters));
        if(b.dataset.addModel){toast('先保存独立模型连接配置，再设置参数和功能映射；原内置线路保留。');return edit({name:b.dataset.supplier+' · 模型配置',supplier:b.dataset.supplier,adapter:b.dataset.addModel,enabled:false})}
        if(b.dataset.unmap!=null){const m=data.mappings[Number(b.dataset.unmap)];if(!confirm('删除 '+m.kind+' / '+m.front+' 的功能映射？\n后续新请求恢复原有默认调用方式，并非停用该功能。已有任务不受影响。'))return;b.disabled=true;await post('unmap',{selector:m.kind+':'+m.front,expected:m});await load();return}
        if(b.dataset.map!=null)return editMap(data.mappings[Number(b.dataset.map)]);
        if(b.dataset.runs){renderRuns(b.dataset.runs);el('cmRuns').scrollIntoView({behavior:'smooth'});return}
        if(b.dataset.test){if(b.dataset.test==='full'){const c=data.items.find(c=>c.id===b.dataset.id);if(!confirm('发起 '+c.name+' 的完整生成测试？\n素材：'+(c.fixture?.prompt||'未准备提示词')+'，参考图 '+Number(c.material_count||0)+' 张。\n预留费用 '+Number(c.test_cost||0)+' 元；可能产生供应商费用，未知结果也占用预算。'))return}b.disabled=true;await post('test',{id:b.dataset.id,kind:b.dataset.test});toast('测试已排队，可在最近记录查看');await load()}
        if(b.dataset.rollback){if(!confirm('恢复为 v'+b.dataset.rollback+' 的配置并创建新版本？仅影响新任务。\n关联映射：'+data.mappings.filter(m=>m.channel===editing.id||m.backup===editing.id).map(m=>m.label||m.front).join('、')))return;b.disabled=true;await post('rollback',{id:editing.id,target_version:Number(b.dataset.rollback),enabled:!!editing.enabled});workspace.close();await load()}
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
          delete v.materials;const affected=data.mappings.filter(m=>m.channel===editing.id||m.backup===editing.id).map(m=>m.label||m.front);if(!confirm('保存渠道 '+v.name+' 为新版本？\n实际模型：'+(editing.model||'未配置')+' → '+v.model+'\n接单：'+(v.enabled?'启用':'停用')+'\n关联功能：'+(affected.join('、')||'尚未配置映射')+'\n已有任务保留原版本，仅新任务受影响。'))return;await post('save',v);workspace.close();
        }else if(f.id==='cmMapForm'){const old=data.mappings.find(m=>m.kind===v.kind&&m.front===v.front),name=id=>data.items.find(c=>c.id===id)?.name||'未配置';if(!confirm('确认修改 '+v.kind+' / '+v.front+' 的路由？\n原渠道：'+name(old?.channel)+'\n新渠道：'+name(v.channel)+'\n'+el('cmMappingImpact').textContent))return;await post('mapping',v);workspace.close()}
        else if(f.id==='cmNoticeForm'){await post('notifications',v);el('cmNotifications').innerHTML=''}
        toast('已保存');await load();
      }catch(err){toast(err.message)}finally{if(b)b.disabled=false}
    });
    setInterval(()=>{if(env.active()&&el('cmDrawer').hidden&&!document.querySelector('.cm-lifecycle-dialog[open]')&&!document.querySelector('#cmNoticeForm:focus-within'))load()},15000);
    return load;
  };
})();
