const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const zlib = require('node:zlib');

const ROOT = path.resolve(__dirname, '..');
const center = require(path.join(ROOT, 'site/workbench/short-drama-center.js'));
const html = fs.readFileSync(path.join(ROOT, 'site/workbench/short-drama.html'), 'utf8');
const shell = fs.readFileSync(path.join(ROOT, 'site/workbench/cloud-shell.js'), 'utf8');
const centerScript = fs.readFileSync(path.join(ROOT, 'site/workbench/short-drama-center.js'), 'utf8');
const centerStyle = fs.readFileSync(path.join(ROOT, 'site/workbench/short-drama-center.css'), 'utf8');

function chromeCandidates(platform = process.platform) {
  if (platform === 'win32') {
    return [
      'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
      'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    ];
  }
  if (platform === 'darwin') {
    return [
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      '/Applications/Chromium.app/Contents/MacOS/Chromium',
      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    ];
  }
  return [
    '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium', '/usr/bin/chromium-browser',
  ];
}

function findChromeExecutable() {
  return chromeCandidates().find(candidate => fs.existsSync(candidate));
}

test('一级导航包含独立短剧入口和专用图标', () => {
  assert.match(shell, /\{k:'short-drama',l:'短剧创作',i:'clapper'\}/);
  assert.match(shell, /clapper:/);
  assert.match(html, /data-active="short-drama"/);
});

test('项目中心提供列表筛选、创建和详情入口', () => {
  for (const id of ['shortDramaGrid', 'shortDramaSearch', 'shortDramaStageFilter',
    'shortDramaCreate', 'shortDramaDialog', 'shortDramaDrawer', 'shortDramaDeleteProject']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /进入对话式工作区/);
  assert.match(html, />删除短剧<\/button>/);
  assert.doesNotMatch(html, /value="1:1"/);
});

test('创建短剧提供想法、灵感和导入已有剧本三种入口', () => {
  assert.match(html, /data-create-mode="idea"/);
  assert.match(html, /data-create-mode="inspiration"/);
  assert.match(html, /data-create-mode="import"/);
  assert.match(html, /我有想法/);
  assert.match(html, /我没有想法/);
  assert.match(html, /导入已有剧本/);
  assert.match(html, /id="shortDramaIdeaChat"/);
  assert.match(html, /id="shortDramaRecommendations"/);
  for (const id of ['shortDramaImport', 'shortDramaImportFile', 'shortDramaImportText',
    'shortDramaAnalyzeImport', 'shortDramaImportForm', 'shortDramaImportSubmit',
    'shortDramaImportFileText', 'shortDramaRemoveImportFile']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /aria-label="删除已导入的剧本"/);
  assert.match(centerScript, /if\(mode==='inspiration'\)\{startPlanner\(\);return;\}/);
});

test('导入分析识别人名、场景并映射到当前短剧规格', () => {
  const script = `《雨夜来信》\n场景一 外景 雨夜车站\n林夏：你还是来了。\n周野：这封信，我等了十年。\n场景二 内景 末班车\n林夏：结局不该是这样。`;
  const result = center.analyzeImportedScript(script, '雨夜来信.md');
  assert.equal(result.title, '雨夜来信');
  assert.equal(result.character_count, 2);
  assert.equal(result.scene_count, 2);
  assert.deepEqual(result.characters, ['林夏', '周野']);
  assert.ok([30, 45, 60].includes(result.duration));
  assert.ok([6, 8, 10].includes(result.shot_count));
  assert.match(result.synopsis, /雨夜来信/);
});

test('长剧本作为单个完整原稿快照提交', () => {
  const source = '人物：这是一段对白。'.repeat(900);
  const analysis = center.analyzeImportedScript(source, '长剧本.txt');
  const form = {elements:{title:{value:'长剧本'},ratio:{value:'16:9'},target_duration:{value:'60'},shot_count:{value:'10'},visual_style:{value:'电影写实'}}};
  const payload = center.importProjectPayload(form, analysis, 'faithful');
  assert.equal(payload.source_text, source);
  assert.equal(payload.import_mode, 'faithful');
  assert.equal(Object.hasOwn(center, 'buildImportMessages'), false);
});

test('导入项目沿用独立短剧创建字段', () => {
  const analysis = center.analyzeImportedScript('场景一\n林夏：我决定回家。', '回家.txt');
  const fields = {
    title:{value:'回家'}, ratio:{value:'9:16'}, target_duration:{value:'45'},
    shot_count:{value:'8'}, visual_style:{value:'温暖写实'}
  };
  assert.deepEqual(center.importProjectPayload({elements:fields}, analysis, 'faithful'), {
    title:'回家', synopsis:analysis.synopsis, ratio:'9:16', target_duration:45,
    shot_count:8, visual_style:'温暖写实', source_text:analysis.source,
    filename:'回家.txt', import_mode:'faithful'
  });
});

test('灵感助手按缺失信息动态追问并输出三个可编辑方向', () => {
  assert.match(center.advisorStep([]).message, /哪一类内容/);
  const payload = {visual_style:'电影感写实'};
  const answers = {topic:'家庭情感'};
  assert.equal(center.advisorStep(['家庭情感'], payload, answers).field, 'protagonist');
  answers.protagonist = '独居老人';
  assert.equal(center.advisorStep(['家庭情感', '独居老人'], payload, answers).field, 'conflict');
  Object.assign(answers, {conflict:'老人必须在一天内找到失联的女儿', emotion:'温暖治愈', ending:'人物成长', audience:'家庭观众'});
  const result = center.advisorStep(Object.values(answers), payload, answers);
  assert.equal(result.recommendations.length, 3);
  assert.deepEqual(result.recommendations.map(item => item.id), ['steady', 'conflict', 'creative']);
  for (const item of result.recommendations) {
    assert.ok(item.title);
    assert.match(item.premise, /家庭情感/);
    assert.ok(item.reason);
  }
});

test('选择推荐方向后仍通过统一项目表单创建', () => {
  const recommendations = center.buildRecommendations(['校园成长', '笑中带泪', '人物成长']);
  assert.equal(recommendations.length, 3);
  assert.match(recommendations[0].premise, /校园成长/);
  assert.match(center.projectUrl('project a'), /short-drama\.html\?project=/);
  assert.match(center.compactIdea('  我想做家庭故事。 '), /我想做家庭故事/);
});

test('编号回复会解析为当前推荐方向的完整语义', () => {
  const context = {field:'conflict', items:['必须隐瞒真相','关系即将破裂','时间只剩一天']};
  for (const value of ['3','第三个','选3','方向三','③','我选第三个']) {
    const resolved = center.plannerResolveChoice(value, context);
    assert.equal(resolved.matched, true);
    assert.equal(resolved.valid, true);
    assert.equal(resolved.index, 3);
    assert.equal(resolved.choice, '时间只剩一天');
    assert.match(resolved.value, /方向 3：时间只剩一天/);
  }
  assert.equal(center.plannerResolveChoice('我想换个故事', context).matched, false);
  assert.equal(center.plannerResolveChoice('4', context).valid, false);
  assert.equal(center.plannerResolveChoice('3', {field:'conflict',items:[]}).valid, false);
});

test('前置策划生成结构化剧本并在人工确认后准备正式对话', () => {
  const messages = ['家庭情感', '温暖治愈', '人物成长'];
  const direction = center.buildRecommendations(messages)[0];
  const preview = center.buildPlannerPreview({
    title:'回家吃饭', synopsis:'一家人重新学会沟通', ratio:'9:16',
    target_duration:45, shot_count:8, visual_style:'电影写实'
  }, messages, direction);
  assert.equal(preview.title, '回家吃饭');
  assert.equal(preview.ratio, '9:16');
  assert.equal(preview.duration_seconds, 45);
  assert.equal(preview.beats.length, 8);
  assert.equal(preview.shots.length, 8);
  assert.equal(preview.shots.reduce((sum, shot) => sum + shot.duration, 0), 45);
  assert.ok(preview.shots.every(shot => shot.scene && shot.action && shot.expression && shot.camera));
  assert.ok(preview.shots.every(shot => Array.isArray(shot.characters) && shot.characters.length));
  assert.equal(preview.quality.blocking, false);
  assert.equal(preview.story_plan.schema_version, 'short-drama-story-plan-v1');
  assert.equal(preview.story_plan.acts.length, 3);
  assert.equal(preview.scenes[0].shot_start, 1);
  assert.equal(preview.scenes.at(-1).shot_end, 8);
  assert.ok(preview.scenes.every(scene => scene.objective && scene.turn));
  assert.doesNotMatch(preview.shots.map(shot => shot.dialogue).join(' '), /事情怎么会这样|先听我说|我需要一个答案/);
  assert.ok(['passed','needs_revision'].includes(preview.review.status));
  assert.equal(center.plannerProgress(messages, direction, preview).score, 100);
  preview.shots[0].sound = 'CONFIRMED_SOUND_MARKER';
  preview.shots[0].transition = 'CONFIRMED_TRANSITION_MARKER';
  preview.shots[0].continuity = 'CONFIRMED_CONTINUITY_MARKER';
  const contract = center.plannerConfirmedContract(preview);
  assert.equal(contract.creative_memory.schema_version, 'short-drama-creative-memory-v1');
  assert.equal(contract.creative_memory.fields.topic, '一家人重新学会沟通');
  assert.equal(contract.shots[0].sound, 'CONFIRMED_SOUND_MARKER');
  assert.equal(contract.shots[0].transition, 'CONFIRMED_TRANSITION_MARKER');
  assert.equal(contract.shots[0].continuity, 'CONFIRMED_CONTINUITY_MARKER');
  assert.equal(center.confirmedContractMatches({confirmed_contract:contract}, contract), true);
  const changed = JSON.parse(JSON.stringify(contract));
  changed.shots[0].sound = 'SERVER_CHANGED_SOUND';
  assert.equal(center.confirmedContractMatches({confirmed_contract:changed}, contract), false);
  assert.match(centerScript, /client\.promote\(\{/);
  assert.match(centerScript, /planning_messages:plannerPromotionMessages\(plannerPreview\)/);
  assert.match(centerScript, /confirmed_contract:contract/);
  const promotion = center.plannerPromotionMessages(preview);
  assert.equal(promotion.length, 3);
  assert.match(promotion[0], /核心设定/);
  assert.match(promotion[2], /逐镜剧本/);
  assert.match(promotion[2], /说话人=|无台词/);
  assert.match(promotion[2], /CONFIRMED_SOUND_MARKER/);
  assert.match(promotion[2], /CONFIRMED_TRANSITION_MARKER/);
  assert.match(promotion[2], /CONFIRMED_CONTINUITY_MARKER/);
  assert.doesNotMatch(promotion[0], /[“”]/);
});

test('生成响应丢失后复用相同确认合同并只继续锁定', async () => {
  const contract = {schema_version:'preproject-confirmed-shot-contract-v1', shots:[{index:1}]};
  const workspace = {
    conversation:{state:'script_review', revision:9},
    current_script:{id:'version-1', script:{confirmed_contract:contract}}
  };
  let generates = 0;
  let locks = 0;
  const result = await center.continuePlannerContract({
    generate(){generates += 1; throw new Error('不应重复生成');},
    lock(body,key){
      locks += 1;
      assert.equal(body.version_id, 'version-1');
      assert.equal(body.conversation_revision, 9);
      assert.equal(key, 'preproject-project-1-lock');
      return Promise.resolve({conversation:{state:'script_locked',revision:10},current_script:workspace.current_script});
    }
  }, 'project-1', workspace, contract);
  assert.equal(generates, 0);
  assert.equal(locks, 1);
  assert.equal(result.conversation.state, 'script_locked');
});

test('锁定响应丢失后识别已锁定合同且不重复请求', async () => {
  const contract = {schema_version:'preproject-confirmed-shot-contract-v1', shots:[{index:1}]};
  const workspace = {
    conversation:{state:'script_locked', revision:10},
    current_script:{id:'version-1', script:{confirmed_contract:contract}}
  };
  let requests = 0;
  const client = {generate(){requests += 1;}, lock(){requests += 1;}};
  const result = await center.continuePlannerContract(client, 'project-1', workspace, contract);
  assert.equal(requests, 0);
  assert.equal(result, workspace);
});

test('逐镜剧本识别角色、展示对白并阻止超时台词确认', () => {
  const messages = ['雨天被困便利店的女孩无法回家，外卖小哥赠送雨衣', '温暖治愈', '温暖圆满'];
  const direction = center.buildRecommendations(messages)[0];
  const preview = center.buildPlannerPreview({
    title:'街边便利店门口', synopsis:messages[0], ratio:'16:9',
    target_duration:30, shot_count:6, visual_style:'电影感写实'
  }, messages, direction);
  assert.deepEqual(preview.characters.slice(0, 2), ['女孩', '外卖小哥']);
  assert.equal(preview.shots[1].speaker, '外卖小哥');
  assert.match(preview.shots[1].dialogue, /雨衣/);
  assert.ok(preview.shots[1].reading_seconds < preview.shots[1].duration);
  preview.shots[0].dialogue_kind = 'dialogue';
  preview.shots[0].dialogue = '这是一句明显超过五秒镜头可以正常说完的特别特别长的测试台词';
  const quality = center.plannerQuality(preview);
  assert.equal(quality.blocking, true);
  assert.equal(quality.blockers[0].index, 1);
});

test('剧本审稿识别模板对白并自动修复安全问题', () => {
  const preview = center.buildPlannerPreview({title:'测试',synopsis:'女孩必须在车站找到失踪的父亲',target_duration:30,shot_count:6}, ['女孩寻找父亲'], center.buildRecommendations(['女孩寻找父亲'])[0], {topic:'家庭',protagonist:'女孩',conflict:'必须在末班车前找到父亲',emotion:'紧张',ending:'父女和解',audience:'年轻人',style:'写实'});
  preview.shots[1].dialogue_kind = 'dialogue';
  preview.shots[1].dialogue = '事情怎么会这样？';
  preview.shots[1].speaker = '女孩';
  preview.review = center.plannerReview(preview);
  assert.ok(preview.review.issues.some(item => item.code === 'generic_dialogue'));
  center.repairPlannerPreview(preview);
  assert.equal(preview.shots[1].dialogue_kind, 'silence');
  assert.ok(!preview.review.issues.some(item => item.code === 'generic_dialogue'));
});

test('前置策划页面提供聊天、结构化卡片和人工确认入口', () => {
  for (const id of [
    'shortDramaIdeaChat', 'shortDramaRecommendations', 'shortDramaScriptPreview',
    'shortDramaPlannerStages', 'shortDramaShowChat', 'shortDramaShowCanvas',
    'shortDramaPlannerBrief', 'shortDramaPlannerScore', 'shortDramaPlannerMissing',
    'shortDramaAdvisorMode', 'shortDramaPlannerUndo',
    'shortDramaImportGlobal',
    'shortDramaCompleteBrief', 'shortDramaGeneratePreview', 'shortDramaDownloadWord',
    'shortDramaPlannerAckInput', 'shortDramaConfirmScript'
  ]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(html, /确认剧本并创建项目/);
  assert.match(html, /保存设置并进入剧本策划/);
});

test('长剧本导入建立覆盖开场到结局的全局理解', () => {
  const source = ['第一场 家中','林夏：我必须找到父亲。','林夏带着旧信离开。','第二场 车站','周野阻止林夏登车。','林夏发现信件背后的真相。','第三场 月台','林夏作出选择。','父女最终和解。'].join('\n');
  const analysis = center.analyzeImportedScript(source, '长剧本.md');
  assert.equal(analysis.global_structure.schema_version, 'short-drama-import-global-v1');
  assert.equal(analysis.global_structure.coverage.analyzed_from_start, true);
  assert.equal(analysis.global_structure.coverage.analyzed_from_end, true);
  assert.match(analysis.global_structure.ending, /和解|选择/);
});

test('创作理解按主题、人物、冲突、情绪、结局和观众计算完整度', () => {
  const understanding = center.plannerUnderstanding([], {ratio:'16:9',target_duration:30,shot_count:6,visual_style:'电影感写实'}, {
    topic:'雨夜重逢', protagonist:'独居女孩', conflict:'必须在末班车前找到父亲',
    emotion:'紧张悬疑', ending:'人物成长', audience:'年轻人'
  });
  assert.equal(center.plannerCompleteness(understanding).score, 100);
  assert.equal(center.plannerCompleteness(understanding).ready, true);
  const incomplete = center.plannerCompleteness(center.plannerUnderstanding(['家庭情感'], {}, {topic:'家庭情感'}));
  assert.ok(incomplete.score < 80);
  assert.ok(incomplete.missing.includes('conflict'));
});

test('Word 确认稿与结构化预览使用同一镜头内容', () => {
  const preview = center.buildPlannerPreview({title:'雨夜来信',synopsis:'旧友在雨夜重逢',ratio:'16:9',target_duration:30,shot_count:6,visual_style:'电影感写实'}, ['旧友在雨夜重逢'], center.buildRecommendations(['旧友在雨夜重逢'])[0], {protagonist:'林夏',conflict:'必须在末班车前说出真相',emotion:'温暖治愈',ending:'人物成长',audience:'年轻人'});
  preview.shots[0].dialogue_kind = 'dialogue';
  preview.shots[0].speaker = '林夏';
  preview.shots[0].dialogue = 'WORD_CONFIRMATION_MARKER';
  const document = center.plannerWordDocumentHtml(preview, {protagonist:'林夏',emotion:'温暖治愈',audience:'年轻人'});
  assert.match(document, /短剧创作需求确认书/);
  assert.match(document, /WORD_CONFIRMATION_MARKER/);
  assert.match(center.plannerWordFilename(preview), /雨夜来信_v1\.doc$/);
});

test('剧本共创室使用两栏、阶段导航和按需切换的对话优先布局', () => {
  assert.match(centerStyle, /\.short-drama-create-shell\{[^}]*box-sizing:border-box[^}]*overflow:hidden/);
  assert.match(html, /data-planner-step="chat"[\s\S]*data-planner-step="review"/);
  assert.match(centerStyle, /\.short-drama-planner-grid\{[^}]*grid-template-columns:minmax\(0,1fr\) 320px[^}]*overflow:hidden/);
  assert.match(centerStyle, /data-planner-panel="chat"[\s\S]*\.short-drama-planner-canvas/);
  assert.match(centerStyle, /@media\(max-width:900px\)[^{]*\{[^}]*\.short-drama-create-dialog:has/);
});

test('移动端收起创作记忆后仍可聚焦并再次展开', async () => {
  const chrome = findChromeExecutable();
  assert.ok(chrome, '真实响应式测试需要 Chrome 或 Chromium');
  const probe = `<script>addEventListener('DOMContentLoaded',function(){setTimeout(function(){try{var dialog=document.getElementById('shortDramaDialog'),inspiration=document.getElementById('shortDramaInspiration'),grid=document.querySelector('.short-drama-planner-grid'),inspector=document.querySelector('.short-drama-planner-inspector'),button=document.getElementById('shortDramaPlannerMemoryToggle'),brief=document.getElementById('shortDramaPlannerBrief');inspiration.hidden=false;dialog.showModal();button.click();button.focus();var checks=[matchMedia('(max-width:900px)').matches,grid.classList.contains('memory-collapsed'),getComputedStyle(inspector).display!=='none',button.getClientRects().length>0,document.activeElement===button,button.getAttribute('aria-expanded')==='false'];button.click();checks.push(!grid.classList.contains('memory-collapsed'),button.getAttribute('aria-expanded')==='true',getComputedStyle(brief).display!=='none');document.documentElement.setAttribute('data-responsive-memory-test',checks.every(Boolean)?'pass':'fail-'+checks.map(function(value){return value?'1':'0';}).join(''));}catch(error){document.documentElement.setAttribute('data-responsive-memory-test','error');}},200);});<\/script>`;
  const testHtml = html.replace('</body>', probe + '</body>');
  const siteRoot = path.join(ROOT, 'site');
  const server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname);
    if (pathname === '/workbench/short-drama.html') {
      response.writeHead(200, {'Content-Type':'text/html; charset=utf-8'});response.end(testHtml);return;
    }
    const filename = path.resolve(siteRoot, pathname.replace(/^\/+/, ''));
    if (!filename.startsWith(siteRoot) || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) {response.writeHead(404);response.end('not found');return;}
    const contentType = filename.endsWith('.css') ? 'text/css' : filename.endsWith('.js') ? 'text/javascript' : 'application/octet-stream';
    response.writeHead(200, {'Content-Type':contentType});response.end(fs.readFileSync(filename));
  });
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'hq-responsive-'));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const address = server.address();
    const output = await new Promise((resolve, reject) => {
      const browser = spawn(chrome, ['--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--hide-scrollbars','--window-size=390,844','--virtual-time-budget=3000','--user-data-dir='+profile,'--dump-dom',`http://127.0.0.1:${address.port}/workbench/short-drama.html`]);
      let stdout='',stderr='';browser.stdout.on('data',chunk => {stdout+=chunk;});browser.stderr.on('data',chunk => {stderr+=chunk;});
      const timeout = setTimeout(() => {browser.kill();reject(new Error('Chrome 响应式测试超时'));},15000);
      browser.on('error',reject);browser.on('close',code => {clearTimeout(timeout);code===0?resolve(stdout):reject(new Error(stderr||`Chrome exited ${code}`));});
    });
    assert.match(output, /data-responsive-memory-test="pass"/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(profile, {
      recursive:true, force:true, maxRetries:8, retryDelay:100,
    });
  }
});

