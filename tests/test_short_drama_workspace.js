const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const workspace = require(path.join(ROOT, 'site/workbench/short-drama-workspace.js'));
const workspaceSource = fs.readFileSync(
  path.join(ROOT, 'site/workbench/short-drama-workspace.js'), 'utf8'
);
const html = fs.readFileSync(path.join(ROOT, 'site/workbench/short-drama.html'), 'utf8');
const stamp = fs.readFileSync(path.join(ROOT, 'scripts/stamp_assets.py'), 'utf8');

const workspaceStyle = fs.readFileSync(
  path.join(ROOT, 'site/workbench/short-drama-workspace.css'), 'utf8'
);

test('独立页面加载三栏对话工作区资源', () => {
  assert.match(html, /id="shortDramaWorkspace"/);
  assert.match(html, /short-drama-workspace\.css\?v=/);
  assert.match(html, /short-drama-workspace\.js\?v=/);
  assert.match(stamp, /Asset\("short-drama-workspace\.js"/);
  assert.match(stamp, /Asset\("short-drama-workspace\.css"/);
});

test('镜头编辑允许填写更完整的运镜和连续性要求', () => {
  assert.match(workspaceSource, /机位与运镜<textarea name="camera" maxlength="300"/);
  assert.match(workspaceSource, /连续性要求<textarea name="continuity" maxlength="360"/);
});

test('镜头编辑提供单一声音设计区域并独立保存', () => {
  assert.match(workspaceSource, /声音设计<textarea name="sound_design" maxlength="600"/);
  assert.match(workspaceSource, /环境声、动作音效、音乐、声音转场/);
  assert.match(workspaceSource, /sound_design:values\.sound_design/);
  assert.match(workspaceSource, /sound_design:text\(fields\.sound_design/);
});

test('执行编辑器把最终提示词改为可选补充而不覆盖结构化字段', () => {
  assert.match(workspaceSource, /补充生成要求（可选）/);
  assert.match(workspaceSource, /结构化设置会始终加入最终提示词/);
  assert.doesNotMatch(
    workspaceSource,
    /name="provider_prompt" required maxlength="1600"/
  );
  assert.match(
    workspaceSource,
    /hasOwnProperty\.call\(saved,key\)\?saved\[key\]/
  );
  assert.match(
    workspaceSource,
    /value\('provider_prompt',''\)/
  );
  assert.doesNotMatch(
    workspaceSource,
    /value\('provider_prompt',shot\.provider_prompt\)/
  );
  assert.match(
    workspaceSource,
    /execution\.prompt_semantics='structured-supplement-v1'/
  );
});

test('生成执行编辑器显示并提交声音设计', () => {
  const soundDesignFields = workspaceSource.match(
    /声音设计<textarea name="sound_design" maxlength="600"/g
  ) || [];
  assert.equal(soundDesignFields.length, 2);
  assert.match(
    workspaceSource,
    /\['visual','camera','performance','scene','lighting','composition_style','continuity','sound_design','negative_prompt','provider_prompt'\]/
  );
  assert.match(
    workspaceSource,
    /\['visual','camera','performance','scene','lighting','composition_style','continuity','sound_design','negative_prompt','provider_prompt'\]\.forEach\(function\(fieldName\)/
  );
});

test('剧本确认后正式项目切换为两栏并将聊天收进只读创作记录', () => {
  const css = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.css'), 'utf8'
  );
  assert.match(workspaceSource, /project-ready/);
  assert.match(workspaceSource, /历史创作记录（只读）/);
  assert.match(workspaceSource, /创作记录/);
  assert.match(css, /\.sd-workspace-grid\.project-ready\{grid-template-columns:/);
  assert.match(css, /\.project-ready>.sd-chat\{display:none\}/);
  assert.match(css, /\.project-ready\.history-open>.sd-chat/);
});

test('project workspace uses immersive shell and a collapsible summary panel', () => {
  assert.match(workspaceSource, /data-action="toggle-inspector"/);
  assert.match(workspaceSource, /inspector-collapsed/);
  assert.match(workspaceSource, /inspectorExpanded/);
  assert.match(workspaceStyle, /html\.short-drama-immersive #hqSideNav/);
  assert.match(workspaceStyle, /html\.short-drama-immersive \.hq-main-scroll/);
  assert.match(workspaceStyle, /\.short-drama-center\.workspace-mode\{[^}]*height:100dvh/);
  assert.match(workspaceStyle, /\.sd-workspace-grid\.project-ready\.inspector-collapsed/);
  assert.match(workspaceSource, /项目概况/);
  assert.match(workspaceSource, /当前步骤/);
  assert.match(workspaceSource, /故事摘要/);
  assert.match(workspaceSource, /id="sdStorySummary"/);
  assert.match(workspaceSource, /展开故事详情/);
  assert.match(workspaceStyle, /-webkit-line-clamp:4/);
  assert.match(workspaceStyle, /minmax\(280px,310px\)/);
});

test('direction-confirmed workspace hides chat and offers first-script generation', () => {
  assert.match(workspaceSource, /messageForm\.hidden=projectReady/);
  assert.match(workspaceSource, /生成第一版完整剧本/);
  assert.match(workspaceSource, /生成前，请检查这三项内容/);
  assert.match(workspaceSource, /querySelectorAll\('\[data-action="generate"\]'\)/);
  assert.match(workspaceStyle, /\.sd-project-review/);
});

test('workspace mode renderer keeps unconfirmed phases editable and only archives authoritative ready states', () => {
  function mountedConversationDom() {
    const nodes = {};
    function node() {
      const classes = new Set();
      return {
        hidden:false, disabled:false, textContent:'', attributes:{},
        classList:{
          toggle:(name, enabled) => enabled ? classes.add(name) : classes.delete(name),
          contains:name => classes.has(name),
        },
        setAttribute(name, value) { this.attributes[name] = value; },
      };
    }
    for (const id of [
      'sdWorkspaceGrid','sdChatToggle','sdHistoryButton','sdMessageForm',
      'sdChatTitle','sdChatDescription',
    ]) nodes[id] = node();
    const textarea = node(), button = node();
    return {
      doc:{getElementById:id => nodes[id]},
      root:{querySelector:selector => selector.endsWith('textarea') ? textarea : button},
      nodes, textarea, button,
    };
  }
  function state(phase, changes) {
    return Object.assign({
      conversation:{state:'direction_review', current_version_id:null,
        understanding:{phase, direction_confirmed:false}},
      current_script:null,
      permissions:{can_edit:true},
    }, changes||{});
  }
  for (const phase of ['discovering','recommending','refining','import_review','direction_ready']) {
    const mounted = mountedConversationDom();
    const mode = workspace.conversationWorkspaceMode(state(phase));
    workspace.applyConversationMode(mounted.doc, mounted.root, mode, false);
    assert.equal(mode.projectReady, false, phase);
    assert.equal(mode.canMessage, true, phase);
    assert.equal(mounted.nodes.sdMessageForm.hidden, false, phase);
    assert.equal(mounted.textarea.disabled, false, phase);
  }
  const archived = [
    state('direction_ready', {conversation:{state:'direction_review',current_version_id:null,
      understanding:{phase:'direction_ready',direction_confirmed:true}}}),
    state('refining', {conversation:{state:'direction_review',current_version_id:'script-1',
      understanding:{phase:'refining',direction_confirmed:false}}}),
    state('refining', {conversation:{state:'script_locked',current_version_id:'script-1',
      understanding:{phase:'refining',direction_confirmed:false}}}),
    state('import_review', {permissions:{can_edit:false}}),
  ];
  for (const current of archived) {
    const mounted = mountedConversationDom();
    const mode = workspace.conversationWorkspaceMode(current);
    workspace.applyConversationMode(mounted.doc, mounted.root, mode, false);
    assert.equal(mode.canMessage, false);
    assert.equal(mounted.nodes.sdMessageForm.hidden, true);
    assert.equal(mounted.textarea.disabled, true);
  }
});

test('read-only creation history removes actions and the event path has a defensive write gate', () => {
  const item = {role:'assistant', content:'Choose one', metadata:{
    recommendations:[{title:'Direction A',hook:'Hook',summary:'Summary'}],
    quick_replies:['Confirm direction'],
  }};
  assert.match(workspace.messageHtml(item, false), /data-action="quick-reply"/);
  assert.doesNotMatch(workspace.messageHtml(item, true), /data-action="quick-reply"/);
  assert.match(workspaceSource, /if\(!conversationWorkspaceMode\(state\)\.canMessage\)return Promise\.resolve\(state\)/);
  assert.match(workspaceSource, /if\(!conversationWorkspaceMode\(state\)\.canMessage\)return;/);
});

test('项目概要展示确认门禁、修改后重确认和结构化理解摘要', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.js'), 'utf8'
  );
  const css = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.css'), 'utf8'
  );
  assert.match(source, /确认项目内容/);
  assert.match(source, /内容修改后需要重新确认/);
  assert.doesNotMatch(source, /助手会先理解想法、给出建议并与你确认/);
  assert.match(source, /understanding\.direction_confirmed/);
  assert.match(source, /项目概况/);
  assert.match(source, /当前步骤/);
  assert.match(source, /故事摘要/);
  assert.match(source, /id="sdOverviewTitle"/);
  assert.match(source, /id="sdTechnicalContract"/);
  assert.match(source, /用户补充/);
  assert.match(source, /refining:'修改后待确认'/);
  assert.match(css, /\.sd-direction-gate\.pending/);
  assert.match(css, /\.sd-advisor-state\.refining/);
});

test('创作助手请求期间显示可恢复的思考状态', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-center.js'), 'utf8'
  );
  const css = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-center.css'), 'utf8'
  );
  assert.match(source, /正在思考，请稍候/);
  assert.match(source, /还在认真整理你的想法，请再稍候/);
  assert.match(source, /setAttribute\('aria-busy',advisorBusy\?'true':'false'\)/);
  assert.match(source, /removeAdvisorThinkingIndicator\(\)/);
  assert.match(source, /advisorSubmit\.textContent=advisorBusy\?'思考中…'/);
  assert.match(css, /\.short-drama-chat-bubble\.thinking/);
  assert.match(css, /@keyframes short-drama-thinking-pulse/);
  assert.match(css, /prefers-reduced-motion:reduce/);
});

test('advisor stores confirmation, recap, and follow-up as one multiline turn', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-center.js'), 'utf8'
  );
  const css = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-center.css'), 'utf8'
  );
  assert.match(source, /function plannerAssistantTurn\(parts\)/);
  assert.match(source, /messages\.join\('\\n\\n'\)/);
  assert.match(source, /assistantParts\.push\(reply\.message\)/);
  assert.match(source, /chatBubble\('assistant',plannerAssistantTurn\(assistantParts\)\)/);
  assert.match(css, /white-space:pre-line/);
});

test('创作助手每轮提供至多三个方向并让用户修改后再发送', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-center.js'), 'utf8'
  );
  assert.match(source, /function plannerGuidedQuestion\(field,question,items,understanding,fillDefaults\)/);
  assert.match(source, /你更倾向哪个方向？也可以直接说说自己的想法。/);
  assert.match(source, /choices\.length<3/);
  assert.match(source, /visible\.length<3/);
  assert.match(source, /title="填入输入框，修改后再发送"/);
  assert.match(source, /ideaInput\.value=node\.getAttribute\('data-idea-reply'\)/);
  assert.doesNotMatch(source, /if\(node\)submitIdea\(node\.getAttribute\('data-idea-reply'\)\)/);
});

test('导入原稿展示模式化理解快照与待确认优化边界', () => {
  const faithful = workspace.importContractHtml({
    source_hash:'abc123', import_mode:'faithful',
    revision:2, contract_hash:'contract-abc',
    characters:['林夏','周明'],
    plot_points:[
      {position:'start',excerpt:'雨夜车站相遇'},
      {position:'middle',excerpt:'录音揭开误会'},
      {position:'end',excerpt:'清晨重新出发'}
    ],
    key_dialogues:[{speaker:'林夏',text:'别走。'}],
    proposed_changes:[],
    required_preservations:[{
      kind:'dialogue', source:'真相在这里。', source_offset:32
    }]
  });
  assert.match(faithful, /原稿理解快照/);
  assert.match(faithful, /尊重原稿/);
  assert.match(faithful, /开场/);
  assert.match(faithful, /真相在这里|别走/);
  assert.doesNotMatch(faithful, /contract-abc/);
  assert.match(faithful, /用户追加的必须保留内容/);
  assert.match(faithful, /原稿位置 32/);
  const technical = workspace.importContractTechnicalHtml({
    source_hash:'abc123', import_mode:'faithful',
    revision:2, contract_hash:'contract-abc'
  });
  assert.match(technical, /第 2 版/);
  assert.match(technical, /contract-abc/);
  assert.match(technical, /abc123/);
  const optimize = workspace.importContractHtml({
    source_hash:'def456', import_mode:'optimize', characters:['林夏'],
    proposed_changes:[
      {label:'结构与节奏', summary:'只调整结构', status:'confirmed'},
      {label:'对白精炼', summary:'保留事实并压缩重复表达', status:'denied'}
    ]
  });
  assert.match(optimize, /AI 协助优化/);
  assert.match(optimize, /对白精炼/);
  assert.match(optimize, /已排除/);
  const source = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.js'), 'utf8'
  );
  assert.match(source, /import_review:'原稿内容待确认'/);
  assert.match(source, /请核对核心故事、角色和分镜要求/);
});

test('客户端使用 Cookie 会话、独立接口和幂等键', async () => {
  const calls = [];
  const previousStorage = global.localStorage;
  const storage = new Map();
  const storageMock = {
    getItem:key => storage.has(key) ? storage.get(key) : null,
    setItem:(key,value) => storage.set(key,String(value)),
    removeItem:key => storage.delete(key),
    key:index => Array.from(storage.keys())[index]||null
  };
  Object.defineProperty(storageMock,'length',{get:() => storage.size});
  global.localStorage = storageMock;
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>'{}'};
  };
  const client = workspace.createClient(fetchImpl);
  try {
  await client.workspace('project a');
  await client.message({project_id:'a', conversation_revision:1, message:'你好'});
  await client.generate({project_id:'a', conversation_revision:2});
  await client.restore({project_id:'a', conversation_revision:3, version_id:'v1'});
  await client.lock({project_id:'a', conversation_revision:4, version_id:'v2'});
  await client.characterStudio('project a');
  await client.saveCharacterProfile({project_id:'a', project_revision:1, character_key:'lead', identity_text:'记者', personality:'敏锐', appearance_prompt:'短发', wardrobe_prompt:'风衣'});
  await client.bindCharacterAvatar({project_id:'a', project_revision:2, character_key:'lead', avatar_id:'7'});
  await client.generateCharacterImage({project_id:'a', revision:3, character_key:'lead'},'alice');
  await client.preflight('project a');
  await client.prepare({project_id:'a', conversation_revision:5, quality_route:'quick_draft'});
  await client.confirmPlan({project_id:'a', plan_id:'p1', plan_version:1, accepted_issue_keys:[]});
  await client.autodraft('project a');
  await client.providerPreflight({project_id:'a', plan_id:'p1', shot_key:'shot_01', avatar_id:'avatar-1'});
  await client.providerQuote({project_id:'a', plan_id:'p1', shot_key:'shot_01', avatar_id:'avatar-1'});
  await client.selectProviderVersion({project_id:'a',shot_key:'shot_01',version_id:'v1'});
  await client.startProviderJob({quote_token:'quote-1'});
  await client.providerJob('project a','provider/1');
  await client.startDraft({project_id:'a', plan_id:'p1'});
  await client.draftJob('project a','job/1');
  await client.createProject({title:'新版本'});
  assert.equal(calls[0].url, '/api/gen/short-drama/conversation?project_id=project%20a');
  assert.equal(calls[5].url, '/api/gen/short-drama/character-studio?project_id=project%20a');
  assert.equal(calls[6].url, '/api/gen/short-drama/character-studio/profile');
  assert.equal(calls[7].url, '/api/gen/short-drama/character-studio/bind-avatar');
  assert.equal(calls[8].url, '/api/gen/short-drama/generate-character-reference');
  assert.equal(calls[9].url, '/api/gen/short-drama/preflight?project_id=project%20a');
  assert.equal(calls[12].url, '/api/gen/short-drama/autodraft?project_id=project%20a');
  assert.equal(calls[13].url, '/api/gen/short-drama/autodraft/provider-preflight');
  assert.equal(calls[14].url, '/api/gen/short-drama/autodraft/provider-quote');
  assert.equal(calls[15].url, '/api/gen/short-drama/autodraft/provider-version/select');
  assert.equal(calls[16].url, '/api/gen/short-drama/autodraft/provider-jobs');
  assert.equal(calls[17].url, '/api/gen/short-drama/autodraft/provider-jobs/provider%2F1?project_id=project%20a');
  assert.equal(calls[19].url, '/api/gen/short-drama/autodraft/jobs/job%2F1?project_id=project%20a');
  assert.equal(calls[20].url, '/api/gen/short-drama/projects');
  for (const call of calls) {
    assert.equal(call.options.credentials, 'same-origin');
    assert.equal(call.options.headers.Authorization, 'Bearer __cookie__');
    assert.equal(Object.hasOwn(call.options.headers, 'X-Canvas-Board-Id'), false);
  }
  for (const call of calls.filter(call => call.options.method === 'POST')) {
    assert.ok(call.options.headers['Idempotency-Key']);
  }
  } finally {
    global.localStorage = previousStorage;
  }
});

