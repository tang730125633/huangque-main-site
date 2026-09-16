(function(){
  window.initChannelWorkspace=function(env){
    const {el,esc,toast}=env,C=window.ChannelCatalog,api=env.api;
    let data={},rows=[],tab='matrix',matrixPage='image',matrixShowHidden=false,selected=null,returnFocus=null;
    const matrixGroupSelection={};
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
    function catalogRoute(c){
      const baseUrls=unique([c.base_url,c.env_base_url,c.pool_base_url,c.image_primary_base_url,c.image_fallback_base_url]);
      const management={kind:c.source==='managed'?'managed_channel':(c.pool_provider?'provider_pool':'server_env'),uid:c.uid,provider:c.pool_provider||''};
      return {id:c.uid,name:c.name,supplier:c.supplier,connection_type:c.connection_type||'unknown',base_host:c.env_base_host||c.pool_base_host||'',base_urls:baseUrls,model:c.model||'',credential_source:c.source==='managed'?'渠道密钥库':(c.pool_provider?'后台密钥号池 / 服务器兼容线路':'服务器环境变量'),configured:c.configured!==false,enabled:!!c.enabled,auth:catalogProof(c),full:{state:'unverified',label:'请进入测试与健康查看完整证据'},source:c.source,management};
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
    function manageAction(item){
      const management=item?.management;if(!management?.uid)return '';
      if(management.kind==='managed_channel'){
        const id=management.uid.replace(/^managed:/,'');
        return '<button type="button" class="mini primary" data-cm-managed-edit="'+esc(id)+'">修改 API Key 与 Base URL</button>';
      }
      const label=management.kind==='provider_pool'?'修改 API Key 与 Base URL':'查看服务器托管凭据';
      return '<button type="button" class="mini" data-cm-inline-route="'+esc(management.uid)+'" data-cm-inline-kind="'+esc(management.kind)+'">'+esc(label)+'</button>';
    }
    function channelDetail(item,prefix){
      if(!item)return '<div class="cm-route"><strong>'+esc(prefix+'：未配置')+'</strong></div>';
      const baseUrls=(item.base_urls||[]).filter(Boolean);
      const base=baseUrls.length?baseUrls.map(value=>'<code>'+esc(value)+'</code>').join(''):item.base_host?'<code>https://'+esc(item.base_host)+'</code>':'<span>未配置</span>';
      return '<div class="cm-route"><strong>'+esc(prefix+'：'+item.name)+'</strong>'
        +'<small>'+esc((item.supplier||'未标注供应商')+' · '+transportName(item.connection_type)+(item.base_host?' · '+item.base_host:''))+'</small>'
        +'<small class="cm-route-base"><span>Base URL</span>'+base+'</small>'
        +(item.model?'<small>实际模型：<code>'+esc(item.model)+'</code></small>':'')
        +'<small>凭据：'+esc(item.credential_source||'尚无凭据来源')+'</small>'
        +'<small>配置：'+esc(item.configured?'已配置':'未配置')+' · 渠道：'+esc(item.enabled?'已启用':'未启用')+'</small>'
        +'<small>鉴权：'+esc(item.auth?.label||'未验证')+'（'+esc(date(item.auth?.checked_at))+ '）</small>'
        +'<small>成品：'+esc(item.full?.label||'未验证')+'（'+esc(date(item.full?.checked_at))+ '）</small>'+manageAction(item)+'</div>';
    }
    function routeOverview(item){
      if(!item)return '<div class="cm-current-route empty">当前模型尚未配置主渠道。</div>';
      const baseUrls=(item.base_urls||[]).filter(Boolean);
      const base=baseUrls[0]||(item.base_host?'https://'+item.base_host:'未配置');
      const proof=item.auth?.label||'未验证';
      return '<article class="cm-current-route"><div class="cm-current-route-head"><div><span>当前主渠道</span><h3>'+esc(item.name||'未命名渠道')+'</h3></div><span class="cm-transport '+esc(item.connection_type||'unknown')+'">'+esc(transportName(item.connection_type))+'</span></div>'
        +'<dl><div><dt>供应商</dt><dd>'+esc(item.supplier||'未标注')+'</dd></div><div><dt>Base URL</dt><dd><code>'+esc(base)+'</code></dd></div><div><dt>凭据来源</dt><dd>'+esc(item.credential_source||'尚未登记')+'</dd></div><div><dt>验证状态</dt><dd>'+esc(proof)+'</dd></div></dl>'+manageAction(item)+'</article>';
    }
    function openMatrixModel(pageKey,productKey,modelKey){
      const page=matrixPages().find(item=>item.page===pageKey);
      const product=(page?.products||[]).find(item=>item.key===productKey);
      const model=(product?.models||[]).find(item=>item.key===modelKey);
      if(!page||!product||!model)return;
      if(!closeLegacy())return;closeGuard=null;selected=null;shell(product.label+' · '+model.label);
      const status=modelStatus(product,model);
      const managers=modelManagers(model),legacyManagers=managers.map(item=>({item,target:rows.find(row=>row.uid===item.management.uid)})).filter(entry=>entry.target?.source==='legacy');
      const routes=(model.routes||[]).map(route=>'<section class="cm-model-route-detail"><h4>'+esc((route.capability||'生成')+' · '+({legacy:'现有线路',managed:'统一托管',shadow:'现有线路运行 / 影子观察',paused:'已暂停'}[route.control_state]||route.control_state))+'</h4>'
        +channelDetail(route.primary,'主渠道')+(route.backup?channelDetail(route.backup,'备用渠道'):'')+(route.candidate?channelDetail(route.candidate,'影子候选'):'')
        +(route.reason?'<p class="cm-matrix-detail-warning">'+esc(route.reason)+'</p>':'')+'</section>').join('');
      const managedActions=managers.filter(item=>item.management.kind==='managed_channel').map(item=>'<button type="button" class="primary" data-cm-managed-edit="'+esc(item.management.uid.replace(/^managed:/,''))+'">修改 '+esc(item.name)+' 的 Key / Base URL</button>').join('');
      const legacySwitch=legacyManagers.map((entry,index)=>'<button type="button" class="'+(index?'':'active')+'" data-cm-inline-route="'+esc(entry.item.management.uid)+'" data-cm-inline-kind="'+esc(entry.item.management.kind)+'" aria-pressed="'+String(!index)+'">'+esc(entry.item.name)+'</button>').join('');
      const inline='<section class="cm-model-inline-config"><div class="cm-model-inline-head"><div><span>凭据与连接配置</span><h3>修改当前模型使用的线路</h3><p>更换密钥会先验证，通过后才保存；验证失败时保留原配置。</p></div>'+managedActions+'</div>'
        +(legacySwitch?'<nav class="cm-model-inline-tabs" aria-label="选择要配置的底层线路">'+legacySwitch+'</nav><div id="cmLegacyEditorHost"></div><div id="cmLegacyKeys"></div><details class="cm-model-inline-journeys"><summary>查看关联功能与测试入口</summary><div id="cmLegacyJourneys"></div></details>':'')
        +(!legacySwitch&&!managedActions?'<p class="muted">当前模型没有可在线管理的渠道配置。</p>':'')+'</section>';
      const currentRoutes=modelLegs(model,['primary']).map(([,item])=>routeOverview(item)).join('');
      el('cmDetail').innerHTML='<div class="cm-matrix-detail-head"><span class="cm-matrix-status '+status.state+'">'+esc(status.label)+'</span>'
        +'<p>'+esc(page.label||matrixPageMeta.find(x=>x[0]===pageKey)?.[1]||pageKey)+' · '+esc(product.visible?'前台显示':'前台隐藏')+'</p>'
        +'<code>'+esc(model.actual_model||'实际模型待配置')+'</code>'
        +'<p>支持能力：'+esc((model.capabilities||[]).join(' / ')||'尚未登记')+'</p></div>'
        +'<section class="cm-current-routes">'+(currentRoutes||'<div class="empty">尚无主渠道。</div>')+'</section>'
        +((model.warnings||[]).length?'<div class="cm-matrix-detail-warning"><b>需要处理</b><span>'+esc(model.warnings.join('；'))+'</span></div>':'')
        +inline+'<details class="cm-model-advanced"><summary><span>备用线路、能力与验证详情</span><small>按需展开</small></summary><div class="cm-model-advanced-body">'+(routes||'<p class="muted">尚无路由。</p>')+'</div></details>';
      if(legacyManagers[0])env.detail(legacyManagers[0].target,{managementKind:legacyManagers[0].item.management.kind});
    }
    function renderMatrix(){
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
          return models.map((model,index)=>{
            const routes=model.routes||[],status=modelStatus(product,model);
            const channel=compactValue(routes.map(route=>route.primary?.name||'未配置'));
            const transport=compactValue(routes.map(route=>route.primary?transportName(route.primary.connection_type):'未标注'));
            const productCell=index?'':'<td class="cm-model-product" rowspan="'+models.length+'"><b>'+esc(product.label)+'</b><small>'+esc(product.description||product.visibility_reason||'前端产品')+'</small><span class="cm-matrix-pill '+(hidden?'muted':'ok')+'">'+(hidden?'前台隐藏':'前台显示')+'</span></td>';
            return '<tr class="'+(modelNeedsAction(product,model)?'attention ':'')+(hidden?'hidden':'')+'" data-cm-model-page="'+esc(matrix.page)+'" data-cm-model-product="'+esc(product.key)+'" data-cm-model-key="'+esc(model.key)+'">'+productCell
              +'<td><button type="button" class="cm-model-list-link" data-cm-model-page="'+esc(matrix.page)+'" data-cm-model-product="'+esc(product.key)+'" data-cm-model-key="'+esc(model.key)+'">'+esc(model.label)+'</button></td>'
              +'<td class="cm-model-actual"><code title="'+esc(model.actual_model||'实际模型待配置')+'">'+esc(model.actual_model||'实际模型待配置')+'</code></td>'
              +'<td class="cm-model-channel"><strong>'+esc(channel)+'</strong></td>'
              +'<td class="cm-model-transport"><span class="cm-transport '+(transport==='官方直连'?'official':transport==='中转 API'?'relay':'unknown')+'">'+esc(transport)+'</span></td>'
              +'<td><span class="cm-matrix-status '+status.state+'">'+esc(status.label)+'</span></td>'
              +'<td class="cm-model-action"><button type="button" class="mini" data-cm-model-page="'+esc(matrix.page)+'" data-cm-model-product="'+esc(product.key)+'" data-cm-model-key="'+esc(model.key)+'">查看配置</button></td></tr>';
          });
        }).join('');
        if(!body)return '<div class="empty">当前板块没有前台显示的模型或服务。</div>';
        return '<div class="cm-model-list"><table><thead><tr><th>前端产品</th><th>模型档位</th><th class="cm-model-actual">实际模型</th><th>当前主渠道</th><th class="cm-model-transport">接入方式</th><th>状态</th><th>操作</th></tr></thead><tbody>'+body+'</tbody></table></div>';
      };
      const visibleTable=renderModelTable(visibleProducts);
      const hiddenTable=hiddenProducts.length?renderModelTable(hiddenProducts,true):'';
      host.innerHTML='<div class="cm-function-workspace"><div class="cm-function-heading"><div><span>'+esc(activeGroup.label)+'</span><h3>'+esc(matrix.label||matrix.page||'前端模型与渠道')+'</h3><p class="muted">一行对应一个前端模型；点击模型即可管理渠道、API Key 与 Base URL。</p></div><div class="actions"><button type="button" data-cm-view="layout">调整前台展示</button></div></div>'+businessTabs+subnav
        +'<div class="cm-overview-strip"><span><i class="ok"></i>可接单 <b>'+statusCounts.ok+'</b></span><span><i class="pending"></i>待验证 <b>'+statusCounts.pending+'</b></span><span><i class="warn"></i>需要处理 <b>'+statusCounts.issue+'</b></span><button type="button" class="cm-overview-hidden" data-cm-matrix-hidden aria-pressed="'+String(matrixShowHidden)+'"><i class="muted"></i>前台隐藏 <b>'+hiddenProducts.reduce((total,product)=>total+(product.models||[]).length,0)+'</b></button><small>'+Number(summary.products||0)+' 个产品 · '+Number(summary.models||0)+' 个模型 / 服务</small></div>'
        +(matrix.precision==='service'?'<p class="cm-precision-note">本板块按已登记的真实前端功能与依赖服务展示；尚未建立独立模型档位的功能会标为“服务配置”。</p>':'')
        +visibleTable
        +(hiddenTable?'<details class="cm-hidden-products" '+(matrixShowHidden?'open':'')+'><summary><span><b>前台隐藏与历史</b><small>不会出现在用户主页面，但仍保留配置与记录</small></span><em>'+hiddenProducts.reduce((total,product)=>total+(product.models||[]).length,0)+'</em></summary>'+hiddenTable+'</details>':'')+'</div>';
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
      el('cmSupplier').value=filters.supplier;renderMatrix();list();health();showTab(tab);
      if(selected?.source==='managed'&&!el('cmDrawer').hidden&&el('cmEditor').hidden&&el('cmMappingEditor').hidden)open(selected.uid,true);
    }
    let closeGuard=null;
    const closeLegacy=()=>env.closeLegacy()!==false;
    function close(){if(closeGuard&&!closeGuard())return;if(!el('cmDrawer').hidden){if(!closeLegacy())return;closeGuard=null;el('cmEditor').innerHTML='';el('cmMappingEditor').innerHTML='';el('cmDrawer').hidden=true;document.body.classList.remove('cm-drawer-open');selected=null;if(returnFocus?.isConnected)returnFocus.focus()}}
    function shell(title){returnFocus=document.activeElement;el('cmDrawer').hidden=false;document.body.classList.add('cm-drawer-open');el('cmDrawerTitle').textContent=title;el('cmDetail').hidden=false;el('cmEditor').hidden=true;el('cmMappingEditor').hidden=true;el('cmDrawerClose').focus()}
    function open(uid,refresh=false){
      const c=rows.find(r=>r.uid===uid);if(!c)return;
      if(!refresh){if(!closeLegacy())return;shell(c.name)}selected=c;
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
    root.addEventListener('click',e=>{const modelTarget=e.target.closest('[data-cm-model-key]');if(modelTarget?.dataset?.cmModelKey){openMatrixModel(modelTarget.dataset.cmModelPage,modelTarget.dataset.cmModelProduct,modelTarget.dataset.cmModelKey);return}const b=e.target.closest('button');if(!b)return;
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
    [['cmSearch','q','input'],['cmSupplier','supplier','change'],['cmTransport','transport','change'],['cmState','status','change'],['cmHistory','history','change']].forEach(([id,key,event])=>el(id).addEventListener(event,()=>{filters[key]=key==='history'?el(id).checked:el(id).value;list()}));
    el('cmDrawer').addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();close()}if(e.key==='Tab'){const nodes=Array.from(el('cmDrawer').querySelectorAll('button,input,select,textarea,a[href]')).filter(n=>!n.disabled&&n.getClientRects().length);if(!nodes.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}});
    return {render,open,close,editor,showTab,setCloseGuard:guard=>{closeGuard=guard}};
  };
})();