test('仅展示个人独立项目并正确计算概览', () => {
  const projects = [
    {id:'a', title:'春日', synopsis:'公园', stage:'setup', board_id:null},
    {id:'b', title:'雨夜', synopsis:'来信', stage:'voice_review', board_id:null},
    {id:'c', title:'交付', synopsis:'完成', stage:'completed', board_id:null},
    {id:'d', title:'共享', synopsis:'画布', stage:'setup', board_id:'board-1'},
  ];
  assert.deepEqual(center.filterProjects(projects, '雨', '').map(p => p.id), ['b']);
  assert.deepEqual(center.metrics(projects), {all:3, active:1, blocked:1, done:1});
});

test('创建请求不携带 board_id 或画布身份', () => {
  const fields = {
    title:{value:'雨夜来信'}, synopsis:{value:'两位旧友在雨夜重新相遇'},
    ratio:{value:'16:9'}, target_duration:{value:'45'}, shot_count:{value:'6'},
    visual_style:{value:'电影感写实'}
  };
  const payload = center.createPayload({elements:fields});
  assert.equal(payload.title, '雨夜来信');
  assert.equal(payload.target_duration, 45);
  assert.equal(Object.hasOwn(payload, 'board_id'), false);
});

test('客户端使用 Cookie 会话并支持安全删除独立短剧', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>'{"items":[]}'};
  };
  const client = center.createClient(fetchImpl);
  await client.list();
  await client.create({title:'项目'}, 'project-create-key');
  await client.message({project_id:'project-1', conversation_revision:1, message:'确认方向'}, 'planner-message');
  await client.generate({project_id:'project-1', conversation_revision:2}, 'planner-generate');
  await client.lock({project_id:'project-1', conversation_revision:3, version_id:'script-1'}, 'planner-lock');
  await client.deleteProject({id:'project-1', revision:4});
  assert.equal(calls[0].url, '/api/gen/short-drama/projects?page=1&page_size=50');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer __cookie__');
  assert.equal(calls[1].options.headers['Idempotency-Key'], 'project-create-key');
  assert.equal(calls[2].url, '/api/gen/short-drama/conversation/messages');
  assert.equal(calls[2].options.headers['Idempotency-Key'], 'planner-message');
  assert.equal(calls[3].url, '/api/gen/short-drama/conversation/script/generate');
  assert.equal(calls[4].url, '/api/gen/short-drama/conversation/script/lock');
  assert.equal(calls[5].url, '/api/gen/short-drama/project/delete');
  assert.equal(calls[5].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[5].options.body), {project_id:'project-1', revision:4});
  for (const call of calls) assert.equal(Object.hasOwn(call.options.headers, 'X-Canvas-Board-Id'), false);
});