test('点数不足响应保留所需点数并生成明确的视频提交提示', async () => {
  const client = workspace.createClient(async () => ({
    ok:false,
    status:402,
    text:async () => JSON.stringify({
      detail:'点数不足',
      code:'charge_rejected',
      need:42,
    }),
  }));
  await assert.rejects(
    client.startProviderJob({project_id:'project a', quote_token:'quote-1'}),
    error => {
      assert.equal(error.status, 402);
      assert.equal(error.need, 42);
      assert.equal(
        workspace.providerStartFailureMessage(error, 36),
        '点数不足，本次需要 42 点，请充值后再试'
      );
      return true;
    }
  );
});

test('Avatar response loss replays the immutable request across project revisions', async () => {
  const calls = [];
  const storage = new Map();
  const previousStorage = global.localStorage;
  const storageMock = {
    getItem:key => storage.has(key) ? storage.get(key) : null,
    setItem:(key,value) => storage.set(key,String(value)),
    removeItem:key => storage.delete(key),
    key:index => Array.from(storage.keys())[index]||null
  };
  Object.defineProperty(storageMock,'length',{get:() => storage.size});
  global.localStorage = storageMock;
  let avatarPosts = 0;
  const client = workspace.createClient(async (url, options) => {
    calls.push({url, options});
    if (url === '/api/gen/avatar' && avatarPosts++ === 0) throw new Error('response lost');
    const response = url.startsWith('/api/gen/job/') ? {status:'done'} : {job_id:7};
    return {ok:true, status:200, text:async () => JSON.stringify(response)};
  });
  const payload = {
    image_data:'data:image/jpeg;base64,AA==', name:'林雨',
    short_drama_binding:{project_id:'p1',project_revision:4,character_key:'lead'}
  };
  try {
    await assert.rejects(client.createAvatar(payload,'alice'), /response lost/);
    await client.createAvatar(Object.assign({}, payload, {
      short_drama_binding:Object.assign({}, payload.short_drama_binding, {project_revision:5})
    }),'alice');
    assert.equal(calls[0].url, '/api/gen/avatar');
    assert.equal(
      calls[0].options.headers['Idempotency-Key'],
      calls[1].options.headers['Idempotency-Key']
    );
    assert.deepEqual(
      JSON.parse(calls[1].options.body).short_drama_binding,
      payload.short_drama_binding
    );
    assert.equal(storage.size,1);
    await client.recoverAvatarOperations('alice');
    assert.equal(storage.size,0);
    assert.match(workspaceSource, /reference_image_url/);
    assert.match(workspaceSource, /创建电影化身并自动绑定/);
    assert.match(workspaceSource, /电影化身正在生成/);
  } finally {
    global.localStorage = previousStorage;
  }
});

test('Paid Avatar recovery is isolated to the trusted session account', async () => {
  const storage = new Map();
  const previousStorage = global.localStorage;
  const storageMock = {
    getItem:key => storage.has(key) ? storage.get(key) : null,
    setItem:(key,value) => storage.set(key,String(value)),
    removeItem:key => storage.delete(key),
    key:index => Array.from(storage.keys())[index]||null
  };
  Object.defineProperty(storageMock,'length',{get:() => storage.size});
  global.localStorage = storageMock;
  const payload = {
    image_data:'data:image/jpeg;base64,QUxJQ0U=',name:'Alice',
    short_drama_binding:{project_id:'alice-project',project_revision:4,character_key:'lead'}
  };
  let aliceKey = '';
  let bobRequests = 0;
  let aliceRecoveryPosts = 0;
  try {
    const aliceClient = workspace.createClient(async (_url,options) => {
      aliceKey = options.headers['Idempotency-Key'];
      throw new Error('response lost');
    });
    await assert.rejects(aliceClient.createAvatar(payload,'alice'), /response lost/);
    storage.set('hq-short-drama-avatar-operation:legacy-unowned',JSON.stringify({
      key:'legacy-key',payload:payload
    }));

    const bobClient = workspace.createClient(async () => {
      bobRequests += 1;
      return {ok:true,status:200,text:async () => JSON.stringify({job_id:77})};
    });
    await bobClient.recoverAvatarOperations('bob');
    assert.equal(bobRequests,0);

    const restoredAliceClient = workspace.createClient(async (url,options) => {
      if(url==='/api/gen/avatar'){
        aliceRecoveryPosts += 1;
        assert.equal(options.headers['Idempotency-Key'],aliceKey);
        assert.deepEqual(JSON.parse(options.body),payload);
        return {ok:true,status:200,text:async () => JSON.stringify({job_id:77})};
      }
      return {ok:true,status:200,text:async () => JSON.stringify({status:'running'})};
    });
    await restoredAliceClient.recoverAvatarOperations('alice');
    assert.equal(aliceRecoveryPosts,1);
  } finally {
    global.localStorage = previousStorage;
  }
});

test('Paid operation owner is resolved from the server session', async () => {
  const calls = [];
  const client = workspace.createClient(async (url) => {
    calls.push(url);
    return {
      ok:true,status:200,
      text:async () => JSON.stringify({user:{username:'alice'}})
    };
  });
  assert.equal(await client.currentUsername(),'alice');
  assert.deepEqual(calls,['/api/auth/me']);
  assert.match(workspaceSource,/client\.currentUsername\(\)/);
  assert.match(workspaceSource,/recoverAvatarOperations\(accountUsername\)/);
});

test('Paid character operations fail closed when browser storage rejects persistence', async () => {
  const previousStorage = global.localStorage;
  let requests = 0;
  const storageMock = {
    getItem:() => null,
    setItem:() => { const error = new Error('quota full'); error.name = 'QuotaExceededError'; throw error; },
    removeItem:() => {},
    key:() => null,
    length:0
  };
  global.localStorage = storageMock;
  const client = workspace.createClient(async () => {
    requests += 1;
    return {ok:true,status:200,text:async () => JSON.stringify({job_id:91})};
  });
  try {
    await assert.rejects(
      async () => client.createAvatar({
        image_data:'data:image/jpeg;base64,AA==',name:'林雨',
        short_drama_binding:{project_id:'p1',project_revision:4,character_key:'lead'}
      },'alice'),
      /浏览器.*存储|storage/i
    );
    await assert.rejects(
      async () => client.generateCharacterImage({project_id:'p1',revision:4,character_key:'lead'},'alice'),
      /浏览器.*存储|storage/i
    );
    assert.equal(requests,0);
    global.localStorage = {
      getItem:() => null,
      setItem:() => {},
      removeItem:() => {},
      key:() => null,
      length:0
    };
    await assert.rejects(
      async () => client.createAvatar({
        image_data:'data:image/jpeg;base64,AA==',name:'林雨',
        short_drama_binding:{project_id:'p1',project_revision:4,character_key:'lead'}
      },'alice'),
      /浏览器.*存储|storage/i
    );
    assert.equal(requests,0);
  } finally {
    global.localStorage = previousStorage;
  }
});

test('Character reference response loss is recovered with one stable paid operation', async () => {
  const calls = [];
  const storage = new Map();
  const previousStorage = global.localStorage;
  const storageMock = {
    getItem:key => storage.has(key) ? storage.get(key) : null,
    setItem:(key,value) => storage.set(key,String(value)),
    removeItem:key => storage.delete(key),
    key:index => Array.from(storage.keys())[index]||null
  };
  Object.defineProperty(storageMock,'length',{get:() => storage.size});
  global.localStorage = storageMock;
  let posts = 0;
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    if (url === '/api/gen/short-drama/generate-character-reference' && posts++ === 0) {
      throw new Error('response lost');
    }
    const response = url.startsWith('/api/gen/job/') ? {status:'running'} : {job_id:19};
    return {ok:true, status:200, text:async () => JSON.stringify(response)};
  };
  const payload = {project_id:'p1',revision:7,character_key:'lead'};
  try {
    const client = workspace.createClient(fetchImpl);
    await assert.rejects(client.generateCharacterImage(payload,'alice'), /response lost/);
    const restoredClient = workspace.createClient(fetchImpl);
    await restoredClient.recoverCharacterImageOperations('alice');
    const submissions = calls.filter(call => call.url === '/api/gen/short-drama/generate-character-reference');
    assert.equal(submissions.length,2);
    assert.equal(submissions[0].options.headers['Idempotency-Key'],submissions[1].options.headers['Idempotency-Key']);
    assert.deepEqual(JSON.parse(submissions[1].options.body),payload);
    assert.equal(storage.size,1);
  } finally {
    global.localStorage = previousStorage;
  }
});

test('Completed stale character reference can be regenerated from the latest profile', () => {
  const character = {
    character_key:'lead',
    reference_job_id:19,
    reference_job_status:'ready',
    reference_image_url:''
  };
  const operation = workspace.characterImageOperationState(character, {
    character_key:'lead',
    phase:'pending',
    message:'仍在生成',
    error:false,
    active:false
  });

  assert.equal(operation.phase,'stale');
  assert.equal(operation.active,false);
  assert.match(operation.message,/角色资料已更新/);
  assert.match(operation.message,/重新生成/);
  assert.equal(workspace.characterImageAction(operation),'generate');
  assert.equal(workspace.characterImageAction(
    workspace.characterImageOperationState({
      character_key:'lead',
      reference_job_id:20,
      reference_job_status:'linked',
      reference_image_url:''
    })
  ),'check');
});

test('角色参考图读取不会把登录凭据发送给外部地址', async () => {
  const calls = [];
  const PreviousFileReader = global.FileReader;
  const previousLocation = global.location;
  global.FileReader = class {
    readAsDataURL() {
      this.result = 'data:image/png;base64,AA==';
      this.onload();
    }
  };
  try {
    const client = workspace.createClient(async (url, options) => {
      calls.push({url, options});
      return {
        ok:true,
        status:200,
        blob:async () => ({type:'image/png'})
      };
    });
    global.location = new URL('https://workbench.example.com/project/1');
    await client.imageData('https://cdn.example.com/role.png');
    await client.imageData('//evil.example/role.png');
    await client.imageData('http://workbench.example.com/role.png');
    await client.imageData('https://workbench.example.com:444/role.png');
    await client.imageData('https://workbench.example.com/assets/role.png');
    await client.imageData('/assets/role.png');
    assert.equal(calls[0].options.credentials, 'omit');
    assert.equal(Object.hasOwn(calls[0].options.headers, 'Authorization'), false);
    for (const call of calls.slice(1,4)) {
      assert.equal(call.options.credentials, 'omit');
      assert.equal(Object.hasOwn(call.options.headers, 'Authorization'), false);
    }
    for (const call of calls.slice(4)) {
      assert.equal(call.options.credentials, 'same-origin');
      assert.equal(call.options.headers.Authorization, 'Bearer __cookie__');
    }
  } finally {
    global.FileReader = PreviousFileReader;
    global.location = previousLocation;
  }
});

test('客户端公开单镜头编辑、重生成与锁定接口', async () => {
  const calls = [];
  const client = workspace.createClient(async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>'{}'};
  });
  await client.updateShot({
    project_id:'project-1',
    conversation_revision:3,
    version_id:'version-1',
    shot_key:'shot_01',
    changes:{visual:'雨夜车站近景'}
  });
  await client.regenerateShot({
    project_id:'project-1',
    conversation_revision:4,
    version_id:'version-2',
    shot_key:'shot_01',
    instruction:'保留人物，只调整运镜'
  });
  await client.setShotLock({
    project_id:'project-1',
    conversation_revision:5,
    version_id:'version-3',
    shot_key:'shot_01',
    locked:true
  });
  assert.deepEqual(
    calls.map(item => item.url),
    [
      '/api/gen/short-drama/conversation/script/shot/update',
      '/api/gen/short-drama/conversation/script/shot/regenerate',
      '/api/gen/short-drama/conversation/script/shot/lock'
    ]
  );
  for (const call of calls) {
    assert.equal(call.options.method, 'POST');
    assert.ok(call.options.headers['Idempotency-Key']);
  }
});

test('角色卡直接展示已确认资料且仅在服务需要时处理电影化身', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.js'), 'utf8'
  );
  const css = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.css'), 'utf8'
  );
  assert.doesNotMatch(source, /id="sdCharacterModal"/);
  assert.doesNotMatch(source, /data-action="open-character"/);
  assert.match(source, /data-action="bind-card-avatar"/);
  assert.match(source, /data-action="create-character-avatar"/);
  assert.match(source, /providerRequiresMovieAvatar/);
  assert.match(source, /当前视频服务需要电影化身/);
  assert.match(source, /根据标准图创建并绑定/);
  assert.doesNotMatch(source, /<form id="sdCharacterProfile"/);
  assert.doesNotMatch(source, /<button[^>]+data-action="generate-character-image"/);
  assert.doesNotMatch(source, /<button[^>]+data-action="edit-character-name"/);
  assert.doesNotMatch(source, /form="sdCharacterProfile"/);
  assert.match(css, /\.sd-character-readonly/);
  assert.match(source, /角色标识：/);
  assert.match(source, /avatar_id:providerShot\.primary_avatar_id/);
  assert.match(source, /character_key:providerShot\.primary_character_key/);
  assert.match(css, /\.sd-character-card/);
  assert.match(css, /\.sd-character-card-image/);
  assert.match(css, /\.sd-character-image-lightbox/);
  assert.match(css, /\.sd-character-inline-actions/);
  assert.match(workspaceSource, /data-action="preview-character-image"/);
  assert.match(workspaceSource, /data-action="close-character-image-preview"/);
  assert.match(workspaceSource, /sdCharacterImagePreview/);
  assert.match(workspaceSource, /event\.key==='Escape'&&imageLightbox/);
  assert.equal(workspace.movieAvatarRequired(''), false);
  assert.equal(workspace.movieAvatarRequired('grok'), false);
  assert.equal(workspace.movieAvatarRequired('minimax_h3'), false);
  assert.equal(workspace.movieAvatarRequired('heygen_cinematic'), true);
  assert.equal(workspace.providerLabel('minimax_h3'), '视频生成服务');
  assert.equal(workspace.providerLabel('heygen_cinematic'), '视频生成服务');
  assert.equal(workspace.userFacingVideoMessage('麦克视频生成失败：MiniMax API 拒绝请求'), '视频生成服务生成失败：视频生成服务 API 拒绝请求');
  assert.match(workspace.providerFailureRecoveryHtml({
    status:'failed',billing_recovery:{refunded:true}
  }), /本次扣点已自动退回/);
  assert.match(workspace.providerFailureRecoveryHtml({
    status:'failed',billing_recovery:{refund_pending:true}
  }), /退点正在自动处理中/);
  assert.equal(workspace.providerFailureRecoveryHtml({status:'running'}), '');
});

test('敏感审核失败提供可操作的中文恢复流程', () => {
  const job={
    status:'failed',shot_key:'shot_07',
    error:{provider_code:'1026',provider_message:'input new_sensitive, input text sensitive'},
    request:{reference_images:[
      {character_key:'character_1',name:'陈宇'},
      {character_key:'__continuity_tail__',name:'上一镜头尾帧'}
    ]}
  };
  const characters=[
    {character_key:'character_1',name:'陈宇'},
    {character_key:'character_2',name:'林默'}
  ];
  const shot={provider_prompt:'中国初中教室里，瘦弱男生林默独自收拾书包'};
  const providerShot={character_keys:['character_1']};
  const review=workspace.providerInputReview(shot,providerShot,characters,job,{});
  const html=workspace.providerFailureRecoveryHtml(job,{shot,providerShot,providerCharacters:characters});
  assert.equal(workspace.userFacingVideoMessage(job.error.provider_message), '输入内容未通过审核，请调整镜头文字或参考图后重新预检。');
  assert.equal(review.sensitive,true);
  assert.deepEqual(review.expected,['陈宇']);
  assert.deepEqual(review.unexpected,['林默']);
  assert.match(html,/输入内容未通过审核/);
  assert.match(html,/角色名称不一致/);
  assert.match(html,/上一镜头尾帧 · 同场景同版本时必须保留/);
  assert.match(html,/场景图与同场景同版本的上一镜头尾帧属于连续性约束，不能停用/);
  assert.match(html,/data-action="edit-shot-execution"/);
  assert.equal(workspace.saferProviderPrompt(shot.provider_prompt),'普通室内教室里，清瘦人物林默独自收拾书包');
  assert.match(workspace.syncProviderCharacterNames(shot.provider_prompt,review),/陈宇/);
});

