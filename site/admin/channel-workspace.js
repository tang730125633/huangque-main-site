(function(){
  window.initChannelWorkspace=function(env){
    const {el,esc}=env,C=window.ChannelCatalog;
    let data={},rows=[],tab='catalog',selected=null,returnFocus=null;
    const filters={category:'all',q:'',supplier:'',transport:'',status:'',history:false};
    const date=n=>n?new Date(n*1000).toLocaleString():'未采集';
    const table=(head,body)=>'<div class="cm-table-scroll"><table><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+body.join('')+'</tbody></table></div>';
    const button=(c,label='详情')=>'<button data-cm-detail="'+esc(c.uid)+'">'+label+'</button>';
    function actions(c){
      const action=(name,label)=>'<button data-cm-action="'+name+'" data-cm-uid="'+esc(c.uid)+'">'+label+'</button>';
      if(c.deleted)return '<div class="cm-row-actions">'+button(c)+action('restore','恢复')+'</div>';
      const addModel={openai:'openai_image',minimax:'minimax_h3',xai:'xai_video'}[c.key];
      const modelButton=addModel?'<button data-add-model="'+addModel+'" data-supplier="'+esc(c.name)+'">配置模型参数</button>':'';
      const edit=c.source==='managed'?'<button data-edit="'+esc(c.id)+'">编辑</button>':button(c,'编辑密钥');
      const toggle=c.source==='managed'||(c.scope&&c.accepts_new_jobs!==false)?action(c.enabled?'disable':'enable',c.enabled?'停用':'启用'):'<button disabled title="该内置服务尚未接入统一接单控制">启停未接入</button>';
      const remove=c.source==='managed'?action('delete','移入回收站'):'<span class="muted">内置线路不可删除</span>';
      return '<div class="cm-row-actions">'+button(c)+edit+modelButton+(c.source==='managed'?'<button data-parameters="'+esc(c.id)+'">参数</button>':'')+toggle+'<details><summary>更多</summary>'+remove+'</details></div>';
    }
    function showTab(name){tab=name;document.querySelectorAll('[data-cm-panel]').forEach(n=>n.hidden=n.dataset.cmPanel!==name);document.querySelectorAll('[data-cm-tab]').forEach(n=>{n.classList.toggle('active',n.dataset.cmTab===name);n.setAttribute('aria-pressed',String(n.dataset.cmTab===name))})}
    function list(){
      const visible=C.filter(rows,filters);
      el('cmCount').textContent=visible.length+' / '+rows.length+' 个渠道 · 历史或停用 '+rows.filter(c=>c.retired).length+' 个';
      el('cmCategories').innerHTML=C.categories.map(([key,label])=>'<button data-cm-category="'+key+'" aria-pressed="'+(filters.category===key)+'" class="'+(filters.category===key?'active':'')+'">'+label+' <small>'+C.filter(rows,{...filters,category:key}).length+'</small></button>').join('');
      el('cmList').innerHTML=visible.length?table(['渠道 / 供应商','功能分类','接单状态','最近证据','操作'],visible.map(c=>'<tr><td><b>'+esc(c.name)+'</b><small>'+esc(c.source==='managed'?c.supplier+' · '+c.model:'现有供应商线路')+'</small></td><td>'+c.categories.map(k=>esc(C.categories.find(x=>x[0]===k)[1])).join(' / ')+'</td><td>'+(c.deleted?'回收站':!c.enabled?(c.source==='legacy'&&c.scope&&c.accepts_new_jobs!==false?'范围内任务已停用':'已停用 / 历史'):c.source==='managed'?'已启用':'按功能接单')+(c.scope?'<small>控制范围：'+esc(c.scope)+'</small>':'')+'</td><td><span class="cm-evidence '+(c.attention?'warn':'')+'">'+esc(c.health)+'</span><small>'+esc(c.source==='managed'?'生成验证与鉴权分别记录':'线路证据不等于成品交付')+'</small></td><td>'+actions(c)+'</td></tr>')):'<div class="empty">没有符合条件的渠道，请调整分类或筛选。</div>';
    }
    function health(){
      el('cmHealth').innerHTML=table(['渠道','连接','鉴权','生成 / 交付','巡检计划','操作'],rows.filter(c=>!c.retired).map(c=>{
        const result=k=>C.checkLabel((c.checks||[]).find(r=>r.kind===k));
        return '<tr><td><b>'+esc(c.name)+'</b><small>'+esc(c.source==='legacy'?c.health:'配置 v'+c.version)+'</small></td><td>'+esc(result('connection'))+'</td><td>'+esc(result('auth'))+'</td><td>'+esc(result('full'))+'</td><td>'+esc(c.source==='legacy'?'按关联功能检查与验收':(c.monitor?'连接：'+date(c.schedule?.light_due):'连接巡检未启用')+'；'+(c.daily_test?'生成：'+date(c.schedule?.full_due):'每日生成未启用'))+'</td><td>'+button(c,'检查与测试')+'</td></tr>';
      }));
    }
    function render(next){
      data=next;rows=C.catalog(data,env.legacy());
      const suppliers=[...new Set(rows.map(c=>c.supplier))].sort();
      el('cmSupplier').innerHTML='<option value="">全部供应商</option>'+suppliers.map(s=>'<option value="'+esc(s)+'">'+esc(s)+'</option>').join('');
      el('cmSupplier').value=filters.supplier;list();health();showTab(tab);
      if(selected?.source==='managed'&&!el('cmDrawer').hidden&&el('cmEditor').hidden&&el('cmMappingEditor').hidden)open(selected.uid,true);
    }
    let closeGuard=null;
    function close(){if(closeGuard&&!closeGuard())return;if(!el('cmDrawer').hidden){closeGuard=null;env.closeLegacy();el('cmEditor').innerHTML='';el('cmMappingEditor').innerHTML='';el('cmDrawer').hidden=true;document.body.classList.remove('cm-drawer-open');selected=null;if(returnFocus?.isConnected)returnFocus.focus()}}
    function shell(title){returnFocus=document.activeElement;el('cmDrawer').hidden=false;document.body.classList.add('cm-drawer-open');el('cmDrawerTitle').textContent=title;el('cmDetail').hidden=false;el('cmEditor').hidden=true;el('cmMappingEditor').hidden=true;el('cmDrawerClose').focus()}
    function open(uid,refresh=false){
      const c=rows.find(r=>r.uid===uid);if(!c)return;
      if(!refresh){env.closeLegacy();shell(c.name)}selected=c;
      let body='<p class="muted">'+esc(c.categories.map(k=>C.categories.find(x=>x[0]===k)[1]).join(' / '))+' · '+esc(c.source==='managed'?'可配置渠道':'现有供应商线路')+'</p>';
      body+='<h3>功能与调用关系</h3><div class="cm-flow">'+esc((c.features||[]).join('、')||'尚未配置功能映射')+' → '+esc(c.name)+' → '+esc(c.model||'模型按功能配置')+'</div>';
      if(c.deleted){
        body+='<p>已移入回收站，保留历史配置与调用记录。恢复后仍为停用状态。</p><p>'+esc(c._lifecycle?.reason||'')+'</p><button data-cm-action="restore" data-cm-uid="'+esc(c.uid)+'">恢复渠道</button>';
      }else if(c.source==='managed'){
        body+='<p>当前版本 v'+c.version+' · '+esc(c.enabled?'接单已启用':'已停用')+' · '+esc(c.health)+'</p><p class="muted">API：'+esc(c.base_url)+'<br>网络：'+esc(c.proxy||'直连')+'<br>参考图：'+Number(c.material_count||0)+' 张</p><div class="actions"><button data-edit="'+esc(c.id)+'">连接配置与巡检</button><button data-cm-mapping="'+esc(c.id)+'">配置功能映射</button></div>';
        body+='<h3>测试与素材</h3><p class="muted">连接可达、鉴权通过、生成成品分别验证。完整测试可能产生供应商费用；不代表用户已接收。</p><div class="actions">'+['connection','auth','full'].map((kind,i)=>'<button data-test="'+kind+'" data-id="'+esc(c.id)+'">'+['连接检测','鉴权检测','完整生成测试'][i]+'</button>').join('')+'<button data-cm-refresh="1">刷新结果</button></div>';
        body+='<p>测试提示词：'+esc(c.fixture?.prompt||'未准备')+'<br>素材：'+(data.adapters?.[c.adapter]?.references?'可使用参考图，已准备 '+Number(c.material_count||0)+' 张':'当前协议仅支持文本输入')+'</p><h3>最近调用与故障</h3>';
        const runs=(data.runs||[]).filter(r=>r.channel===c.id).slice(0,10);
        body+=runs.map(r=>'<div class="task-proof-line"><b>'+esc(date(r.started)+' · v'+r.version+' · '+r.kind+' · '+r.state)+'</b><span>'+esc(r.detail||'无额外说明')+'</span><span>供应商工单：'+esc(r.provider_id||'未采集')+'</span>'+(r.job_id?'<button data-cm-job="'+esc(r.job_id)+'">查看任务 #'+esc(r.job_id)+'</button>':'')+'</div>').join('')||'<p class="muted">暂无调用记录</p>';
      }else{
        body+='<p class="muted">现有线路沿用各功能的调度与配置。下方可管理已接入的密钥号池；服务器环境配置不能在此直接改写。通用模型映射仅适用于已接入的托管协议。</p><h3>连接与密钥</h3><div id="cmLegacyEditorHost"></div><div id="cmLegacyKeys"></div><h3>用户旅程与完整测试</h3><div id="cmLegacyJourneys"></div>';
      }
      el('cmDetail').innerHTML=body;
      if(c.source==='legacy')env.detail(c);
    }
    function editor(title){closeGuard=null;el('cmEditor').oninput=null;el('cmEditor').onchange=null;el('cmEditor').onclick=null;if(el('cmDrawer').hidden)shell(title);el('cmDrawerTitle').textContent=title;el('cmDetail').hidden=true}
    const root=document.querySelector('[data-module="managedChannels"]');
    root.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
      if(b.dataset.cmTab)showTab(b.dataset.cmTab);
      if(b.dataset.cmCategory){filters.category=b.dataset.cmCategory;list()}
      if(b.dataset.cmDetail)open(b.dataset.cmDetail);
      if(b.dataset.cmAction)env.lifecycle(rows.find(c=>c.uid===b.dataset.cmUid),b.dataset.cmAction);
      if(b.dataset.cmRefresh)env.refresh();
      if(b.id==='cmDrawerClose')close();
      if(b.dataset.cmMapping)env.mapping(data.mappings.find(m=>m.channel===b.dataset.cmMapping)||{channel:b.dataset.cmMapping,kind:data.adapters[data.items.find(c=>c.id===b.dataset.cmMapping).adapter].kind});
      if(b.dataset.cmJob){close();env.task(b.dataset.cmJob)}
      if(b.dataset.cmJourney){close();env.journey(b.dataset.cmJourney)}
    });
    [['cmSearch','q','input'],['cmSupplier','supplier','change'],['cmTransport','transport','change'],['cmState','status','change'],['cmHistory','history','change']].forEach(([id,key,event])=>el(id).addEventListener(event,()=>{filters[key]=key==='history'?el(id).checked:el(id).value;list()}));
    el('cmDrawer').addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();close()}if(e.key==='Tab'){const nodes=Array.from(el('cmDrawer').querySelectorAll('button,input,select,textarea,a[href]')).filter(n=>!n.disabled&&n.getClientRects().length);if(!nodes.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}});
    return {render,open,close,editor,showTab,setCloseGuard:guard=>{closeGuard=guard}};
  };
})();