test('确认剧本原子建项响应丢失后使用同一幂等键重试', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    if(calls.length === 1)throw new Error('response lost after commit');
    return {ok:true,status:200,text:async ()=>'{"project":{"id":"project-once"},"replayed":true}'};
  };
  const client = center.createClient(fetchImpl);
  const body = {
    project:{title:'只创建一次'},planning_messages:['确认方向'],
    confirmed_contract:{schema_version:'preproject-confirmed-shot-contract-v1'}
  };
  await assert.rejects(client.promote(body, 'stable-project-promote'));
  const result = await client.promote(body, 'stable-project-promote');
  assert.equal(result.project.id, 'project-once');
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/api/gen/short-drama/projects/promote');
  assert.equal(calls[0].options.headers['Idempotency-Key'], 'stable-project-promote');
  assert.equal(calls[1].options.headers['Idempotency-Key'], 'stable-project-promote');
  assert.deepEqual(JSON.parse(calls[0].options.body), JSON.parse(calls[1].options.body));
  assert.match(centerScript, /if\(!pendingCreateKey\)pendingCreateKey=newProjectKey\(\)/);
  assert.match(centerScript, /client\.promote\(\{/);
  assert.doesNotMatch(centerScript, /pendingCreatedProject/);
});

test('客户端使用同一幂等键原子导入项目和完整剧本', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>JSON.stringify({id:'project-1',script_import:{replayed:calls.length>1}})};
  };
  const client = center.createClient(fetchImpl);
  const payload = {title:'完整导入',source_text:'首段 中段 末段'};
  await client.importProject(payload, 'stable-import-key');
  await client.importProject(payload, 'stable-import-key');
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/api/gen/short-drama/projects/import');
  assert.equal(calls[0].options.headers['Idempotency-Key'], 'stable-import-key');
  assert.equal(calls[1].options.headers['Idempotency-Key'], 'stable-import-key');
  assert.deepEqual(JSON.parse(calls[0].options.body), payload);
});