test('当前镜头覆盖绑定优先于旧剧本绑定并展示实际提交提示词', () => {
  const job={
    status:'failed',shot_key:'shot_07',
    error:{provider_code:'1026',provider_message:'input text sensitive'},
    request:{prompt:'画面与人物动作：中国初中教室里，瘦弱男生林默整理书包；补充生成要求：镜头缓慢推进',reference_images:[{character_key:'character_2',name:'林默'}]}
  };
  const characters=[{character_key:'character_1',name:'陈宇'},{character_key:'character_2',name:'林默'}];
  const shot={shot_key:'shot_07',provider_prompt:'旧提示词中的陈宇'};
  const providerShot={character_keys:['character_1']};
  const execution={character_keys:['character_2'],visual:'中国初中教室里，瘦弱男生林默整理书包',negative_prompt:'禁止未成年人和字幕',provider_prompt:'补充要求：镜头缓慢推进'};
  const review=workspace.providerInputReview(shot,providerShot,characters,job,execution);
  const html=workspace.providerFailureRecoveryHtml(job,{shot,providerShot,providerCharacters:characters,execution});
  assert.deepEqual(review.expected,['林默']);
  assert.deepEqual(review.unexpected,[]);
  assert.equal(review.prompt,job.request.prompt);
  assert.notEqual(review.prompt,execution.provider_prompt);
  assert.deepEqual(review.candidates,['初中']);
  const optimized=workspace.optimizedSensitiveExecution(shot,providerShot,execution,review);
  assert.equal(optimized.visual,'普通室内教室里，清瘦人物林默整理书包');
  assert.equal(optimized.provider_prompt,execution.provider_prompt);
  assert.equal(optimized.negative_prompt,execution.negative_prompt);
  assert.notEqual(optimized.provider_prompt,review.prompt);
  assert.equal(optimized.prompt_semantics,'structured-supplement-v1');
  assert.match(html,/角色绑定正常/);
  assert.match(html,/优化文字并免费重新预检/);
  assert.doesNotMatch(html,/角色名称不一致/);
  assert.equal(workspace.currentShotExecutionPrompt(shot,{provider_job:job,provider_execution_overrides:{shot_07:execution}}),job.request.prompt);
});

test('视频轮询只局部更新进度并保持工作区视口稳定', () => {
  assert.match(workspaceSource,/function updateProviderProgressDom\(job\)/);
  assert.match(workspaceSource,/data-provider-media-progress/);
  assert.match(workspaceSource,/data-provider-job-progress/);
  assert.match(workspaceSource,/function updateBackgroundProgressDom\(kind,job\)/);
  assert.match(workspaceSource,/function renderPreservingViewport\(\)/);
  assert.match(workspaceSource,/function workspaceViewportState\(\)/);
  assert.match(workspaceStyle,/\.sd-workspace-top\{position:fixed/);
  assert.match(workspaceStyle,/\.sd-workspace\{box-sizing:border-box;height:100dvh;padding-top:68px/);
});

test('视频活动任务显示真实阶段并使用不确定进度条', () => {
  const queued={
    id:'90',shot_key:'shot_05',provider:'minimax_h3',status:'queued',
    phase:'minimax_queued',progress:35,progress_indeterminate:true
  };
  const display=workspace.providerJobDisplay(queued);
  const media=workspace.shotMediaHtml(
    {shot_key:'shot_05'}, {versions:[],job:queued}
  );
  assert.equal(display.label,'视频任务排队中');
  assert.equal(display.indeterminate,true);
  assert.match(media,/视频任务排队中/);
  assert.doesNotMatch(media,/麦克/);
  assert.match(media,/sd-progress indeterminate/);
  assert.doesNotMatch(media,/35%/);
  assert.match(workspaceStyle,/\.sd-progress\.indeterminate i/);
});

test('麦克视频生成中不向用户显示模型名称', () => {
  const running={
    id:'153',shot_key:'shot_09',provider:'minimax_h3',status:'running',
    phase:'minimax_running',progress:35,progress_indeterminate:true
  };
  const display=workspace.providerJobDisplay(running);
  const media=workspace.shotMediaHtml(
    {shot_key:'shot_09'}, {versions:[],job:running}
  );

  assert.equal(display.label,'正在生成视频');
  assert.match(media,/正在生成视频/);
  assert.match(media,/后台任务 153/);
  assert.doesNotMatch(media,/麦克模型正在生成视频/);
});

test('视频生成全流程只显示当前操作而不显示底层模型名称', () => {
  const phases=[
    [{status:'submitting',phase:'minimax_submitting'},'正在提交视频任务'],
    [{status:'queued',phase:'minimax_preparing'},'视频任务排队中'],
    [{status:'running',phase:'minimax_retrying'},'正在重新连接视频服务'],
    [{status:'running',phase:'minimax_running'},'正在生成视频'],
    [{status:'running',phase:'minimax_downloading'},'正在下载并保存视频']
  ];

  phases.forEach(function(entry,index){
    const display=workspace.providerJobDisplay(Object.assign({
      id:String(160+index),provider:'minimax_h3',progress_indeterminate:true
    },entry[0]));
    assert.equal(display.label,entry[1]);
    assert.doesNotMatch(display.heading,/麦克/);
    assert.doesNotMatch(display.taskLabel,/麦克/);
  });
});

test('镜头视频随可用宽度伸缩并为竖屏项目限制合理高度', () => {
  const media={versions:[{
    id:'v1',shot_key:'shot_01',version:1,selected:true,
    url:'/api/gen/file/video/shot-01.mp4'
  }],job:null};
  const landscape=workspace.shotMediaHtml({shot_key:'shot_01'},media,'16:9');
  const portrait=workspace.shotMediaHtml({shot_key:'shot_01'},media,'9:16');

  assert.match(landscape,/sd-shot-media-landscape/);
  assert.match(portrait,/sd-shot-media-portrait/);
  assert.match(landscape,/class="sd-shot-media-frame"/);
  assert.match(workspaceSource,/shotMediaHtml\(shot,mediaByShot\[text\(shot\.shot_key\)\],project&&project\.ratio\)/);
  assert.match(workspaceStyle,/\.sd-shot-media-frame\{[^}]*width:100%[^}]*aspect-ratio:16\/9/);
  assert.match(workspaceStyle,/\.sd-shot-media-portrait \.sd-shot-media-frame\{[^}]*72vh[^}]*aspect-ratio:9\/16/);
  assert.doesNotMatch(workspaceStyle,/\.sd-shot-media video\{[^}]*max-height:430px/);
});

test('镜头角色更换会同步提示词且不改动未替换角色', () => {
  assert.equal(
    workspace.syncShotBindingPrompt('林默走进教室，陈宇回头。',['林默'],['陈宇']),
    '陈宇走进教室，陈宇回头。'
  );
  assert.equal(workspace.syncShotBindingPrompt('林默走进教室。',['林默'],[]),'林默走进教室。');
});

test('同场景同版本严格为尾帧保留第五个参考图位置', () => {
  assert.equal(typeof workspace.shotReferenceSelectionPolicy,'function');
  assert.deepEqual(
    workspace.shotReferenceSelectionPolicy(
      {scene_key:'scene:memorial',reference_identity:'scene-operation-v2'},
      {scene_key:'scene:memorial',reference_identity:'scene-operation-v2'},
      true
    ),
    {
      same_scene_reference:true,tail_required:true,
      selected_reference_limit:4,character_limit:3
    }
  );
  assert.deepEqual(
    workspace.shotReferenceSelectionPolicy(
      {scene_key:'scene:memorial',reference_identity:'scene-operation-v2'},
      {scene_key:'scene:memorial',reference_identity:'scene-operation-v1'},
      true
    ),
    {
      same_scene_reference:false,tail_required:false,
      selected_reference_limit:5,character_limit:4
    }
  );
});

test('沿用故事镜头绑定场景时仍按有效场景身份预留参考图位置', () => {
  assert.deepEqual(
    workspace.effectiveSceneReferenceIdentity(
      {scene_key:'',reference_identity:''},
      {scene_key:'scene:memorial',reference_identity:'scene-operation-v2'}
    ),
    {scene_key:'scene:memorial',reference_identity:'scene-operation-v2'}
  );
  const inherited=workspace.effectiveSceneReferenceIdentity(
    {scene_key:'',reference_identity:''},
    {scene_key:'scene:memorial',reference_identity:'scene-operation-v2'}
  );
  assert.equal(
    workspace.shotReferenceSelectionPolicy(
      inherited,
      {scene_key:'scene:memorial',reference_identity:'scene-operation-v2'},
      true
    ).character_limit,
    3
  );
});

test('镜头生成要求只读展示已确认台词及同时说话关系', () => {
  assert.equal(typeof workspace.confirmedShotDialogueHtml,'function');
  const html=workspace.confirmedShotDialogueHtml([
    {kind:'dialogue',speaker:'顾承川',text:'别回头',speech_rate:1,timing_mode:'sequential'},
    {kind:'dialogue',speaker:'许安',text:'快走',speech_rate:1.15,timing_mode:'simultaneous'},
    {kind:'on_screen_text',text:'六年前',speech_rate:1,timing_mode:'sequential'}
  ]);
  assert.match(html,/已确认台词/);
  assert.match(html,/顾承川/);
  assert.match(html,/别回头/);
  assert.match(html,/许安/);
  assert.match(html,/快走/);
  assert.match(html,/与上一条同时说/);
  assert.match(html,/1\.15×/);
  assert.match(html,/画面文字/);
  assert.doesNotMatch(html,/<textarea|<select|<input/);
});

test('镜头执行编辑器可显式绑定锁定场景并保留场景补充', () => {
  assert.match(workspaceSource,/本镜头场景绑定/);
  assert.match(workspaceSource,/name="scene_key"/);
  assert.match(workspaceSource,/补充当前镜头的场景细节/);
  assert.match(workspaceSource,/execution\.scene_key=/);
  assert.match(workspaceSource,/execution\.include_scene_reference=true/);
  assert.match(workspaceSource,/data-default-scene-key=/);
  assert.match(workspaceSource,/data-default-scene-reference-identity=/);
  assert.doesNotMatch(workspaceSource,/本镜头暂不使用场景图/);
  assert.doesNotMatch(workspaceSource,/fallbackScene/);
  assert.match(workspaceStyle,/\.sd-shot-scene-binding/);
});

test('剧本镜头编辑器使用已有故事场景选择器并支持不绑定', () => {
  assert.match(workspaceSource,/绑定故事场景/);
  assert.match(workspaceSource,/不绑定故事场景/);
  assert.match(workspaceSource,/name="scene_key"/);
  assert.match(workspaceSource,/client\.bindSceneToShot/);
  assert.match(workspaceSource,/scenes\/bind-shot/);
  assert.doesNotMatch(workspaceSource,/<label>场景<input name="scene"/);
});

test('镜头细节默认折叠并提供快捷选择、系统连续性和保护规则', () => {
  assert.match(workspaceSource,/镜头细节（可选）/);
  assert.match(workspaceSource,/data-action="set-shot-detail"/);
  assert.match(workspaceSource,/连续性（系统自动继承）/);
  assert.match(workspaceSource,/data-action="edit-shot-continuity"/);
  assert.match(workspaceSource,/系统保护规则/);
  assert.match(workspaceSource,/补充生成要求（可选）/);
  assert.match(workspaceStyle,/\.sd-shot-detail-chips/);
});

test('剧本审阅阶段继承项目中已锁定的角色标准图', () => {
  assert.match(workspaceSource, /project:function\(id\).*short-drama\/project/);
  assert.match(workspaceSource, /persistedCharacter/);
  assert.match(workspaceSource, /标准图已锁定/);
  assert.match(workspaceSource, /persisted\.reference_url/);
});

test('项目工作区移除创作助手并保留新版本项目参数复制能力', () => {
  const shell = workspace.shellHtml();
  const payload = workspace.cloneProjectPayload({
    title:'暴雨录音', synopsis:'未来录音', ratio:'9:16',
    target_duration:45, shot_count:9, visual_style:'悬疑写实',
    target_platform:'视频号', point_budget:300
  });
  assert.doesNotMatch(shell, /sd-chat/);
  assert.doesNotMatch(shell, /sdMessageForm/);
  assert.doesNotMatch(shell, /和创作助手对话/);
  assert.match(shell, /id="sdScript"/);
  assert.match(shell, /id="sdUnderstanding"/);
  assert.equal(payload.title, '暴雨录音 · 新版本');
  assert.equal(payload.synopsis, '未来录音');
  assert.equal(payload.ratio, '9:16');
  assert.equal(payload.target_duration, 45);
  assert.equal(payload.shot_count, 9);
  assert.equal(payload.point_budget, 300);
});

test('未确认项目在主创作区直接提供内容确认入口', () => {
  const output = workspace.scriptHtml(
    null, true, {}, '', true,
    {direction_confirmed:false,premise:'雨夜中的重逢',import_contract:{characters:['林夏','周野'],plot_points:[{position:'start',excerpt:'两人在雨中相遇'}]}},
    '确认尊重原稿并生成', '', {}, {shot_count:6,target_duration:30}
  );
  assert.match(output, /sd-project-review/);
  assert.match(output, /生成前，请检查这三项内容/);
  assert.match(output, /雨夜中的重逢/);
  assert.match(output, /林夏、周野/);
  assert.match(output, /6 个镜头 · 预计 30 秒/);
  assert.doesNotMatch(output, /data-action="confirm-and-generate"/);
  assert.doesNotMatch(output, /data-action="generate"/);
  assert.doesNotMatch(output, /创作助手/);
});

test('分镜概要从重复原稿中整理出简短的开场发展结尾', () => {
  const repeated = '人物：小晚、阿泽。分镜、时长、画面依次安排：1、0-5秒：少女在街角与少年相撞。2、5-10秒：两人捡起书签。3、10-15秒：两人短暂交谈。4、15-20秒：两人发现共同爱好。5、20-25秒：两人在路口道别。6、25-30秒：街灯亮起，各自离开。';
  const output = workspace.scriptHtml(null, true, {}, '', false, {
    premise:'一次浪漫偶遇',
    import_contract:{characters:['小晚','阿泽'],plot_points:[
      {position:'start',excerpt:repeated},
      {position:'middle',excerpt:repeated},
      {position:'end',excerpt:repeated}
    ]}
  }, '', '', {}, {shot_count:6,target_duration:30});
  assert.match(output, /少女在街角与少年相撞/);
  assert.match(output, /两人发现共同爱好/);
  assert.match(output, /街灯亮起，各自离开/);
  assert.doesNotMatch(output, /人物：小晚、阿泽。分镜、时长/);
  assert.match(output, /3 个关键节点/);
});

test('导入原稿可一次确认方向并生成第一版剧本', () => {
  const output = workspace.scriptHtml(
    null, true, {}, '', false,
    {phase:'import_review',direction_confirmed:false},
    ''
  );
  assert.match(output, /sd-project-review/);
  assert.doesNotMatch(output, /data-action="confirm-and-generate"/);
  assert.match(workspaceSource, /function confirmDirectionAndGenerate/);
  assert.match(workspaceSource, /sendConversationMessage\(message\)\.then/);
  assert.match(workspaceSource, /client\.generate\(payload\(\{instruction:text\(instruction\)\.trim\(\)\}\)\)/);
});

test('结构化剧本主区聚焦角色、镜头和台词，三幕结构移到右侧详情', () => {
  const output = workspace.scriptHtml({
    id:'v1', version:1, status:'draft',
    script:{
      overview:{title:'雨夜来信',logline:'旧友重逢'},
      characters:[{name:'主角',identity:'记者',personality:'敏锐'}],
      acts:[{act:1,name:'钩子',summary:'来信出现'}],
      shots:[{sort_order:1,duration_seconds:5,visual:'雨中近景'}],
      dialogue_lines:[{speaker:'主角',text:'你终于来了'}]
    }
  });
  assert.match(output, /雨夜来信/);
  assert.match(output, /结构化剧本/);
  assert.match(output, /版本 v1/);
  assert.match(output, />草稿</);
  assert.doesNotMatch(output, /三幕结构/);
  assert.match(output, /雨中近景/);
  assert.match(output, /你终于来了/);
  const acts = workspace.storyActsHtml([
    {act:1,name:'钩子',summary:'来信出现'},
    {act:2,name:'冲突',summary:'真相揭开'}
  ]);
  assert.match(acts, /三幕结构/);
  assert.match(acts, /第1幕 · 钩子/);
  assert.match(acts, /真相揭开/);
  assert.match(workspaceSource, /storyActsHtml\(currentScriptBody\.acts\)/);
});

