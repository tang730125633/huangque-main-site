/* Included inside the admin module through its explicit init API. */
(function(){
  window.initChannelManager=function(env){
    const {api,esc,el,toast}=env;
    let data={items:[],mappings:[],runs:[],adapters:{}}, editing=null, loading=false, runFilter=null;
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
    function render(){
      el('cmList').innerHTML=data.items.length?table(['渠道 / 实际模型','接单 / 健康','近24小时任务','最近检测','操作'],data.items.map(c=>'<tr><td><b>'+esc(c.name)+'</b><br>'+esc(c.model)+'<br><small>配置 v'+c.version+' · '+esc(data.adapters[c.adapter]?.name||c.adapter)+'</small></td><td>'+(c.enabled?'已启用':'已停用')+'<br>'+esc(c.health)+'</td><td>'+Number(c.stats.total||0)+' 次 · 失败 '+Number(c.stats.failed||0)+' · 未知 '+Number(c.stats.unknown||0)+'<br>平均 '+(c.stats.avg_duration==null?'未采集':Number(c.stats.avg_duration).toFixed(1)+'秒')+'</td><td>'+(c.checks||[]).map(r=>esc(kinds[r.kind]+'：'+labels[r.state])+'<br><small>'+esc(date(r.updated))+'</small>').join('<br>')+'</td><td><button data-edit="'+esc(c.id)+'">配置</button> <button data-test="connection" data-id="'+esc(c.id)+'">连接</button> <button data-test="auth" data-id="'+esc(c.id)+'">鉴权</button> <button data-test="full" data-id="'+esc(c.id)+'">生成测试</button> <button data-runs="'+esc(c.id)+'">调用记录</button></td></tr>')):'<div class="empty">暂无受管理渠道。新增渠道后配置映射，才会影响新任务。</div>';
      el('cmMappings').innerHTML=data.mappings.length?table(['功能 / 前台标识','后台说明','主渠道 / 备用','状态','操作'],data.mappings.map((m,i)=>'<tr><td>'+esc(m.kind+' / '+m.front)+'</td><td>'+esc(m.label)+'</td><td>'+esc(data.items.find(c=>c.id===m.channel)?.name||m.channel)+' / '+esc(data.items.find(c=>c.id===m.backup)?.name||'未设置')+'</td><td>'+(m.enabled?'已生效':'未启用')+'</td><td><button data-map="'+i+'">修改 / 切换</button></td></tr>')):'<p class="muted">未配置映射，继续使用原有调用方式。</p>';
      el('cmList').querySelectorAll('[data-edit]').forEach(b=>{const c=data.items.find(x=>x.id===b.dataset.edit),p=document.createElement('p');p.className='muted';p.textContent=(c.monitor?'下次连接：'+date(c.schedule?.light_due):'连接巡检未启用')+' / '+(c.daily_test?'下次生成：'+date(c.schedule?.full_due):'每日生成未启用');b.closest('tr').cells[3].appendChild(p)});
      renderRuns(runFilter);
      el('cmAudit').innerHTML=(data.events||[]).map(x=>'<div class="task-proof-line">'+esc(date(x.created)+' · '+x.actor+' · '+x.action+' · '+x.target)+'</div>').join('')||'暂无记录';
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
      editing=c;el('cmEditor').hidden=false;
      el('cmEditor').innerHTML='<form id="cmForm" class="cm-form"><h3>'+(c.id?'编辑渠道 · v'+c.version:'新增渠道')+'</h3><div class="cm-fields">'+field('渠道名称','name',c.name)+select('协议适配器','adapter',Object.entries(data.adapters).map(([k,v])=>[k,v.name]),c.adapter)+field('API 基础地址（含协议版本路径）','base_url',c.base_url)+field('实际模型 ID','model',c.model)+field('API 密钥（留空保留旧值）','secret','','password')+field('网络代理地址（留空直连）','proxy',c.proxy)+field('请求超时 / 秒','timeout',c.timeout||120,'number')+field('最大并发','concurrency',c.concurrency||2,'number')+field('等待队列上限','queue_limit',c.queue_limit??20,'number')+field('每分钟调用上限','rpm',c.rpm||30,'number')+'</div>'+check('启用渠道，允许新任务接单','enabled',c.enabled)+'<h4>测试素材与巡检</h4><div class="cm-fields">'+field('测试提示词','prompt',c.fixture?.prompt||'生成一张纯色背景的产品展示图')+field('视频时长 / 秒','duration',c.fixture?.duration||5,'number')+select('画面比例','ratio',['9:16','16:9','1:1'].map(x=>[x,x]),c.fixture?.ratio||'9:16')+'<label>参考图（视频协议支持；留空保留已有 '+Number(c.material_count||0)+' 张）<input name="materials" type="file" accept="image/png,image/jpeg" multiple></label>'+check('清除已有参考图','clear_materials',false)+field('轻量巡检间隔 / 秒','poll_seconds',c.poll_seconds||900,'number')+field('每日完整测试时间 / 北京时间小时','daily_hour',c.daily_hour??9,'number')+field('每日完整测试最多次数','daily_limit',c.daily_limit??1,'number')+field('单次预留费用 / 元','test_cost',c.test_cost??0,'number')+field('每日预留费用上限 / 元','daily_budget',c.daily_budget??0,'number')+'</div><p class="muted">费用由管理员按供应商价格预估，不是实扣账单。失败和结果未知也占用预算。完整测试核验产物，不代表用户已接收。</p>'+check('启用定时连接检测','monitor',c.monitor)+check('启用每日完整生成测试','daily_test',c.daily_test)+'<div class="actions"><button class="primary">保存为新版本</button><button type="button" id="cmCancel">取消</button></div></form>'+(c.id?'<details><summary>历史版本回滚</summary><div class="actions">'+(c.history||[]).filter(h=>h.version!==c.version).map(h=>'<button data-rollback="'+h.version+'">恢复 v'+h.version+' · '+esc(date(h.created))+'</button>').join('')+'</div><p class="muted">回滚创建新版本，仅影响新任务。历史任务继续使用当时配置。</p></details>':'');
      el('cmEditor').scrollIntoView({behavior:'smooth',block:'start'});
    }
    function editMap(m={}){el('cmMappingEditor').hidden=false;el('cmMappingEditor').innerHTML='<form id="cmMapForm" class="cm-form"><div class="cm-fields">'+select('功能类型','kind',[['image','图片生成'],['xiaole_video','视频生成']],m.kind)+field('前台请求标识','front',m.front)+field('后台展示说明','label',m.label)+select('主渠道','channel',data.items.map(c=>[c.id,c.name]),m.channel)+select('备用渠道（手动切换）','backup',[['','未设置'],...data.items.map(c=>[c.id,c.name])],m.backup)+'</div>'+check('启用该映射','enabled',m.enabled)+'<button class="primary">保存映射</button></form>'}
    document.querySelector('[data-module="managedChannels"]').addEventListener('click',async e=>{
      const b=e.target.closest('button');if(!b)return;
      try{
        if(b.id==='cmReload')return load();if(b.id==='cmNew')return edit();if(b.id==='cmCancel'){el('cmEditor').hidden=true;return}
        if(b.id==='cmMapNew')return editMap();if(b.id==='cmAllRuns')return renderRuns();
        if(b.dataset.edit)return edit(data.items.find(c=>c.id===b.dataset.edit));
        if(b.dataset.map!=null)return editMap(data.mappings[Number(b.dataset.map)]);
        if(b.dataset.runs){renderRuns(b.dataset.runs);el('cmRuns').scrollIntoView({behavior:'smooth'});return}
        if(b.dataset.test){b.disabled=true;await post('test',{id:b.dataset.id,kind:b.dataset.test});toast('测试已排队，可在最近记录查看');await load()}
        if(b.dataset.rollback){b.disabled=true;await post('rollback',{id:editing.id,target_version:Number(b.dataset.rollback),enabled:!!editing.enabled});el('cmEditor').hidden=true;await load()}
      }catch(err){toast(err.message)}finally{b.disabled=false}
    });
    document.querySelector('[data-module="managedChannels"]').addEventListener('submit',async e=>{
      e.preventDefault();const f=e.target,b=f.querySelector('button[type=submit],button.primary');if(b)b.disabled=true;
      try{
        const v=values(f);
        if(f.id==='cmForm'){
          const files=Array.from(f.elements.materials.files);if(files.length>5||files.reduce((n,x)=>n+x.size,0)>8*1024*1024)throw Error('最多5张参考图，总大小不超过8MB');
          const refs=await Promise.all(files.map(file=>new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file)})));
          v.id=editing.id;v.version=editing.version;v.fixture={prompt:v.prompt,duration:Number(v.duration),ratio:v.ratio};
          if(refs.length||v.clear_materials)v.fixture.reference_images=refs;
          delete v.materials;await post('save',v);el('cmEditor').hidden=true;
        }else if(f.id==='cmMapForm'){await post('mapping',v);el('cmMappingEditor').hidden=true}
        else if(f.id==='cmNoticeForm'){await post('notifications',v);el('cmNotifications').innerHTML=''}
        toast('已保存');await load();
      }catch(err){toast(err.message)}finally{if(b)b.disabled=false}
    });
    setInterval(()=>{if(env.active()&&!document.querySelector('#cmEditor:not([hidden]),#cmMappingEditor:not([hidden])'))load()},15000);
    return load;
  };
})();