function docxBuffer(xml, overrides = {}) {
  const name = Buffer.from('word/document.xml');
  const raw = Buffer.from(xml);
  const compressed = zlib.deflateRawSync(raw);
  const local = Buffer.alloc(30 + name.length);
  local.writeUInt32LE(0x04034b50, 0);local.writeUInt16LE(8, 8);
  local.writeUInt32LE(compressed.length, 18);local.writeUInt32LE(raw.length, 22);
  local.writeUInt16LE(name.length, 26);name.copy(local, 30);
  const centralOffset = local.length + compressed.length;
  const central = Buffer.alloc(46 + name.length);
  central.writeUInt32LE(0x02014b50, 0);central.writeUInt16LE(8, 10);
  central.writeUInt32LE(compressed.length, 20);
  central.writeUInt32LE(overrides.uncompressed ?? raw.length, 24);
  central.writeUInt16LE(name.length, 28);
  central.writeUInt32LE(overrides.localOffset ?? 0, 42);name.copy(central, 46);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);eocd.writeUInt16LE(1, 8);eocd.writeUInt16LE(1, 10);
  eocd.writeUInt32LE(central.length, 12);eocd.writeUInt32LE(centralOffset, 16);
  const result = Buffer.concat([local, compressed, central, eocd]);
  return result.buffer.slice(result.byteOffset, result.byteOffset + result.byteLength);
}

