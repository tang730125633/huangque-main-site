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
    function routeBackup(route){
      if(route.backup)return route.backup.name;
      const count=route.primary?.source==='pool'?Number(route.primary.pool_usable||0):0;
      return count>1?'号池轮转（'+count+' 个）':'无';
    }
    function backupSummary(model){
      return compactValue((model.routes||[]).map(routeBackup),'无');
    }
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
    function manageAction(item){
      const management=item?.management;if(!management?.uid)return '';
      const label=management.kind==='managed_channel'?'配置渠道、Key 与 Base URL':management.kind==='provider_pool'?'管理 API Key 与 Base URL':'查看服务器托管凭据';
      return '<button type="button" class="mini" data-cm-route-manage="'+esc(management.uid)+'">'+esc(label)+'</button>';
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
    function openMatrixModel(pageKey,productKey,modelKey){
      const page=matrixPages().find(item=>item.page===pageKey);
      const product=(page?.products||[]).find(item=>item.key===productKey);
      const model=(product?.models||[]).find(item=>item.key===modelKey);
      if(!page||!product||!model)return;
      env.closeLegacy();closeGuard=null;selected=null;shell(product.label+' · '+model.label);
      const status=modelStatus(product,model);
      const routes=(model.routes||[]).map(route=>'<section class="cm-model-route-detail"><h4>'+esc((route.capability||'生成')+' · '+({legacy:'现有线路',managed:'统一托管',shadow:'现有线路运行 / 影子观察',paused:'已暂停'}[route.control_state]||route.control_state))+'</h4>'
        +channelDetail(route.primary,'主渠道')+(route.backup?channelDetail(route.backup,'备用渠道'):'')+(route.candidate?channelDetail(route.candidate,'影子候选'):'')
        +(route.reason?'<p class="cm-matrix-detail-warning">'+esc(route.reason)+'</p>':'')+'</section>').join('');
      el('cmDetail').innerHTML='<div class="cm-matrix-detail-head"><span class="cm-matrix-status '+status.state+'">'+esc(status.label)+'</span>'
        +'<p>'+esc(page.label||matrixPageMeta.find(x=>x[0]===pageKey)?.[1]||pageKey)+' · '+esc(product.visible?'前台显示':'前台隐藏')+'</p>'
        +'<code>'+esc(model.actual_model||'实际模型待配置')+'</code>'
        +'<p>支持能力：'+esc((model.capabilities||[]).join(' / ')||'尚未登记')+'</p></div>'
        +'<h3>真实渠道与验证证据</h3><p class="muted">可在线更换的线路会进入加密号池或版本化渠道编辑器；服务器环境变量只允许查看与核对，避免浏览器直接改写部署配置。</p>'+(routes||'<p class="muted">尚无路由。</p>')
        +((model.warnings||[]).length?'<h3>需要处理</h3><div class="cm-matrix-detail-warning">'+esc(model.warnings.join('；'))+'</div>':'');
    }
    function renderMatrix(){
      const host=el('cmMatrix');if(!host)return;
      const pages=matrixPages();
      if(!pages.some(page=>page.page===matrixPage))matrixPage=pages[0]?.page||'image';
      const matrix=pages.find(page=>page.page===matrixPage)||pages[0]||{},summary=matrix.summary||{},allProducts=matrix.products||[];
      const issueCount=allProducts.reduce((total,product)=>total+(product.models||[]).filter(model=>modelNeedsAction(product,model)).length,0);
      const products=allProducts.map(product=>({...product,models:(product.models||[]).filter(model=>matrixShowHidden||(product.visible&&model.visible!==false))})).filter(product=>matrixShowHidden||product.visible).filter(product=>matrixShowHidden||product.models.length);
      const productCards=products.map(product=>{
        const cards=(product.models||[]).map(model=>{
        const routes=model.routes||[];
        const status=modelStatus(product,model);
        const statusView=status.state==='ok'?'<span class="cm-status-dot" title="可接单" aria-label="可接单"></span>':'<span class="cm-matrix-status '+status.state+'">'+esc(status.label)+'</span>';
        return '<button type="button" class="cm-switch-model '+(modelNeedsAction(product,model)?'attention':'')+'" data-cm-model-page="'+esc(matrix.page)+'" data-cm-model-product="'+esc(product.key)+'" data-cm-model-key="'+esc(model.key)+'"><span class="cm-switch-model-head"><b>'+esc(model.label)+'</b>'+statusView+'</span><code>'+esc(model.actual_model||'实际模型待配置')+'</code><span class="cm-switch-fact"><em>主渠道</em><strong>'+esc(compactValue(routes.map(route=>route.primary?.name||'未配置')))+'</strong></span><span class="cm-switch-fact"><em>接入方式</em><strong>'+esc(compactValue(routes.map(route=>route.primary?transportName(route.primary.connection_type):'未标注')))+'</strong></span><span class="cm-switch-fact"><em>备用渠道</em><strong>'+esc(backupSummary(model))+'</strong></span></button>';
        }).join('');
        const state=product.visible?'前台显示':'前台隐藏';
        const hasIssue=(product.models||[]).some(model=>modelNeedsAction(product,model));
        return '<section class="cm-switch-product '+(hasIssue?'attention':'')+'"><div class="cm-switch-product-head"><div><h4>'+esc(product.label)+'</h4><p>'+esc(product.description||product.visibility_reason||'前端产品')+'</p></div><span class="cm-matrix-pill '+(product.visible?'ok':'')+'">'+esc(state)+'</span></div><div class="cm-switch-models">'+(cards||'<p class="cm-matrix-empty">'+esc(product.warning||'当前没有已登记模型。')+'</p>')+'</div></section>';
      }).join('');
      const activeGroup=matrixGroupForPage(matrixPage);matrixGroupSelection[activeGroup.key]=matrixPage;
      const groupStats=group=>{
        const groupPages=group.pages.map(key=>pages.find(page=>page.page===key)).filter(Boolean);
        const modelCount=groupPages.reduce((total,page)=>total+Number(page.summary?.models||0),0);
        const issueCount=groupPages.reduce((total,page)=>total+(page.products||[]).reduce((sum,product)=>sum+(product.models||[]).filter(model=>modelNeedsAction(product,model)).length,0),0);
        return {modelCount,issueCount};
      };
      const sidebar='<div class="cm-business-label">业务板块</div>'+matrixPageGroups.map(group=>{const stats=groupStats(group),active=group.key===activeGroup.key;return '<button type="button" class="cm-business-item '+(active?'active':'')+'" data-cm-matrix-group="'+esc(group.key)+'" aria-current="'+(active?'page':'false')+'"><span><b>'+esc(group.label)+'</b><small>'+stats.modelCount+' 个'+esc(group.unit)+'</small></span>'+(stats.issueCount?'<em>'+stats.issueCount+' 项异常</em>':'')+'</button>'}).join('');
      const groupPages=activeGroup.pages.map(key=>pages.find(page=>page.page===key)).filter(Boolean);
      const subnav=groupPages.length>1?'<nav class="cm-subfunction-tabs" aria-label="'+esc(activeGroup.label)+'功能">'+groupPages.map(page=>'<button type="button" data-cm-matrix-page="'+esc(page.page)+'" aria-pressed="'+String(page.page===matrixPage)+'" class="'+(page.page===matrixPage?'active':'')+'"><span>'+esc(page.label||page.page)+'</span><small>'+Number(page.summary?.models||0)+'</small></button>').join('')+'</nav>':'';
      const mobile=matrixPageGroups.map(group=>'<optgroup label="'+esc(group.label)+'">'+group.pages.map(key=>pages.find(page=>page.page===key)).filter(Boolean).map(page=>'<option value="'+esc(page.page)+'" '+(page.page===matrixPage?'selected':'')+'>'+esc(page.label||page.page)+' · '+Number(page.summary?.models||0)+' 个</option>').join('')+'</optgroup>').join('');
      host.innerHTML='<div class="cm-function-workspace"><aside class="cm-function-sidebar" aria-label="业务板块">'+sidebar+'</aside><div class="cm-function-content"><label class="cm-function-mobile">选择业务功能<select data-cm-matrix-select aria-label="选择业务功能">'+mobile+'</select></label><div class="cm-function-heading"><div><span>'+esc(activeGroup.label)+'</span><h3>'+esc(matrix.label||matrix.page||'前端模型与渠道')+'</h3><p class="muted">点击具体模型，查看并管理渠道、API Key、Base URL 与验证证据。</p></div><div class="actions"><button type="button" data-cm-view="layout">调整前台展示</button><button type="button" class="cm-hidden-toggle '+(matrixShowHidden?'active':'')+'" data-cm-matrix-hidden aria-pressed="'+String(matrixShowHidden)+'">'+(matrixShowHidden?'隐藏停用项':'显示隐藏 / 停用项')+'</button></div></div>'+subnav
        +'<div class="cm-matrix-summary"><div><span>前端产品</span><b>'+Number(summary.products||0)+'</b></div><div><span>模型 / 服务</span><b>'+Number(summary.models||0)+'</b></div><div><span>允许接单</span><b>'+Number(summary.admitted_models||0)+'</b></div><div><span>异常</span><b>'+issueCount+'</b></div></div>'
        +(matrix.precision==='service'?'<p class="cm-precision-note">本板块按已登记的真实前端功能与依赖服务展示；尚未建立独立模型档位的功能会标为“服务配置”。</p>':'')
        +'<div class="cm-switch-products">'+(productCards||'<div class="empty">当前板块没有'+(matrixShowHidden?'已登记':'前台显示')+'的模型或服务。</div>')+'</div></div></div>';
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
      if(b.dataset.cmView){showTab(b.dataset.cmView);return}
      if(b.dataset.cmMatrixGroup){const group=matrixPageGroups.find(item=>item.key===b.dataset.cmMatrixGroup);if(group){matrixPage=matrixGroupSelection[group.key]||group.pages[0];renderMatrix()}return}
      if(b.dataset.cmMatrixPage){matrixPage=b.dataset.cmMatrixPage;renderMatrix();return}
      if(b.dataset.cmMatrixHidden!=null){matrixShowHidden=!matrixShowHidden;renderMatrix();return}
      if(b.dataset.cmModelKey){openMatrixModel(b.dataset.cmModelPage,b.dataset.cmModelProduct,b.dataset.cmModelKey);return}
      if(b.dataset.cmRouteManage){const target=rows.find(c=>c.uid===b.dataset.cmRouteManage);if(target)open(target.uid);else toast('没有找到对应的渠道配置');return}
      if(b.dataset.cmCategory){filters.category=b.dataset.cmCategory;list()}
      if(b.dataset.cmDetail)open(b.dataset.cmDetail);
      if(b.dataset.cmAction)env.lifecycle(rows.find(c=>c.uid===b.dataset.cmUid),b.dataset.cmAction);
      if(b.dataset.cmRefresh)env.refresh();
      if(b.id==='cmDrawerClose')close();
      if(b.dataset.cmMapping)env.mapping(data.mappings.find(m=>m.channel===b.dataset.cmMapping)||{channel:b.dataset.cmMapping,kind:data.adapters[data.items.find(c=>c.id===b.dataset.cmMapping).adapter].kind});
      if(b.dataset.cmJob){close();env.task(b.dataset.cmJob)}
      if(b.dataset.cmJourney){close();env.journey(b.dataset.cmJourney)}
    });
    root.addEventListener('change',e=>{const select=e.target.closest?.('[data-cm-matrix-select]');if(!select)return;matrixPage=select.value;renderMatrix()});
    [['cmSearch','q','input'],['cmSupplier','supplier','change'],['cmTransport','transport','change'],['cmState','status','change'],['cmHistory','history','change']].forEach(([id,key,event])=>el(id).addEventListener(event,()=>{filters[key]=key==='history'?el(id).checked:el(id).value;list()}));
    el('cmDrawer').addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();close()}if(e.key==='Tab'){const nodes=Array.from(el('cmDrawer').querySelectorAll('button,input,select,textarea,a[href]')).filter(n=>!n.disabled&&n.getClientRects().length);if(!nodes.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}});
    return {render,open,close,editor,showTab,setCloseGuard:guard=>{closeGuard=guard}};
  };
})();
