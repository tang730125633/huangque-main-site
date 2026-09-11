(function(){
  window.initChannelWorkspace=function(env){
    const {el,esc}=env,C=window.ChannelCatalog,api=env.api;
    let data={},rows=[],tab='catalog',selected=null,returnFocus=null;
    let layoutLoading=false;
    const LAYOUT_NAMES={video:{grok:'果肉视频生成',talking:'数字化 IP',cinematic:'电影化身',tryon:'换装换背景',minimax:'麦克视频',micro:'Seedance 视频',sora:'Sora 2',omni:'Omni 视频'},
      image:{gpt:'黄雀引擎 2',banana:'纳米香蕉',seedream:'黄雀引擎 1',xiaole:'果肉生图',zelong2:'泽龙2生图'}};
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
    function showTab(name){tab=name;document.querySelectorAll('[data-cm-panel]').forEach(n=>n.hidden=n.dataset.cmPanel!==name);document.querySelectorAll('[data-cm-tab]').forEach(n=>{n.classList.toggle('active',n.dataset.cmTab===name);n.setAttribute('aria-pressed',String(n.dataset.cmTab===name))});if(name==='layout')loadLayout()}
    function renderLayout(state){
      const host=el('cmLayout');if(!host)return;
      const layout=state||{};
      const page=key=>{
        const cfg=layout[key]||{},order=cfg.order||[],def=cfg.default||(order[0]||'');
        return '<div class="cm-layout-page"><h4>'+(key==='video'?'视频页':'图片页')+'</h4><p class="muted">用上移/下移调整渠道顺序，单选默认渠道。保存后用户页面 15 秒内自动应用；可用性仍由功能开关控制。</p><div class="cm-layout-list">'+order.map((k,i)=>'<div class="cm-layout-row" data-layout-row="'+key+':'+k+'"><button type="button" data-layout-move="'+key+':'+k+':-1" '+(i===0?'disabled':'')+' aria-label="上移 '+esc(LAYOUT_NAMES[key]?.[k]||k)+'">↑</button><button type="button" data-layout-move="'+key+':'+k+':1" '+(i===order.length-1?'disabled':'')+' aria-label="下移 '+esc(LAYOUT_NAMES[key]?.[k]||k)+'">↓</button><span>'+esc(LAYOUT_NAMES[key]?.[k]||k)+'</span><label><input type="radio" name="layoutDefault_'+key+'" value="'+esc(k)+'" '+(def===k?'checked':'')+'> 默认</label></div>').join('')+'</div></div>';
      };
      host.innerHTML='<div class="section-head"><h3>前台布局（渠道顺序与默认）</h3><button type="button" id="cmLayoutSave" class="primary">保存布局</button></div>'+page('video')+page('image')+'<p id="cmLayoutStatus" role="status"></p>';
      host.querySelector('#cmLayoutSave').onclick=async e=>{
        const btn=e.currentTarget,status=el('cmLayoutStatus');btn.disabled=true;status.textContent='';
        const collect=key=>{
          const rows=[...host.querySelectorAll('[data-layout-row]')].filter(r=>r.dataset.layoutRow.startsWith(key+':')).map(r=>r.dataset.layoutRow.slice(key.length+1));
          const def=host.querySelector('input[name="layoutDefault_'+key+'"]:checked')?.value;
          return {order:rows,default:def};
        };
        try{
          await api('/api/admin/channel-manager/layout-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({layout:{video:collect('video'),image:collect('image')}})});
          status.textContent='已保存；用户页面将在 15 秒内应用新布局。';toast('前台布局已保存');
        }catch(error){status.textContent=error.message}
        finally{btn.disabled=false}
      };
    }
    async function loadLayout(){
      if(layoutLoading)return;layoutLoading=true;
      try{
        const result=await api('/api/admin/channel-manager/layout-state',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        renderLayout(result.layout||{});
      }catch(error){const host=el('cmLayout');if(host)host.innerHTML='<p class="muted">'+esc(error.message)+'</p>'}
      finally{layoutLoading=false}
    }
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
      if(b.dataset.layoutMove){
        const parts=b.dataset.layoutMove.split(':'),page=parts[0],key=parts[1],dir=Number(parts[2]);
        const row=el('cmLayout').querySelector('[data-layout-row="'+page+':'+key+'"]');
        if(!row)return;
        const parent=row.parentElement,sibling=dir<0?row.previousElementSibling:row.nextElementSibling&&row.nextElementSibling.nextElementSibling;
        if(dir<0&&!row.previousElementSibling)return;
        if(dir>0&&!row.nextElementSibling)return;
        parent.insertBefore(row,sibling);
        [...parent.querySelectorAll('[data-layout-row]')].forEach((r,i)=>{
          r.querySelector('[data-layout-move$=":-1"]').disabled=i===0;
          r.querySelector('[data-layout-move$=":1"]').disabled=i===parent.children.length-1;
        });
        return;
      }
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