function pdfBuffer(inflated) {
  const compressed = zlib.deflateSync(Buffer.from(inflated));
  const head = Buffer.from(`%PDF-1.4\n1 0 obj\n<< /Length ${compressed.length} /Filter /FlateDecode >>\nstream\n`);
  const tail = Buffer.from('\nendstream\nendobj\n%%EOF');
  const result = Buffer.concat([head, compressed, tail]);
  return result.buffer.slice(result.byteOffset, result.byteOffset + result.byteLength);
}

test('DOCX 安全解压校验中央目录并限制未压缩大小', async () => {
  const valid = docxBuffer('<w:document><w:p><w:t>场景一 人物：安全对白内容</w:t></w:p></w:document>');
  assert.match(await center.extractDocxText(valid), /安全对白/);
  await assert.rejects(
    center.extractDocxText(docxBuffer('<w:t>内容</w:t>', {localOffset:0x7fffffff})),
    /偏移无效/
  );
  await assert.rejects(
    center.extractDocxText(docxBuffer('<w:t>内容</w:t>', {uncompressed:3*1024*1024})),
    /解压后过大/
  );
});

test('PDF 高压缩比流在输出上限处被拒绝', async () => {
  await assert.rejects(
    center.extractPdfText(pdfBuffer('A'.repeat(3*1024*1024))),
    /解压后过大|压缩比异常/
  );
});

