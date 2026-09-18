/* 环境变量型线路的「后台 URL／Key」编辑（试点闭环的前端入口）。
   只做编辑/验证/发布/回滚，不伪造后端能力；生效状态一律以后端返回为准。 */
(function(){
  window.initChannelProviderConfig=function(env){
    const {api,esc,el,toast}=env;
    let items=[], openTarget=null, busy=false;
    const dialogId='cmProviderConfigDialog';
    const stateLabel={draft_only:'草稿已保存',verified:'验证通过（未发布）',publishing:'正在生效',effective:'配置已生效',unconfirmed:'结果待核对',failed:'发布失败',env:'使用环境变量'};
    const opId=()=>'pc-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10);
    const post=(action,body)=>api('/api/admin/provider-config/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});

    function render(){
      const host=el('cmProviderConfig');if(!host)return;
      host.innerHTML=table(items);
      const retry=host.querySelector('[data-pc-reload]');if(retry)retry.onclick=load;
    }
    function table(rows){
      if(!rows.length)return '<p class="muted">暂无可后台管理的线路。</p>';
      const head='<tr><th>功能线路</th><th>供应商</th><th>当前 URL</th><th>密钥</th><th>来源 / 版本</th><th>生效状态</th><th>操作</th></tr>';
      const body=rows.map(it=>{
        const eff=it.effective||{};
        const key=it.key_present?('••••'+esc(it.key_last4||'')):'未配置';
        const src=it.source==='backend'?('后台配置 v'+esc(it.version)):'服务器环境变量';
        const warn=it.pool_shared?'<small style="display:block;color:#e6c77b">该环境变量同时被号池使用，影响范围见弹窗</small>':'';
        const ops=it.editable
          ?'<button type="button" data-pc-edit="'+esc(it.target_id)+'">修改 URL／Key</button>'
          :'<button type="button" disabled title="'+esc(it.deprecated_reason||'不可修改')+'">不可修改</button>';
        return '<tr data-pc-target="'+esc(it.target_id)+'">'
          +'<td><b>'+esc((it.features||[]).join(' / ')||it.target_id)+'</b>'+warn+'</td>'
          +'<td>'+esc(it.provider||'')+'</td>'
          +'<td><code>'+esc(it.url||'未配置')+'</code></td>'
          +'<td>'+key+'</td>'
          +'<td>'+src+'<small style="display:block">'+esc(stateLabel[eff.state]||'')+'</small></td>'
          +'<td>'+esc(eff.label||'')+(eff.not_loaded_instances&&eff.not_loaded_instances.length?'<small style="display:block">未刷新实例 '+eff.not_loaded_instances.length+' 个</small>':'')+'</td>'
          +'<td><div class="cm-row-actions">'+ops+'</div></td></tr>';
      }).join('');
      return '<div style="overflow:auto"><table><thead>'+head+'</thead><tbody>'+body+'</tbody></table></div>'
        +'<p class="muted" style="margin-top:8px">「配置已生效」来自运行服务的实际上报；仅保存或发布成功不会显示为已生效。</p>'
        +'<button type="button" data-pc-reload>刷新</button>';
    }

    async function load(){
      const host=el('cmProviderConfig');if(host)host.innerHTML='<p class="muted">正在读取线路配置…</p>';
      try{
        const d=await api('/api/admin/provider-config');
        items=(d&&d.items)||[];render();
      }catch(e){
        if(host)host.innerHTML='<p class="muted">读取失败：'+esc(e.message)+'</p><button type="button" data-pc-reload>重试</button>';
        const retry=host&&host.querySelector('[data-pc-reload]');if(retry)retry.onclick=load;
      }
    }

    function find(target){return items.find(i=>i.target_id===target)||{}}

    function open(target){
      if(!find(target).editable){toast('这条线路当前不可修改');return}
      close();
      openTarget=target;
      const it=find(target);
      const dlg=document.createElement('dialog');
      dlg.id=dialogId;dlg.className='cm-lifecycle-dialog';
      const poolWarn=it.pool_shared
        ?'<p class="cm-operation-error" role="alert" style="color:#e6c77b">注意：该环境变量同时是号池的运行兜底/快照来源。修改后受影响的功能：'
          +esc((it.features||[]).join('、'))+'；视频侧号池已有快照不会随之变化，需单独核对。</p>'
        :'';
      dlg.innerHTML='<form>'
        +'<h3>修改 API URL／Key · '+esc((it.features||[]).join(' / ')||target)+'</h3>'
        +'<p class="muted">供应商：'+esc(it.provider||'')+' · 当前来源：'+(it.source==='backend'?'后台配置 v'+esc(it.version):'服务器环境变量')+'</p>'
        +'<label>API URL<input class="field" name="url" value="'+esc(it.url||'')+'" spellcheck="false"></label>'
        +'<p class="muted" style="font-size:11px">默认地址：'+esc(it.url_default||'（无）')+'；必须 HTTPS，且域名在服务器允许名单内。</p>'
        +'<label>API Key（留空表示保留当前值，不回显旧密钥）<input class="field" name="secret" type="password" autocomplete="new-password" placeholder="不填则保留已保存密钥"></label>'
        +'<p><b>影响范围</b>：'+esc((it.features||[]).join('、')||'未知')+'</p>'
        +poolWarn
        +'<p id="pcResult" class="muted" role="status">尚未验证。</p>'
        +'<p class="cm-operation-error" role="alert"></p>'
        +'<div class="actions"><button type="button" data-pc-cancel>取消</button>'
        +'<button type="button" data-pc-validate>验证配置</button>'
        +'<button type="button" class="primary" data-pc-publish disabled>确认启用</button>'
        +'<button type="button" data-pc-rollback>回滚到上一版本</button></div>'
        +'</form>';
      document.body.appendChild(dlg);
      dlg.querySelector('[data-pc-cancel]').onclick=close;
      dlg.addEventListener('cancel',()=>{if(busy)event.preventDefault()});
      dlg.onclose=close;
      const form=dlg.querySelector('form');
      const err=dlg.querySelector('[role=alert]');
      // 验证后修改任一输入 → 旧验证立即失效
      const invalidate=()=>{openTarget=target;form.dataset.seq='';form.dataset.ok='';
        dlg.querySelector('[data-pc-publish]').disabled=true;
        dlg.querySelector('#pcResult').textContent='输入已修改，请重新验证。'};
      form.elements.url.oninput=invalidate;
      form.elements.secret.oninput=invalidate;
      const setBusy=v=>{busy=v;[...form.querySelectorAll('button')].forEach(b=>{if(b.dataset.pcCancel===undefined)b.disabled=v})};
      dlg.querySelector('[data-pc-validate]').onclick=async()=>{
        if(busy)return;err.textContent='';setBusy(true);
        try{
          const draft=await post('draft',{target_id:target,url:String(form.elements.url.value||'').trim(),
            secret:String(form.elements.secret.value||'').trim()||undefined,reason:'后台修改 URL/Key'});
          const res=await post('validate',{target_id:target,version:draft.seq});
          form.dataset.seq=String(draft.seq);form.dataset.ok=res.ok?'1':'';
          const c=res.checks||{},names={connection:'连接',auth:'鉴权'},
            line=k=>names[k]+':'+((c[k]&&c[k].ok)?'通过':'未通过');
          dlg.querySelector('#pcResult').textContent='草稿 v'+draft.seq+' · '+line('connection')+' · '+line('auth')
            +'（仅连接与鉴权，不含生成成功率）';
          dlg.querySelector('[data-pc-publish]').disabled=!res.ok;
          if(!res.ok)toast('验证未通过，禁止上线');
        }catch(e){err.textContent=e.message}
        finally{setBusy(false)}
      };
      dlg.querySelector('[data-pc-publish]').onclick=async()=>{
        if(busy)return;const seq=form.dataset.seq;
        if(!seq||!form.dataset.ok){err.textContent='请先验证通过再启用';return}
        err.textContent='';setBusy(true);
        try{
          await post('publish',{target_id:target,version:Number(seq),
            expected_version:find(target).version===null?0:find(target).version,op_id:opId()});
          toast('已发布；生效状态以服务上报为准');
          close();await load();
        }catch(e){err.textContent=e.message}
        finally{setBusy(false)}
      };
      dlg.querySelector('[data-pc-rollback]').onclick=async()=>{
        if(busy)return;if(!confirm('回滚到上一可用版本？只影响之后创建的新任务。'))return;
        err.textContent='';setBusy(true);
        try{
          await post('rollback',{target_id:target,
            expected_version:find(target).version===null?0:find(target).version,op_id:opId()});
          toast('已回滚');close();await load();
        }catch(e){err.textContent=e.message}
        finally{setBusy(false)}
      };
      dlg.showModal();
    }

    function close(){
      const dlg=el(dialogId);
      if(dlg&&dlg.open)dlg.close();
      if(dlg&&dlg.parentNode)dlg.parentNode.removeChild(dlg);
      openTarget=null;
    }

    document.addEventListener('click',e=>{
      const t=e.target.closest&&e.target.closest('[data-pc-edit]');
      if(t){open(t.getAttribute('data-pc-edit'));return}
      const r=e.target.closest&&e.target.closest('[data-pc-reload]');
      if(r){load()}
    });
    return {load,render,close};
  };
})();
