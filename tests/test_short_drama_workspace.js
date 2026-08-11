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

test('scene image generation carries project billing identity', () => {
  assert.match(
    workspaceSource,
    /short_drama_scene_binding:\{project_id:payload\.project_id,scene_key:payload\.scene_key\}/
  );
});

test('workspace aria modals trap focus, close on Escape, and restore trigger focus', () => {
  assert.match(workspaceSource, /function modalKeydown\(modal,event,onClose\)/);
  assert.match(workspaceSource, /event\.key==='Escape'/);
  assert.match(workspaceSource, /event\.key!=='Tab'/);
  assert.match(workspaceSource, /if\(shotEditorTrigger\)shotEditorTrigger\.focus\(\)/);
  assert.match(workspaceSource, /if\(characterImagePreviewTrigger\)characterImagePreviewTrigger\.focus\(\)/);
});

test('shot progress jump moves focus and respects reduced motion', () => {
  assert.match(
    workspaceSource,
    /tabindex="-1"><header>/
  );
  assert.match(workspaceSource, /targetShot\.focus\(\{preventScroll:true\}\)/);
  assert.match(
    workspaceSource,
    /matchMedia\('\(prefers-reduced-motion: reduce\)'\)\.matches/
  );
  assert.match(workspaceSource, /behavior:reduceMotion\?'auto':'smooth'/);
  assert.match(workspaceStyle, /@media\(prefers-reduced-motion:reduce\)/);
  assert.match(workspaceStyle, /\.sd-shot\.focused\{animation:none\}/);
});

test('poll rendering preserves a dirty open shot editor', () => {
  assert.match(
    workspaceSource,
    /if\(!wasHidden&&shotEditorDirty&&body\.querySelector\('form'\)\)return/
  );
  assert.match(
    workspaceSource,
    /closest\('#sdShotEditor,#sdShotExecutionEditor'\).*shotEditorDirty=true/
  );
});

test('image lightbox backdrop is outside the focus order and trap scope', () => {
  assert.match(
    workspaceSource,
    /<div class="sd-character-image-lightbox-backdrop"[^>]*aria-hidden="true"/
  );
  assert.doesNotMatch(
    workspaceSource,
    /<button[^>]*sd-character-image-lightbox-backdrop/
  );
  assert.match(
    workspaceSource,
    /modalKeydown\(imageLightbox\.querySelector\('\[role="dialog"\]'\),event\)/
  );
});