test('流式解压达到累计上限时立即取消 reader', async () => {
  let cancelled = false, reads = 0;
  const stream = {getReader(){return {
    async read(){reads += 1;return reads <= 2 ? {done:false,value:new Uint8Array(6)} : {done:true};},
    async cancel(){cancelled = true;}
  };}};
  await assert.rejects(center.readLimitedStream(stream, 8, '输出超限'), /输出超限/);
  assert.equal(cancelled, true);
  assert.equal(reads, 2);
});

test('静态服务误返回 HTML 时显示可理解的接口提示', async () => {
  const client = center.createClient(async () => ({
    ok:false, status:404, text:async ()=>'<!DOCTYPE HTML><html><title>Error response</title></html>'
  }));
  await assert.rejects(client.list(), /本地接口未连接/);
});

test('提问和求推荐不会被写入核心冲突', () => {
  assert.equal(center.plannerLocalIntent('你觉得呢'), 'ask_recommendation');
  assert.equal(center.plannerLocalIntent('帮我推荐'), 'ask_recommendation');
  const advice = center.plannerLocalAdvice('你觉得呢', 'conflict');
  assert.equal(advice.extracted_fields.conflict, undefined);
  assert.deepEqual(center.applyAdvisorResult({protagonist:'青春期学生'}, advice), {protagonist:'青春期学生'});
  assert.match(advice.reply, /几个适合的核心冲突方案/);
  assert.equal(center.plannerUnderstanding(['校园成长'], {}, {}).conflict, '');
});

test('只有高置信度明确回答才更新结构化理解', () => {
  const original = {protagonist:'青春期学生'};
  assert.deepEqual(center.applyAdvisorResult(original, {
    intent:'answer', confidence:.4, extracted_fields:{conflict:'时间只剩一天'}
  }), original);
  assert.deepEqual(center.applyAdvisorResult(original, {
    intent:'answer', confidence:.91, extracted_fields:{conflict:'时间只剩一天', admin:'bad'}
  }), {protagonist:'青春期学生', conflict:'时间只剩一天'});
});

test('结构化修改支持替换、清空和显式理解复述', () => {
  const original = {topic:'校园成长', style:'悬疑'};
  const changed = center.applyAdvisorResult(original, {
    intent:'modify', field_updates:[
      {field:'style', operation:'set', value:'温暖写实', confidence:.94},
      {field:'topic', operation:'clear', value:'', confidence:.91}
    ]
  });
  assert.deepEqual(changed, {topic:'', style:'温暖写实'});
  assert.equal(center.plannerUnderstanding(['校园成长'], {synopsis:'校园成长'}, changed).topic, '');
  assert.match(center.plannerRecap(original, changed, {}), /已取消故事主题/);
  assert.match(center.plannerRecap(original, changed, {}), /视觉风格改为“温暖写实”/);
});