test('角色列表严格采用创建阶段确认的角色合同', () => {
  const result = workspace.authoritativeCharacterList(
    [
      {character_key:'boy',name:'男孩'},
      {character_key:'girl',name:'女孩'},
      {character_key:'friend_a',name:'朋友甲'},
      {character_key:'friend_b',name:'朋友乙'}
    ],
    [
      {character_key:'boy',name:'男孩',reference_url:'/boy.png'},
      {character_key:'girl',name:'女孩',reference_url:'/girl.png'},
      {character_key:'friend_a',name:'朋友甲'}
    ],
    [],
    [
      {character_key:'boy',name:'男孩',role_type:'main'},
      {character_key:'girl',name:'女孩',role_type:'support'}
    ]
  );
  assert.deepEqual(result.map(item => item.character_key), ['boy','girl']);
  assert.equal(result[0].reference_url, '/boy.png');
  assert.equal(result[1].role_type, 'support');
});

test('v4 故事板主区展示节拍和单镜头操作，质量门禁移到右侧', () => {
  const version = {
    id:'v4',
    version:4,
    status:'draft',
    model_version:'conversation-storyboard-v4',
    script:{
      schema_version:'short-drama-conversation-script-v4',
      overview:{title:'查分',logline:'母女共同面对一次落差'},
      quality_gate:{status:'pass',score:96,blockers:[],warnings:[]},
      characters:[{character_key:'daughter',name:'女儿',identity:'高三学生',personality:'克制'}],
      acts:[{act:1,name:'建立',summary:'凌晨查分'}],
      story_beats:[{phase:'setup',purpose:'交代成绩落差'}],
      shots:[{
        shot_key:'shot_01',
        sort_order:1,
        purpose:'交代成绩落差',
        beat:'女儿盯着成绩页面',
        duration_seconds:4,
        visual:'凌晨卧室，成绩页面冷光照在女儿脸上',
        camera:'固定中近景',
        continuity:'保持蓝色睡衣和凌晨光线',
        provider_prompt:'电影感写实，凌晨卧室，固定中近景',
        negative_prompt:'水印，文字',
        source_type:'user_storyboard',
        source_text:'镜头 1（0-4s）固定中近景，女儿盯着成绩页面。',
        dialogue_line_ids:['line_01'],
        locked:false
      }],
      dialogue_lines:[{
        id:'line_01',
        kind:'silence',
        speaker:'',
        text:'',
        start_ms:0,
        end_ms:4000
      }]
    }
  };
  const output = workspace.scriptHtml(version, true);
  assert.doesNotMatch(output, /质量门禁/);
  assert.match(output, /sd-script-head ready/);
  assert.match(output, />可锁定</);
  const quality = workspace.storyboardQualityHtml(version.script);
  assert.match(quality, /1\/1 镜检查通过，可以锁定/);
  assert.match(quality, /查看检查详情/);
  assert.match(quality, /镜头时长、对白、剧情推进和生成提示词检查通过/);
  assert.deepEqual(workspace.scriptHeaderState({status:'locked'}), {key:'locked',label:'已锁定'});
  assert.match(output, /交代成绩落差/);
  assert.match(output, /静默表演/);
  assert.match(output, /data-action="edit-shot"/);
  assert.match(output, /data-action="regenerate-shot"/);
  assert.match(output, /data-action="toggle-shot-lock"/);
  assert.match(output, /实际提交提示词/);
  assert.match(output, /用户原稿/);
  assert.match(output, /原稿依据/);
  assert.match(output, /镜头 1（0-4s）/);
});

test('没有用户分镜要求的镜头标记为系统补充', () => {
  const output = workspace.scriptHtml({
    version:1,status:'draft',script:{
      overview:{title:'自动分镜'},characters:[],acts:[],
      shots:[{shot_key:'shot_01',sort_order:1,duration_seconds:5,visual:'系统生成画面',source_type:'system_generated'}],
      dialogue_lines:[]
    }
  });
  assert.match(output, /系统补充/);
});

test('旧通用模板版本显示重建提示', () => {
  const output = workspace.scriptHtml({
    version:3,status:'locked',model_version:'conversation-script-v2',
    script:{overview:{title:'旧项目'},characters:[],acts:[],shots:[],dialogue_lines:[]}
  });
  assert.match(output, /旧通用模板生成/);
  assert.match(output, /创建新版本后重新生成剧本/);
});

test('创作助手渲染推荐卡片和快捷回复并转义服务端内容', () => {
  const output = workspace.messageHtml({
    role:'assistant',
    content:'我整理了三个方向',
    metadata:{
      recommendations:[{
        id:'twist',
        title:'方案二 · 冲突反转',
        hook:'先抛线索',
        summary:'结尾揭开 <真相>'
      }],
      quick_replies:['确认这个方向','<继续补充>']
    }
  });
  assert.match(output, /sd-advisor-recommendations/);
  assert.match(output, /sd-advisor-actions/);
  assert.match(output, /你可以这样继续/);
  assert.match(output, /data-action="quick-reply"/);
  assert.match(output, /方案二 · 冲突反转/);
  assert.match(output, /确认这个方向/);
  assert.match(output, /确认当前创作方向/);
  assert.match(output, /换一批建议/);
  assert.match(output, /&lt;真相&gt;/);
  assert.doesNotMatch(output, /<继续补充>/);
});

test('快捷回复按语义显示图标、说明和主操作层级', () => {
  const suspense = workspace.quickReplyPresentation('我想做悬疑反转', 1);
  const healing = workspace.quickReplyPresentation('我想做温暖治愈', 2);
  const recommend = workspace.quickReplyPresentation('帮我推荐三个方向', 0);
  assert.equal(suspense.icon, '🔍');
  assert.match(suspense.description, /谜题、线索和反转/);
  assert.equal(healing.icon, '🌤️');
  assert.match(healing.description, /人物关系/);
  assert.equal(recommend.primary, true);
  assert.match(recommend.description, /几套不同的故事方向/);
});

test('历史版本恢复按钮标记当前版本且转义内容', () => {
  const output = workspace.versionHtml(
    {id:'v<1',version:2,change_summary:'<script>',status:'draft'},
    'v<1'
  );
  assert.match(output, /class="sd-version current"/);
  assert.doesNotMatch(output, /<script>/);
  assert.match(output, /&lt;script&gt;/);
});

test('锁定剧本后展示制作体检、估算、风险和一次确认', () => {
  const output = workspace.preflightHtml(
    {state:'script_locked'},
    {
      current_plan:{
        id:'plan-1',
        version:1,
        status:'draft',
        plan:{
          quality_route:'quick_draft',
          estimate:{points:55,minutes:12,resolution:'720p'},
          route_options:[
            {key:'quick_draft',name:'快速草稿',estimated_points:55},
            {key:'formal',name:'正式制作',estimated_points:162},
          ],
          checks:[
            {key:'duration',label:'时长',status:'warning',summary:'需要调整节奏',suggestion:'接受系统建议'},
            {key:'consistency',label:'一致性',status:'pass',summary:'引用一致'},
          ],
          duration:{target_ms:30000,shots:[{shot_key:'shot_1'}]},
          assets:[{key:'character_1'}],
          required_acceptance:['duration_compression'],
          ready:true,
        },
      },
    },
    true
  );
  assert.match(output, /PR-3 · 制作准备/);
  assert.match(output, /55 点/);
  assert.match(output, /时长/);
  assert.match(output, /我已了解并接受/);
  assert.match(output, /确认制作方案/);
});

test('已确认方案进入只读交接状态', () => {
  const output = workspace.preflightHtml(
    {state:'script_locked'},
    {
      current_plan:{
        version:2,
        status:'confirmed',
        plan:{
          quality_route:'formal',
          estimate:{points:160,minutes:36,resolution:'1080p'},
          route_options:[{key:'formal',name:'正式制作',estimated_points:160}],
          checks:[],
          duration:{target_ms:60000,shots:[]},
          assets:[],
          required_acceptance:[],
          ready:true,
        },
      },
    },
    true
  );
  assert.match(output, /制作方案已确认/);
  assert.match(output, /下一阶段可据此生成自动草稿/);
  assert.doesNotMatch(output, /data-action="confirm-plan"/);
});

test('真实 Provider 未接入时禁止生成并明确说明不会播放固定示例', () => {
  const ready = workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1',plan:{material_plan:[{shot_key:'shot_01'}]}},
    billing:{cost:55,mode:'charged_on_start'},
    production:{
      ready:false,
      mode:'unavailable',
      message:'尚未选择真实画面 Provider',
      provider:{selected:null,configured:false}
    }
  }, true);
  assert.match(ready, /尚未选择视频生成服务/);
  assert.match(ready, /视频生成总览/);
  assert.match(ready, /左侧“镜头与台词”/);
  assert.match(ready, /预检和报价不扣点/);
  assert.doesNotMatch(ready, /data-action="provider-quote"/);
  assert.doesNotMatch(ready, /data-action="provider-start"/);
  assert.doesNotMatch(ready, /data-action="start-draft"/);

  const running = workspace.autodraftActionsHtml({
    current_job:{status:'running',phase:'visuals',progress:45},
    confirmed_plan:{id:'plan-1'}
  }, true);
  assert.match(running, /45%/);
  assert.match(running, /visuals/);
});

test('显式演示模式必须标识固定示例不可交付', () => {
  const action = workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1'},
    production:{ready:true,mode:'demo'}
  }, true);
  assert.match(action, /演示模式/);
  assert.match(action, /不会根据剧本生成真实画面/);
  assert.match(action, /生成演示草稿/);

  const output = workspace.draftHtml({
    current_version:{
      version:1,is_demo:true,url:'/assets/meiye_video.mp4',manifest:{}
    }
  });
  assert.match(output, /固定界面联调视频/);
  assert.match(output, /与当前剧本无关/);
  assert.match(output, /<video/);
});

test('已完成草稿渲染播放器、镜头状态和问题清单', () => {
  const output = workspace.draftHtml({
    current_version:{
      version:1,
      status:'degraded',
      url:'/assets/meiye_video.mp4',
      manifest:{
        duration_ms:30000,
        issues:[{code:'safe_visual_fallback'}],
        shots:[
          {shot_key:'shot_01',sort_order:1,status:'ready'},
          {shot_key:'shot_02',sort_order:2,status:'degraded',issue:{message:'已使用安全替代画面'}},
        ],
      },
    },
  });
  assert.match(output, /<video/);
  assert.match(output, /meiye_video\.mp4/);
  assert.match(output, /2 个镜头/);
  assert.match(output, /1 个待优化/);
  assert.match(output, /已使用安全替代画面/);
});

test('PR-5 客户端提供精修预览、镜头任务、确认、报价与正式导出接口', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>'{}'};
  };
  const client = workspace.createClient(fetchImpl);
  await client.refinement('project a');
  await client.previewRefinement({project_id:'project a',shot_key:'shot_02'});
  await client.refineShot({project_id:'project a',shot_key:'shot_02'});
  await client.reassembleRefinementCandidates({project_id:'project a',version_id:'rv2'});
  await client.refinementJob('project a','job/2');
  await client.confirmRefinement({project_id:'project a',version_id:'rv2'});
  await client.restoreRefinement({project_id:'project a',version_id:'rv1'});
  await client.deliveryQuote({project_id:'project a',version_id:'rv2'});
  await client.startDelivery({project_id:'project a',quote_token:'quote1'});
  await client.deliveryJob('project a','delivery/1');
  assert.equal(calls[0].url, '/api/gen/short-drama/refinement?project_id=project%20a');
  assert.equal(calls[3].url, '/api/gen/short-drama/refinement/candidates/reassemble');
  assert.equal(calls[4].url, '/api/gen/short-drama/refinement/jobs/job%2F2?project_id=project%20a');
  assert.equal(calls[9].url, '/api/gen/short-drama/delivery/jobs/delivery%2F1?project_id=project%20a');
  for (const call of calls.filter(call => call.options.method === 'POST')) {
    assert.ok(call.options.headers['Idempotency-Key']);
  }
});

test('PR-5 精修工作区展示问题镜头、单镜重做和确认门禁', () => {
  const output = workspace.refinementHtml({
    current_refinement:{
      version:2,status:'draft',url:'/assets/meiye_video.mp4',
      shots:[
        {shot_key:'shot_01',sort_order:1,status:'ready'},
        {shot_key:'shot_02',sort_order:2,status:'degraded',issue:{message:'安全替代画面'}},
      ],
      issues:[{shot_key:'shot_02'}],
    },
    refinement_versions:[{id:'r2'},{id:'r1'}],
  });
  assert.match(output, /PR-5 · 智能精修/);
  assert.match(output, /data-action="enter-refinement-redo"/);
  assert.match(output, /data-shot-key="shot_02"/);
  assert.match(output, /1 个待处理/);
  assert.doesNotMatch(output, /data-action="refine-shot"/);

  const blocked = workspace.refinementActionsHtml({
    current_refinement:{id:'r2',status:'draft',issues:[{shot_key:'shot_02'}]},
  }, true);
  assert.match(blocked, /data-action="confirm-refinement" disabled/);
});

test('problem shots expose isolated candidate selection before one final reassembly', () => {
  const refinement = {
    current_refinement:{
      id:'refinement-v3',version:3,status:'draft',url:'/api/gen/file/full-preview.mp4',
      assembly_status:{reassembly_required:false},
      shots:[
        {shot_key:'shot_01',sort_order:1,status:'ready',provider_version:2},
        {
          shot_key:'shot_02',sort_order:2,status:'degraded',provider_version:2,
          issue:{message:'人物形象跳变',provider_version_floor:2}
        },
        {shot_key:'shot_03',sort_order:3,status:'ready',provider_version:1},
      ],
      issues:[{shot_key:'shot_02'}],
    },
  };
  const autodraft = {
    provider_poc:{
      characters:[{character_key:'character_1',name:'小男孩',binding_ready:true}],
      shots:[{
        shot_key:'shot_02',sort_order:2,scene:'长椅',binding_ready:true,
        sequence_ready:true,previous_shot_key:'shot_01',character_keys:['character_1']
      }]
    },
    provider_preview:{
      ready:true,message:'参数检查通过',shot:{shot_key:'shot_02'},
      request:{prompt:'重新生成的小男孩镜头'}
    },
    provider_quote:{cost:40,shot:{shot_key:'shot_02'}},
    provider_versions:[
      {id:'shot-02-v2',shot_key:'shot_02',version:2,url:'/api/gen/file/shot-02-v2.mp4'},
      {id:'shot-02-v3',shot_key:'shot_02',version:3,url:'/api/gen/file/shot-02-v3.mp4',selected:true},
    ],
  };
  const preview = workspace.refinementHtml(refinement,autodraft);
  assert.match(preview, /进入镜头重做/);
  assert.doesNotMatch(preview, /候选镜头版本/);

  const output = workspace.refinementRedoHtml(refinement,autodraft,'shot_02',true);
  assert.match(output, /data-refinement-redo-workspace/);
  assert.match(output, /sd-refinement-redo-layout/);
  assert.match(output, /sd-refinement-candidate-rail/);
  assert.match(output, /sd-refinement-redo-sticky/);
  assert.match(output, /class="selected"/);
  assert.match(output, /当前采用版本 v2/);
  assert.match(output, /只处理已标记的问题镜头/);
  assert.match(output, /data-refinement-redo-generation/);
  assert.match(output, /data-action="edit-shot-execution" data-shot-key="shot_02"/);
  assert.match(output, /data-action="provider-preflight" data-shot-key="shot_02"/);
  assert.match(output, /data-action="provider-quote" data-shot-key="shot_02"/);
  assert.match(output, /data-action="provider-start" data-shot-key="shot_02"/);
  assert.ok(output.indexOf('修改提示词') < output.indexOf('免费检查参数'));
  assert.ok(output.indexOf('免费检查参数') < output.indexOf('获取报价'));
  assert.ok(output.indexOf('获取报价') < output.indexOf('确认重新生成'));
  assert.match(output, /40 点/);
  assert.match(output, /src="\/api\/gen\/file\/shot-02-v2\.mp4"/);
  assert.match(output, /src="\/api\/gen\/file\/shot-02-v3\.mp4"/);
  assert.match(output, /候选 v3<\/b><span>当前选择/);
  assert.match(output, /data-action="refine-shot" data-shot-key="shot_02"/);
  assert.match(output, /data-action="keep-original-refinement-shot"/);
  assert.match(output, /保留原视频并取消重做/);
  assert.match(output, /接受当前已知问题/);
  assert.match(output, /整片需要最后统一重新合成/);
  assert.match(workspaceSource, /暂无可采用的候选版本/);
  assert.match(workspaceSource, /重新生成完成后，请先选择满意版本再采用/);
  assert.doesNotMatch(output, /shot_03/);
  assert.match(workspaceSource, /refinement\/candidates\/adopt/);
  assert.match(workspaceSource, /client\.adoptRefinementCandidate/);
  assert.match(workspaceSource, /refinement\/candidates\/reassemble/);
  assert.match(workspaceSource, /client\.reassembleRefinementCandidates/);
  assert.match(workspaceSource, /client\.keepOriginalRefinementShot/);
  assert.match(workspaceSource, /classList\.toggle\('refinement-redo-active',refinementRedoMode\)/);
  assert.match(workspaceStyle, /\.sd-workspace-grid\.refinement-redo-active\{grid-template-columns:minmax\(0,1fr\)!important\}/);
  assert.match(workspaceStyle, /\.sd-workspace-grid\.refinement-redo-active>\.sd-inspector\{display:none!important\}/);
});

