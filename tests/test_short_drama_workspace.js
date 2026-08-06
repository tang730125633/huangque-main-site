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
});

test('创作助手展示确认门禁、修改后重确认和结构化理解摘要', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.js'), 'utf8'
  );
  const css = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.css'), 'utf8'
  );
  assert.match(source, /请先确认创作方向/);
  assert.match(source, /修改后需要重新确认/);
  assert.match(source, /understanding\.direction_confirmed/);
  assert.match(source, /助手建议/);
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
  assert.match(faithful, /第 2 版/);
  assert.match(faithful, /contract-abc/);
  assert.match(faithful, /用户追加的必须保留内容/);
  assert.match(faithful, /原稿位置 32/);
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
  assert.match(source, /补充必须保留 \/ 允许优化的内容/);
});

test('客户端使用 Cookie 会话、独立接口和幂等键', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>'{}'};
  };
  const client = workspace.createClient(fetchImpl);
  await client.workspace('project a');
  await client.message({project_id:'a', conversation_revision:1, message:'你好'});
  await client.generate({project_id:'a', conversation_revision:2});
  await client.restore({project_id:'a', conversation_revision:3, version_id:'v1'});
  await client.lock({project_id:'a', conversation_revision:4, version_id:'v2'});
  await client.characterStudio('project a');
  await client.saveCharacterProfile({project_id:'a', project_revision:1, character_key:'lead', identity_text:'记者', personality:'敏锐', appearance_prompt:'短发', wardrobe_prompt:'风衣'});
  await client.bindCharacterAvatar({project_id:'a', project_revision:2, character_key:'lead', avatar_id:'7'});
  await client.generateCharacterImage({project_id:'a', revision:3, character_key:'lead'});
  await client.preflight('project a');
  await client.prepare({project_id:'a', conversation_revision:5, quality_route:'quick_draft'});
  await client.confirmPlan({project_id:'a', plan_id:'p1', plan_version:1, accepted_issue_keys:[]});
  await client.autodraft('project a');
  await client.providerPreflight({project_id:'a', plan_id:'p1', shot_key:'shot_01', avatar_id:'avatar-1'});
  await client.providerQuote({project_id:'a', plan_id:'p1', shot_key:'shot_01', avatar_id:'avatar-1'});
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
  assert.equal(calls[15].url, '/api/gen/short-drama/autodraft/provider-jobs');
  assert.equal(calls[16].url, '/api/gen/short-drama/autodraft/provider-jobs/provider%2F1?project_id=project%20a');
  assert.equal(calls[18].url, '/api/gen/short-drama/autodraft/jobs/job%2F1?project_id=project%20a');
  assert.equal(calls[19].url, '/api/gen/short-drama/projects');
  for (const call of calls) {
    assert.equal(call.options.credentials, 'same-origin');
    assert.equal(call.options.headers.Authorization, 'Bearer __cookie__');
    assert.equal(Object.hasOwn(call.options.headers, 'X-Canvas-Board-Id'), false);
  }
  for (const call of calls.filter(call => call.options.method === 'POST')) {
    assert.ok(call.options.headers['Idempotency-Key']);
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

test('角色工作室包含档案、形象生成、形象库绑定和角色感知预检交互', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.js'), 'utf8'
  );
  const css = fs.readFileSync(
    path.join(ROOT, 'site/workbench/short-drama-workspace.css'), 'utf8'
  );
  assert.match(source, /id="sdCharacterModal"/);
  assert.match(source, /data-action="open-character"/);
  assert.match(source, /data-action="generate-character-image"/);
  assert.match(source, /data-action="bind-character-avatar"/);
  assert.match(source, /data-action="create-character-avatar"/);
  assert.match(source, /data-action="lock-script-for-character"/);
  assert.match(source, /先锁定剧本，再选择人物形象/);
  assert.match(source, /锁定后可选择/);
  assert.match(source, /data-action="retry-character-studio"/);
  assert.match(source, /avatar_id:providerShot\.primary_avatar_id/);
  assert.match(source, /character_key:providerShot\.primary_character_key/);
  assert.match(css, /\.sd-character-modal/);
  assert.match(css, /\.sd-character-card/);
  assert.match(css, /\.sd-character-prerequisite/);
});

test('锁定后对话区变为可折叠只读历史并支持复制为新项目', () => {
  const shell = workspace.shellHtml();
  const payload = workspace.cloneProjectPayload({
    title:'暴雨录音', synopsis:'未来录音', ratio:'9:16',
    target_duration:45, shot_count:9, visual_style:'悬疑写实',
    target_platform:'视频号', point_budget:300
  });
  assert.match(shell, /data-action="toggle-history"/);
  assert.match(shell, /data-action="clone-project"/);
  assert.match(shell, /基于当前项目创建新版本/);
  assert.equal(payload.title, '暴雨录音 · 新版本');
  assert.equal(payload.synopsis, '未来录音');
  assert.equal(payload.ratio, '9:16');
  assert.equal(payload.target_duration, 45);
  assert.equal(payload.shot_count, 9);
  assert.equal(payload.point_budget, 300);
});

test('结构化剧本渲染角色、三幕、镜头和台词', () => {
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
  assert.match(output, /三幕结构/);
  assert.match(output, /雨中近景/);
  assert.match(output, /你终于来了/);
});

test('v4 故事板展示质量门禁、节拍、静默镜头和单镜头操作', () => {
  const output = workspace.scriptHtml({
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
  }, true);
  assert.match(output, /质量门禁/);
  assert.match(output, /交代成绩落差/);
  assert.match(output, /静默表演/);
  assert.match(output, /data-action="edit-shot"/);
  assert.match(output, /data-action="regenerate-shot"/);
  assert.match(output, /data-action="toggle-shot-lock"/);
  assert.match(output, /Provider 提示词/);
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
  assert.match(ready, /尚未选择真实画面 Provider/);
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
  assert.match(output, /镜头 shot_01 已由 heygen_cinematic 生成完成/);
  assert.doesNotMatch(output, /镜头 shot_01 正在由 heygen_cinematic 处理/);
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
  assert.match(output, /历史视频版本（2）/);
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
  assert.equal(
    workspace.avatarCreateUrl(),
    '/workbench/video.html?function=cinematic&action=create-avatar'
  );
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
  assert.match(output, /6 个真实 Provider 镜头/);
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
  assert.match(output, /问题镜头真实重生成/);
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
