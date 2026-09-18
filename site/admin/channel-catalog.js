/* Pure catalog rules shared by the channel workspace and its regression tests.
   三个维度严格分开：配置（字段/密钥是否配置，绝不泄露明文）、验证（连接/鉴权/完整生成，各自带时间与配置版本）、
   生产（是否接入、旧线路、影子候选、托管主渠道/备用/候补、暂停）。启用 ≠ 验证通过 ≠ 生产已切换。 */
(function(root){
  const VALIDITY_WINDOW = 86400; // 证据有效期 24 小时（秒）
  const CLOCK_SKEW = 60;         // 允许的时间前倾（秒），超过视为时间异常
  const categories=[['all','全部'],['image','图片生成'],['video','视频生成'],['avatar','数字人'],['audio','音频与配音'],['text','文本与助手'],['collect','采集与解析'],['process','视频处理'],['other','其他服务']];

  function classify(text){
    const rules=[['image',/图片|生图|绘图/],['video',/视频生成/],['avatar',/数字人|数字化 IP|数字化IP|形象|口播/],['audio',/音频|配音|语音|声音|音乐/],['text',/文本|文案|助手|语言|提示词/],['collect',/采集|解析|搜索|抓取/],['process',/视频处理|成片|剪辑|合成|字幕|换装|背景/]];
    const found=rules.filter(([,rule])=>rule.test(text||'')).map(([key])=>key);
    return found.length?found:['other'];
  }

  function checkLabel(check,now=Date.now()/1000){
    if(!check)return '未验证';
    if(!check.updated||now-Number(check.updated)>VALIDITY_WINDOW)return '证据已过期';
    return {passed:'通过',failed:'失败',unknown:'结果未知',running:'检测中',queued:'排队中',blocked:'条件未满足'}[check.state]||'未验证';
  }

  const mappingChannels=m=>Array.isArray(m?.channels)?m.channels:[m?.channel,m?.backup].filter(Boolean);

  const num=v=>Number.isFinite(Number(v))?Number(v):null;

  // 单个验证维度（connection/auth/full）的证据解析。返回：state/label/time/version，绝不把旧版本或无法归属的证据当成当前版本通过。
  function resolveCheck(c,kind,now=Date.now()/1000){
    const checks=(c.checks||[]).filter(r=>r&&r.kind===kind);
    const base={kind,state:'missing',label:'未验证',time:null,version:null,missing:true};
    if(!checks.length)return base;
    // 关键：不凭空假设 checks 携带 version。若没有任何一条带 version，无法确认它属于当前配置版本，不能判通过、也不能当普通“未验证”。
    const versioned=checks.filter(r=>num(r.version)!==null);
    if(!versioned.length){
      return {kind,state:'unattributed',label:'验证记录未标注配置版本，无法确认属于当前配置',time:null,version:null,unattributed:true};
    }
    const currentVersion=num(c.version);
    const current=checks.filter(r=>num(r.version)===currentVersion).sort((a,b)=>(num(b.updated)||0)-(num(a.updated)||0));
    if(!current.length){
      const newestOther=checks.slice().sort((a,b)=>(num(b.updated)||0)-(num(a.updated)||0))[0];
      return {kind,state:'stale-version',label:'当前版本待验证（仅有历史版本记录）',time:num(newestOther.updated),version:num(newestOther.version),staleVersion:true};
    }
    const latest=current[0],updated=num(latest.updated),version=num(latest.version)!=null?num(latest.version):currentVersion;
    if(updated==null||now-updated>VALIDITY_WINDOW||updated>now+CLOCK_SKEW){
      return {kind,state:'expired',label:'证据已过期或时间异常',time:updated,version,expired:true,rawState:latest.state};
    }
    const map={passed:{state:'ok',label:'通过'},failed:{state:'failed',label:'失败'},unknown:{state:'unknown',label:'结果未知'},running:{state:'running',label:'执行中'},queued:{state:'queued',label:'排队中'},blocked:{state:'blocked',label:'条件未满足'},terminated:{state:'unknown',label:'已终止'},captured:{state:'unknown',label:'已记录'}};
    const m=map[latest.state]||{state:'unknown',label:'结果未知（'+String(latest.state)+'）'};
    return {kind,...m,time:updated,version,rawState:latest.state};
  }

  // 三合一验证结论。较新的异常必须覆盖旧的通过记录；“完整测试通过”绝不等于“用户已收到成品”。
  function verificationStatus(c,now=Date.now()/1000){
    const parts={connection:resolveCheck(c,'connection',now),auth:resolveCheck(c,'auth',now),full:resolveCheck(c,'full',now)};
    const all=[parts.connection,parts.auth,parts.full];
    const okAll=all.every(p=>p.state==='ok');
    const currentChecks=(c.checks||[]).filter(r=>num(r.version)!=null&&num(r.version)===num(c.version));
    const newest=currentChecks.slice().sort((a,b)=>(num(b.updated)||0)-(num(a.updated)||0))[0];
    let overall;
    if(okAll&&(!newest||newest.state==='passed')){
      overall={state:'ok',label:'当前版本验证通过'};
    }else if(okAll&&newest&&['failed','unknown'].includes(newest.state)){
      overall={state:'attention',label:'存在较新异常：'+(newest.state==='failed'?'失败':'结果未知')};
    }else{
      const order=['failed','unknown','blocked','expired','unattributed','stale-version','running','queued','missing'];
      const worst=order.map(s=>all.find(p=>p.state===s)).find(Boolean);
      if(worst)overall={state:worst.state,label:worst.label,kind:worst.kind};
      else overall={state:'neutral',label:'待验证'};
    }
    return {overall,parts,version:c.version};
  }

  // 配置维度：必要字段是否完整；密钥只给“已配置/未配置/未知”，绝不出现明文。
  function configStatus(c){
    const required=[['name','渠道名称'],['supplier','供应商'],['adapter','协议适配器'],['base_url','API 基础地址'],['model','实际模型 ID']];
    const missing=required.filter(([k])=>!c[k]||!String(c[k]).trim()).map(([,label])=>label);
    let key;
    if(typeof c.secret_configured==='boolean')key=c.secret_configured;
    else if(typeof c.has_secret==='boolean')key=c.has_secret;
    else if(c.secret!==undefined&&c.secret!==null&&String(c.secret)!=='')key=true;
    else if(c.credential!==undefined&&c.credential!==null)key=true;
    else key=null;
    const keyState=key===true?'configured':key===false?'missing':'unknown';
    const details=[...missing];
    if(keyState==='missing')details.push('密钥未配置');
    if(keyState==='unknown')details.push('密钥配置状态未知');
    const complete=!missing.length;
    const state=complete&&keyState==='configured'?'ok':complete?'neutral':'warn';
    return {complete,missing,key:keyState,state,label:complete?'配置完整':'缺字段',details};
  }

  // 生产维度：渠道在哪些功能映射里处于什么角色。旧 kind/front 兼容映射单独说明，不能混进稳定映射。
  function productionRoles(c,data){
    const opMappings=data.operation_mappings||[];
    const legacyMappings=data.mappings||[];
    const id=String(c.id);
    const rankOf=m=>mappingChannels(m).map(String).indexOf(id);
    const opEntries=opMappings.filter(m=>rankOf(m)>=0).map(m=>{
      const rank=rankOf(m),state=m.state;
      let role,tone;
      if(state==='managed'){role=rank===0?'生产主渠道':rank===1?'备用渠道':'生产候补 #'+(rank+1);tone=rank===0?'ok':'neutral';}
      else if(state==='shadow'){role='影子候选（不改生产路由）';tone='neutral';}
      else if(state==='paused'){role='功能已暂停';tone='muted';}
      else{role='仍走旧线路';tone='muted';}
      return {type:'operation',operationId:m.operation_id,label:m.label||m.operation_id,state,role,tone,rank,revision:Number(m.revision||0),channels:mappingChannels(m).map(String),published:true};
    });
    const legacyEntries=legacyMappings.filter(m=>rankOf(m)>=0).map(m=>{
      const rank=rankOf(m);
      return {type:'legacy',operationId:m.kind+':'+m.front,label:(m.kind+' / '+m.front)||m.label||'旧映射',state:'legacy',role:'兼容期旧映射 · '+(rank===0?'主渠道':'候补'),tone:'muted',rank,revision:0,channels:mappingChannels(m).map(String),published:true};
    });
    const entries=[...opEntries,...legacyEntries];
    const integrated=entries.length>0;
    return {
      integrated,entries,
      managed:entries.filter(e=>e.state==='managed'),shadow:entries.filter(e=>e.state==='shadow'),
      paused:entries.filter(e=>e.state==='paused'),
      summary:integrated?entries.map(e=>e.label+'：'+e.role+' · r'+e.revision).join('；'):'未接入稳定生产路由（兼容期旧映射需另核对）'
    };
  }

  function managedProof(c,now=Date.now()/1000){
    const v=verificationStatus(c,now).overall;
    return {state:v.state==='ok'?'ok':'off',label:v.label};
  }

  function catalog(data,legacy){
    const now=Date.now()/1000;
    return (legacy||[]).map(c=>({...c,uid:'legacy:'+c.key,source:'legacy',supplier:c.name,
      categories:classify(c.category),enabled:c.accepts_new_jobs!==false&&data.legacy_controls?.[c.key]?.enabled!==false,retired:c.accepts_new_jobs===false||data.legacy_controls?.[c.key]?.enabled===false,
      scope:data.legacy_scopes?.[c.key],version:data.legacy_controls?.[c.key]?.revision||0,control:data.legacy_controls?.[c.key],
      connection_type:'unknown',health:c.evidence?.label||'未验证',attention:['fail','warn','neutral'].includes(c.evidence?.state)||!c.evidence,
      checks:[],model:c.model||'按功能配置',
      _config:{complete:true,key:'unknown',state:'neutral',label:'内置线路',details:[]},
      _verification:{overall:{state:'neutral',label:'按关联功能检查'},parts:{}},
      _production:{integrated:false,entries:[],summary:'按关联功能检查'}})).concat((data.items||[]).map(c=>({...c,uid:'managed:'+c.id,source:'managed',
        supplier:c.supplier||'未标注供应商',connection_type:c.connection_type||'unknown',
        categories:[data.adapters?.[c.adapter]?.kind==='image'?'image':'video'],retired:!c.enabled,deleted:!!c._lifecycle?.deleted,
        attention:verificationStatus(c,now).overall.state!=='ok'||!configStatus(c).complete,
        features:[...(data.mappings||[]),...(data.operation_mappings||[])]
          .filter(m=>mappingChannels(m).includes(c.id)).map(m=>m.label||m.operation_id||m.front),
        _config:configStatus(c),_verification:verificationStatus(c,now),_production:productionRoles(c,data)})));
  }

  function filter(rows,f){return rows.filter(c=>(f.status==='deleted'?c.deleted:!c.deleted)&&(f.history||['disabled','deleted'].includes(f.status)||!c.retired)&&(f.category==='all'||c.categories.includes(f.category))&&(!f.supplier||c.supplier===f.supplier)&&(!f.transport||c.connection_type===f.transport)&&(!f.status||f.status==='deleted'||(f.status==='enabled'?c.enabled:f.status==='disabled'?!c.enabled:c.attention))&&(!f.q||[c.name,c.supplier,c.model,...(c.features||[])].join(' ').toLowerCase().includes(f.q.toLowerCase())))}

  function compatible(data,kind){return (data.items||[]).filter(c=>!c._lifecycle?.deleted&&data.adapters?.[c.adapter]?.kind===kind)}

  root.ChannelCatalog={categories,classify,checkLabel,catalog,filter,compatible,mappingChannels,managedProof,productionRoles,verificationStatus,configStatus,resolveCheck,VALIDITY_WINDOW};
})(typeof window==='undefined'?globalThis:window);