test('adopting a non-latest candidate binds preview and adoption to that exact version', () => {
  const output = workspace.refinementShotCandidateHtml({
    shot_key:'shot_02',provider_version:1,
    issue:{provider_version_floor:1},
  }, {
    provider_versions:[
      {id:'shot-02-v2',shot_key:'shot_02',version:2,url:'/v2.mp4',selected:true},
      {id:'shot-02-v3',shot_key:'shot_02',version:3,url:'/v3.mp4'},
    ],
  });
  assert.match(output, /候选 v2 已选中/);
  assert.match(output, /data-action="refine-shot" data-shot-key="shot_02" data-version-id="shot-02-v2"/);

  const first = workspace.refinementCandidateRequest('project-1','shot_02','shot-02-v2');
  assert.deepEqual(first.preview, {
    project_id:'project-1',shot_key:'shot_02',replacement_provider_version_id:'shot-02-v2',
  });
  const bound = workspace.refinementCandidateRequest('project-1','shot_02','shot-02-v2', {
    source_version_id:'refinement-v4',replacement_provider_version_id:'shot-02-v2',
  });
  assert.equal(bound.adoption.replacement_provider_version_id, 'shot-02-v2');
  assert.throws(
    () => workspace.refinementCandidateRequest('project-1','shot_02','shot-02-v2', {
      source_version_id:'refinement-v4',replacement_provider_version_id:'shot-02-v3',
    }),
    /候选版本已变化/,
  );
});

test('keeping the original shot is disabled while paid redo work is active', () => {
  const refinement = {
    current_refinement:{
      id:'refinement-v3',version:3,status:'draft',url:'/api/gen/file/full-preview.mp4',
      shots:[{shot_key:'shot_02',sort_order:2,status:'degraded',provider_version:2,issue:{message:'人物形象跳变'}}],
      issues:[{shot_key:'shot_02'}],
    },
  };
  ['running','submit_unknown'].forEach((status) => {
    const autodraft = {
      provider_poc:{shots:[{shot_key:'shot_02',binding_ready:true,sequence_ready:true,character_keys:[]}],characters:[]},
      provider_job:{id:'latest-other-shot',shot_key:'shot_03',status:'failed'},
      provider_jobs:[
        {id:'same-shot-active',shot_key:'shot_02',status},
        {id:'other-shot-active',shot_key:'shot_03',status:'running'},
      ],
      provider_versions:[{id:'shot-02-v2',shot_key:'shot_02',version:2,url:'/api/gen/file/shot-02-v2.mp4'}],
    };
    const output = workspace.refinementRedoHtml(refinement,autodraft,'shot_02',true);
    assert.match(output, /data-action="keep-original-refinement-shot"[^>]* disabled/);
    assert.match(output, /当前重做任务执行中，不能取消/);
  });

  const otherShotOnly = workspace.refinementRedoHtml(refinement,{
    provider_poc:{shots:[{shot_key:'shot_02',binding_ready:true,sequence_ready:true,character_keys:[]}],characters:[]},
    provider_jobs:[{id:'other-shot-active',shot_key:'shot_03',status:'running'}],
    provider_versions:[{id:'shot-02-v2',shot_key:'shot_02',version:2,url:'/api/gen/file/shot-02-v2.mp4'}],
  },'shot_02',true);
  assert.doesNotMatch(otherShotOnly, /data-action="keep-original-refinement-shot"[^>]* disabled/);
});

test('accepted original shots remain auditable instead of appearing fixed', () => {
  const output = workspace.refinementHtml({
    current_refinement:{
      version:4,status:'draft',url:'/api/gen/file/full-preview.mp4',issues:[],
      assembly_status:{reassembly_required:false},
      shots:[{
        shot_key:'shot_17',sort_order:17,status:'ready',issue:null,
        refinement_resolution:{
          decision:'keep_original',issue_code:'identity_mismatch',
          issue_message:'人物不一致，需要重新检查并生成该镜头'
        }
      }]
    }
  });
  assert.match(output, /已保留原片/);
  assert.match(output, /已人工接受原片的已知问题/);
  assert.match(output, /人物不一致，需要重新检查并生成该镜头/);
  assert.doesNotMatch(output, /该镜头已通过精修检查/);
});

test('redo steps keep async results inside stable cards and preserve the viewport once', () => {
  const output = workspace.refinementRedoGenerationHtml({
    shot_key:'shot_02',sort_order:2
  }, {
    provider_poc:{
      shots:[{shot_key:'shot_02',sort_order:2,binding_ready:true,sequence_ready:true,character_keys:['hero']}],
      characters:[{character_key:'hero',name:'主角',binding_ready:true}]
    },
    provider_preview:{shot:{shot_key:'shot_02'},ready:true,message:'参数检查通过',request:{prompt:'稳定镜头提示词'}},
    provider_quote:{shot:{shot_key:'shot_02'},cost:30},
    provider_job:{shot_key:'shot_02',status:'succeeded',progress:100}
  }, true);
  assert.match(output, /data-provider-step="2"[\s\S]*sd-refinement-step-status[\s\S]*参数检查通过[\s\S]*<\/article>/);
  assert.match(output, /data-provider-step="3"[\s\S]*sd-refinement-step-status[\s\S]*30 点[\s\S]*<\/article>/);
  assert.match(output, /data-provider-step="4"[\s\S]*sd-refinement-step-status[\s\S]*镜头任务[\s\S]*<\/article>/);
  assert.match(output, /<details class="sd-refinement-step-details">/);
  assert.doesNotMatch(output, /sd-refinement-redo-steps">[\s\S]*<\/article><div class="sd-check/);

  const handlerSource = workspaceSource.slice(
    workspaceSource.indexOf("==='provider-preflight'"),
    workspaceSource.indexOf("==='jump-to-shot'")
  );
  assert.equal((handlerSource.match(/renderPreservingViewport\(\)/g)||[]).length, 3);
  assert.doesNotMatch(handlerSource, /\.finally\(function\(\)\{busy\(false\);render\(\);\}\)/);
});

test('refinement redo sidebar is a read-only progress summary', () => {
  const output = workspace.refinementRedoSummaryHtml({
    current_refinement:{
      shots:[{shot_key:'shot_02',sort_order:2,status:'degraded',issue:{message:'人物形象跳变'}}],
      issues:[{shot_key:'shot_02'}]
    }
  }, {
    provider_versions:[{id:'shot-02-v3',shot_key:'shot_02',version:3}]
  }, 'shot_02');
  assert.match(output, /处理进度/);
  assert.match(output, /当前镜头/);
  assert.match(output, /#2 · shot_02/);
  assert.match(output, /候选版本/);
  assert.doesNotMatch(output, /id="sdProviderShot"/);
  assert.doesNotMatch(output, /data-action="provider-preflight"/);
  assert.doesNotMatch(output, /data-action="provider-quote"/);
  assert.doesNotMatch(output, /data-action="provider-start"/);
});

test('full-film reassembly is offered only after every problem shot is accepted', () => {
  const ready = workspace.refinementHtml({
    current_refinement:{
      id:'refinement-v4',version:4,status:'draft',url:'/api/gen/file/full-preview.mp4',
      shots:[{shot_key:'shot_01',sort_order:1,status:'ready'}],issues:[],
      assembly_status:{reassembly_required:true,staged_count:1},
    },
  });
  assert.match(ready, /1 个候选镜头已采用/);
  assert.match(ready, /data-action="reassemble-refinement"/);
  assert.match(ready, /重新合成完整视频/);

  const pending = workspace.refinementHtml({
    current_refinement:{
      id:'refinement-v4',version:4,status:'draft',url:'/api/gen/file/full-preview.mp4',
      shots:[{shot_key:'shot_02',sort_order:2,status:'degraded',issue:{message:'仍需调整'}}],
      issues:[{shot_key:'shot_02'}],
      assembly_status:{reassembly_required:true,staged_count:1},
    },
  });
  assert.match(pending, /请继续处理剩余 1 个问题镜头/);
  assert.doesNotMatch(pending, /data-action="reassemble-refinement"/);
});

test('refinement shot timeline follows rendered order and actual durations', () => {
  const timeline = workspace.refinementShotTimeline([
    {shot_key:'shot_03',sort_order:3,media_validation:{duration_ms:7000}},
    {
      shot_key:'shot_01',sort_order:1,start_ms:1000,end_ms:5000,
      media_validation:{duration_ms:9000}
    },
    {shot_key:'shot_02',sort_order:2},
  ], {
    source_duration_ms:17000,
    shot_durations:[
      {shot_key:'shot_01',duration_ms:9999},
      {shot_key:'shot_02',duration_ms:6000},
      {shot_key:'shot_03',duration_ms:9999},
    ],
  });
  assert.equal(timeline.total_ms,25998);
  assert.deepEqual(timeline.entries.map(item => [
    item.shot_key,item.sort_order,item.start_ms,item.end_ms,item.duration_ms,
  ]), [
    ['shot_01',1,0,9999,9999],
    ['shot_02',2,9999,15999,6000],
    ['shot_03',3,15999,25998,9999],
  ]);
});

test('refinement player exposes shot locator, seeking and current-shot marking', () => {
  const output = workspace.refinementHtml({
    current_refinement:{
      version:4,status:'draft',url:'/api/gen/file/refinement.mp4',
      assembly_status:{source_duration_ms:9000,shot_durations:[
        {shot_key:'shot_01',duration_ms:4000},
        {shot_key:'shot_02',duration_ms:5000},
      ]},
      shots:[
        {shot_key:'shot_01',sort_order:1,status:'ready'},
        {shot_key:'shot_02',sort_order:2,status:'degraded',issue:{message:'背景跳变'}},
      ],
      issues:[{shot_key:'shot_02'}],
    },
  });
  assert.match(output, /data-refinement-player/);
  assert.match(output, /data-refinement-shot-locator/);
  assert.match(output, /data-total-ms="9000"/);
  assert.match(output, /data-action="seek-refinement-shot"[^>]*data-shot-key="shot_01"[^>]*data-start-ms="0"[^>]*data-end-ms="4000"/);
  assert.match(output, /data-shot-key="shot_02"[^>]*data-start-ms="4000"[^>]*data-end-ms="9000"/);
  assert.match(output, /sd-refinement-locator-shot ready current/);
  assert.match(output, /sd-refinement-locator-shot flagged/);
  assert.match(output, /data-action="mark-current-refinement-shot" data-shot-key="shot_01"/);
  assert.match(output, /有问题/);
  assert.match(workspaceSource, /\['loadedmetadata','durationchange','timeupdate','seeking','seeked'\]\.forEach/);
  assert.match(workspaceSource, /currentMs=currentMs\*timelineTotal\/videoDurationMs/);
  assert.match(workspaceSource, /refinementTargetMs=refinementTargetMs\*refinementVideoDurationMs\/refinementTimelineTotal/);
  assert.match(workspaceSource, /refinementVideo\.currentTime=refinementTargetMs\/1000/);
  assert.doesNotMatch(workspaceSource, /refinementVideo\.focus\(\)/);
  assert.match(workspaceStyle, /\.sd-refinement-locator-scroll\{overflow-x:auto/);
  assert.match(workspaceStyle, /\.sd-refinement-locator-shot\.current/);
  assert.match(workspaceStyle, /\.sd-refinement-locator-shot\{flex:0 0 var\(--shot-share\)\}/);
});

test('staged candidate pauses locator until the old full preview is reassembled', () => {
  const output = workspace.refinementHtml({
    current_refinement:{
      version:5,status:'draft',url:'/api/gen/file/old-refinement.mp4',
      assembly_status:{
        available:true,reassembly_required:true,staged_count:1,
        preview_duration_ms:9000,source_duration_ms:10000,
        shot_durations:[
          {shot_key:'shot_01',duration_ms:5000},
          {shot_key:'shot_02',duration_ms:5000},
        ],
      },
      shots:[
        {shot_key:'shot_01',sort_order:1,status:'ready'},
        {shot_key:'shot_02',sort_order:2,status:'ready'},
      ],
      issues:[],
    },
  });
  assert.match(output, /data-refinement-player/);
  assert.match(output, /data-refinement-shot-locator-paused/);
  assert.match(output, /当前完整预览与逐镜素材暂不一致/);
  assert.doesNotMatch(output, /data-action="seek-refinement-shot"/);
  assert.doesNotMatch(output, /data-action="mark-current-refinement-shot"/);
});

test('read-only permissions are reapplied to controls replaced by a render', () => {
  function control(action, insideSection) {
    const attributes = {};
    return {
      disabled:false,
      getAttribute(name){return name==='data-action'?action:(attributes[name]??null);},
      hasAttribute(name){return Object.hasOwn(attributes,name);},
      setAttribute(name,value){attributes[name]=String(value);},
      removeAttribute(name){delete attributes[name];},
      closest(){return insideSection?{}:null;},
    };
  }
  const seek = control('seek-refinement-shot', true);
  const history = control('toggle-history', true);
  const mark = control('mark-current-refinement-shot', true);
  const input = control('', true);
  let controls = [seek, history, mark, input];
  const root = {
    classList:{toggle(){}},
    querySelectorAll(){return controls;},
  };

  workspace.setWorkspaceBusyState(root, false, false);
  assert.equal(seek.disabled, false);
  assert.equal(history.disabled, false);
  assert.equal(mark.disabled, true);
  assert.equal(input.disabled, true);

  const rerenderedSeek = control('seek-refinement-shot', true);
  const rerenderedHistory = control('toggle-history', true);
  const rerenderedMark = control('mark-current-refinement-shot', true);
  const rerenderedInput = control('', true);
  controls = [rerenderedSeek, rerenderedHistory, rerenderedMark, rerenderedInput];
  workspace.setWorkspaceBusyState(root, false, false);
  assert.equal(rerenderedSeek.disabled, false);
  assert.equal(rerenderedHistory.disabled, false);
  assert.equal(rerenderedMark.disabled, true);
  assert.equal(rerenderedInput.disabled, true);
  assert.match(
    workspaceSource,
    /setWorkspaceBusyState\(root,workspaceBusy,state\.permissions\.can_edit\);\s*\n\s*}/,
  );
});

test('busy round trips preserve business-disabled controls for editable users', () => {
  function control(action, disabled) {
    const attributes = {};
    return {
      disabled:!!disabled,
      getAttribute(name){return name==='data-action'?action:(attributes[name]??null);},
      hasAttribute(name){return Object.hasOwn(attributes,name);},
      setAttribute(name,value){attributes[name]=String(value);},
      removeAttribute(name){delete attributes[name];},
      closest(){return {};},
    };
  }
  const gated = control('refine-shot', true);
  const ready = control('toggle-history', false);
  const root = {classList:{toggle(){}},querySelectorAll(){return [gated,ready];}};

  workspace.setWorkspaceBusyState(root, false, true);
  assert.equal(gated.disabled, true);
  assert.equal(ready.disabled, false);
  workspace.setWorkspaceBusyState(root, true, true);
  assert.equal(gated.disabled, true);
  assert.equal(ready.disabled, true);
  workspace.setWorkspaceBusyState(root, false, true);
  assert.equal(gated.disabled, true);
  assert.equal(ready.disabled, false);
  workspace.setWorkspaceBusyState(root, false, true);
  assert.equal(gated.disabled, true);
  assert.equal(ready.disabled, false);
});

test('restoring edit permission keeps the current rendered control state', () => {
  const attributes = {};
  const input = {
    disabled:true,
    getAttribute(name){return name==='data-action'?'':(attributes[name]??null);},
    hasAttribute(name){return Object.hasOwn(attributes,name);},
    setAttribute(name,value){attributes[name]=String(value);},
    removeAttribute(name){delete attributes[name];},
    closest(){return {};},
  };
  const root = {classList:{toggle(){}},querySelectorAll(){return [input];}};

  workspace.setWorkspaceBusyState(root, false, false);
  assert.equal(input.disabled, true);
  assert.equal(input.getAttribute('data-workspace-disabled-before-readonly'), 'true');

  input.disabled = false;
  workspace.setWorkspaceBusyState(root, false, true);
  assert.equal(input.disabled, false);
  assert.equal(input.hasAttribute('data-workspace-disabled-before-readonly'), false);
});

test('restoring edit permission revives persistent controls unless render recomputed their gate', () => {
  function control(disabled) {
    const attributes = {};
    return {
      disabled:!!disabled,
      getAttribute(name){return name==='data-action'?'':(attributes[name]??null);},
      hasAttribute(name){return Object.hasOwn(attributes,name);},
      setAttribute(name,value){attributes[name]=String(value);},
      removeAttribute(name){delete attributes[name];},
      closest(){return {};},
    };
  }
  const persistent = control(false);
  const recomputed = control(false);
  const recomputedReady = control(false);
  const root = {classList:{toggle(){}},querySelectorAll(){return [persistent,recomputed,recomputedReady];}};

  workspace.setWorkspaceBusyState(root, false, false);
  assert.equal(persistent.disabled, true);
  assert.equal(recomputed.disabled, true);
  assert.equal(recomputedReady.disabled, true);

  workspace.setWorkspaceControlDisabled(recomputed, true);
  workspace.setWorkspaceControlDisabled(recomputedReady, false);
  workspace.setWorkspaceBusyState(root, true, true);
  workspace.setWorkspaceBusyState(root, false, true);
  assert.equal(persistent.disabled, false);
  assert.equal(recomputed.disabled, true);
  assert.equal(recomputedReady.disabled, false);
  assert.equal(persistent.hasAttribute('data-workspace-disabled-before-readonly'), false);
  assert.equal(recomputed.hasAttribute('data-workspace-disabled-recomputed'), false);
});

test('镜头问题标记使用页面内弹窗并提供明确的问题类型', () => {
  assert.match(workspaceSource, /id="sdRefinementIssueModal"/);
  assert.match(workspaceSource, /id="sdRefinementIssueForm"/);
  assert.match(workspaceSource, /background_continuity/);
  assert.match(workspaceSource, /character_consistency/);
  assert.match(workspaceSource, /action_continuity/);
  assert.match(workspaceSource, /visual_artifact/);
  assert.doesNotMatch(workspaceSource, /window\.prompt\('请简要说明该镜头的问题'/);
  assert.match(workspaceStyle, /\.sd-refinement-issue-form/);
});

test('PR-5 正式交付展示 2K 播放器和不可变快照证据', () => {
  const output = workspace.refinementHtml({
    project:{title:'晚风偶遇'},
    current_delivery:{
      version:1,status:'ready',url:'/assets/meiye_video.mp4',
      input_hash:'abc123',
      snapshot:{resolution:'2k',refinement_version:3,immutable:true,deliverable:true},
    },
  });
  assert.match(output, /2K 正式成片 v1/);
  assert.match(output, /不可变交付快照/);
  assert.match(output, /abc123/);
  assert.doesNotMatch(output, /单独打开/);
  assert.match(output, /下载 2K 成片/);
  assert.match(output, /download="晚风偶遇-v1-2k\.mp4"/);
});

test('formal delivery stays disabled when the real executor is unavailable', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{id:'r2',status:'confirmed',issues:[]},
    billing:{
      formal_cost:0,
      mode:'disabled',
      delivery_enabled:false,
      deliverable:false,
      reason:'formal_executor_unavailable'
    }
  }, true);
  assert.match(output, /真实 2K 交付暂未启用/);
  assert.match(output, /不会询价、建单或扣点/);
  assert.match(output, /disabled/);
  assert.doesNotMatch(output, /data-action="start-delivery"/);
});