test('独立页面加载三栏对话工作区资源', () => {
  assert.match(html, /id="shortDramaWorkspace"/);
  assert.match(html, /short-drama-workspace\.css\?v=/);
  assert.match(html, /short-drama-workspace\.js\?v=/);
  assert.match(stamp, /Asset\("short-drama-workspace\.js"/);
  assert.match(stamp, /Asset\("short-drama-workspace\.css"/);
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
  assert.match(workspaceSource, /项目摘要/);
  assert.match(workspaceSource, /querySelectorAll\('\[data-action="generate"\]'\)/);
  assert.match(workspaceStyle, /\.sd-script-empty-action/);
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
  assert.match(source, /请先确认创作方向/);
  assert.match(source, /修改后需要重新确认/);
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
  assert.match(source, /import_review:'原稿理解待确认'/);
  assert.match(source, /请核对右侧的故事设定与原稿理解/);
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

test('Scene image response loss recovers the paid job and binds with the latest graph revision', async () => {
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
  const payload = {
    project_id:'project-1',scene_key:'station',
    prompt:'empty cinematic station',ratio:'16:9'
  };
  let firstKey = '';
  try {
    const interrupted = workspace.createClient(async (url,options) => {
      firstKey = options.headers['Idempotency-Key'];
      throw new Error('response lost');
    });
    await assert.rejects(
      interrupted.generateSceneImage(payload,'alice'), /response lost/
    );
    assert.equal(storage.size,1);

    const restored = workspace.createClient(async (url,options) => {
      calls.push({url,options});
      let response = {};
      if(url==='/api/gen/image'){
        assert.equal(options.headers['Idempotency-Key'],firstKey);
        response={job_id:41};
      }else if(url==='/api/gen/job/41'){
        response={status:'done',result:{urls:['/content_out/station.png']}};
      }else if(url==='/api/gen/short-drama/asset-graph/scenes?project_id=project-1'){
        response={project_id:'project-1',graph_revision:9,scenes:[]};
      }else if(url==='/api/gen/short-drama/asset-graph/scenes/reference'){
        const body=JSON.parse(options.body);
        assert.equal(body.graph_revision,9);
        assert.equal(body.scene_key,'station');
        assert.equal(body.asset_job_id,41);
        assert.equal(body.asset_url,'/content_out/station.png');
        response={graph_revision:10,scenes:[]};
      }
      return {ok:true,status:200,text:async()=>JSON.stringify(response)};
    });
    const recovered = await restored.recoverSceneImageOperations('alice');
    assert.equal(recovered.length,1);
    assert.equal(storage.size,0);
    assert.deepEqual(calls.map(call=>call.url),[
      '/api/gen/image',
      '/api/gen/job/41',
      '/api/gen/short-drama/asset-graph/scenes?project_id=project-1',
      '/api/gen/short-drama/asset-graph/scenes/reference'
    ]);
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
    {direction_confirmed:false},
    '确认尊重原稿并生成'
  );
  assert.match(output, /data-action="confirm-direction"/);
  assert.match(output, /确认尊重原稿并生成/);
  assert.doesNotMatch(output, /data-action="generate"/);
  assert.doesNotMatch(output, /创作助手/);
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
  assert.match(output, /生成提示词/);
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
  await client.refinementJob('project a','job/2');
  await client.confirmRefinement({project_id:'project a',version_id:'rv2'});
  await client.restoreRefinement({project_id:'project a',version_id:'rv1'});
  await client.deliveryQuote({project_id:'project a',version_id:'rv2'});
  await client.startDelivery({project_id:'project a',quote_token:'quote1'});
  await client.deliveryJob('project a','delivery/1');
  assert.equal(calls[0].url, '/api/gen/short-drama/refinement?project_id=project%20a');
  assert.equal(calls[3].url, '/api/gen/short-drama/refinement/jobs/job%2F2?project_id=project%20a');
  assert.equal(calls[8].url, '/api/gen/short-drama/delivery/jobs/delivery%2F1?project_id=project%20a');
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
  assert.match(output, /data-action="refine-shot"/);
  assert.match(output, /data-shot-key="shot_02"/);
  assert.match(output, /1 个待处理/);

  const blocked = workspace.refinementActionsHtml({
    current_refinement:{id:'r2',status:'draft',issues:[{shot_key:'shot_02'}]},
  }, true);
  assert.match(blocked, /data-action="confirm-refinement" disabled/);
});

test('PR-5 正式交付展示 1080p 播放器和不可变快照证据', () => {
  const output = workspace.refinementHtml({
    current_delivery:{
      version:1,status:'ready',url:'/assets/meiye_video.mp4',
      input_hash:'abc123',
      snapshot:{resolution:'1080p',refinement_version:3,immutable:true,deliverable:true},
    },
  });
  assert.match(output, /1080p 正式成片 v1/);
  assert.match(output, /不可变交付快照/);
  assert.match(output, /abc123/);
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
  assert.match(output, /真实 1080p 交付暂未启用/);
  assert.match(output, /不会询价、建单或扣点/);
  assert.match(output, /disabled/);
  assert.doesNotMatch(output, /data-action="start-delivery"/);
});

test('local deterministic delivery is labelled as a free non-deliverable demo', () => {
  const output = workspace.refinementHtml({
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
  assert.match(output, /不是 1080p 正式交付文件/);
  assert.match(output, /不可交付的演示快照/);
  assert.doesNotMatch(output, /1080p 正式成片/);
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
        duration_seconds:5
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
      single_shot_executor_ready:true,
      provider:{selected:'heygen_cinematic',configured:true}
    }
  }, true);
  assert.match(output, /镜头 shot_01 已生成完成/);
  assert.doesNotMatch(output, /heygen_cinematic|MiniMax|麦克视频/);
  assert.match(output, /data-action="jump-to-shot"/);
  assert.doesNotMatch(output, /<video/);
});

test('generated Provider videos render under their matching script shots', () => {
  const version={
    version:2,status:'locked',script:{overview:{title:'测试剧本'},characters:[],acts:[],dialogue_lines:[],shots:[
      {shot_key:'shot_01',sort_order:1,duration_seconds:5,beat:'建立',visual:'第一镜'},
      {shot_key:'shot_02',sort_order:2,duration_seconds:5,beat:'冲突',visual:'第二镜'}
    ]}
  };
  const output=workspace.scriptHtml(version,false,{
    provider_versions:[
      {id:'v2',shot_key:'shot_02',version:2,provider:'heygen_cinematic',url:'/api/files/video/shot-02-v2.mp4',created_at:22},
      {id:'v1',shot_key:'shot_02',version:1,provider:'heygen_cinematic',url:'/api/files/video/shot-02-v1.mp4',created_at:11}
    ],
    provider_job:{id:'job-2',shot_key:'shot_02',status:'succeeded',progress:100,provider:'heygen_cinematic'}
  });
  const first=output.indexOf('data-shot-key="shot_01"');
  const second=output.indexOf('data-shot-key="shot_02"');
  const video=output.indexOf('/api/files/video/shot-02-v2.mp4');
  assert.ok(first>=0&&second>first&&video>second);
  assert.match(output, /尚未生成镜头视频/);
  assert.match(output, /镜头视频 · v2/);
  assert.match(output, /视频版本（2）/);
  assert.match(output, /采用此版本/);
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
  assert.equal((completed.match(/data-action="jump-to-shot"/g)||[]).length,4);

  const failed=workspace.shotGenerationOverviewHtml(shots,{
    provider_job:{shot_key:'shot_03',status:'failed',progress:10}
  });
  assert.match(failed,/1 个失败/);
  assert.match(failed,/3 个未生成/);
});

test('all active Provider shot jobs are indexed by their own shot', () => {
  const shots=[
    {shot_key:'shot_01',sort_order:1},
    {shot_key:'shot_02',sort_order:2},
    {shot_key:'shot_03',sort_order:3}
  ];
  const state={provider_jobs:[
    {id:'job-1',shot_key:'shot_01',status:'running',progress:20},
    {id:'job-2',shot_key:'shot_02',status:'queued',progress:5}
  ]};

  const index=workspace.shotMediaIndex(state);
  const output=workspace.shotGenerationOverviewHtml(shots,state);

  assert.equal(index.shot_01.job.id,'job-1');
  assert.equal(index.shot_02.job.id,'job-2');
  assert.match(output,/2 个生成中/);
  assert.match(output,/1 个未生成/);
});

test('an active job on one shot does not block another shot submission', () => {
  const state={
    provider_job:{id:'job-1',shot_key:'shot_01',status:'running',progress:20},
    provider_jobs:[{id:'job-1',shot_key:'shot_01',status:'running',progress:20}],
    provider_poc:{
      shots:[{shot_key:'shot_02',binding_ready:true,character_keys:[]}],
      characters:[]
    }
  };

  const output=workspace.providerShotControlsHtml(
    {shot_key:'shot_02'},state,true,'shot_02'
  );

  assert.doesNotMatch(output,/另一个镜头正在生成/);
  assert.match(output,/data-action="provider-preflight"[^>]*>/);
  assert.doesNotMatch(output,/data-action="provider-preflight"[^>]*disabled/);
});

test('Provider summary counts every active shot job', () => {
  const output=workspace.autodraftActionsHtml({
    confirmed_plan:{id:'plan-1'},
    provider_poc:{shots:[{shot_key:'shot_01'},{shot_key:'shot_02'}],characters:[]},
    provider_job:{id:'job-1',shot_key:'shot_01',status:'running',progress:20},
    provider_jobs:[
      {id:'job-1',shot_key:'shot_01',status:'running',progress:20},
      {id:'job-2',shot_key:'shot_02',status:'queued',progress:5}
    ],
    production:{ready:false,mode:'provider_poc',message:'ready',provider:{configured:true}}
  },true);

  assert.match(output,/<b>2<\/b> 个任务处理中/);
  assert.match(output,/shot_01/);
  assert.match(output,/shot_02/);
});

test('polling selects every active Provider shot job', () => {
  const jobs=workspace.activeProviderJobs({
    provider_jobs:[
      {id:'job-1',status:'running'},
      {id:'job-2',status:'billing'},
      {id:'job-3',status:'succeeded'}
    ]
  });

  assert.deepEqual(jobs.map(item=>item.id),['job-1','job-2']);
  assert.match(workspaceSource,/Promise\.all\(providerJobs\.map/);
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
  assert.doesNotMatch(output, /sd-shot-provider-disabled-reason/);
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

test('all Provider shots expose the 720p assembly stage without charging again', () => {
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
  assert.match(output, /合成 720p 预览/);
  assert.match(output, /本次合成不重复扣点/);
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
  assert.match(workspaceSource, /replacement_provider_version_id:preview\.replacement_provider_version_id/);
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

test('confirmed refinement exposes real 1080p export when local renderer is enabled', () => {
  const output = workspace.refinementActionsHtml({
    current_refinement:{id:'r3',status:'confirmed',issues:[]},
    billing:{
      formal_cost:0,
      mode:'local_ffmpeg',
      delivery_enabled:true,
      deliverable:true,
      reason:'local_1080p_renderer'
    }
  }, true);
  assert.match(output, /精修版本已确认/);
  assert.match(output, /1080p · 不可变快照/);
  assert.match(output, /data-action="start-delivery"/);
  assert.match(output, /生成 1080p 正式成片/);
  assert.match(output, /不重复扣点/);
});

test('scene locking offers upload, prompt generation, preview and explicit confirmation', () => {
  const output = workspace.sceneLockingHtml({
    graph_revision:3,
    scenes:[{
      scene_key:'scene-group-1',name:'小区长椅',description:'傍晚的小区长椅',locked:false,
      shots:[{shot_key:'shot_01',sort_order:1},{shot_key:'shot_02',sort_order:2}],
      preview:{url:'/api/gen/file/scene.png',prompt:'暖色夕阳下的小区长椅',status:'draft'}
    }]
  }, true);
  assert.match(output, /场景锁定/);
  assert.match(output, /用于镜头 #1、#2/);
  assert.match(output, /data-scene-upload/);
  assert.match(output, /data-action="generate-scene-image"/);
  assert.match(output, /data-action="lock-scene-reference"/);
  assert.match(output, /data-action="preview-character-image"/);
  assert.match(workspaceSource, /asset-graph\/scenes\/reference/);
  assert.match(workspaceSource, /reference_source:'ai_generation'/);
});