test('基础引导模式识别撤销和否定后替换', () => {
  assert.equal(center.plannerLocalIntent('撤销上次修改'), 'undo');
  assert.equal(center.plannerLocalAdvice('撤销上次修改', 'style').intent, 'undo');
  const replacement = center.plannerLocalAdvice('不要悬疑，改成温暖治愈', 'emotion');
  assert.equal(replacement.intent, 'modify');
  assert.equal(replacement.degraded, true);
  assert.equal(replacement.field_updates[0].value, '温暖治愈');
});

test('基础引导模式一次提取多个设定并保留证据与确认状态', () => {
  const updates = center.plannerLocalFieldUpdates('我想拍一个雨夜便利店的故事，女主刚失业，最后想温暖一点。', 'topic', {});
  const fields = Object.fromEntries(updates.map(update => [update.field, update]));
  assert.equal(fields.topic.value, '雨夜便利店');
  assert.match(fields.protagonist.value, /女主刚失业/);
  assert.equal(fields.emotion.value, '温暖');
  assert.equal(fields.ending.status, 'inferred');
  assert.match(fields.topic.evidence, /故事/);
});

test('创作记忆保存字段证据、待确认状态和冲突', () => {
  const meta = center.applyAdvisorMetadata({}, {
    field_updates:[
      {field:'protagonist',operation:'set',value:'刚失业的女性',confidence:.94,evidence:'女主刚失业',status:'confirmed'},
      {field:'ending',operation:'set',value:'温暖',confidence:.72,evidence:'最后想温暖一点',status:'inferred'},
      {field:'emotion',operation:'set',value:'温暖',confidence:.7,evidence:'也可以温暖',status:'inferred'}
    ],
    conflicts:[{field:'emotion',existing_value:'紧张悬疑',proposed_value:'温暖',requires_confirmation:true}]
  });
  assert.equal(meta.protagonist.status, 'confirmed');
  assert.equal(meta.protagonist.evidence, '女主刚失业');
  assert.equal(meta.ending.status, 'inferred');
  assert.equal(meta.emotion.status, 'conflicted');
});

test('确定性创作流程每轮只选择最高价值缺口并按阶段推进', () => {
  const payload = {visual_style:'电影感写实'};
  const partial = {topic:'雨夜便利店',protagonist:'刚失业的女性',emotion:'温暖',ending:'温暖',audience:'年轻人'};
  let flow = center.plannerFlowState([], payload, partial, {}, null, null, []);
  assert.equal(flow.phase, 'collect');
  assert.equal(flow.focus_field, 'conflict');
  const complete = {...partial, conflict:'必须在妈妈到来前隐瞒失业真相'};
  flow = center.plannerFlowState([], payload, complete, {}, null, null, []);
  assert.equal(flow.phase, 'directions');
  flow = center.plannerFlowState([], payload, complete, {}, {id:'steady'}, null, []);
  assert.equal(flow.phase, 'script');
  flow = center.plannerFlowState([], payload, complete, {}, {id:'steady'}, {title:'草稿'}, []);
  assert.equal(flow.phase, 'review');
  flow = center.plannerFlowState([], payload, complete, {ending:{status:'conflicted',conflict:{existing_value:'温暖',proposed_value:'反转'}}}, {id:'steady'}, {title:'草稿'}, []);
  assert.equal(flow.phase, 'collect');
  assert.equal(flow.focus_field, 'ending');
});

test('修改设定只标记受影响层并在更新时保留其他结构', () => {
  assert.deepEqual(center.plannerAffectedLayers(['style']), ['shots']);
  assert.deepEqual(center.plannerAffectedLayers(['emotion']), ['scenes','shots']);
  assert.deepEqual(center.plannerAffectedLayers(['protagonist']), ['story','scenes','shots']);
  const previous = {story_plan:{theme:'旧主题',emotion:'紧张'},scenes:[{index:1}],logline:'旧梗概',conflict:'旧冲突',ending:'旧结局',characters:['旧角色'],shots:[{index:1,action:'旧镜头'}]};
  const fresh = {story_plan:{theme:'新主题',emotion:'温暖'},scenes:[{index:2}],logline:'新梗概',conflict:'新冲突',ending:'新结局',characters:['新角色'],shots:[{index:1,action:'新镜头'}]};
  const styleOnly = center.rebuildPlannerPreview(previous, structuredClone(fresh), ['shots']);
  assert.equal(styleOnly.story_plan.theme, '旧主题');
  assert.equal(styleOnly.scenes[0].index, 1);
  assert.equal(styleOnly.shots[0].action, '新镜头');
  const storyChange = center.rebuildPlannerPreview(previous, structuredClone(fresh), ['story','scenes','shots']);
  assert.equal(storyChange.story_plan.theme, '新主题');
});

test('前置策划客户端调用无项目语义顾问接口', async () => {
  let captured;
  const client = center.createClient(async (url, options) => {
    captured = {url, options};
    return {ok:true, status:200, text:async ()=>'{'+'"intent":"question"'+'}'};
  });
  await client.advisor({user_message:'你觉得呢', expected_field:'conflict'});
  assert.equal(captured.url, '/api/gen/short-drama/advisor');
  assert.equal(captured.options.method, 'POST');
  assert.equal(JSON.parse(captured.options.body).expected_field, 'conflict');
});