test('local deterministic delivery is labelled as a free non-deliverable demo', () => {
  const output = workspace.refinementHtml({
    project:{title:'晚风偶遇'},
    current_delivery:{
      version:2,status:'ready',url:'/assets/demo.mp4',input_hash:'demo123',
      snapshot:{
        resolution:'source',
        refinement_version:4,
        immutable:true,
        deliverable:false,
        output_kind:'demo_preview',
        adapter:'local_deterministic'
      }
    }
  });
  assert.match(output, /本地演示预览 v2/);
  assert.match(output, /不是 2K 正式交付文件/);
  assert.match(output, /不可交付的演示快照/);
  assert.match(output, /下载演示预览/);
  assert.match(output, /download="晚风偶遇-v2-preview\.mp4"/);
  assert.doesNotMatch(output, /2K 正式成片/);
});

test('delivery explains when the generated file address is missing', () => {
  const output = workspace.refinementHtml({
    project:{title:'晚风偶遇'},
    current_delivery:{version:1,status:'ready',url:'',snapshot:{deliverable:true}},
  });
  assert.match(output, /成片文件地址缺失，请刷新后重试/);
  assert.doesNotMatch(output, /下载 2K 成片/);
});

test('Provider executor renders preflight, quote, paid confirmation and result state', () => {
  const state = {
    confirmed_plan:{id:'plan-1',plan:{material_plan:[{shot_key:'shot_01'}]}},
    provider_poc:{
      provider:'heygen_cinematic',
      shots:[{
        shot_key:'shot_01',sort_order:1,duration_ms:5000,scene:'雨夜街道',
        character_keys:['reporter'],primary_character_key:'reporter',
        primary_avatar_id:'avatar-1',binding_ready:true
      }],
      characters:[{
        character_key:'reporter',name:'记者林夏',avatar_id:'avatar-1',
        binding_ready:true
      }],
      avatars:[{id:'avatar-1',name:'记者林夏',provider_bound:true}]
    },
    provider_preview:{
      ready:true,
      message:'预检通过',
      shot:{shot_key:'shot_01'},
      avatar:{id:'avatar-1'},
      character_key:'reporter',
      request:{
        prompt:'电影感写实短剧镜头',
        ratio:'16:9',
        resolution:'720p',
        duration_seconds:4,
        timeline_duration_seconds:3,
        assembly_trim_required:true
      },
      next_action:'可进入单镜头付费确认'
    },
    provider_quote:{
      quote_token:'quote-1',cost:50,shot:{shot_key:'shot_01'}
    },
    provider_job:{
      id:'job-1',shot_key:'shot_01',provider:'heygen_cinematic',
      status:'running',progress:45
    },
    production:{
      ready:false,
      mode:'provider_poc',
      message:'Provider 已配置',
      single_shot_executor_ready:true,
      provider:{selected:'heygen_cinematic',configured:true}
    }
  };
  const output = workspace.autodraftActionsHtml(state, true);
  const controls = workspace.providerShotControlsHtml({shot_key:'shot_01'}, state, true, 'shot_01');
  assert.match(output, /视频生成总览/);
  assert.match(controls, /data-action="provider-preflight"/);
  assert.doesNotMatch(output, /id="sdProviderShot"/);
  assert.doesNotMatch(output, /id="sdProviderAvatar"/);
  assert.match(output, /shot_01/);
  assert.match(output, /1\/1 个角色已锁定/);
  assert.match(controls, /免费检查生成参数/);
  assert.match(controls, /电影感写实短剧镜头/);
  assert.match(controls, /16:9 · 720p · 4 秒/);
  assert.match(controls, /剧本镜头为 3 秒；生成服务最低返回 4 秒/);
  assert.match(controls, /保留服务实际返回的完整镜头/);
  assert.match(controls, /确认扣 50 点并生成/);
  assert.match(controls, /视频任务 · running · 45%/);
  assert.match(output, /预检和报价不扣点/);
  assert.doesNotMatch(output, /create-provider-avatar/);
  assert.doesNotMatch(output, /refresh-provider-avatars/);
  assert.doesNotMatch(output, /data-action="start-draft"/);
});

test('Provider executor describes a succeeded shot as completed', () => {
  const output = workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1'},
    provider_poc:{
      provider:'heygen_cinematic',
      shots:[{
        shot_key:'shot_01',sort_order:1,duration_ms:5000,scene:'rainy street',
        character_keys:['reporter'],primary_character_key:'reporter',
        primary_avatar_id:'avatar-1',binding_ready:true
      }],
      characters:[{
        character_key:'reporter',name:'记者林夏',avatar_id:'avatar-1',
        binding_ready:true
      }],
      avatars:[{id:'avatar-1',name:'记者林夏',provider_bound:true}]
    },
    provider_job:{
      id:'job-1',shot_key:'shot_01',provider:'heygen_cinematic',
      status:'succeeded',progress:100,
      result:{url:'/api/gen/file/video/shot-01.mp4'}
    },
    production:{
      ready:false,
      mode:'provider_poc',
      message:'Provider 已配置',
      assembly:{required_count:6,ready_count:1},
      single_shot_executor_ready:true,
      provider:{selected:'heygen_cinematic',configured:true}
    }
  }, true);
  assert.match(output, /镜头 shot_01 已生成完成/);
  assert.match(output, /整体进度 · 已完成 1\/6 个镜头/);
  assert.match(output, /最近任务：shot_01 · succeeded · 100%/);
  assert.doesNotMatch(output, /heygen_cinematic|MiniMax|麦克视频/);
  assert.match(output, /data-action="jump-to-shot"/);
  assert.doesNotMatch(output, /<video/);
});

test('generated Provider video renders only when its matching shot is selected', () => {
  const version={
    version:2,status:'locked',script:{overview:{title:'测试剧本'},characters:[],acts:[],dialogue_lines:[],shots:[
      {shot_key:'shot_01',sort_order:1,duration_seconds:5,beat:'建立',visual:'第一镜'},
      {shot_key:'shot_02',sort_order:2,duration_seconds:5,beat:'冲突',visual:'第二镜'}
    ]}
  };
  const autodraft={
    provider_versions:[
      {id:'v2',shot_key:'shot_02',version:2,provider:'heygen_cinematic',url:'/api/files/video/shot-02-v2.mp4',created_at:22},
      {id:'v1',shot_key:'shot_02',version:1,provider:'heygen_cinematic',url:'/api/files/video/shot-02-v1.mp4',created_at:11}
    ],
    provider_job:{id:'job-2',shot_key:'shot_02',status:'succeeded',progress:100,provider:'heygen_cinematic'}
  };
  const output=workspace.scriptHtml(version,false,autodraft,'',true,{},'','',{}, {},'shot_02');
  const workspaceStart=output.indexOf('sd-single-shot-workspace');
  assert.match(output.slice(workspaceStart),/data-shot-key="shot_02"/);
  assert.doesNotMatch(output.slice(workspaceStart),/data-shot-key="shot_01"/);
  assert.match(output,/\/api\/files\/video\/shot-02-v2\.mp4/);
  assert.match(output, /镜头视频 · v2/);
  assert.match(output, /视频版本（2）/);
  assert.match(output, /采用此版本/);
});

test('历史视频版本可按需展开和收起完整生成提示词', () => {
  const prompt='开场固定中近景，人物缓慢转身。\n随后镜头向右平移，完整展示纪念墙上的照片与姓名，结尾停留在顾承川的表情上。<不得截断>';
  const version={
    version:2,status:'locked',script:{overview:{title:'测试剧本'},characters:[],acts:[],dialogue_lines:[],shots:[
      {shot_key:'shot_01',sort_order:1,duration_seconds:7,beat:'发现',visual:'查看纪念墙'}
    ]}
  };
  const output=workspace.scriptHtml(version,false,{
    provider_versions:[{
      id:'v1',shot_key:'shot_01',version:1,provider:'minimax_h3',
      url:'/api/files/video/shot-01-v1.mp4',selected:true,
      request_snapshot:{duration_seconds:7,resolution:'2k',prompt}
    }]
  },'',true,{},'','',{}, {},'shot_01');

  assert.match(output, /class="sd-shot-history-prompt"/);
  assert.match(output, /展开完整提示词/);
  assert.match(output, /收起提示词/);
  assert.match(output, /结尾停留在顾承川的表情上。&lt;不得截断&gt;/);
  assert.match(workspaceStyle, /\.sd-shot-history-prompt\[open\]/);
});

test('shot generation overview shows completed active failed and pending shots', () => {
  const shots=[
    {shot_key:'shot_01',sort_order:1},
    {shot_key:'shot_02',sort_order:2},
    {shot_key:'shot_03',sort_order:3},
    {shot_key:'shot_04',sort_order:4}
  ];
  const completed=workspace.shotGenerationOverviewHtml(shots,{
    provider_versions:[{shot_key:'shot_01',version:1}],
    provider_job:{shot_key:'shot_02',status:'running',progress:45}
  });
  assert.match(completed,/已生成 1 \/ 4 个镜头/);
  assert.match(completed,/还有 3 个镜头未完成/);
  assert.match(completed,/1 个生成中/);
  assert.match(completed,/2 个未生成/);
  assert.match(completed,/aria-valuenow="1"/);
  assert.match(completed,/style="width:25%"/);
  assert.equal((completed.match(/data-action="show-workspace-shot"/g)||[]).length,4);

  const failed=workspace.shotGenerationOverviewHtml(shots,{
    provider_job:{shot_key:'shot_03',status:'failed',progress:10}
  });
  assert.match(failed,/1 个失败/);
  assert.match(failed,/3 个未生成/);
});

