(function(){
  window.initChannelWorkspace=function(env){
    const {el,esc,toast}=env,C=window.ChannelCatalog,api=env.api;
    const priorityRequest=window.ChannelRequest?window.ChannelRequest.createClient(api,{writeTimeout:15000}):null;
    let data={},rows=[],tab='matrix',matrixPage='image',matrixShowHidden=false,selected=null,returnFocus=null,matrixExpanded=null,draggedPriorityChannel='';
    const matrixGroupSelection={};
    const priorityDrafts={};
    const latencyResults={},latencyChoices={},latencyBusy=new Set(),priorityErrors={};
    const latencyIdentities={};
    let priorityBusy=false,priorityUncertain=false;
    const serverReplacementTemplates={};
    let layoutLoading=false;
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
    function showTab(name){tab=name;document.querySelectorAll('[data-cm-panel]').forEach(n=>n.hidden=n.dataset.cmPanel!==name);document.querySelectorAll('.cm-admin-menu[open]').forEach(n=>n.removeAttribute('open'));if(name==='layout')loadLayout()}
    function renderLayout(state,catalog,effectiveState){
      const host=el('cmLayout');if(!host)return;
      const layout=state||{},effective=effectiveState||layout,directory=catalog||{};
      const page=key=>{
        const cfg=layout[key]||{},order=cfg.order||[],def=effective[key]?.default||(cfg.default||order[0]||'');
        const entries=Array.isArray(directory[key])?directory[key]:order.map(k=>({key:k,label:k,visible:true,defaultable:true,reason:'等待状态目录'}));
        const byKey=Object.fromEntries(entries.map(item=>[item.key,item]));
        const items=order.map((k,i)=>{
          const meta=byKey[k]||{key:k,label:k,visible:false,defaultable:false,reason:'入口目录中不存在'};
          const unavailable=meta.visible===false||meta.defaultable===false;
          const models=(meta.models||[]).join(' · ');
          return '<div class="cm-layout-row '+(unavailable?'unavailable':'')+'" data-layout-row="'+key+':'+k+'"><button type="button" data-layout-move="'+key+':'+k+':-1" '+(i===0?'disabled':'')+' aria-label="上移 '+esc(meta.label)+'">↑</button><button type="button" data-layout-move="'+key+':'+k+':1" '+(i===order.length-1?'disabled':'')+' aria-label="下移 '+esc(meta.label)+'">↓</button><span><b>'+esc(meta.label)+'</b><small>'+esc(models||meta.reason||'')+'</small></span><em class="cm-layout-badge '+(unavailable?'off':'on')+'">'+(meta.visible===false?'用户页隐藏':meta.defaultable===false?'暂停接单':'用户页显示')+'</em><label><input type="radio" name="layoutDefault_'+key+'" value="'+esc(k)+'" '+(def===k?'checked':'')+' '+(meta.defaultable===false||meta.visible===false?'disabled':'')+'> 默认</label></div>';
        }).join('');
        const preview=order.map(k=>byKey[k]).filter(item=>item&&item.visible!==false).map(item=>'<span class="cm-layout-preview-item">'+esc(item.label)+(item.key===def?' · 默认':'')+'</span>').join('');
        const fallback=cfg.default&&cfg.default!==def?'<p class="cm-layout-warning">已配置的默认渠道当前不可用，用户页会自动改用「'+esc(byKey[def]?.label||def||'无可用渠道')+'」。</p>':'';
        return '<div class="cm-layout-page"><h4>'+(key==='video'?'视频页':'图片页')+'</h4><p class="muted">上移/下移决定用户页顺序；只有当前可接单的渠道能设为默认。保存后约 15 秒生效。</p>'+fallback+'<div class="cm-layout-list">'+(items||'<div class="empty cm-layout-empty">当前页面没有可排列的功能。</div>')+'</div><div class="cm-layout-preview"><b>当前用户页预览</b><div>'+(preview||'<span class="muted">当前没有可显示渠道</span>')+'</div></div></div>';
      };
      host.innerHTML='<div class="section-head"><h3>前台布局（渠道顺序与默认）</h3><button type="button" id="cmLayoutSave" class="primary">保存布局</button></div>'+page('video')+page('image')+'<p id="cmLayoutStatus" role="status"></p>';
      const save=host.querySelector('#cmLayoutSave'),hasCompleteLayout=['video','image'].every(key=>(layout[key]?.order||[]).length&&(!Array.isArray(directory[key])||directory[key].some(item=>item.visible!==false&&item.defaultable!==false)));
      save.disabled=!hasCompleteLayout;if(!hasCompleteLayout)save.title='视频页和图片页都需要至少一个可接单的默认渠道';
      save.onclick=async e=>{
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
    function renderLayoutError(error){
      const host=el('cmLayout');if(!host)return;
      host.innerHTML='<div class="empty cm-layout-error" role="alert"><p>前台布局读取失败：'+esc(error?.message||'未知错误')+'</p><button type="button" id="cmLayoutRetry">重新加载</button></div>';
      host.querySelector('#cmLayoutRetry').onclick=loadLayout;
    }
    async function loadLayout(){
      if(layoutLoading)return;layoutLoading=true;
      const host=el('cmLayout');if(host)host.innerHTML='<p class="muted" role="status">正在读取前台布局…</p>';
      try{
        const result=await api('/api/admin/channel-manager/layout-state',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        const layout=result?.layout||(result&&(result.video||result.image)?result:null);
        if(!layout)throw Error('布局接口未返回有效配置');
        renderLayout(layout,result?.entries,result?.effective_layout);
      }catch(error){renderLayoutError(error)}
      finally{layoutLoading=false}
    }
    const matrixPageMeta=[['image','生图'],['video','生视频'],['avatar','数字人'],['audio','音频与配音'],['text','文本与助手'],['collect','采集与解析'],['process','视频处理'],['other','系统依赖']];
    const matrixPageGroups=[
      {key:'creation',label:'内容创作',pages:['image','video'],unit:'模型'},
      {key:'people',label:'人物与声音',pages:['avatar','audio'],unit:'模型'},
      {key:'tools',label:'智能工具',pages:['text','collect','process'],unit:'服务'},
      {key:'infrastructure',label:'基础服务',pages:['other'],unit:'服务'},
    ];
    function catalogProof(c){
      const state=c.evidence?.verification_state||c.evidence?.state;
      if(state==='ok')return {state:'ok',label:c.evidence.label||c.health||'通过'};
      if(['pending','neutral'].includes(state))return {state:'pending',label:c.evidence.label||c.health||'验证中'};
      if(['attention','stale','fail','warn'].includes(state))return {state:state==='stale'?'stale':'attention',label:c.evidence.label||c.health||'验证异常'};
      return {state:'unverified',label:c.health||'未验证'};
    }
    function partToProof(p){
      if(!p)return {state:'unverified',label:'未验证'};
      const state={ok:'ok',failed:'fail',unknown:'warn',running:'pending',queued:'pending',blocked:'warn',expired:'stale',missing:'unverified',unattributed:'warn','stale-version':'unverified',attention:'warn',neutral:'pending',off:'muted'}[p.state]||'unverified';
      return {state,label:p.label,checked_at:p.time,version:p.version};
    }
    function catalogRoute(c){
      const baseUrls=unique([c.base_url,c.env_base_url,c.pool_base_url,c.image_primary_base_url,c.image_fallback_base_url]);
      const management={kind:c.source==='managed'?'managed_channel':(c.pool_provider?'provider_pool':'server_env'),uid:c.uid,provider:c.pool_provider||''};
      const v=c._verification||{},cfg=c._config||{};
      const auth=v.parts?.auth?partToProof(v.parts.auth):catalogProof(c);
      const full=v.parts?.full?partToProof(v.parts.full):{state:'unverified',label:'请进入测试与健康查看完整证据'};
      const connection=v.parts?.connection?partToProof(v.parts.connection):{state:'unverified',label:'未验证'};
      return {id:c.uid,name:c.name,supplier:c.supplier,connection_type:c.connection_type||'unknown',base_host:c.env_base_host||c.pool_base_host||'',base_urls:baseUrls,model:c.model||'',credential_source:c.source==='managed'?'渠道密钥库':(c.pool_provider?'后台密钥号池 / 服务器兼容线路':'服务器环境变量'),configured:c.source==='managed'?(cfg.complete!==false):(c.configured!==false),key:cfg.key||'unknown',enabled:!!c.enabled,connection,auth,full,production:c._production||null,source:c.source,management};
    }
    function servicePage(key,label){
      const services=rows.filter(c=>c.categories?.includes(key));
      const products=services.map(c=>{
        const route=catalogRoute(c),admitted=!!c.enabled&&c.configured!==false;
        return {key:c.uid,label:c.name,description:(c.features||[]).join(' / ')||c.category||'底层服务',visible:!c.retired,visibility_reason:c.retired?'已停用或历史服务':'',admitted,attention:!!c.attention,models:[{key:c.uid,label:c.model&&c.model!=='按功能配置'?c.model:'服务配置',actual_model:c.model||'按前端功能选择',capabilities:c.features||[],visible:!c.retired,admitted,attention:!!c.attention,warnings:c.attention?[c.health||'需要检查']:[],routes:[{operation_id:'service:'+c.uid,capability:(c.features||[]).join(' / ')||label,control_state:c.source==='managed'?'managed':'legacy',primary:route,backup:null,candidate:null,admitted,reason:admitted?'':'服务未配置或未启用'}]}]};
      });
      return {page:key,label,read_only:false,precision:'service',summary:{page:key,products:products.length,models:products.length,visible_products:products.filter(x=>x.visible).length,admitted_models:products.filter(x=>x.admitted).length,attention_models:products.filter(x=>x.attention).length},products};
    }
    function matrixPages(){
      const root=data.frontend_matrix||{},exact=Array.isArray(root.pages)&&root.pages.length?root.pages:[root];
      const exactByPage=Object.fromEntries(exact.filter(Boolean).map(page=>[page.page,page]));
      return matrixPageMeta.map(([key,label])=>exactByPage[key]?{...exactByPage[key],label}:servicePage(key,label));
    }
    const matrixGroupForPage=page=>matrixPageGroups.find(group=>group.pages.includes(page))||matrixPageGroups[0];
    const transportName=value=>({official:'官方直连',relay:'中转 API',unknown:'未标注'}[value]||'未标注');
    const unique=values=>[...new Set(values.filter(Boolean))];
    function modelLegs(model,roles=['primary','backup','candidate']){
      return (model.routes||[]).flatMap(route=>roles.map(role=>[role,route[role]]))
        .filter(([,item])=>item)
        .filter(([role,item],index,all)=>all.findIndex(([otherRole,other])=>otherRole===role&&other?.id===item.id)===index);
    }
    const compactValue=(values,empty='未配置')=>{
      const items=unique(values);return items.length>1?'按能力分流':items[0]||empty;
    };
    function modelStatus(product,model){
      if(!product.visible||model.visible===false)return {label:'前台隐藏',state:'muted'};
      if(!model.admitted){
        return {label:String(model.reason||'').includes('前台当前未开放')?'暂未开放':'不可接单',state:'bad'};
      }
      if((model.routes||[]).some(route=>route.admitted===false)){
        return {label:'部分能力不可接单',state:'warn'};
      }
      const primary=modelLegs(model,['primary']).map(([,item])=>item);
      const proofs=primary.flatMap(item=>[item.auth,item.full]);
      const issue=proofs.find(proof=>proof&&!['ok','unverified','pending'].includes(proof.state));
      if(issue)return {label:issue.label||'验证异常',state:'warn'};
      const pending=proofs.find(proof=>proof?.state==='pending');
      if(pending)return {label:pending.label||'验证中',state:'neutral'};
      if(!primary.length||proofs.some(proof=>!proof||proof.state==='unverified'))return {label:'待验证',state:'neutral'};
      return {label:'可接单',state:'ok'};
    }
    const modelNeedsAction=(product,model)=>['bad','warn'].includes(modelStatus(product,model).state);
    function modelManagers(model){
      const seen=new Set();
      return modelLegs(model).map(([,item])=>item).filter(item=>{
        const management=item?.management,identity=management?.uid+'|'+management?.kind;if(!management?.uid||seen.has(identity))return false;seen.add(identity);return true;
      });
    }
    const mappingForOperation=operationId=>(data.operation_mappings||[]).find(item=>item.operation_id===operationId)||(data.operations||[]).find(item=>item.operation_id===operationId)?.mapping||null;
    const mappingChannels=mapping=>{
      if(Array.isArray(mapping?.channels))return unique(mapping.channels.map(String));
      return unique([mapping?.channel,mapping?.backup]);
    };
    function priorityDraft(route){
      const operationId=route?.operation_id||'';
      if(!operationId)return null;
      if(!priorityDrafts[operationId]){
        const mapping=mappingForOperation(operationId);
        priorityDrafts[operationId]={operation_id:operationId,state:mapping?.state||'shadow',revision:Number(mapping?.revision||0),channels:mappingChannels(mapping)};
        const kind=(data.operations||[]).find(o=>o.operation_id===operationId)?.channel_kind;
        // 候选 = 同类型（kind）的未删除托管渠道。
        // 旧写法要求 item.model === route.primary.model（原厂线路的模型），于是
        // 「托管渠道用的是别家模型实现同一功能」这个本来的场景全被挡在门外；
        // 视频侧更是连名字都对不上（grok-imagine-video vs Grok Image Video），
        // 导致除生图外所有分类的候选列表恒为空、无从拖拽。
        // 兼容性改由「候选彼此同模型/协议」（与后端 _same_parameter_contract 同口径）
        // 在发布时收敛，见 publishPriority。
        for(const item of data.items||[]){
          if(data.adapters?.[item.adapter]?.kind===kind&&!item._lifecycle?.deleted&&!priorityDrafts[operationId].channels.includes(item.id))priorityDrafts[operationId].channels.push(item.id);
        }
      }
      return priorityDrafts[operationId];
    }
    function compatiblePriorityChannels(operationId){
      const operation=(data.operations||[]).find(item=>item.operation_id===operationId);
      const mapping=mappingForOperation(operationId),kind=operation?.channel_kind||mapping?.kind||'';
      if(!operation||!kind)return [];
      const page=matrixPages().find(p=>p.page===matrixExpanded?.page);
      const model=page?.products?.find(p=>p.key===matrixExpanded?.product)?.models?.find(m=>m.key===matrixExpanded?.model);
      const primary=(data.items||[]).find(item=>item.id===mappingChannels(mapping)[0]);
      const modelName=model?.actual_model||primary?.model;
      return (data.items||[]).filter(item=>!item._lifecycle?.deleted&&data.adapters?.[item.adapter]?.kind===kind&&modelName&&item.model===modelName&&(!primary||item.adapter===primary.adapter));
    }
    function refreshPriority(){
      const host=el('cmModelPriority');
      if(host&&matrixExpanded){
        const page=matrixPages().find(p=>p.page===matrixExpanded.page);
        const product=page?.products?.find(p=>p.key===matrixExpanded.product);
        const model=product?.models?.find(m=>m.key===matrixExpanded.model);
        if(model){
          host.innerHTML=priorityEditor(product,model);
          if(priorityUncertain)host.querySelectorAll('button:not([data-cm-priority-close]),select').forEach(node=>{node.disabled=true});
        }
      }else renderMatrix();
    }
    async function publishPriority(operationId,automatic=false){
      const draft=priorityDrafts[operationId];if(!draft||priorityBusy||priorityUncertain)return;
      const mapping=mappingForOperation(operationId);
      let movedHint='';
      if(automatic){
        // 「拖至首位即选定主渠道」。这里不再拿原厂线路的模型做基准（那会把
        // 「从原厂切到托管渠道」这个本来的目的堵死），改成与服务端一致的
        // 口径：主渠道必须可用，候补必须与主渠道同模型/协议/参数契约。
        const published=mappingChannels(mapping);
        draft.channels=draft.channels.filter((id,index)=>index===0||published.includes(id));
        const find=id=>(data.items||[]).find(c=>c.id===id)||null;
        // 可用性只取决于「已启用、未删除」；类型（kind）由草稿构造保证（候选只来自同 kind）。
        const usable=c=>!!c&&!!c.enabled&&!c._lifecycle?.deleted;
        const primary=find(draft.channels[0]);
        const routeForLegacy=matrixPages().flatMap(p=>p.products||[]).flatMap(p=>p.models||[]).flatMap(m=>m.routes||[]).find(r=>r.operation_id===operationId);
        const legacyModel=String(routeForLegacy?.primary?.model||'');
        if(!usable(primary)){
          delete priorityDrafts[operationId];refreshPriority();
          toast('未应用：主渠道必须是已启用、同类型的托管渠道，原生产顺序不变。');return;
        }
        const kept=[primary],moved=[];
        draft.channels.slice(1).forEach(id=>{
          const c=find(id);
          if(usable(c)&&c.model===primary.model&&c.adapter===primary.adapter)kept.push(c);else moved.push(id);
        });
        // 跨模型接管不静默：先把「换成哪家的模型」说清楚，确认了才发。
        if(legacyModel&&primary.model&&primary.model!==legacyModel){
          const ok=typeof confirm==='function'?confirm('这条功能的实现模型将从「'+legacyModel+'」换成「'+primary.model+'」。\n换的是线路供应商的模型，用户看到的生成结果可能不同。\n确认切换？'):true;
          if(!ok){delete priorityDrafts[operationId];refreshPriority();toast('已取消：未切换模型，原生产顺序不变。');return}
        }
        draft.channels=kept.map(item=>item.id);
        draft.state='managed';
        if(moved.length)movedHint='已移出 '+moved.length+' 条与主渠道不同模型/协议的候补；';
      }
      priorityBusy=true;
      delete priorityErrors[operationId];
      const editor=root.querySelector('[data-cm-priority-editor="'+operationId+'"]');
      if(editor)editor.inert=true;
      const status=root.querySelector('[data-cm-priority-status="'+operationId+'"]');
      if(status)status.textContent='正在保存，尚未确认生效…';
      try{
        const body={operation_id:operationId,state:draft.state,channels:[...draft.channels],expected_revision:draft.revision};
        const response=priorityRequest?await priorityRequest.post('/api/admin/channel-manager/operation-mapping',body):await api('/api/admin/channel-manager/operation-mapping',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if(response?.ok===false)throw Error(response.detail||response.error||'发布失败');
        delete priorityDrafts[operationId];
        if(await env.refresh()===false)throw Error('服务端顺序读取失败');
        const saved=mappingForOperation(operationId);
        if(saved?.state!==draft.state||Number(saved?.revision)<=draft.revision||JSON.stringify(mappingChannels(saved))!==JSON.stringify(draft.channels))throw Error('发布结果待核对，请刷新后确认；不要重复提交。');
        toast((saved.state==='managed'?'顺序已生效：新任务按此优先级接单。':'顺序已保存，当前未接管生产。')+movedHint);
      }catch(error){
        priorityErrors[operationId]={message:'未应用或结果待核对：'+error.message,channel:draft.channels[0]};
        // A network failure may happen after commit. Read back; never claim rollback.
        delete priorityDrafts[operationId];
        toast('未确认生效：'+error.message+' 请刷新核对服务端顺序。');
        try{if(await env.refresh()===false)throw Error('读取失败')}catch(_){priorityUncertain=true;if(status)status.textContent='结果未知，请刷新核对后再操作。';return}
        const recovered=mappingForOperation(operationId);
        if(Number(recovered?.revision)>draft.revision&&recovered.state===draft.state&&JSON.stringify(mappingChannels(recovered))===JSON.stringify(draft.channels)){
          delete priorityErrors[operationId];
          toast(recovered.state==='managed'?'响应中断，但已读回确认顺序生效。':'响应中断，但已读回确认草稿状态已发布。');
        }
      }finally{priorityBusy=false;if(editor)editor.inert=false;refreshPriority()}
    }
    function movePriority(operationId,channelId,targetId,direction){
      const draft=priorityDrafts[operationId];if(!draft)return;
      const from=draft.channels.indexOf(channelId);if(from<0)return;
      if(!targetId){
        const to=from+Number(direction||0);if(to<0||to>=draft.channels.length||to===from)return;
        [draft.channels[from],draft.channels[to]]=[draft.channels[to],draft.channels[from]];return;
      }
      if(targetId===channelId)return;
      const [item]=draft.channels.splice(from,1),to=draft.channels.indexOf(targetId);
      if(to<0){draft.channels.splice(from,0,item);return}draft.channels.splice(to,0,item);
    }
    function recentFailoverEvidence(operationId,byId){
      const run=(data.runs||[]).find(item=>item.operation_id===operationId&&(item.execution_snapshot?.attempts||[]).length);
      if(!run)return '';
      const snapshot=run.execution_snapshot||{},attempts=snapshot.attempts||[];
      const steps=attempts.map(item=>{
        const channel=byId[item.channel];
        return '<li><b>'+esc(channel?.name||item.channel)+'</b><span>未受理，已安全切换</span>'+(item.detail?'<small>'+esc(item.detail)+'</small>':'')+'</li>';
      });
      const current=byId[run.channel]||byId[snapshot.id];
      if(current)steps.push('<li class="current"><b>'+esc(current.name)+'</b><span>'+esc(run.state==='passed'?'生成成功':'当前尝试 · '+(run.state||'处理中'))+'</span></li>');
      return '<aside class="cm-priority-evidence"><div><strong>最近安全切换</strong><small>任务 #'+esc(run.job_id||run.id||'—')+' · 映射 r'+esc(run.mapping_revision||snapshot.mapping_revision||'—')+'</small></div><ol>'+steps.join('')+'</ol></aside>';
    }
    // 「立即完整测试」：后端 start_test 已支持 kind='full'，但界面一直没有入口。
    // 没有它，渠道的完整生成测试只能靠每日定时任务（daily_test 默认是关的），
    // 一旦超过 24 小时，该渠道就会被发布门槛拦下（主渠道 require_ready）。
    let fullTestBusy=new Set(),fullTestNote={};
    function fullTestControls(id){
      if(!id||String(id).indexOf(':')>=0)return '';   // 只对托管渠道（有真实渠道 id）
      const busy=fullTestBusy.has(id);
      return '<span class="cm-fulltest"><button type="button" class="mini" data-cm-fulltest="'+esc(id)+'" '+(busy?'disabled':'')+'>'
        +(busy?'完整测试中…':'立即完整测试')+'</button><small role="status">'+esc(fullTestNote[id]||'')+'</small></span>';
    }
    // 触发一次完整生成测试（真调供应商、真花钱），然后轮询到终态。
    // 结果落 routing.runs(kind='full')，就是发布门槛要读的那条证据。
    async function runFullTest(id){
      if(!id||fullTestBusy.has(id))return;
      if(typeof confirm==='function'&&!confirm('对渠道 '+id+' 执行一次完整生成测试？\n\n会真实调用供应商生成一次（产生费用），结果 24 小时内有效。\n失败与结果未知同样占用当日测试预算。'))return;
      fullTestBusy.add(id);fullTestNote[id]='已排队，正在生成…';refreshPriority();
      try{
        const started=await api('/api/admin/channel-manager/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,kind:'full'})});
        fullTestNote[id]='已排队 '+String(started?.run_id||'').slice(0,8)+'，等待结果…';refreshPriority();
        const deadline=Date.now()+300000;
        while(Date.now()<deadline){
          await new Promise(resolve=>setTimeout(resolve,6000));
          try{if(await env.refresh()===false)break}catch(_){fullTestNote[id]='读取结果失败，请刷新核对';break}
          const item=(data.items||[]).find(row=>row.id===id)||{};
          const health=String(item.health||'');
          // health 的终态：成品核验通过 / 异常 / 结果未知；中间态是 检测中、待检测
          if(health&&health!=='检测中'&&health!=='待检测'){fullTestNote[id]=health;break}
          fullTestNote[id]='生成中…'+(health?('（'+health+'）'):'');refreshPriority();
        }
        if(!fullTestNote[id]||/等待结果|生成中/.test(String(fullTestNote[id])))fullTestNote[id]='仍未返回，请刷新核对';
        toast('完整测试'+(fullTestNote[id]==='成品核验通过'?'通过：该渠道现在可以发布为托管。':'结果：'+fullTestNote[id]));
      }catch(e){
        fullTestNote[id]='触发失败：'+String((e&&e.message)||e).slice(0,60);
        toast('完整测试未能触发：'+fullTestNote[id]);
      }finally{fullTestBusy.delete(id);refreshPriority()}
    }
    function latencyControls(uid){
      if(!uid)return '';
      const result=latencyResults[uid];
      const choices=(latencyChoices[uid]||[]).map(source=>'<button type="button" data-cm-latency="'+esc(uid)+'" data-cm-latency-source="'+esc(source)+'">'+esc(({env:'环境配置',pool:'号池',image_primary:'生图主线路',image_fallback:'生图备用',video:'视频线路'})[source]||source)+'</button>').join('');
      return '<span class="cm-latency"><button type="button" data-cm-latency="'+esc(uid)+'" '+(latencyBusy.has(uid)?'disabled':'')+'>'+(latencyBusy.has(uid)?'检测中…':'检测延迟')+'</button><small role="status">'+esc(result||'')+'</small>'+choices+'</span>';
    }
    function latencyIdentity(uid){
      if(uid.startsWith('managed:')){
        const item=(data.items||[]).find(c=>c.id===uid.slice(8));
        return item?JSON.stringify([item.version,item.base_url]):null;
      }
      const item=rows.find(c=>c.uid===uid);
      return item?JSON.stringify(['base_url','env_base_url','pool_base_url','image_primary_base_url','image_fallback_base_url','video_base_url'].map(key=>item[key]||'')):null;
    }
    function invalidateLatency(){
      for(const uid of Object.keys(latencyIdentities))if(latencyIdentities[uid]!==latencyIdentity(uid)){
        delete latencyResults[uid];delete latencyChoices[uid];delete latencyIdentities[uid];
      }
    }
    async function detectLatency(uid,source){
      if(latencyBusy.has(uid))return;
      const identity=latencyIdentity(uid);latencyIdentities[uid]=identity;
      latencyBusy.add(uid);latencyResults[uid]='';refreshPriority();
      try{
        const result=await api('/api/admin/channel-manager/latency',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(source?{uid,source}:{uid})});
        if(identity!==latencyIdentity(uid))return;
        if(uid.startsWith('managed:')&&Number(result.version)!==Number((data.items||[]).find(c=>c.id===uid.slice(8))?.version))return;
        if(result?.ok===false)throw Error('检测未完成');
        latencyChoices[uid]=Array.isArray(result.sources)?result.sources:[];
        const ms=Number.isFinite(result?.latency_ms)?Math.round(result.latency_ms)+' ms':'';
        const labels={reachable:'网络可达',http_error:'HTTP '+result.http_status,redirect_blocked:'重定向未跟随',timeout:'超时',network_error:'连接失败',unavailable:'当前线路暂不支持检测'};
        latencyResults[uid]=latencyChoices[uid].length?'请选择要检测的地址来源':[ms,source,labels[result.state]||'结果未知'].filter(Boolean).join(' · ');
      }catch(_){if(identity===latencyIdentity(uid))latencyResults[uid]='检测失败，请重试'}
      finally{latencyBusy.delete(uid);refreshPriority()}
    }
    function livePrimaryRow(route,model,product){
      if(route.control_state==='paused')return '<p class="cm-live-status" role="status">当前功能已暂停，不展示接单主渠道。</p>';
      const item=route.primary;
      if(!item)return '<p class="cm-live-status" role="status">当前主渠道未知，不能以候选顺序代替生产状态。</p>';
      const management=item.management||{};
      const ready=route.admitted===true&&model.admitted===true&&product.admitted===true;
      const action=management.kind==='managed_channel'
        ?'<button type="button" data-cm-managed-edit="'+esc((management.uid||'').replace(/^managed:/,''))+'">编辑</button>'
        :management.uid?'<button type="button" data-cm-live-detail="'+esc(management.uid)+'">查看配置</button>':'';
      return '<div class="cm-priority-channel cm-live-primary" data-cm-live-primary="'+esc(item.id||management.uid||'')+'" data-cm-priority-anchor="'+esc(route.operation_id||'')+'"><span aria-hidden="true">●</span><span class="cm-priority-rank">1</span><div class="cm-priority-info"><strong>'+esc(item.name||'当前线路')+'</strong><small>'+esc((item.supplier||'未标注供应商')+' · '+(item.model||route.capability||'模型按功能配置'))+'</small><small class="cm-live-label">'+esc(ready?'当前生产主渠道':'已配置主线路 · 功能未开放或就绪状态待核对')+'</small></div><span class="cm-priority-role primary">'+(ready?'当前主渠道':'未就绪')+'</span><div class="cm-priority-actions">'+latencyControls(management.uid)+action+'</div></div>';
    }
    function priorityEditor(product,model){
      const routes=(model.routes||[]).filter(route=>(data.operations||[]).some(item=>item.operation_id===route.operation_id));
      if(!routes.length)return '<section class="cm-priority-editor"><div class="cm-priority-list">'+((model.routes||[]).map(route=>livePrimaryRow(route,model,product)).join('')||'<p role="status">当前主渠道未知</p>')+'</div><p class="cm-priority-empty">现有线路保持不变；此功能尚未接通托管候选发布。</p></section>';
      const active=routes.find(route=>route.operation_id===matrixExpanded?.operationId)||routes[0];
      matrixExpanded.operationId=active.operation_id;
      const draft=priorityDraft(active),mapping=mappingForOperation(active.operation_id),candidates=compatiblePriorityChannels(active.operation_id);
      const byId=Object.fromEntries((data.items||[]).map(item=>[item.id,item]));
      const actualId=active.primary?.management?.kind==='managed_channel'?(active.primary.management.uid||'').replace(/^managed:/,''):'';
      const published= mappingChannels(mapping);
      const liveInline=active.control_state==='managed'&&draft.state==='managed'&&actualId&&draft.channels[0]===actualId&&published[0]===actualId;
      const prefix=liveInline?'':livePrimaryRow(active,model,product);
      const ready=active.admitted===true&&model.admitted===true&&product.admitted===true;
      // 原厂线路当前用的实际模型。托管渠道若与它不同名，说明换的是另一家的模型，
      // 用户看到的生成结果可能不同 —— 这种候选保留可选，但必须在界面上标出来，
      // 并在拖至首位时二次确认（不静默接管）。
      const legacyModel=String(active.primary?.model||'');
      const ordered=draft.channels.map((id,index)=>{
        const channel=byId[id]||{id,name:'已删除或不可见渠道',supplier:'未知',model:'',base_url:'',connection_type:'unknown',enabled:false,health:'不可用'};
        const isPublished=draft.state===mapping?.state&&published[index]===id;
        const role=liveInline&&isPublished?(index===0?(ready?'当前主渠道':'已配置主线路 · 未就绪'):'已发布候补 '+index):'候选 '+(index+1)+(isPublished?' · 未接管':' · 未发布');
        const proof=C.verificationStatus(channel).overall||{state:'neutral',label:'待验证'};
        const tone={ok:'ok',failed:'bad',unknown:'warn',running:'neutral',queued:'neutral',blocked:'warn',expired:'warn',missing:'neutral',unattributed:'warn','stale-version':'neutral',attention:'warn',neutral:'neutral',off:'muted'};
        const healthTone=channel.enabled?tone[proof.state]||'neutral':'off';
        const healthLabel=channel.enabled?proof.label:'已停用';
        return '<div class="cm-priority-channel" draggable="true" data-cm-priority-channel="'+esc(id)+'" data-cm-priority-operation="'+esc(active.operation_id)+'"><button type="button" class="cm-priority-drag" aria-label="拖动 '+esc(channel.name)+'">⋮⋮</button><span class="cm-priority-rank">'+(index+(prefix&&active.primary&&active.control_state!=='paused'?2:1))+'</span><div class="cm-priority-info"><strong>'+esc(channel.name)+'</strong><small>'+esc((channel.supplier||'未标注供应商')+' · '+(channel.model||'模型待配置'))+'</small>'+(index===0&&liveInline&&!ready?'<small class="cm-live-label">已配置主线路 · 功能未开放或就绪状态待核对</small>':'')+'<code>'+esc(channel.base_url||'Base URL 未配置')+'</code></div><span class="cm-priority-role '+(index===0?'primary':'')+'">'+role+'</span>'+(channel.model&&legacyModel&&channel.model!==legacyModel?'<span class="cm-priority-diff" title="原厂线路模型是 '+esc(legacyModel)+'，这条渠道是 '+esc(channel.model)+'">与原厂不同模型</span>':'')+'<span class="cm-priority-health '+healthTone+'">'+esc(healthLabel)+'</span><div class="cm-priority-actions">'+latencyControls('managed:'+id)+fullTestControls(id)+'<button type="button" class="mini" data-cm-managed-edit="'+esc(id)+'">'+(simpleView?'编辑':'修改 Key / URL')+'</button><details class="cm-row-tools"><summary>更多</summary><button type="button" class="mini" data-cm-priority-move="-1" data-operation="'+esc(active.operation_id)+'" data-channel="'+esc(id)+'" '+(index===0?'disabled':'')+' aria-label="上移 '+esc(channel.name)+'">↑</button><button type="button" class="mini" data-cm-priority-move="1" data-operation="'+esc(active.operation_id)+'" data-channel="'+esc(id)+'" '+(index===draft.channels.length-1?'disabled':'')+' aria-label="下移 '+esc(channel.name)+'">↓</button><button type="button" data-cm-channel-history="'+esc(id)+'">配置回滚</button></details></div></div>';
      }).join('');
      const available=candidates.filter(item=>!draft.channels.includes(item.id));
      const routeTabs=routes.length>1?'<nav class="cm-priority-route-tabs" aria-label="模型能力">'+routes.map(route=>'<button type="button" data-cm-priority-route="'+esc(route.operation_id)+'" class="'+(route.operation_id===active.operation_id?'active':'')+'" aria-pressed="'+String(route.operation_id===active.operation_id)+'">'+esc(route.capability||route.operation_id)+'</button>').join('')+'</nav>':'';
      const addOptions=available.map(item=>'<option value="'+esc(item.id)+'" '+(!item.enabled?'disabled':'')+'>'+esc(item.name+' · '+(item.model||'模型待配置')+(!item.enabled?'（已停用）':''))+'</option>').join('');
      const histories=(mapping?.history||[]).filter(item=>Number(item.revision)!==Number(mapping.revision));
      const history=histories.length?'<details class="cm-priority-history"><summary>历史版本与回滚</summary><div>'+histories.map(item=>'<button type="button" class="mini" data-cm-priority-rollback="'+Number(item.revision)+'" data-operation="'+esc(active.operation_id)+'" data-expected-revision="'+Number(mapping.revision||0)+'">恢复 r'+Number(item.revision)+' · '+esc(item.state||'')+' · '+esc(date(item.created))+'</button>').join('')+'</div></details>':'';
      const failoverEvidence=recentFailoverEvidence(active.operation_id,byId);
      const operation=(data.operations||[]).find(item=>item.operation_id===active.operation_id);
      const rule=operation?.task_match||{};
      const sample=Object.fromEntries(Object.entries(rule).filter(([key])=>!['kind','reference_count','mask_present'].includes(key)));
      sample.prompt='在这里填写图片描述';
      if(rule.reference_count==='>0')sample.reference_images=['<已上传的参考图地址>'];
      if(rule.mask_present)sample.mask='<已上传的蒙版地址>';
      const example=operation?.channel_kind==='image'?'<details class="cm-priority-example"><summary>主站调用示例（执行会产生费用）</summary><p>使用已登录账号，由服务端按此功能的渠道顺序路由；不要在前端放供应商 Key。</p><pre>'+esc("fetch('/api/gen/image', {\n  method: 'POST', credentials: 'include',\n  headers: {'Content-Type': 'application/json'},\n  body: JSON.stringify("+JSON.stringify(sample,null,2)+")\n})")+'</pre></details>':'';
      const publishedLine=(()=>{const pub=mapping;const stateTxt={legacy:'旧线路',shadow:'影子',managed:'托管',paused:'暂停'}[pub?.state]||pub?.state||'未发布';const pubTxt=pub&&pub.revision?('已发布 r'+pub.revision+' · '+stateTxt):'未发布稳定映射';const changed=JSON.stringify(draft.channels)!==JSON.stringify(mappingChannels(pub))||draft.state!==(pub?.state||'shadow');return '<div class="cm-priority-published"><span>服务端已发布：</span><strong>'+esc(pubTxt)+'</strong>'+(changed?'<em>待应用：将候选拖至首位</em>':'<em class="muted">草稿与已发布一致</em>')+'</div>'})();
      return '<section class="cm-priority-editor" data-cm-priority-editor="'+esc(active.operation_id)+'"><div class="cm-priority-head"><div><span>渠道优先级</span><h4>'+esc(product.label+' · '+model.label)+'</h4><p>'+esc(active.capability||'生成')+' · <code>'+esc(active.operation_id)+'</code></p></div><button type="button" class="mini" data-cm-priority-close>收起</button></div>'+routeTabs
        +publishedLine+'<div class="cm-priority-list">'+prefix+(ordered||'<p class="cm-priority-empty">尚未添加托管候选。</p>')+'</div>'
        +'<p role="status" data-cm-priority-status="'+esc(active.operation_id)+'">'+esc(priorityUncertain?'结果未知，请刷新核对后再操作。':priorityErrors[active.operation_id]?.message||'拖至首位自动申请应用，仅影响新任务；原线路仅作只读展示。')+'</p>'
        +(priorityErrors[active.operation_id]?.channel?'<button type="button" data-cm-live-detail="managed:'+esc(priorityErrors[active.operation_id].channel)+'">查看渠道验证与未应用原因</button>':'')
        +(simpleView?'':'<p class="cm-priority-notice">结果未知或已受理后失败均不会切换，避免重复生成与重复计费。</p>')+failoverEvidence+'</section>';
    }
    function manageAction(item){
      const management=item?.management;if(!management?.uid)return '';
      if(management.kind==='managed_channel'){
        const id=management.uid.replace(/^managed:/,'');
        return '<button type="button" class="mini primary" data-cm-managed-edit="'+esc(id)+'">修改 API Key 与 Base URL</button>';
      }
      const label=management.kind==='provider_pool'?'修改 API Key 与 Base URL':'查看服务器托管来源';
      return '<button type="button" class="mini" data-cm-inline-route="'+esc(management.uid)+'" data-cm-inline-kind="'+esc(management.kind)+'">'+esc(label)+'</button>';
    }
    function channelDetail(item,prefix){
      if(!item)return '<div class="cm-route"><strong>'+esc(prefix+'：未配置')+'</strong></div>';
      const baseUrls=(item.base_urls||[]).filter(Boolean);
      const base=baseUrls.length?baseUrls.map(value=>'<code>'+esc(value)+'</code>').join(''):item.base_host?'<code>https://'+esc(item.base_host)+'</code>':'<span>未配置</span>';
      const keyLabel={configured:'已配置',missing:'未配置',unknown:'配置状态未知'}[item.key]||'配置状态未知';
      const proofLine=(kind,label,proof)=>'<small>'+esc(label)+'：'+esc(proof?.label||'未验证')+(proof?.checked_at?'（'+esc(date(proof.checked_at))+(proof.version!=null?' · v'+esc(proof.version):'')+'）':'')+'</small>';
      return '<div class="cm-route"><strong>'+esc(prefix+'：'+item.name)+'</strong>'
        +'<small>'+esc((item.supplier||'未标注供应商')+' · '+transportName(item.connection_type)+(item.base_host?' · '+item.base_host:''))+'</small>'
        +'<small class="cm-route-base"><span>Base URL</span>'+base+'</small>'
        +(item.model?'<small>实际模型：<code>'+esc(item.model)+'</code></small>':'')
        +'<small>凭据：'+esc(item.credential_source||'尚无凭据来源')+'</small>'
        +'<small>配置：'+esc(item.configured?'完整':'不完整')+' · 密钥：'+esc(keyLabel)+' · 渠道：'+esc(item.enabled?'已启用':'未启用')+'</small>'
        +proofLine('connection','连接',item.connection)
        +proofLine('auth','鉴权',item.auth)
        +proofLine('full','完整生成',item.full)
        +manageAction(item)+'</div>';
    }
    function routeOverview(item){
      if(!item)return '<div class="cm-current-route empty">当前模型尚未配置主渠道。</div>';
      const baseUrls=(item.base_urls||[]).filter(Boolean);
      const base=baseUrls[0]||(item.base_host?'https://'+item.base_host:'未配置');
      const keyLabel={configured:'已配置',missing:'未配置',unknown:'配置状态未知'}[item.key]||'配置状态未知';
      const proof=item.auth?.label||'未验证';
      const full=item.full?.label||'未验证';
      return '<article class="cm-current-route"><div class="cm-current-route-head"><div><span>当前主渠道</span><h3>'+esc(item.name||'未命名渠道')+'</h3></div><span class="cm-transport '+esc(item.connection_type||'unknown')+'">'+esc(transportName(item.connection_type))+'</span></div>'
        +'<dl><div><dt>供应商</dt><dd>'+esc(item.supplier||'未标注')+'</dd></div><div><dt>Base URL</dt><dd><code>'+esc(base)+'</code></dd></div><div><dt>凭据来源</dt><dd>'+esc(item.credential_source||'尚未登记')+'</dd></div><div><dt>密钥</dt><dd>'+esc(keyLabel)+'</dd></div><div><dt>鉴权</dt><dd>'+esc(proof)+(item.auth?.checked_at?' <span class="muted">'+esc(date(item.auth.checked_at))+(item.auth.version!=null?' · v'+esc(item.auth.version):'')+'</span>':'')+'</dd></div><div><dt>完整生成</dt><dd>'+esc(full)+(item.full?.checked_at?' <span class="muted">'+esc(date(item.full.checked_at))+(item.full.version!=null?' · v'+esc(item.full.version):'')+'</span>':'')+'</dd></div><div><dt>生产角色</dt><dd>'+esc(item.production?.summary||'按路由状态另核')+'</dd></div></dl>'+manageAction(item)+'</article>';
    }
    function newModelChannel(pageKey,productKey,modelKey){
      const page=matrixPages().find(p=>p.page===pageKey);
      const product=page?.products?.find(p=>p.key===productKey);
      const model=product?.models?.find(m=>m.key===modelKey);
      const routes=(model?.routes||[]).filter(r=>(data.operations||[]).some(op=>op.operation_id===r.operation_id));
      const route=routes.find(r=>r.operation_id===matrixExpanded?.operationId)||routes[0];
      const operation=(data.operations||[]).find(op=>op.operation_id===route?.operation_id);
      if(!model?.actual_model||!operation){toast('当前模型尚未接通新增托管渠道');return}
      // Only copy protocol metadata from an exact-model primary, never shadow candidates or credentials.
      const primary=route.primary;
      const managed=(data.items||[]).find(c=>primary?.management?.kind==='managed_channel'&&c.id===primary.management.uid?.replace(/^managed:/,'')&&c.model===model.actual_model);
      const legacy=rows.find(c=>c.uid===primary?.management?.uid);
      const adapter=managed?.adapter||(primary?.management?.kind==='server_env'?{gemini:'gemini_image',openai:'openai_image'}[legacy?.key]:'');
      if(!adapter||data.adapters?.[adapter]?.kind!==operation.channel_kind){toast('当前模型暂不支持新增兼容供应商；不会套用其他模型的协议');return}
      if(!closeLegacy())return;
      env.newChannel?.({
        _modelCreate:{page:pageKey,product:productKey,model:modelKey,operationId:operation.operation_id},
        name:model.label,model:model.actual_model,adapter,supplier:'',base_url:'',
        connection_type:'unknown',enabled:true,monitor:false,daily_test:false,
        test_cost:0,daily_budget:0,daily_limit:0,
        fixture:{prompt:'一张简洁的产品展示图',ratio:'1:1',duration:5}
      });
    }
    function addCreatedChannel(channel,context){
      if(!(data.items||[]).some(c=>c.id===channel.id)){toast('渠道已保存，但列表读回未确认；请刷新核对，不要重复新增');return}
      const draft=priorityDraft({operation_id:context.operationId});
      if(!draft.channels.includes(channel.id))draft.channels.push(channel.id);
      renderMatrix();
      toast('新渠道已保存并加入当前列表草稿；原渠道未改。检测并发布后才参与生产。');
    }
    function openMatrixModel(pageKey,productKey,modelKey){
      const page=matrixPages().find(item=>item.page===pageKey);
      const product=(page?.products||[]).find(item=>item.key===productKey);
      const model=(product?.models||[]).find(item=>item.key===modelKey);
      if(!page||!product||!model)return;
      if(simpleView){
        const primary=modelLegs(model,['primary']).map(([,item])=>item);
        if(primary.length===1&&primary[0].management?.kind==='managed_channel'){
          if(!closeLegacy())return;
          env.editChannel?.(primary[0].management.uid.replace(/^managed:/,''));return;
        }
        const entry=primary.length===1?rows.find(row=>row.uid===primary[0].management?.uid):null;
        const target={gemini:'image.banana.nb2',openai:'image.openai',seedance:'image.seedream',runninghub:'video.tryon.classic',wavespeed:'video.tryon.fast'}[entry?.key];
        if(target&&env.openProviderConfig){if(!closeLegacy())return;env.openProviderConfig(target,{model:model.actual_model});return}
      }
      matrixExpanded={page:pageKey,product:productKey,model:modelKey,operationId:matrixExpanded?.model===modelKey?matrixExpanded.operationId:''};
      if(!closeLegacy())return;closeGuard=null;selected=null;shell(product.label+' · '+model.label);
      const status=modelStatus(product,model);
      const managers=modelManagers(model),legacyManagers=managers.map(item=>({item,target:rows.find(row=>row.uid===item.management.uid)})).filter(entry=>entry.target?.source==='legacy');
      const routes=(model.routes||[]).map(route=>'<section class="cm-model-route-detail"><h4>'+esc((route.capability||'生成')+' · '+({legacy:'现有线路',managed:'统一托管',shadow:'现有线路运行 / 影子观察',paused:'已暂停'}[route.control_state]||route.control_state))+'</h4>'
        +channelDetail(route.primary,'主渠道')+(route.backup?channelDetail(route.backup,'备用渠道'):'')+(route.candidate?channelDetail(route.candidate,'影子候选'):'')
        +(route.reason?'<p class="cm-matrix-detail-warning">'+esc(route.reason)+'</p>':'')+'</section>').join('');
      const managedActions=managers.filter(item=>item.management.kind==='managed_channel').map(item=>'<button type="button" class="primary" data-cm-managed-edit="'+esc(item.management.uid.replace(/^managed:/,''))+'">修改 '+esc(item.name)+' 的 Key / Base URL</button>').join('');
      const legacySwitch=legacyManagers.map((entry,index)=>'<button type="button" class="'+(index?'':'active')+'" data-cm-inline-route="'+esc(entry.item.management.uid)+'" data-cm-inline-kind="'+esc(entry.item.management.kind)+'" aria-pressed="'+String(!index)+'">'+esc(entry.item.name)+'</button>').join('');
      const serverEntry=legacyManagers.find(entry=>entry.item.management.kind==='server_env');
      const replacementKey=pageKey+'.'+productKey+'.'+modelKey;
      const providerKey=String(serverEntry?.target?.key||'');
      const replacementAdapter={gemini:'gemini_image',openai:'openai_image'}[providerKey]||'';
      const liveConfigTarget={gemini:'image.banana.nb2',openai:'image.openai',seedance:'image.seedream',runninghub:'video.tryon.classic',wavespeed:'video.tryon.fast'}[providerKey]||'';
      const replacementBase=(serverEntry?.item?.base_urls||[])[0]||(serverEntry?.item?.base_host?'https://'+serverEntry.item.base_host:'');
      if(serverEntry&&replacementAdapter&&data.adapters?.[replacementAdapter])serverReplacementTemplates[replacementKey]={
        name:(serverEntry.item.name||serverEntry.target.name||'官方渠道')+' · '+model.label,
        supplier:serverEntry.item.supplier||serverEntry.target.name||'',connection_type:serverEntry.item.connection_type||'official',
        adapter:replacementAdapter,base_url:replacementBase,model:model.actual_model||'',enabled:true,
        fixture:{prompt:'生成一张纯色背景的产品展示图',ratio:'1:1',duration:5},
        // 没有完整测试预算就永远跑不出 24 小时内的通过证据，也就永远切不了主渠道：预填一次 1 元。
        test_cost:1,daily_limit:1,daily_budget:1,
        _replacement:{page:pageKey,product:productKey,model:modelKey,operations:[...new Set((model.routes||[]).map(route=>route.operation_id).filter(Boolean))]}
      };else delete serverReplacementTemplates[replacementKey];
      const replacementAction=serverEntry?(liveConfigTarget?'<button type="button" class="primary" data-pc-edit="'+esc(liveConfigTarget)+'">修改 URL／Key · 版本发布</button>':serverReplacementTemplates[replacementKey]?'<button type="button" class="primary" data-cm-server-replace="'+esc(replacementKey)+'">创建托管候选</button>':'<span class="muted">此线路尚未接通在线配置</span>'):'';
      const inline='<section class="cm-model-inline-config"><div class="cm-model-inline-head"><div><span>凭据与连接配置</span><h3>修改当前模型使用的线路</h3><p>新 Key 保存为候选后自动检测；验证失败不会切换生产线路。</p></div>'+managedActions+'</div>'
        +(serverEntry?'<div class="cm-server-managed-note"><div><b>线路配置 · 验证后发布</b><p>修改 URL 和 Key，免费验证后确认启用。新任务使用新版本；在途任务保留原版本，可回滚。实际可编辑状态以后端为准。</p></div>'+replacementAction+'</div>':'')
        +(legacySwitch?'<nav class="cm-model-inline-tabs" aria-label="选择要配置的底层线路">'+legacySwitch+'</nav><div id="cmLegacyEditorHost"></div><div id="cmLegacyKeys"></div><details class="cm-model-inline-journeys"><summary>查看关联功能与测试入口</summary><div id="cmLegacyJourneys"></div></details>':'')
        +(!legacySwitch&&!managedActions?'<p class="muted">当前模型没有可在线管理的渠道配置。</p>':'')+'</section>';
      const currentRoutes=modelLegs(model,['primary']).map(([,item])=>routeOverview(item)).join('');
      el('cmDetail').innerHTML='<div class="cm-matrix-detail-head"><span class="cm-matrix-status '+status.state+'">'+esc(status.label)+'</span>'
        +'<p>'+esc(page.label||matrixPageMeta.find(x=>x[0]===pageKey)?.[1]||pageKey)+' · '+esc(product.visible?'前台显示':'前台隐藏')+'</p>'
        +'<code>'+esc(model.actual_model||'实际模型待配置')+'</code>'
        +'<p>支持能力：'+esc((model.capabilities||[]).join(' / ')||'尚未登记')+'</p></div>'
        +(simpleView?'':'<div id="cmModelPriority">'+priorityEditor(product,model)+'</div>')
        +((model.warnings||[]).length?'<div class="cm-matrix-detail-warning"><b>需要处理</b><span>'+esc(model.warnings.join('；'))+'</span></div>':'')
        +inline+'<details class="cm-model-advanced"><summary><span>备用线路、能力与验证详情</span><small>按需展开</small></summary><div class="cm-model-advanced-body"><section class="cm-current-routes">'+currentRoutes+'</section>'+(routes||'<p class="muted">尚无路由。</p>')+'</div></details>';
      if(legacyManagers[0])env.detail(legacyManagers[0].target,{managementKind:legacyManagers[0].item.management.kind});
    }
    let simpleView=true;
    function renderMatrix(){
      if(!simpleView){renderAdvancedMatrix();const host=el('cmMatrix');if(host)host.innerHTML='<button type="button" data-cm-simple-toggle>返回简洁视图</button>'+host.innerHTML;return}
      const host=el('cmMatrix');if(!host)return;
      const pages=matrixPages(),page=pages.find(p=>p.page===matrixPage)||pages[0]||{};
      const models=(page.products||[]).flatMap(product=>(product.models||[]).map(model=>({product,model,hidden:!product.visible||model.visible===false})));
      const active=models.find(({product,model})=>matrixExpanded?.page===page.page&&matrixExpanded.product===product.key&&matrixExpanded.model===model.key)||models.find(item=>!item.hidden);
      if(active&&!(matrixExpanded?.page===page.page&&matrixExpanded.product===active.product.key&&matrixExpanded.model===active.model.key))matrixExpanded={page:page.page,product:active.product.key,model:active.model.key,operationId:''};
      const name=({product,model})=>model.label===product.label?product.label:product.label+' · '+model.label;
      const modelButton=item=>'<button type="button" data-cm-model-page="'+esc(page.page)+'" data-cm-model-product="'+esc(item.product.key)+'" data-cm-model-key="'+esc(item.model.key)+'" aria-pressed="'+String(item===active)+'"><strong>'+esc(name(item))+'</strong><small>'+esc(item.model.actual_model||'模型按功能配置')+'</small></button>';
      const hidden=models.filter(item=>item.hidden);
      host.innerHTML='<div class="cm-simple"><div class="cm-category-toolbar"><details class="cm-category-picker"><summary>☰ <span>'+esc(page.label||'选择功能')+'</span> <small>切换功能 ▾</small></summary><div class="cm-category-sheet"><header><strong>选择业务功能</strong><button type="button" data-cm-category-close aria-label="关闭功能选择">×</button></header><nav aria-label="业务功能">'+pages.map(p=>'<button type="button" data-cm-matrix-page="'+esc(p.page)+'" aria-pressed="'+String(p.page===page.page)+'">'+esc(p.label)+'</button>').join('')+'</nav></div></details><details class="cm-view-tools"><summary>更多</summary><button type="button" data-cm-simple-toggle>高级视图</button></details></div><nav class="cm-model-strip" aria-label="模型选择">'+(models.filter(item=>!item.hidden).map(modelButton).join('')||'<p>当前分类没有可显示的模型。</p>')+'</nav>'+(hidden.length?'<details class="cm-simple-history"><summary>历史 / 隐藏模型（'+hidden.length+'）</summary><nav class="cm-model-strip">'+hidden.map(modelButton).join('')+'</nav></details>':'')+(active?'<section class="cm-selected-model"><div class="cm-simple-heading"><div><h3>'+esc(name(active))+'</h3><p>'+esc(active.model.actual_model||'模型按功能配置')+'</p></div><button type="button" data-cm-model-add data-cm-model-page="'+esc(page.page)+'" data-cm-model-product="'+esc(active.product.key)+'" data-cm-model-key="'+esc(active.model.key)+'">＋新增渠道</button></div><p class="cm-inline-hint">下方仅排列当前模型的兼容渠道，第一位为优先线路。托管排序发布成功后才生效。</p><div id="cmModelPriority">'+priorityEditor(active.product,active.model)+'</div></section>':'')+'</div>';
    }
    function renderAdvancedMatrix(){
      const host=el('cmMatrix');if(!host)return;
      const pages=matrixPages();
      if(!pages.some(page=>page.page===matrixPage))matrixPage=pages[0]?.page||'image';
      const matrix=pages.find(page=>page.page===matrixPage)||pages[0]||{},summary=matrix.summary||{},allProducts=matrix.products||[];
      const issueCount=allProducts.reduce((total,product)=>total+(product.models||[]).filter(model=>modelNeedsAction(product,model)).length,0);
      const visibleProducts=allProducts.map(product=>({...product,models:(product.models||[]).filter(model=>product.visible&&model.visible!==false)})).filter(product=>product.visible&&product.models.length);
      const hiddenProducts=allProducts.map(product=>({...product,models:(product.models||[]).filter(model=>!product.visible||model.visible===false)})).filter(product=>product.models.length||!product.visible);
      const activeGroup=matrixGroupForPage(matrixPage);matrixGroupSelection[activeGroup.key]=matrixPage;
      const groupStats=group=>{
        const groupPages=group.pages.map(key=>pages.find(page=>page.page===key)).filter(Boolean);
        const productCount=groupPages.reduce((total,page)=>total+Number(page.summary?.products||0),0);
        const issueCount=groupPages.reduce((total,page)=>total+(page.products||[]).reduce((sum,product)=>sum+(product.models||[]).filter(model=>modelNeedsAction(product,model)).length,0),0);
        return {productCount,issueCount};
      };
      const hiddenTotal=pages.reduce((total,page)=>total+(page.products||[]).reduce((sum,product)=>sum+(product.models||[]).filter(model=>!product.visible||model.visible===false).length,0),0);
      const businessTabs='<nav class="cm-business-tabs" aria-label="业务板块">'+matrixPageGroups.map(group=>{const stats=groupStats(group),active=group.key===activeGroup.key;return '<button type="button" class="'+(active?'active':'')+'" data-cm-matrix-group="'+esc(group.key)+'" aria-pressed="'+String(active)+'"><span>'+esc(group.label)+'</span><small>'+stats.productCount+' 个产品 / 服务</small>'+(stats.issueCount?'<em>'+stats.issueCount+' 项需处理</em>':'')+'</button>'}).join('')+'</nav>';
      const groupPages=activeGroup.pages.map(key=>pages.find(page=>page.page===key)).filter(Boolean);
      const subnav=groupPages.length>1?'<nav class="cm-subfunction-tabs" aria-label="'+esc(activeGroup.label)+'功能">'+groupPages.map(page=>'<button type="button" data-cm-matrix-page="'+esc(page.page)+'" aria-pressed="'+String(page.page===matrixPage)+'" class="'+(page.page===matrixPage?'active':'')+'"><span>'+esc(page.label||page.page)+'</span><small>'+Number(page.summary?.models||0)+'</small></button>').join('')+'</nav>':'';
      const visibleModels=visibleProducts.flatMap(product=>(product.models||[]).map(model=>[product,model]));
      const statusCounts=visibleModels.reduce((counts,[product,model])=>{const state=modelStatus(product,model).state;if(state==='ok')counts.ok++;else if(state==='neutral')counts.pending++;else counts.issue++;return counts},{ok:0,pending:0,issue:0});
      const renderModelTable=(products,hidden=false)=>{
        const body=products.flatMap(product=>{
          const models=product.models||[];
          const expandedIndex=models.findIndex(model=>matrixExpanded&&matrixExpanded.page===matrix.page&&matrixExpanded.product===product.key&&matrixExpanded.model===model.key);
          const productRowspan=models.length+(expandedIndex>=0?1:0);
          return models.flatMap((model,index)=>{
            const routes=model.routes||[],status=modelStatus(product,model);
            const channel=compactValue(routes.map(route=>route.primary?.name||'未配置'));
            const transport=compactValue(routes.map(route=>route.primary?transportName(route.primary.connection_type):'未标注'));
            const expanded=index===expandedIndex;
            const productCell=index?'':'<td class="cm-model-product" rowspan="'+productRowspan+'"><b>'+esc(product.label)+'</b><small>'+esc(product.description||product.visibility_reason||'前端产品')+'</small><span class="cm-matrix-pill '+(hidden?'muted':'ok')+'">'+(hidden?'前台隐藏':'前台显示')+'</span></td>';
            const row='<tr class="'+(modelNeedsAction(product,model)?'attention ':'')+(hidden?'hidden ':'')+(expanded?'expanded':'')+'" data-cm-model-page="'+esc(matrix.page)+'" data-cm-model-product="'+esc(product.key)+'" data-cm-model-key="'+esc(model.key)+'" aria-expanded="'+String(expanded)+'">'+productCell
              +'<td><button type="button" class="cm-model-list-link" data-cm-model-page="'+esc(matrix.page)+'" data-cm-model-product="'+esc(product.key)+'" data-cm-model-key="'+esc(model.key)+'">'+esc(model.label)+'</button></td>'
              +'<td class="cm-model-actual"><code title="'+esc(model.actual_model||'实际模型待配置')+'">'+esc(model.actual_model||'实际模型待配置')+'</code></td>'
              +'<td class="cm-model-channel"><strong>'+esc(channel)+'</strong></td>'
              +'<td class="cm-model-transport"><span class="cm-transport '+(transport==='官方直连'?'official':transport==='中转 API'?'relay':'unknown')+'">'+esc(transport)+'</span></td>'
              +'<td><span class="cm-matrix-status '+status.state+'">'+esc(status.label)+'</span></td>'
              +'<td class="cm-model-action"><button type="button" class="mini" data-cm-model-config data-cm-model-page="'+esc(matrix.page)+'" data-cm-model-product="'+esc(product.key)+'" data-cm-model-key="'+esc(model.key)+'">配置渠道</button></td></tr>';
            return expanded?[row,'<tr class="cm-model-priority-row"><td colspan="6">'+priorityEditor(product,model)+'</td></tr>']:[row];
          });
        }).join('');
        if(!body)return '<div class="empty">当前板块没有前台显示的模型或服务。</div>';
        return '<div class="cm-model-list"><table><thead><tr><th>前端产品</th><th>模型档位</th><th class="cm-model-actual">实际模型</th><th>当前主渠道</th><th class="cm-model-transport">接入方式</th><th>状态</th><th>操作</th></tr></thead><tbody>'+body+'</tbody></table></div>';
      };
      const visibleTable=renderModelTable(visibleProducts);
      const hiddenTable=hiddenProducts.length?renderModelTable(hiddenProducts,true):'';
      host.innerHTML='<div class="cm-function-workspace"><div class="cm-function-heading"><div><span>'+esc(activeGroup.label)+'</span><h3>'+esc(matrix.label||matrix.page||'前端模型与渠道')+'</h3><p class="muted">点击模型展开渠道优先级，并直接修改托管渠道的 API Key 与 Base URL。</p></div><div class="actions"><button type="button" data-cm-view="layout">调整前台展示</button></div></div>'+businessTabs+subnav
        +'<div class="cm-overview-strip"><span><i class="ok"></i>可接单 <b>'+statusCounts.ok+'</b></span><span><i class="pending"></i>待验证 <b>'+statusCounts.pending+'</b></span><span><i class="warn"></i>需要处理 <b>'+statusCounts.issue+'</b></span><button type="button" class="cm-overview-hidden" data-cm-matrix-hidden aria-pressed="'+String(matrixShowHidden)+'"><i class="muted"></i>前台隐藏 <b>'+hiddenProducts.reduce((total,product)=>total+(product.models||[]).length,0)+'</b></button><small>'+Number(summary.products||0)+' 个产品 · '+Number(summary.models||0)+' 个模型 / 服务</small></div>'
        +(matrix.precision==='service'?'<p class="cm-precision-note">本板块按已登记的真实前端功能与依赖服务展示；尚未建立独立模型档位的功能会标为“服务配置”。</p>':'')
        +visibleTable
        +(hiddenTable?'<details class="cm-hidden-products" '+(matrixShowHidden?'open':'')+'><summary><span><b>前台隐藏与历史</b><small>不会出现在用户主页面，但仍保留配置与记录</small></span><em>'+hiddenProducts.reduce((total,product)=>total+(product.models||[]).length,0)+'</em></summary>'+hiddenTable+'</details>':'')+'</div>';
    }
    function list(){
      const visible=C.filter(rows,filters);
      const tone=v=>({ok:'ok',failed:'bad',unknown:'warn',running:'neutral',queued:'neutral',blocked:'warn',expired:'warn',missing:'neutral',unattributed:'warn','stale-version':'neutral',attention:'warn',neutral:'neutral',off:'muted'}[v]||'neutral');
      el('cmCount').textContent=visible.length+' / '+rows.length+' 个渠道 · 历史或停用 '+rows.filter(c=>c.retired).length+' 个';
      el('cmCategories').innerHTML=C.categories.map(([key,label])=>'<button data-cm-category="'+key+'" aria-pressed="'+(filters.category===key)+'" class="'+(filters.category===key?'active':'')+'">'+label+' <small>'+C.filter(rows,{...filters,category:key}).length+'</small></button>').join('');
      el('cmList').innerHTML=visible.length?table(['渠道 / 供应商','配置','验证','生产路由','操作'],visible.map(c=>{
        const v=c._verification||{},cfg=c._config||{},prod=c._production||{};
        const overall=v.overall||{state:'neutral',label:'未验证'};
        const keyLabel={configured:'密钥已配置',missing:'密钥未配置',unknown:'密钥状态未知'}[cfg.key]||'密钥状态未知';
        const configLine=c.source==='legacy'?'内置线路':(cfg.complete?'完整':'缺字段')+' · '+keyLabel;
        const prodLine=c.source==='legacy'?(c.scope?('控制范围：'+c.scope):'按关联功能'):(prod.summary||'未接入');
        return '<tr><td><b>'+esc(c.name)+'</b><small>'+esc((c.source==='managed'?c.supplier+' · '+c.model:'现有供应商线路')+' · '+c.categories.map(k=>C.categories.find(x=>x[0]===k)[1]).join(' / '))+'</small></td>'
          +'<td>'+esc(configLine)+'</td>'
          +'<td><span class="cm-evidence '+tone(overall.state)+'">'+esc(overall.label)+'</span></td>'
          +'<td><small>'+esc(prodLine)+'</small></td>'
          +'<td>'+actions(c)+'</td></tr>';
      })):'<div class="empty">没有符合条件的渠道，请调整分类或筛选。</div>';
    }
    function health(){
      const tone=v=>({ok:'ok',failed:'bad',unknown:'warn',running:'neutral',queued:'neutral',blocked:'warn',expired:'warn',missing:'neutral',unattributed:'warn','stale-version':'neutral',attention:'warn',neutral:'neutral',off:'muted'}[v]||'neutral');
      const cell=p=>'<span class="cm-evidence '+tone(p.state)+'">'+esc(p.label)+'</span>'+((p.time&&p.state!=='missing'&&p.state!=='unattributed')?'<small>'+esc(date(p.time))+(p.version!=null?' · v'+esc(p.version):'')+'</small>':'');
      el('cmHealth').innerHTML=table(['渠道','配置','连接','鉴权','完整生成','生产路由','巡检计划','操作'],rows.filter(c=>!c.retired).map(c=>{
        const v=c._verification||{},cfg=c._config||{},prod=c._production||{},parts=v.parts||{};
        const keyLabel={configured:'密钥已配置',missing:'密钥未配置',unknown:'密钥状态未知'}[cfg.key]||'密钥状态未知';
        const configLine=c.source==='legacy'?'内置线路':(cfg.complete?'完整':'缺字段')+' · '+keyLabel;
        const conn=parts.connection||{state:'missing',label:'未验证'},auth=parts.auth||{state:'missing',label:'未验证'},full=parts.full||{state:'missing',label:'未验证'};
        const prodLine=c.source==='legacy'?'按关联功能':(prod.summary||'未接入');
        return '<tr><td><b>'+esc(c.name)+'</b><small>'+esc(c.source==='legacy'?c.health:'配置 v'+c.version)+'</small></td>'
          +'<td>'+esc(configLine)+'</td>'
          +'<td>'+cell(conn)+'</td><td>'+cell(auth)+'</td><td>'+cell(full)+'</td>'
          +'<td><small>'+esc(prodLine)+'</small></td>'
          +'<td>'+esc(c.source==='legacy'?'按关联功能检查与验收':(c.monitor?'连接：'+date(c.schedule?.light_due):'连接巡检未启用')+'；'+(c.daily_test?'生成：'+date(c.schedule?.full_due):'每日生成未启用'))+'</td>'
          +'<td>'+button(c,'检查与测试')+'</td></tr>';
      }));
    }
    function render(next){
      priorityUncertain=false;
      data=next;rows=C.catalog(data,env.legacy());
      invalidateLatency();
      const suppliers=[...new Set(rows.map(c=>c.supplier))].sort();
      el('cmSupplier').innerHTML='<option value="">全部供应商</option>'+suppliers.map(s=>'<option value="'+esc(s)+'">'+esc(s)+'</option>').join('');
      el('cmSupplier').value=filters.supplier;renderMatrix();list();health();showTab(tab);
      if(selected?.source==='managed'&&!el('cmDrawer').hidden&&el('cmEditor').hidden&&el('cmMappingEditor').hidden)open(selected.uid,true);
    }
    let closeGuard=null;
    const closeLegacy=()=>env.closeLegacy()!==false;
    function close(){if(closeGuard&&!closeGuard())return;if(!el('cmDrawer').hidden){if(!closeLegacy())return;closeGuard=null;el('cmEditor').innerHTML='';el('cmMappingEditor').innerHTML='';el('cmDrawer').hidden=true;document.body.classList.remove('cm-drawer-open');selected=null;if(returnFocus?.isConnected)returnFocus.focus()}}
    function shell(title){returnFocus=document.activeElement;el('cmDrawer').hidden=false;document.body.classList.add('cm-drawer-open');el('cmDrawerTitle').textContent=title;el('cmDetail').hidden=false;el('cmEditor').hidden=true;el('cmMappingEditor').hidden=true;el('cmDrawerClose').focus()}
    function threeStatusHtml(c){
      const v=c._verification||{},cfg=c._config||{},prod=c._production||{},parts=v.parts||{};
      const tone=x=>({ok:'ok',failed:'bad',unknown:'warn',running:'neutral',queued:'neutral',blocked:'warn',expired:'warn',missing:'neutral',unattributed:'warn','stale-version':'neutral',attention:'warn',neutral:'neutral',off:'muted'}[x]||'neutral');
      const overall=v.overall||{state:'neutral',label:'未验证'};
      const keyLabel={configured:'已配置',missing:'未配置',unknown:'未知'}[cfg.key]||'未知';
      const part=(kind,label)=>parts[kind]?('<div class="cm-three-part"><span>'+esc(label)+'</span><b class="'+tone(parts[kind].state)+'">'+esc(parts[kind].label)+'</b>'+(parts[kind].time?'<small>'+esc(date(parts[kind].time))+(parts[kind].version!=null?' · v'+esc(parts[kind].version):'')+'</small>':'')+'</div>'):('<div class="cm-three-part"><span>'+esc(label)+'</span><b>未验证</b></div>');
      return '<div class="cm-three-status">'
        +'<div class="cm-three-col"><h4>配置</h4><div class="cm-three-part"><b>'+esc(cfg.complete?'完整':'缺字段')+'</b>'+(cfg.details&&cfg.details.length?'<small>'+esc(cfg.details.join('、'))+'</small>':'')+'<small>密钥：'+esc(keyLabel)+'</small><small>接单：'+esc(c.enabled?'已启用':'已停用')+'</small></div></div>'
        +'<div class="cm-three-col"><h4>验证 <span class="cm-evidence '+tone(overall.state)+'">'+esc(overall.label)+'</span></h4>'+part('connection','连接')+part('auth','鉴权')+part('full','完整生成')+'</div>'
        +'<div class="cm-three-col"><h4>生产路由</h4><div class="cm-three-part"><b>'+esc(prod.summary||'未接入')+'</b><small>启用 ≠ 验证通过；验证通过 ≠ 生产已切换</small></div></div>'
        +'</div>';
    }
    function open(uid,refresh=false){
      const c=rows.find(r=>r.uid===uid);if(!c)return;
      if(!refresh){if(!closeLegacy())return;shell(c.name)}selected=c;
      let body='<p class="muted">'+esc(c.categories.map(k=>C.categories.find(x=>x[0]===k)[1]).join(' / '))+' · '+esc(c.source==='managed'?'可配置渠道':'现有供应商线路')+'</p>';
      body+='<h3>功能与调用关系</h3><div class="cm-flow">'+esc((c.features||[]).join('、')||'尚未配置功能映射')+' → '+esc(c.name)+' → '+esc(c.model||'模型按功能配置')+'</div>';
      if(c.deleted){
        body+='<p>已移入回收站，保留历史配置与调用记录。恢复后仍为停用状态。</p><p>'+esc(c._lifecycle?.reason||'')+'</p><button data-cm-action="restore" data-cm-uid="'+esc(c.uid)+'">恢复渠道</button>';
      }else if(c.source==='managed'){
        body+='<p>当前版本 v'+c.version+' · '+esc(c.enabled?'接单已启用':'已停用')+'</p>'+threeStatusHtml(c)+'<p class="muted">API：'+esc(c.base_url)+'<br>网络：'+esc(c.proxy||'直连')+'<br>参考图：'+Number(c.material_count||0)+' 张</p><div class="actions"><button data-edit="'+esc(c.id)+'">连接配置与巡检</button><button data-cm-mapping="'+esc(c.id)+'">配置功能映射</button></div>';
        body+='<h3>测试与素材</h3><p class="muted">连接可达、鉴权通过、生成成品分别验证。完整测试可能产生供应商费用；不代表用户已接收。</p><div class="actions"><button type="button" data-cm-validation-settings="'+esc(c.id)+'">设置验证素材与预算</button>'+['connection','auth','full'].map((kind,i)=>'<button data-test="'+kind+'" data-id="'+esc(c.id)+'">'+['连接检测','鉴权检测','完整生成测试'][i]+'</button>').join('')+'<button data-cm-refresh="1">刷新结果</button></div>';
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
    root.addEventListener('click',async e=>{const simpleToggle=e.target.closest?e.target.closest('[data-cm-simple-toggle]'):null;if(simpleToggle&&simpleToggle.dataset&&'cmSimpleToggle' in simpleToggle.dataset){simpleView=!simpleView;renderMatrix();return}const addTarget=e.target.closest('[data-cm-model-add]');if(addTarget?.dataset?.cmModelKey){newModelChannel(addTarget.dataset.cmModelPage,addTarget.dataset.cmModelProduct,addTarget.dataset.cmModelKey);return}const configTarget=e.target.closest('[data-cm-model-config]');if(configTarget?.dataset?.cmModelKey){openMatrixModel(configTarget.dataset.cmModelPage,configTarget.dataset.cmModelProduct,configTarget.dataset.cmModelKey);return}const modelTarget=e.target.closest('[data-cm-model-key]');if(modelTarget?.dataset?.cmModelKey){const same=matrixExpanded&&matrixExpanded.page===modelTarget.dataset.cmModelPage&&matrixExpanded.product===modelTarget.dataset.cmModelProduct&&matrixExpanded.model===modelTarget.dataset.cmModelKey;matrixExpanded=same?null:{page:modelTarget.dataset.cmModelPage,product:modelTarget.dataset.cmModelProduct,model:modelTarget.dataset.cmModelKey,operationId:''};renderMatrix();return}const b=e.target.closest('button');if(!b)return;
      if(b.dataset.cmCategoryClose!=null){b.closest('details').open=false;return}
      if(b.dataset.cmLiveDetail){open(b.dataset.cmLiveDetail);return}
      if(b.dataset.cmLatency){await detectLatency(b.dataset.cmLatency,b.dataset.cmLatencySource);return}
      if(b.dataset.cmFulltest){await runFullTest(b.dataset.cmFulltest);return}
      if(b.dataset.cmValidationSettings){env.validationSettings?.(b.dataset.cmValidationSettings);return}
      if(b.dataset.cmPriorityClose!=null){if(el('cmModelPriority'))el('cmModelPriority').innerHTML='';matrixExpanded=null;renderMatrix();return}
      if((priorityBusy||priorityUncertain)&&Object.keys(b.dataset).some(key=>key.startsWith('cmPriority')))return;
      if(b.dataset.cmPriorityRoute){matrixExpanded.operationId=b.dataset.cmPriorityRoute;refreshPriority();return}
      if(b.dataset.cmPriorityMove){movePriority(b.dataset.operation,b.dataset.channel,'',Number(b.dataset.cmPriorityMove));await publishPriority(b.dataset.operation,true);return}
      if(b.dataset.cmPriorityRemove){const draft=priorityDrafts[b.dataset.operation];if(draft)draft.channels=draft.channels.filter(id=>id!==b.dataset.cmPriorityRemove);refreshPriority();return}
      if(b.dataset.cmPriorityAdd){const editor=b.closest('.cm-priority-editor'),choice=editor?.querySelector('[data-cm-priority-add-choice]')?.value,draft=priorityDrafts[b.dataset.cmPriorityAdd];if(choice&&draft&&!draft.channels.includes(choice)){draft.channels.push(choice);refreshPriority()}return}
      if(b.dataset.cmPriorityTest){const operationId=b.dataset.cmPriorityTest,draft=priorityDrafts[operationId],status=el('cmMatrix').querySelector('[data-cm-priority-status="'+operationId+'"]');b.disabled=true;if(status)status.textContent='正在提交连接检测…';try{for(const id of draft?.channels||[])await api('/api/admin/channel-manager/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,kind:'connection'})});if(status)status.textContent='全部连接检测已排队，请稍后刷新查看结果。';toast('渠道连接检测已排队')}catch(error){if(status)status.textContent=error.message;toast(error.message)}finally{b.disabled=false}return}
      if(b.dataset.cmPrioritySave){if(!confirm('发布此模型的渠道优先级？仅影响新任务。'))return;await publishPriority(b.dataset.cmPrioritySave);return}
      if(b.dataset.cmPriorityRollback){const operationId=b.dataset.operation;if(!confirm('恢复 '+operationId+' 到 r'+b.dataset.cmPriorityRollback+'？恢复会发布一个新修订。'))return;b.disabled=true;try{await api('/api/admin/channel-manager/operation-mapping-rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:operationId,target_revision:Number(b.dataset.cmPriorityRollback),expected_revision:Number(b.dataset.expectedRevision)})});delete priorityDrafts[operationId];toast('历史优先级已恢复');await env.refresh()}catch(error){toast(error.message)}finally{b.disabled=false}return}
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
      if(b.dataset.cmView){showTab(b.dataset.cmView);return}
      if(b.dataset.cmMatrixGroup){const group=matrixPageGroups.find(item=>item.key===b.dataset.cmMatrixGroup);if(group){matrixPage=matrixGroupSelection[group.key]||group.pages[0];renderMatrix()}return}
      if(b.dataset.cmMatrixPage){matrixPage=b.dataset.cmMatrixPage;renderMatrix();return}
      if(b.dataset.cmMatrixHidden!=null){matrixShowHidden=!matrixShowHidden;renderMatrix();return}
      if(b.dataset.cmManagedEdit){if(!closeLegacy())return;env.editChannel?.(b.dataset.cmManagedEdit);return}
      if(b.dataset.cmChannelHistory){if(!closeLegacy())return;env.channelHistory?.(b.dataset.cmChannelHistory);return}
      if(b.dataset.cmServerReplace){const template=serverReplacementTemplates[b.dataset.cmServerReplace];if(!template){toast('该线路暂不支持后台直接修改');return}if(!closeLegacy())return;env.newChannel?.({...template,_replacement:{...template._replacement,operations:[...template._replacement.operations]}});return}
      if(b.dataset.cmNewChannel!=null){if(!closeLegacy())return;env.newChannel?.();return}
      if(b.dataset.cmInlineRoute){const target=rows.find(c=>c.uid===b.dataset.cmInlineRoute);if(!target){toast('没有找到对应的渠道配置');return}if(!closeLegacy())return;el('cmDetail').querySelectorAll('[data-cm-inline-route]').forEach(item=>{const active=item.dataset.cmInlineRoute===b.dataset.cmInlineRoute&&item.dataset.cmInlineKind===b.dataset.cmInlineKind;item.classList.toggle('active',active);item.setAttribute('aria-pressed',String(active))});env.detail(target,{managementKind:b.dataset.cmInlineKind||''});return}
      if(b.dataset.cmCategory){filters.category=b.dataset.cmCategory;list()}
      if(b.dataset.cmDetail)open(b.dataset.cmDetail);
      if(b.dataset.cmAction)env.lifecycle(rows.find(c=>c.uid===b.dataset.cmUid),b.dataset.cmAction);
      if(b.dataset.cmRefresh)env.refresh();
      if(b.id==='cmDrawerClose')close();
      if(b.dataset.cmMapping)env.mapping(data.mappings.find(m=>m.channel===b.dataset.cmMapping)||{channel:b.dataset.cmMapping,kind:data.adapters[data.items.find(c=>c.id===b.dataset.cmMapping).adapter].kind});
      if(b.dataset.cmJob){close();env.task(b.dataset.cmJob)}
      if(b.dataset.cmJourney){close();env.journey(b.dataset.cmJourney)}
    });
    root.addEventListener('wheel',e=>{
      const strip=e.target.closest?.('.cm-model-strip');
      if(!strip||e.ctrlKey||strip.scrollWidth<=strip.clientWidth)return;
      const delta=(Math.abs(e.deltaX)>Math.abs(e.deltaY)?e.deltaX:e.deltaY)*(e.deltaMode===1?20:e.deltaMode===2?strip.clientWidth:1);
      if(!delta)return;
      e.preventDefault();
      strip.scrollLeft=Math.max(0,Math.min(strip.scrollWidth-strip.clientWidth,strip.scrollLeft+delta));
    },{passive:false});
    root.addEventListener('change',e=>{const operationId=e.target?.dataset?.cmPriorityState;if(!operationId||!priorityDrafts[operationId])return;priorityDrafts[operationId].state=e.target.value});
    root.addEventListener('dragstart',e=>{const row=e.target.closest('[data-cm-priority-channel]');if(!row)return;if(window.ChannelPriorityDrag){e.preventDefault();return}draggedPriorityChannel=row.dataset.cmPriorityChannel;e.dataTransfer?.setData('text/plain',draggedPriorityChannel);if(e.dataTransfer)e.dataTransfer.effectAllowed='move'});
    root.addEventListener('dragover',e=>{if(e.target.closest('[data-cm-priority-channel]'))e.preventDefault()});
    root.addEventListener('drop',async e=>{const row=e.target.closest('[data-cm-priority-channel]');if(!row||!draggedPriorityChannel||priorityBusy)return;e.preventDefault();movePriority(row.dataset.cmPriorityOperation,draggedPriorityChannel,row.dataset.cmPriorityChannel,0);draggedPriorityChannel='';await publishPriority(row.dataset.cmPriorityOperation,true)});
    if(window.ChannelPriorityDrag)window.ChannelPriorityDrag(root,async(operationId,order)=>{
      if(priorityBusy||!priorityDrafts[operationId])return;
      priorityDrafts[operationId].channels=order;
      await publishPriority(operationId,true);
    },()=>priorityBusy||priorityUncertain);
    [['cmSearch','q','input'],['cmSupplier','supplier','change'],['cmTransport','transport','change'],['cmState','status','change'],['cmHistory','history','change']].forEach(([id,key,event])=>el(id).addEventListener(event,()=>{filters[key]=key==='history'?el(id).checked:el(id).value;list()}));
    el('cmDrawer').addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();close()}if(e.key==='Tab'){const nodes=Array.from(el('cmDrawer').querySelectorAll('button,input,select,textarea,a[href]')).filter(n=>!n.disabled&&n.getClientRects().length);if(!nodes.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}});
    return {render,open,close,editor,showTab,addCreatedChannel,setCloseGuard:guard=>{closeGuard=guard}};
  };
})();