test('删除冲突显示面向用户的说明', () => {
  assert.match(center.deleteErrorMessage({code:'short_drama_unapplied_paid_job'}), /付费任务/);
  assert.match(center.deleteErrorMessage({code:'revision_conflict'}), /刷新/);
});

test('项目链接保持在独立短剧页面', () => {
  assert.equal(center.projectUrl('project a'), 'short-drama.html?project=project%20a');
  assert.doesNotMatch(center.projectUrl('project a'), /canvas\.html/);
});

test('project route activates immersive workspace mode', () => {
  assert.match(centerScript, /documentElement\.classList\.add\('short-drama-immersive'\)/);
  assert.match(centerScript, /documentElement\.classList\.remove\('short-drama-immersive'\)/);
});

test('phase three planner controls are present', () => {
  for (const id of ['shortDramaPlannerHistory', 'shortDramaPlannerHistoryList',
    'shortDramaPlannerAuditScore', 'shortDramaPlannerAuditSummary', 'shortDramaRestartPlanner']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(centerScript, /hq-short-drama-planner-draft-v3/);
  assert.match(centerScript, /localStorage/);
  assert.match(centerStyle, /short-drama-message-feedback/);
});

test('planner drafts are isolated by authenticated account', () => {
  assert.equal(center.plannerDraftStorageKey('alice'), 'hq-short-drama-planner-draft-v3:alice');
  assert.equal(center.plannerDraftStorageKey('bob'), 'hq-short-drama-planner-draft-v3:bob');
  assert.equal(center.plannerDraftStorageKey(''), '');
  const draft = {version:3,username:'alice'};
  assert.equal(center.plannerDraftMatchesUser(draft, 'alice'), true);
  assert.equal(center.plannerDraftMatchesUser({version:4,username:'alice'}, 'alice'), true);
  assert.equal(center.plannerDraftMatchesUser({version:5,username:'alice'}, 'alice'), false);
  assert.equal(center.plannerDraftMatchesUser(draft, 'bob'), false);
  assert.match(centerScript, /me:function\(\)\{return request\('\/api\/auth\/me'\)/);
});

test('current planner draft survives a storage round trip with choices and project checkpoint', () => {
  const values = new Map();
  const storage = {
    getItem:key => values.has(key) ? values.get(key) : null,
    setItem:(key,value) => values.set(key, String(value)),
    removeItem:key => values.delete(key)
  };
  const key = center.plannerDraftStorageKey('alice');
  const draft = {
    version:4, username:'alice', saved_at:1700000000000, payload:{title:'雨夜来信'},
    active_field:'conflict', active_choices:{field:'conflict',items:['隐瞒真相','关系破裂','时间将尽'],updated_at:1699999999000},
    pending_create_key:'project-create-stable'
  };
  assert.equal(center.writePlannerDraftRecord(storage, key, draft, 'alice'), true);
  const restored = center.readPlannerDraftRecord(storage, key, 'alice', 1700000001000);
  assert.equal(restored.pending_create_key, 'project-create-stable');
  assert.deepEqual(center.plannerDraftActiveChoices(restored), draft.active_choices);
});

test('deployed v3 planner draft remains readable with safe choice defaults', () => {
  const key = center.plannerDraftStorageKey('alice');
  const stored = JSON.stringify({version:3,username:'alice',saved_at:1700000000000,active_field:'ending',pending_create_key:'legacy-create-key'});
  let value = stored;
  const storage = {getItem:() => value,setItem:(_key,next) => {value=next;},removeItem:() => {value=null;}};
  const restored = center.readPlannerDraftRecord(storage, key, 'alice', 1700000001000);
  assert.equal(restored.version, 3);
  assert.equal(restored.pending_create_key, 'legacy-create-key');
  assert.deepEqual(center.plannerDraftActiveChoices(restored), {field:'ending',items:[]});
  assert.equal(value, stored);
});

test('atomic promotion idempotency checkpoint is persisted before request', () => {
  const requestAt = centerScript.indexOf('client.promote({');
  const beforeAt = centerScript.lastIndexOf('savePlannerDraft(true)', requestAt);
  assert.ok(requestAt > 0);
  assert.ok(beforeAt > 0 && beforeAt < requestAt);
  assert.match(centerScript, /无法安全保存创建恢复点/);
  assert.doesNotMatch(centerScript, /pendingCreatedProject/);
});

test('planner conversation audit detects repeated questions and negative feedback', () => {
  const audit = center.plannerConversationAudit([
    {role:'assistant', message:'你希望故事最后如何结束？'},
    {role:'user', message:'温暖一点'},
    {role:'assistant', message:'你希望故事最后如何结束？'}
  ], [{rating:'wrong'}], {ending:{status:'conflicted'}}, 5);
  assert.equal(audit.repeated_questions, 1);
  assert.equal(audit.negative_feedback, 1);
  assert.equal(audit.conflicts, 1);
  assert.equal(audit.corrections, 5);
  assert.equal(audit.score, 44);
});

test('浏览器运行时只使用模块内已定义的全局引用', () => {
  assert.match(centerScript, /var runtimeRoot=/);
  assert.doesNotMatch(centerScript, /\broot\.location\b/);
});