test('single shot workspace defaults to failed shot and renders only the selected shot', () => {
  const version={version:3,status:'locked',script:{overview:{title:'测试短剧',logline:'一句话故事'},characters:[],dialogue_lines:[],shots:[
    {shot_key:'shot_01',sort_order:1,duration_seconds:5,beat:'建立',visual:'第一个镜头',purpose:'开场'},
    {shot_key:'shot_02',sort_order:2,duration_seconds:5,beat:'冲突',visual:'失败镜头',purpose:'冲突'},
    {shot_key:'shot_03',sort_order:3,duration_seconds:5,beat:'收束',visual:'第三个镜头',purpose:'结局'}
  ]}};
  const autodraft={provider_job:{shot_key:'shot_02',status:'failed',progress:20},provider_poc:{shots:[],characters:[]}};
  const failedDefault=workspace.scriptHtml(version,false,autodraft,'',true,{},'','',{},{});
  assert.match(failedDefault,/当前镜头 2 \/ 3/);
  assert.match(failedDefault,/失败镜头/);
  assert.doesNotMatch(failedDefault,/第一个镜头/);
  assert.doesNotMatch(failedDefault,/第三个镜头/);
  assert.equal((failedDefault.match(/class="sd-shot /g)||[]).length,1);
  assert.match(failedDefault,/data-action="show-workspace-shot"/);
  assert.match(failedDefault,/aria-pressed="true"/);

  const selected=workspace.scriptHtml(version,false,autodraft,'',true,{},'','',{}, {},'shot_03');
  assert.match(selected,/当前镜头 3 \/ 3/);
  assert.match(selected,/第三个镜头/);
  assert.doesNotMatch(selected,/失败镜头/);
  assert.match(selected,/data-action="step-workspace-shot" data-direction="1" disabled/);
});

test('Provider jobs remain indexed per shot across providers and terminal states', () => {
  const state={
    provider_job:{id:'legacy-latest',shot_key:'shot_03',status:'failed',provider:'minimax_h3'},
    provider_jobs:[
      {id:'job-running',shot_key:'shot_01',status:'running',progress:35,provider:'heygen_cinematic'},
      {id:'job-terminal',shot_key:'shot_02',status:'failed',progress:10,provider:'grok'},
      {id:'legacy-latest',shot_key:'shot_03',status:'failed',provider:'minimax_h3'}
    ]
  };

  const index=workspace.shotMediaIndex(state);

  assert.equal(index.shot_01.job.id,'job-running');
  assert.equal(index.shot_02.job.id,'job-terminal');
  assert.equal(index.shot_03.job.id,'legacy-latest');
});

test('Provider job collection counts and polls every active shot while keeping legacy compatibility', () => {
  const jobs=workspace.activeProviderJobs({
    provider_job:{id:'job-running',shot_key:'shot_01',status:'running'},
    provider_jobs:[
      {id:'job-running',shot_key:'shot_01',status:'running'},
      {id:'job-billing',shot_key:'shot_02',status:'billing'},
      {id:'job-done',shot_key:'shot_03',status:'succeeded'}
    ]
  });
  const legacy=workspace.activeProviderJobs({
    provider_job:{id:'legacy-only',shot_key:'shot_04',status:'queued'}
  });

  assert.deepEqual(jobs.map(item=>item.id),['job-running','job-billing']);
  assert.deepEqual(legacy.map(item=>item.id),['legacy-only']);
  assert.match(workspaceSource,/Promise\.all\(providerJobs\.map/);
});

test('Provider summary reports both active shot jobs after refresh', () => {
  const output=workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1'},
    provider_poc:{shots:[{shot_key:'shot_01'},{shot_key:'shot_02'}],characters:[]},
    provider_job:{id:'job-newer',shot_key:'shot_02',status:'queued',progress:5},
    provider_jobs:[
      {id:'job-older',shot_key:'shot_01',status:'running',progress:35,provider:'heygen_cinematic'},
      {id:'job-newer',shot_key:'shot_02',status:'queued',progress:5,provider:'grok'}
    ],
    production:{ready:false,mode:'provider_poc',message:'ready',provider:{configured:true}}
  },true);

  assert.match(output,/<b>2<\/b>/);
  assert.match(output,/shot_01/);
  assert.match(output,/shot_02/);
});

test('starting another Provider shot keeps the existing task collection', () => {
  const jobs=workspace.providerJobsWithResult({
    provider_job:{id:'old-same-shot',shot_key:'shot_02',status:'failed'},
    provider_jobs:[
      {id:'job-other-shot',shot_key:'shot_01',status:'running'},
      {id:'old-same-shot',shot_key:'shot_02',status:'failed'}
    ]
  },{id:'new-same-shot',shot_key:'shot_02',status:'queued'});

  assert.deepEqual(jobs.map(item=>item.id),['new-same-shot','job-other-shot']);
});

test('Provider PoC directs missing character bindings to the left character cards', () => {
  const state = {
    confirmed_plan:{id:'plan-1'},
    provider_poc:{
      provider:'heygen_cinematic',
      shots:[{
        shot_key:'shot_01',sort_order:1,duration_ms:5000,scene:'rainy street',
        character_keys:['reporter'],primary_character_key:'reporter',
        primary_avatar_id:'',binding_ready:false
      }],
      characters:[{
        character_key:'reporter',name:'记者林夏',avatar_id:'',
        binding_ready:false
      }],
      avatars:[]
    },
    production:{
      ready:false,
      mode:'provider_poc',
      message:'真实画面 Provider 已可预检；付费任务执行器尚未启用。',
      provider:{selected:'heygen_cinematic',configured:true}
    }
  };
  const output = workspace.autodraftActionsHtml(state, true);
  const controls = workspace.providerShotControlsHtml({shot_key:'shot_01'}, state, true, 'shot_01');
  assert.match(output, /角色形象尚未准备完整/);
  assert.match(output, /未绑定：记者林夏/);
  assert.match(output, /点击左侧角色卡/);
  assert.doesNotMatch(output, /data-action="create-provider-avatar"/);
  assert.doesNotMatch(output, /data-action="refresh-provider-avatars"/);
  assert.match(controls, /data-action="provider-preflight" data-shot-key="shot_01" type="button" disabled/);
});

test('locked scripts keep Provider video generation available while script editing stays disabled', () => {
  const providerState = {
    confirmed_plan:{id:'plan-1'},
    provider_poc:{
      provider:'grok',
      shots:[{
        shot_key:'shot_01',sort_order:1,duration_ms:5000,scene:'park',
        character_keys:['boy'],primary_character_key:'boy',binding_ready:true
      }],
      characters:[{character_key:'boy',name:'Boy',binding_ready:true}]
    },
    production:{provider:{selected:'grok'}}
  };
  const version = {
    version:1,status:'locked',script:{
      overview:{title:'Locked story'},characters:[],dialogue_lines:[],
      shots:[{shot_key:'shot_01',sort_order:1,duration_seconds:5,beat:'setup',visual:'Boy sits in a park.'}]
    }
  };
  const output = workspace.scriptHtml(version, false, providerState, 'shot_01', true);
  assert.match(output, /data-action="provider-preflight" data-shot-key="shot_01" type="button">/);
  assert.doesNotMatch(output, /data-action="edit-shot"/);
  assert.doesNotMatch(output, /data-action="move-shot-up"/);
  assert.doesNotMatch(output, /data-action="move-shot-down"/);
  assert.doesNotMatch(output, /data-action="copy-shot"/);
  assert.doesNotMatch(output, /data-action="add-shot-before"/);
  assert.doesNotMatch(output, /data-action="add-shot-after"/);
  assert.doesNotMatch(output, /data-action="smart-insert-shot"/);
  assert.doesNotMatch(output, /data-action="delete-shot"/);
  assert.doesNotMatch(output, /sd-shot-provider-disabled-reason/);
});

test('locked adjacent shots disable only structure actions that cross their boundary', () => {
  const shots = [
    {shot_key:'shot_01',sort_order:1,duration_seconds:5,visual:'start',dialogue_line_ids:['line_01']},
    {shot_key:'shot_02',sort_order:2,duration_seconds:5,visual:'locked middle',dialogue_line_ids:['line_02'],locked:true},
    {shot_key:'shot_03',sort_order:3,duration_seconds:5,visual:'end',dialogue_line_ids:['line_03']}
  ];
  assert.deepEqual(workspace.shotStructureCapabilities(shots,0,true), {
    enabled:true,moveUp:false,moveDown:false,copy:false,insertBefore:true,
    insertAfter:false,smartInsert:false,deleteShot:false
  });
  assert.deepEqual(workspace.shotStructureCapabilities(shots,2,true), {
    enabled:true,moveUp:false,moveDown:false,copy:true,insertBefore:false,
    insertAfter:true,smartInsert:true,deleteShot:false
  });
  const outerLocked = [
    {shot_key:'a',locked:true},{shot_key:'b'},{shot_key:'c'},{shot_key:'d',locked:true}
  ];
  assert.equal(workspace.shotStructureCapabilities(outerLocked,1,true).moveDown,false);
  assert.equal(workspace.shotStructureCapabilities(outerLocked,2,true).moveUp,false);

  const output = workspace.scriptHtml({version:1,status:'draft',script:{
    overview:{title:'Boundary story'},characters:[],shots:shots,
    dialogue_lines:[
      {id:'line_01',kind:'silence'},
      {id:'line_02',kind:'silence'},
      {id:'line_03',kind:'silence'}
    ]
  }}, true, {}, '', false, {}, '', '', {}, {}, 'shot_01');
  assert.match(output, /data-action="move-shot-down" data-shot-key="shot_01" disabled/);
  assert.match(output, /data-action="copy-shot" data-shot-key="shot_01" disabled/);
  assert.match(output, /data-action="add-shot-after" data-shot-key="shot_01" disabled/);
  assert.match(output, /data-action="smart-insert-shot" data-shot-key="shot_01" disabled/);
  assert.match(output, /data-action="delete-shot" data-shot-key="shot_01" disabled/);
  assert.match(output, /data-action="add-shot-before" data-shot-key="shot_01">/);
});

test('Provider video generation explains why it is disabled before the script is locked', () => {
  const providerState = {
    confirmed_plan:{id:'plan-1'},
    provider_poc:{
      provider:'grok',
      shots:[{
        shot_key:'shot_01',sort_order:1,duration_ms:5000,scene:'park',
        character_keys:['boy'],primary_character_key:'boy',binding_ready:true
      }],
      characters:[{character_key:'boy',name:'Boy',binding_ready:true}]
    },
    production:{provider:{selected:'grok'}}
  };
  const output = workspace.providerShotControlsHtml(
    {shot_key:'shot_01'}, providerState, false, 'shot_01',
    '请先确认并锁定当前剧本，再生成镜头视频。'
  );
  assert.match(output, /data-action="provider-preflight" data-shot-key="shot_01" type="button" disabled/);
  assert.match(output, /sd-shot-provider-disabled-reason/);
  assert.match(output, /请先确认并锁定当前剧本/);
});

test('single-shot generation errors render only inside the matching shot provider panel', () => {
  const providerState = {
    confirmed_plan:{id:'plan-1'},
    provider_poc:{
      provider:'minimax_h3_video',
      shots:[{
        shot_key:'shot_01',sort_order:1,duration_ms:5000,scene:'park',
        character_keys:['boy'],primary_character_key:'boy',binding_ready:true,
        sequence_ready:true
      }],
      characters:[{character_key:'boy',name:'Boy',binding_ready:true}]
    },
    production:{provider:{selected:'minimax_h3_video'}}
  };
  const message = '点数不足，本次需要 42 点，请充值后再试';
  const matching = workspace.providerShotControlsHtml(
    {shot_key:'shot_01'}, providerState, true, 'shot_01', '',
    {shot_01:message}
  );
  const otherShot = workspace.providerShotControlsHtml(
    {shot_key:'shot_01'}, providerState, true, 'shot_01', '',
    {shot_02:message}
  );

  assert.match(matching, /class="sd-check warning sd-shot-provider-error" role="alert"/);
  assert.match(matching, /本次生成未提交/);
  assert.match(matching, /点数不足，本次需要 42 点，请充值后再试/);
  assert.doesNotMatch(otherShot, /点数不足，本次需要 42 点，请充值后再试/);
});

test('all Provider shots expose the free 1080p assembly stage', () => {
  const output = workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1'},
    billing:{cost:0,mode:'provider_assets_already_charged'},
    production:{
      ready:true,
      mode:'provider_poc',
      assembly:{required_count:6,ready_count:6,missing_shot_keys:[],all_ready:true}
    }
  }, true);
  assert.match(output, /PR-4 · 合成预览/);
  assert.match(output, /全部镜头已完成/);
  assert.match(output, /6 个已生成镜头/);
  assert.match(output, /data-action="start-draft"/);
  assert.match(output, /合成 1080p 草稿/);
  assert.match(output, /本次合成不重复扣点/);
});

test('historical 768p MiniMax shots must be regenerated before sharp assembly', () => {
  const output = workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1'},
    billing:{cost:0,mode:'provider_assets_already_charged'},
    provider_poc:{provider:'minimax_h3',shots:[],characters:[]},
    production:{
      ready:false,
      mode:'provider_poc',
      message:'历史 768p 版本不符合高清草稿要求。',
      provider:{selected:'minimax_h3',configured:true},
      assembly:{
        required_count:2,ready_count:2,assets_ready:true,quality_ready:false,
        low_resolution_shot_keys:['shot_01'],all_ready:false
      }
    }
  }, true);
  assert.match(output, /shot_01/);
  assert.match(output, /768p/);
  assert.match(output, /原生 2K/);
  assert.doesNotMatch(output, /data-action="start-draft"/);
});

test('legacy reported 2K shots missing evidence are not mislabeled as 768p', () => {
  const output = workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1'},
    billing:{cost:0,mode:'provider_assets_already_charged'},
    permissions:{can_edit:true,can_recover_legacy_media:true},
    provider_poc:{provider:'minimax_h3',shots:[],characters:[]},
    production:{
      ready:false,
      mode:'provider_poc',
      message:'历史 2K 镜头缺少媒体校验记录。',
      provider:{selected:'minimax_h3',configured:true},
      assembly:{
        required_count:2,ready_count:2,assets_ready:true,quality_ready:false,
        low_resolution_shot_keys:[],
        media_verification_missing_shot_keys:['shot_01'],all_ready:false
      }
    }
  }, true);
  assert.match(output, /shot_01/);
  assert.match(output, /历史 2K/);
  assert.match(output, /缺少媒体校验记录/);
  assert.match(output, /data-action="recover-legacy-media"/);
  assert.match(output, /验证并恢复历史原片/);
  assert.doesNotMatch(output, /历史 768p/);
  assert.doesNotMatch(output, /data-action="start-draft"/);
  assert.match(workspaceSource, /client\.recoverLegacyMedia/);
  assert.match(workspaceSource, /data-action'\)==='recover-legacy-media'/);
});

test('failed 1080p assembly explains the failure and allows a safe retry', () => {
  const output = workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1'},
    current_job:{
      status:'failed',
      error:{detail:'服务器未安装或无法调用 FFprobe'}
    },
    billing:{cost:0,mode:'provider_assets_already_charged'},
    production:{
      ready:true,
      mode:'provider_poc',
      assembly:{required_count:6,ready_count:6,missing_shot_keys:[],all_ready:true}
    }
  }, true);
  assert.match(output, /上次合成失败/);
  assert.match(output, /FFprobe/);
  assert.match(output, /已经生成的镜头均已保留/);
  assert.match(output, /重新合成 1080p 草稿/);
  assert.match(output, /data-action="start-draft"/);
});

test('completed 1080p assembly exposes playback, open and free download actions', () => {
  const output = workspace.draftHtml({
    current_version:{
      version:1,status:'ready',url:'/api/gen/file/preview.mp4',
      manifest:{resolution:'1080p',duration_ms:30000,issues:[],shots:[]}
    }
  });
  assert.match(output, /1080p 全片草稿/);
  assert.match(output, /<video[^>]*controls/);
  assert.match(output, /单独打开/);
  assert.match(output, /下载预览/);
  assert.match(output, /download/);
});

test('refinement view keeps the completed full-film player easy to download', () => {
  const output = workspace.refinementHtml({
    current_refinement:{
      version:1,status:'draft',url:'/api/gen/file/preview.mp4',shots:[],issues:[]
    },
    refinement_versions:[{version:1}]
  });
  assert.match(output, /<video controls/);
  assert.doesNotMatch(output, /单独打开/);
  assert.match(output, /下载预览/);
  assert.match(output, /download/);
});

test('refinement warns when old preview truncated physical shots and offers free reassembly', () => {
  const output = workspace.refinementHtml({
    current_refinement:{
      id:'r-old',version:2,status:'draft',url:'/api/gen/file/old.mp4',shots:[],issues:[],
      assembly_status:{reassembly_required:true,source_duration_ms:64450,preview_duration_ms:60000}
    }
  });
  assert.match(output, /预览需要重新装配/);
  assert.match(output, /约 64 秒/);
  assert.match(output, /不会调用视频模型，也不会扣点/);
  assert.match(output, /data-action="reassemble-refinement"/);
});

test('refinement requires explicit full-film acceptance before locking', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{id:'r3',status:'draft',issues:[]}
  }, true);
  assert.match(output, /PR-5 · 全片验收/);
  assert.match(output, /无黑帧、花屏或明显生成瑕疵/);
  assert.equal((output.match(/data-acceptance-check/g)||[]).length, 6);
  assert.match(output, /data-acceptance-check="story_continuity"/);
  assert.match(output, /data-acceptance-check="subtitle_timing"/);
  assert.match(output, /data-action="confirm-refinement" disabled/);
  assert.match(output, /全片验收通过并锁定/);
  assert.match(workspaceSource, /source_hashes:requirements\.source_hashes/);
  assert.match(workspaceSource, /\/api\/gen\/short-drama\/refinement\/issues/);
  assert.match(workspaceSource, /preview\.replacement_ready!==true/);
  assert.match(workspaceSource, /replacement_provider_version_id:replacementVersionId/);
});

test('incomplete or unverifiable assembly disables full-film acceptance', () => {
  for (const assembly_status of [
    {available:false,reassembly_required:false},
    {available:true,reassembly_required:true}
  ]) {
    const output = workspace.refinementActionsHtml({
      current_refinement:{id:'r-blocked',status:'draft',issues:[],assembly_status},
      acceptance_requirements:{media:{ready:true}}
    }, true);
    assert.match(output, /data-acceptance-check="story_continuity" disabled/);
    assert.match(output, /data-action="confirm-refinement" disabled/);
    assert.match(output, /完整镜头时长|重新装配/);
  }
});

test('historically confirmed incomplete assembly cannot start delivery', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{
      id:'r-confirmed-old',status:'confirmed',issues:[],
      assembly_status:{available:true,reassembly_required:true}
    },
    billing:{delivery_enabled:true,mode:'local_ffmpeg',formal_cost:0}
  }, true);
  assert.match(output, /正式交付不可用/);
  assert.doesNotMatch(output, /data-action="start-delivery"/);
});

test('failed refinement assembly explains that the new shot is retained and retryable', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{id:'r-failed',status:'draft',issues:[{shot_key:'shot_02'}]},
    current_refinement_job:{
      status:'failed',shot_key:'shot_02',
      error:{detail:'上次重新合成没有完成'}
    }
  }, true);
  assert.match(output, /shot_02 尚未替换到全片/);
  assert.match(output, /新镜头不会丢失/);
  assert.match(output, /不会再次扣镜头生成费用/);
  assert.match(output, /data-action="jump-to-shot" data-shot-key="shot_02"/);
  assert.match(output, /回到这个镜头重试/);
});

test('media preparation is separate from issue shots and requires native video audio', () => {
  const refinement = {
    current_refinement:{
      id:'r-media',status:'draft',
      issues:[{code:'locked_voice_timeline_missing',message:'missing media timeline'}]
    },
    acceptance_requirements:{
      media:{
        ready:false,reason:'provider_native_audio_incomplete',mode:'provider_audio',
        invalid_shot_keys:['shot_02']
      }
    }
  };
  const groups = workspace.refinementIssueGroups(refinement.current_refinement);
  assert.equal(groups.shots.length, 0);
  assert.equal(groups.preparation.length, 1);
  const actions = workspace.refinementActionsHtml(refinement, true);
  assert.match(actions, /还有 1 项验收准备未完成/);
  assert.match(actions, /视频原生声音/);
  assert.match(actions, /shot_02/);
  assert.match(actions, /调整.*声音设计.*重新生成/);
  assert.doesNotMatch(actions, /data-action="go-to-voice-settings"/);
  assert.doesNotMatch(actions, /data-action="confirm-provider-audio"/);
  assert.doesNotMatch(actions, /data-action="confirm-silent-media"/);
  assert.doesNotMatch(actions, /还有 1 个问题镜头/);
  const provider = workspace.refinementProviderHtml({provider_poc:{shots:[]}}, refinement, true);
  assert.equal(provider, '');
});

test('provider audio mode keeps generated shot sound and does not require subtitles', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{id:'r-source-audio',status:'draft',issues:[]},
    acceptance_requirements:{media:{ready:true,mode:'provider_audio'}}
  }, true);
  assert.match(output, /镜头原声连续且音量正常/);
  assert.match(output, /当前声音：视频原生声音/);
  assert.match(output, /台词、环境声、动作音效和音乐由视频生成服务随画面生成/);
  assert.match(output, /已确认本片无需字幕/);
  assert.doesNotMatch(output, /静音模式符合预期/);
});

test('delivered projects show their immutable historical sound mode without new mode buttons', () => {
  const output = workspace.refinementActionsHtml({
    current_delivery:{snapshot:{deliverable:true}},
    media_preference:{mode:'silent'}
  }, true);
  assert.match(output, /当前交付快照保持不变/);
  assert.match(output, /当前声音：完全静音/);
  assert.doesNotMatch(output, /data-action="go-to-voice-settings"/);
  assert.doesNotMatch(output, /data-action="confirm-provider-audio"/);
  assert.doesNotMatch(output, /data-action="confirm-silent-media"/);
});

test('current workspace has no active separate voice or silent media handlers', () => {
  assert.doesNotMatch(workspaceSource, /getAttribute\('data-action'\)==='go-to-voice-settings'/);
  assert.doesNotMatch(workspaceSource, /getAttribute\('data-action'\)==='confirm-provider-audio'/);
  assert.doesNotMatch(workspaceSource, /getAttribute\('data-action'\)==='confirm-silent-media'/);
});

test('silent media acceptance uses silent-specific checklist labels', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{id:'r-silent',status:'draft',issues:[]},
    acceptance_requirements:{media:{ready:true,mode:'silent'}}
  }, true);
  assert.match(output, /静音模式符合预期/);
  assert.match(output, /已确认本片无需字幕/);
  assert.doesNotMatch(output, /配音\/字幕时间线尚未确认/);
});

test('refinement exposes the paid real-provider regeneration flow for issue shots', () => {
  const output = workspace.refinementProviderHtml({
    provider_poc:{shots:[{shot_key:'shot_02',sort_order:2,scene:'park',binding_ready:true}]},
    provider_preview:{ready:true,request:{prompt:'new physical shot'}},
    provider_quote:{cost:40}
  }, {
    current_refinement:{issues:[{shot_key:'shot_02'}]}
  }, true);
  assert.match(output, /问题镜头重新生成/);
  assert.match(output, /id="sdProviderShot"/);
  assert.match(output, /data-action="provider-preflight"/);
  assert.match(output, /data-action="provider-start"/);
  assert.match(output, /40 点/);
});

test('confirmed refinement exposes paid 2K export when local renderer is enabled', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{id:'r3',status:'confirmed',issues:[]},
    billing:{
      formal_cost:10,
      mode:'local_ffmpeg',
      delivery_enabled:true,
      deliverable:true,
      reason:'local_2k_renderer'
    }
  }, true);
  assert.match(output, /精修版本已确认/);
  assert.match(output, /10 点/);
  assert.match(output, /2K · 不可变快照/);
  assert.match(output, /原生 2K 镜头重新合成正式成片/);
  assert.doesNotMatch(output, /1080p 草稿生成2K/);
  assert.match(output, /data-action="start-delivery"/);
  assert.match(output, /导出 2K 正式成片/);
  assert.match(output, /确认后扣点/);
  assert.match(workspaceSource, /确认扣除.*点，导出 2K 正式成片/);
});

test('failed 2K export explains retry without regenerating shots', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{id:'r3',status:'confirmed',issues:[]},
    current_delivery_job:{
      id:'delivery-1',status:'failed',error:{detail:'正式成片时长与锁定时间线不一致'}
    },
    billing:{formal_cost:10,mode:'local_ffmpeg',delivery_enabled:true}
  }, true);
  assert.match(output, /上次导出未完成/);
  assert.match(output, /正式成片时长与锁定时间线不一致/);
  assert.match(output, /重新导出 2K 正式成片/);
  assert.match(output, /不会重新生成镜头或重复扣点/);
});

test('scene locking offers upload, prompt generation, preview and explicit confirmation', () => {
  const output = workspace.sceneLockingHtml({
    graph_revision:3,
    scenes:[{
      scene_key:'scene-group-1',name:'小区长椅',description:'傍晚的小区长椅',locked:false,custom:true,
      shots:[{shot_key:'shot_01',sort_order:1},{shot_key:'shot_02',sort_order:2}],
      preview:{url:'/api/gen/file/scene.png',prompt:'暖色夕阳下的小区长椅',status:'draft'}
    }]
  }, true, {}, 'scene-group-1');
  assert.match(output, /场景锁定/);
  assert.match(output, /已锁定 0 \/ 1/);
  assert.match(output, /aria-label="场景锁定进度"/);
  assert.match(output, /sd-scene-shot-tags/);
  assert.match(output, />#1</);
  assert.match(output, />#2</);
  assert.match(output, /sd-scene-prompt-editor/);
  assert.match(output, /编辑场景描述/);
  assert.match(output, /data-action="add-scene"/);
  assert.match(workspaceSource, /sceneLockingHtml\(sceneWorkspace,\(canEdit\|\|canGenerate\),sceneImageOperations,pendingSceneDeleteKey\)/);
  assert.match(output, /data-action="choose-scene-asset"/);
  assert.match(output, /data-scene-upload/);
  assert.match(output, /data-action="generate-scene-image"/);
  assert.match(output, /data-action="lock-scene-reference"/);
  assert.match(output, /data-action="preview-character-image"/);
  assert.match(workspaceSource, /asset-graph\/scenes\/reference/);
  assert.match(workspaceSource, /reference_source:'ai_generation'/);
  assert.match(workspaceSource, /client\.createScene/);
  assert.match(workspaceSource, /client\.updateScene/);
  assert.match(workspaceSource, /client\.deleteScene/);
  assert.match(workspaceSource, /client\.restoreScene/);
  assert.match(output, /确认移入回收站/);
  assert.match(output, /退出项目不会删除/);
  assert.match(workspaceSource, /关联镜头/);
});

test('scene image generation shows per-scene progress and survives refresh', () => {
  assert.match(workspaceSource, /recoverSceneImageOperations/);
  assert.match(workspaceSource, /正在提交任务/);
  assert.match(workspaceSource, /背景图生成中/);
  assert.match(workspaceSource, /正在保存背景图/);
  assert.match(workspaceSource, /可以继续处理其他场景或镜头/);
  assert.match(workspaceSource, /该场景的背景图正在生成，请勿重复提交/);
  assert.match(workspaceStyle, /\.sd-scene-generation-overlay/);
  assert.match(workspaceStyle, /@keyframes sd-scene-spin/);
});

test('scene workspace reloads for editable script review as well as locked scripts', () => {
  assert.equal(workspace.sceneWorkspaceRequired({
    conversation:{state:'script_review'},
    current_script:{id:'script-1',script:{shots:[{shot_key:'shot_01'}]}}
  }), true);
  assert.equal(workspace.sceneWorkspaceRequired({
    conversation:{state:'script_locked'},
    current_script:{id:'script-1',script:{shots:[]}}
  }), true);
  assert.equal(workspace.sceneWorkspaceRequired({
    conversation:{state:'direction_review'},current_script:null
  }), false);
  assert.match(workspaceSource, /if\(sceneWorkspaceRequired\(state\)\)\{\s*tasks\.push\(loadSceneWorkspace\(\)\)/);
});

test('shot workspace supports flexible structure and duration guidance', () => {
  assert.match(workspaceSource, /data-action="add-shot-after"/);
  assert.match(workspaceSource, /data-action="smart-insert-shot"/);
  assert.match(workspaceSource, /data-action="delete-shot"/);
  assert.match(workspaceSource, /data-action="copy-shot"/);
  assert.match(workspaceSource, /data-action="move-shot-up"/);
  assert.match(workspaceSource, /不会强制截断镜头/);
  assert.match(workspaceSource, /shot\/structure/);
});

test('locked shots do not expose structure mutation controls', () => {
  const rendered = workspace.scriptHtml({id:'script-1',script:{
    overview:{}, story_beats:[], characters:[],
    dialogue_lines:[{id:'line-1',kind:'silence',text:''}],
    shots:[{
      shot_key:'shot-1',sort_order:1,duration_seconds:5,locked:true,
      dialogue_line_ids:['line-1'],purpose:'locked',visual:'locked shot',
    }],
  }},true,{},'',true,{},'','',{graph_revision:1,scenes:[]},{},'shot-1',{},'');
  assert.match(rendered, /data-action="toggle-shot-lock"/);
  assert.doesNotMatch(rendered, /data-action="(?:move-shot|copy-shot|add-shot|delete-shot)/);
});

test('shot drafts are isolated by trusted account and legacy unowned keys are discarded', () => {
  const aliceKey = workspace.shotDraftStorageKey('alice','project-1','shot-1');
  const bobKey = workspace.shotDraftStorageKey('bob','project-1','shot-1');
  assert.notEqual(aliceKey,bobKey);
  assert.match(aliceKey,/^hq-short-drama-shot-draft:/);
  assert.doesNotMatch(aliceKey,/alice/);

  const storage = new Map();
  const legacyKey = 'hq-short-drama-shot-draft:project-1:shot-1';
  storage.set(legacyKey,JSON.stringify({dialogue_text:'private draft'}));
  workspace.discardLegacyShotDraft({removeItem:key => storage.delete(key)},'project-1','shot-1');
  assert.equal(storage.has(legacyKey),false);
});

test('镜头编辑只拦截错误字段并保留其余有效修改', () => {
  assert.equal(workspace.shotTimingIssue({
    duration_seconds:5, dialogue_kind:'dialogue', character_key:'character_1', dialogue_text:'你好'
  }), null);
  const longDialogue = workspace.shotTimingIssue({
    duration_seconds:4,
    dialogue_kind:'dialogue',
    character_key:'character_1',
    dialogue_text:'这是一段明显无法在四秒钟以内自然说完的镜头台词内容'
  });
  assert.equal(longDialogue.field, 'dialogue_text');
  assert.equal(longDialogue.relatedField, 'duration_seconds');
  assert.match(longDialogue.message, /请选择更快语速、精简台词、延长镜头或拆分到下一镜头/);
  assert.ok(workspace.dialogueReadingSeconds('这是一段需要加快语速的测试台词',1.5)<workspace.dialogueReadingSeconds('这是一段需要加快语速的测试台词',1));
  assert.equal(workspace.shotTimingIssue({
    duration_seconds:5, dialogue_kind:'dialogue', character_key:'character_1',
    dialogue_text:'这是一段需要加快语速才能说完的测试台词', speech_rate:1.5
  }), null);
  const fastStatus = workspace.shotTimingStatus({
    duration_seconds:10,
    dialogue_kind:'dialogue',
    character_key:'character_1',
    dialogue_text:'欢迎来到谁是大赢家辩论赛现场！今日辩题：面对对手的无理指责，究竟是该“大度容忍”还是“当场硬刚”？正反双方，开撕！',
    speech_rate:1.5
  });
  assert.equal(fastStatus.issue, null);
  assert.ok(fastStatus.reading_seconds < 10);
  assert.ok(fastStatus.remaining_seconds > 0);
  const extremeDialogue = '这是一段需要极快语速才能在短镜头内说完的测试台词';
  const extremeStatus = workspace.shotTimingStatus({
    duration_seconds:5,
    dialogue_kind:'dialogue',
    character_key:'character_1',
    dialogue_text:extremeDialogue,
    speech_rate:2
  });
  assert.equal(extremeStatus.issue, null);
  assert.ok(extremeStatus.reading_seconds < workspace.dialogueReadingSeconds(extremeDialogue, 1.5));
  assert.equal(workspace.shotTimingIssue({
    duration_seconds:3, dialogue_kind:'silence', dialogue_text:''
  }).field, 'duration_seconds');
  assert.match(workspaceSource, /hq-short-drama-shot-draft:/);
  assert.match(workspaceSource, /name="speech_rate"/);
  assert.match(workspaceSource, /极快 · 2\.0×/);
  assert.match(workspaceSource, /只影响当前镜头/);
  assert.match(workspaceSource, /data-shot-timing-hint/);
  assert.match(workspaceSource, /speech_rate:values\.speech_rate/);
  assert.match(workspaceSource, /保存未完成，请修改标红内容/);
  assert.match(workspaceSource, /if\(!issue\)\{\s*changes\.duration_seconds/);
  assert.match(workspaceSource, /function refreshShotTimingValidation\(form,shotKey\)/);
  assert.match(workspaceSource, /clearShotIssuePresentation\(form,shotKey\)/);
  assert.match(workspaceStyle, /\.sd-shot-editor label\.has-error textarea/);
  assert.match(workspaceStyle, /\.sd-shot-field-error/);
  assert.match(workspaceStyle, /\.sd-shot-timing-hint\.ready/);
});

test('单镜头支持多角色有序台词并按总朗读时间校验', () => {
  const dialogues = [
    {kind:'dialogue', character_key:'character_1', text:'你终于来了。', speech_rate:1},
    {kind:'dialogue', character_key:'character_2', text:'路上耽搁了一会儿。', speech_rate:1.15},
  ];
  assert.equal(workspace.shotTimingIssue({duration_seconds:6, dialogues}), null);
  const status = workspace.shotTimingStatus({duration_seconds:6, dialogues});
  assert.equal(status.dialogue_count, 2);
  assert.ok(status.reading_seconds > 0);
  assert.equal(
    status.reading_seconds,
    Math.round((
      workspace.dialogueReadingSeconds(dialogues[0].text, 1) +
      workspace.dialogueReadingSeconds(dialogues[1].text, 1.15)
    ) * 100) / 100,
  );
  const tooMany = workspace.shotTimingIssue({
    duration_seconds:15,
    dialogues:Array.from({length:7}, (_, index) => ({
      kind:'dialogue', character_key:'character_1', text:'第'+index+'句', speech_rate:2,
    })),
  });
  assert.equal(tooMany.code, 'dialogue_count_invalid');
  const missingSpeaker = workspace.shotTimingIssue({
    duration_seconds:6,
    dialogues:[{kind:'dialogue', character_key:'', text:'没有角色', speech_rate:1}],
  });
  assert.equal(missingSpeaker.dialogueIndex, 0);
  assert.match(workspaceSource, /data-dialogue-row/);
  assert.match(workspaceSource, /data-action="add-shot-dialogue"/);
  assert.match(workspaceSource, /data-action="remove-shot-dialogue"/);
  assert.match(workspaceSource, /data-action="move-shot-dialogue-up"/);
  assert.match(workspaceSource, /data-action="move-shot-dialogue-down"/);
  assert.match(workspaceSource, /changes\.dialogues=values\.dialogues/);
});

test('重复台词可保存且同时说话按并行组最长时长计算', () => {
  const repeated = '这块饼干是我的呀';
  const dialogues = [
    {kind:'dialogue', character_key:'character_1', text:repeated, speech_rate:1, timing_mode:'sequential'},
    {kind:'dialogue', character_key:'character_2', text:repeated, speech_rate:1, timing_mode:'simultaneous'},
  ];
  const status = workspace.shotTimingStatus({duration_seconds:4, dialogues});
  assert.equal(status.issue, null);
  assert.equal(status.reading_seconds, workspace.dialogueReadingSeconds(repeated, 1));
  assert.ok(status.reading_seconds < workspace.dialogueReadingSeconds(repeated, 1) * 2);
  assert.match(workspaceSource, /data-dialogue-field="timing_mode"/);
  assert.match(workspaceSource, /与上一条同时说/);
  assert.match(workspaceSource, /timing_mode:text\(field\('timing_mode'\)/);
});

test('重新打开镜头编辑器会保留服务端保存的同时说话顺序', () => {
  const restored = workspace.editableShotDialogues([
    {id:'line-1', kind:'dialogue', character_key:'character_1', text:'一起走。', speech_rate:1, timing_mode:'sequential'},
    {id:'line-2', kind:'dialogue', character_key:'character_2', text:'一起走。', speech_rate:1, timing_mode:'simultaneous'},
  ]);
  assert.deepEqual(restored.map(item => item.timing_mode), ['sequential', 'simultaneous']);
  assert.equal(workspace.shotTimingStatus({duration_seconds:4, dialogues:restored}).dialogue_count, 2);
});

test('镜头保存错误在当前编辑器内提示而不是外层通知', () => {
  assert.match(workspaceSource, /function presentShotIssue\(form,shotKey,issue\)/);
  assert.match(workspaceSource, /issue&&issue\.partial\?'镜头内容已保存，场景绑定需要重试'/);
  assert.match(workspaceSource, /镜头文字和其他有效信息已经保存；故事场景暂未绑定/);
  assert.match(workspaceSource, /function bindShotScene\(shotKey,sceneKey\)/);
  assert.match(workspaceSource, /保存未完成，请修改标红内容/);
  assert.match(workspaceSource, /relatedField:'duration_seconds'/);
  assert.match(workspaceSource, /presentShotIssue\(form,shot\.shot_key,backendShotIssue\(error,values\)\)/);
  assert.doesNotMatch(workspaceSource, /show\('其他可用内容已保存；请修改标红的内容后再次保存。',true\)/);
  assert.match(workspaceStyle, /\.sd-shot-save-summary\{position:sticky/);
});
