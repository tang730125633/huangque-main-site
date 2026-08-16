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

const CHROME_TEST_TIMEOUT_MS = process.env.CI ? 30000 : 15000;

test('一级导航包含独立短剧入口和专用图标', () => {
  assert.match(shell, /\{k:'short-drama',l:'短剧创作',i:'clapper'\}/);
  assert.match(shell, /clapper:/);
  assert.match(html, /data-active="short-drama"/);
});

test('项目中心提供列表筛选、创建和详情入口', () => {
  for (const id of ['shortDramaGrid', 'shortDramaSearch',
    'shortDramaCreate', 'shortDramaDialog', 'shortDramaDrawer', 'shortDramaDeleteProject']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /进入对话式工作区/);
  assert.doesNotMatch(html, /id="shortDramaStageFilter"/);
  assert.match(html, />删除短剧<\/button>/);
  assert.doesNotMatch(html, /value="1:1"/);
});

test('创建短剧提供真人、漫剧和数字人口播三种内容类型入口', () => {
  assert.match(html, /data-content-type="live_action"/);
  assert.match(html, /data-content-type="comic"/);
  assert.match(html, /data-content-type="digital_presenter"/);
  assert.match(html, /AI 真人短剧/);
  assert.match(html, /AI 漫剧/);
  assert.match(html, /AI 数字人口播/);
  assert.match(html, /id="shortDramaLiveActionForm"/);
  assert.match(html, /id="shortDramaRoleConfirm"/);
  assert.match(html, /id="shortDramaCoreStory"/);
  assert.match(html, /id="shortDramaCoreStoryForm"/);
  assert.match(html, /id="shortDramaRoleTabs"/);
  assert.match(html, /id="shortDramaSaveRole"/);
  assert.match(html, /下一步：剧本设计/);
  assert.match(centerScript, /团队正在努力开发该功能/);
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

test('真人短剧先确认剧本再确认角色和角色形象', async () => {
  assert.match(centerScript, /live_action_story/);
  assert.doesNotMatch(centerScript, /showCreateStep\('live_action_visuals'\)/);
  assert.match(centerScript, /确认剧本，下一步：角色确认/);
  assert.match(centerScript, /核对并保存角色资料，然后在同一界面选择、上传或生成角色标准图/);
  assert.match(centerScript, /确认角色并创建项目/);
  const calls = [];
  const client = center.createClient(async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>'{}'};
  });
  const coreStory = {
    title:'分享糖果', logline:'两个孩子学会分享。', setup:'男孩独自吃糖。',
    development:'女孩出现。', turning_point:'男孩决定打开糖袋。',
    climax:'男孩把糖递给女孩。', ending:'两人一起分享。',
    central_conflict:'独占还是分享。', theme:'分享带来友谊。',
    preservation_notes:'保留原稿结局。'
  };
  await client.confirmLiveActionCoreStory({id:'project-1', revision:5}, coreStory);
  assert.equal(calls[0].url, '/api/gen/short-drama/projects/live-action/core-story');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    project_id:'project-1', revision:5, core_story:coreStory
  });
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
  assert.equal(preview.duration_seconds, 48);
  assert.equal(preview.beats.length, 8);
  assert.equal(preview.shots.length, 8);
  assert.equal(preview.shots.reduce((sum, shot) => sum + shot.duration, 0), 48);
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
  assert.equal(analysis.global_structure.schema_version, 'short-drama-import-global-v2');
  assert.equal(analysis.global_structure.coverage.analyzed_from_start, true);
  assert.equal(analysis.global_structure.coverage.analyzed_from_end, true);
  assert.match(analysis.global_structure.ending, /和解|选择/);
});

test('核心故事按分镜语义整理并过滤标题规格与重复节点', () => {
  const source = [
    '放学路上女孩撑伞，相册被风吹落，热心少年帮忙捡拾。',
    '角色',
    '女孩：17岁，学生。',
    '少年：18岁，学生。',
    '分镜＋台词＋视频生成提示词（9:16 竖屏）',
    '分镜1 | 0‑5s | 全景，女孩独自走在放学路上。',
    '提示词：9:16 竖屏，二次元动漫，黄昏街道。',
    '分镜2 | 5-11s | 中景，女孩踩到石子脚下打滑，相册直接飞出去。',
    '分镜3 | 11-16s | 近景，少年弯腰捡起相册并发现失主。',
    '少年（疑惑）：这是你的吗？',
    '分镜4 | 16-21s | 双人镜头，女孩追上少年取回相册。',
    '分镜5 | 21-26s | 近景，少年将相册递还给女孩。',
    '分镜6 | 26-30s | 全景，两人道谢后分别离开。',
  ].join('\n');
  const story = center.analyzeImportedScript(source, '').global_structure;
  assert.deepEqual(center.analyzeImportedScript(source, '').characters, ['女孩','少年']);
  assert.match(story.premise, /放学路上女孩撑伞/);
  assert.doesNotMatch(Object.values(story).join(' '), /分镜＋台词＋视频生成提示词|9:16 竖屏/);
  assert.match(story.setup, /独自走在放学路上/);
  assert.match(story.development, /脚下打滑/);
  assert.match(story.turning_point, /捡起相册/);
  assert.match(story.climax, /递还给女孩/);
  assert.match(story.ending, /分别离开/);
  assert.match(story.central_conflict, /人物需要应对/);
  assert.notEqual(story.central_conflict, story.development);
  assert.equal([story.premise,story.setup,story.development,story.turning_point,story.climax,story.ending,story.central_conflict].join(' ').includes('提示词'), false);
  assert.equal(new Set([story.setup,story.development,story.turning_point,story.climax,story.ending]).size, 5);
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
      const timeout = setTimeout(() => {browser.kill();reject(new Error('Chrome 响应式测试超时'));},CHROME_TEST_TIMEOUT_MS);
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
    {id:'e', title:'未完故事', synopsis:'本地草稿', stage:'setup', board_id:null, creation_status:'draft'},
  ];
  assert.deepEqual(center.filterProjects(projects, '雨', '').map(p => p.id), ['b']);
  assert.deepEqual(center.filterProjects(projects, '', 'all_projects').map(p => p.id), ['a','b','c','e']);
  assert.deepEqual(center.filterProjects(projects, '', 'active_projects').map(p => p.id), ['b']);
  assert.deepEqual(center.filterProjects(projects, '', 'blocked_projects').map(p => p.id), ['a']);
  assert.deepEqual(center.filterProjects(projects, '', 'completed').map(p => p.id), ['c']);
  assert.deepEqual(center.filterProjects(projects, '', 'creation_draft').map(p => p.id), ['e']);
  assert.deepEqual(center.metrics(projects), {all:4, active:1, blocked:1, done:1, draft:1});
  assert.match(html, /data-project-view="all_projects"/);
  assert.match(centerScript, /activeProjectView=view\|\|'all_projects'/);
  assert.match(centerScript, /selectProjectView\(metric\.getAttribute\('data-project-view'\)\)/);
  assert.match(centerScript, /aria-pressed/);
});

test('项目卡片提供快捷菜单、轻量切换和增强搜索', () => {
  const card = center.cardHtml({
    id:'project-1', title:'雨夜来信', synopsis:'旧友在雨夜重新相遇',
    stage:'setup', ratio:'16:9', target_duration:30, shot_count:6,
  }, '雨夜');
  assert.match(card, /data-card-menu/);
  assert.match(card, /data-card-action="rename"/);
  assert.match(card, /data-card-action="duplicate"/);
  assert.match(card, /data-card-action="delete"/);
  assert.match(card, /short-drama-search-match/);
  for (const id of ['shortDramaSearchClear', 'shortDramaSearchCount',
    'shortDramaEmptyAction', 'shortDramaProjectActionDialog',
    'shortDramaProjectRenameInput']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(centerScript, /复制基础设置、原稿和角色文字资料/);
  assert.match(centerScript, /activeProjectView='creation_draft'/);
  assert.match(centerScript, /projectActionDialog\.showModal\(\)/);
  assert.match(centerStyle, /short-drama-card-enter/);
  assert.match(centerStyle, /short-drama-count-pop/);
  assert.match(centerStyle, /height:320px/);
  assert.match(centerStyle, /-webkit-line-clamp:4/);
  assert.match(card, /<p title="旧友在雨夜重新相遇">/);
});

test('项目卡片以最近更新时间替代内部修订号', () => {
  const now = Date.UTC(2026, 7, 10, 8, 0, 0);
  assert.equal(center.projectUpdatedAt(now - 30 * 1000, now).label, '刚刚更新');
  assert.equal(center.projectUpdatedAt((now - 12 * 60 * 1000) / 1000, now).label, '12分钟前');
  assert.equal(center.projectUpdatedAt(now - 3 * 60 * 60 * 1000, now).label, '3小时前');
  assert.equal(center.projectUpdatedAt(now - 2 * 24 * 60 * 60 * 1000, now).label, '2天前');
  const card = center.cardHtml({id:'p1', title:'项目', synopsis:'足够长度的项目简介', stage:'setup', updated_at:(Date.now() - 30 * 1000) / 1000});
  assert.match(card, /刚刚更新/);
  assert.doesNotMatch(card, />R\d+</);
});

test('项目卡片按真实制作里程碑展示进度与下一步', () => {
  const card = center.cardHtml({
    id:'p1', title:'项目', synopsis:'项目简介', stage:'draft',
    progress_stage:'video_review', progress_percent:60,
    progress_label:'镜头生成中 3/6', progress_detail:'还有 3 个镜头未完成',
    ratio:'16:9', target_duration:30, shot_count:6,
  });
  assert.equal(center.progress({stage:'setup', progress_percent:60}), 60);
  assert.match(card, /镜头生成中 3\/6/);
  assert.match(card, /还有 3 个镜头未完成/);
  assert.match(card, /aria-valuenow="60"/);
  assert.match(card, /width:60%/);
});

test('角色资料锁定不应禁用标准图确认区域', () => {
  assert.match(centerScript, /<\/section><\/fieldset>'\+\s*'<section class="short-drama-role-reference">/);
  assert.match(centerScript, /data-confirm-role-reference/);
  assert.doesNotMatch(centerScript, /<section class="short-drama-role-reference"[^]*?<\/fieldset>/);
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
  await client.abandonLiveActionProject({id:'live-action-1', revision:7}, 'live-action-abandon-key');
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
  assert.equal(calls[6].url, '/api/gen/short-drama/projects/live-action/abandon');
  assert.equal(calls[6].options.headers['Idempotency-Key'], 'live-action-abandon-key');
  assert.deepEqual(JSON.parse(calls[6].options.body), {project_id:'live-action-1', revision:7});
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

test('live action drafts are account scoped and preserve role progress', () => {
  const values = new Map();
  const storage = {
    getItem:key => values.has(key) ? values.get(key) : null,
    setItem:(key,value) => values.set(key, String(value)),
    removeItem:key => values.delete(key)
  };
  const key = center.liveActionDraftStorageKey('alice');
  const draft = {
    version:1, username:'alice', saved_at:1700000000000, step:'live_action_roles',
    form:{title:'分享糖果',source_text:'男孩把糖果分给女孩。',ratio:'16:9'},
    roles:[{character_key:'character_1',name:'男孩',fixed_clothing:'蓝色短袖'}],
    active_role:0, pending_project:{id:'draft-project',revision:3,characters:[]}
  };
  assert.equal(key, 'hq-short-drama-live-action-draft-v1:alice');
  assert.equal(center.writeLiveActionDraftRecord(storage, key, draft, 'alice'), true);
  const restored = center.readLiveActionDraftRecord(storage, key, 'alice', 1700000001000);
  assert.equal(restored.step, 'live_action_roles');
  assert.equal(restored.roles[0].fixed_clothing, '蓝色短袖');
  assert.equal(restored.pending_project.id, 'draft-project');
  assert.equal(center.readLiveActionDraftRecord(storage, key, 'bob', 1700000001000), null);
  assert.equal(center.liveActionDraftSynopsis(draft), '尚未整理一句话故事');
  draft.core_story = {logline:'男孩最终决定与女孩分享糖果。'};
  assert.equal(center.liveActionDraftSynopsis(draft), '男孩最终决定与女孩分享糖果。');
  assert.doesNotMatch(center.liveActionDraftSynopsis(draft), /蓝色短袖|角色资料/);
  assert.match(centerScript, /已恢复上次保存的草稿。'.*autoHide:5000/);
  assert.match(centerScript, /clearTimeout\(liveActionNoticeTimers\[timerKey\]\)/);
  assert.match(centerStyle, /height:100%;max-height:none;box-sizing:border-box;padding:16px;overflow:auto/);
  assert.match(centerStyle, /short-drama-role-card::after\{display:block;height:104px/);
  assert.match(centerStyle, /-webkit-line-clamp:2/);
});

test('closing unfinished live action creation offers save discard and continue choices', () => {
  assert.match(html, /id="shortDramaCloseDraftPrompt"/);
  assert.match(html, /data-draft-close="save"/);
  assert.match(html, /data-draft-close="discard"/);
  assert.match(html, /data-draft-close="continue"/);
  assert.match(html, /id="shortDramaLiveActionDraftResume"/);
  assert.match(centerScript, /dialog\.addEventListener\('cancel'/);
  assert.match(centerScript, /if\(event\.target===dialog\)requestCreateClose\(\)/);
  assert.match(centerScript, /liveSteps=\['live_action_setup','live_action_story','live_action_roles'\]/);
  assert.match(centerScript, /roles:JSON\.parse\(JSON\.stringify\(liveActionRoles\|\|\[\]\)\)/);
  assert.match(centerScript, /clearLiveActionDraft\(\);dialog\.close\(\)/);
  assert.match(html, /id="shortDramaMetricDraft"/);
  assert.match(html, /data-project-view="creation_draft"/);
  assert.match(centerScript, /creation_status==='draft'/);
  assert.match(centerScript, /finalizeLiveActionProject/);
});

test('saved planner draft remains optional and clearing it returns to content type choice', async () => {
  const chrome = findChromeExecutable();
  assert.ok(chrome, '草稿入口测试需要 Chrome 或 Chromium');
  const draft = {
    version:4, username:'alice', saved_at:Date.now(), create_mode:'inspiration',
    payload:{title:'旧策划草稿',ratio:'16:9',target_duration:30,shot_count:6,visual_style:'电影感写实'},
    answers:{topic:'家庭情感'}, meta:{}, dirty_fields:[], history:[], transcript:[], feedback:[],
    correction_count:0, selected_direction:null, preview:null, active_field:'protagonist',
    active_choices:{field:'protagonist',items:['独居老人','返乡女儿','社区少年'],updated_at:Date.now()},
    advisor_degraded:false, panel:'auto', pending_create_key:''
  };
  const seed = `<script>localStorage.setItem('hq-short-drama-planner-draft-v3:alice',${JSON.stringify(JSON.stringify(draft))});window.confirm=function(){return true;};</script>`;
  const probe = `<script>addEventListener('DOMContentLoaded',function(){setTimeout(function(){try{var create=document.getElementById('shortDramaCreate'),choice=document.getElementById('shortDramaStartOptions'),planner=document.getElementById('shortDramaInspiration');create.click();setTimeout(function(){var resume=document.getElementById('shortDramaResumePlanner'),initial=[!choice.hidden,planner.hidden,!!resume&&!resume.hidden];if(!initial.every(Boolean)){document.documentElement.setAttribute('data-draft-entry-test','fail-initial-'+initial.map(Number).join(''));return;}resume.click();setTimeout(function(){var resumed=[choice.hidden,!planner.hidden];if(!resumed.every(Boolean)){document.documentElement.setAttribute('data-draft-entry-test','fail-resume-'+resumed.map(Number).join(''));return;}document.getElementById('shortDramaRestartPlanner').click();setTimeout(function(){var cleared=[!choice.hidden,planner.hidden,localStorage.getItem('hq-short-drama-planner-draft-v3:alice')===null];document.documentElement.setAttribute('data-draft-entry-test',cleared.every(Boolean)?'pass':'fail-clear-'+cleared.map(Number).join(''));},80);},80);},80);}catch(error){document.documentElement.setAttribute('data-draft-entry-test','error-'+error.name);}},500);});</script>`;
  const testHtml = html.replace('<script src="cloud-shell.js', seed + '<script src="cloud-shell.js').replace('</body>', probe + '</body>');
  const siteRoot = path.join(ROOT, 'site');
  const server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname);
    if (pathname === '/api/auth/me') {
      response.writeHead(200, {'Content-Type':'application/json'});response.end('{"user":{"username":"alice"}}');return;
    }
    if (pathname === '/api/gen/short-drama/projects') {
      response.writeHead(200, {'Content-Type':'application/json'});response.end('{"items":[]}');return;
    }
    if (pathname === '/workbench/short-drama.html') {
      response.writeHead(200, {'Content-Type':'text/html; charset=utf-8'});response.end(testHtml);return;
    }
    const filename = path.resolve(siteRoot, pathname.replace(/^\/+/, ''));
    if (!filename.startsWith(siteRoot) || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) {response.writeHead(404);response.end('not found');return;}
    const contentType = filename.endsWith('.css') ? 'text/css' : filename.endsWith('.js') ? 'text/javascript' : 'application/octet-stream';
    response.writeHead(200, {'Content-Type':contentType});response.end(fs.readFileSync(filename));
  });
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'hq-draft-entry-'));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const address = server.address();
    const output = await new Promise((resolve, reject) => {
      const browser = spawn(chrome, ['--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--hide-scrollbars','--virtual-time-budget=4000','--user-data-dir='+profile,'--dump-dom',`http://127.0.0.1:${address.port}/workbench/short-drama.html`]);
      let stdout='',stderr='';browser.stdout.on('data',chunk => {stdout+=chunk;});browser.stderr.on('data',chunk => {stderr+=chunk;});
      const timeout = setTimeout(() => {browser.kill();reject(new Error('Chrome 草稿入口测试超时'));},CHROME_TEST_TIMEOUT_MS);
      browser.on('error',reject);browser.on('close',code => {clearTimeout(timeout);code===0?resolve(stdout):reject(new Error(stderr||`Chrome exited ${code}`));});
    });
    assert.match(output, /data-draft-entry-test="pass"/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(profile, {recursive:true, force:true, maxRetries:8, retryDelay:100});
  }
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

test('live action role confirmation is created from the confirmed script', () => {
  const analysis = center.analyzeImportedScript('人物：张三、李四\n张三：我们现在出发。\n李四：好。');
  assert.deepEqual(analysis.characters, ['张三', '李四']);
  const contract = center.characterContractFromAnalysis(analysis);
  assert.equal(contract.length, 2);
  assert.deepEqual(contract.map(item => item.character_key), ['character_1', 'character_2']);
  assert.deepEqual(contract.map(item => item.name), analysis.characters);
  assert.deepEqual(contract.map(item => item.role_type), ['main', 'support']);
  assert.deepEqual(contract[0].reference_views, ['front_full', 'side_full', 'back_full']);
  assert.match(html, /角色已根据确认剧本建立/);
  assert.match(centerScript, /liveActionRoles=characterContractFromAnalysis\(liveActionAnalysis\)/);
  assert.match(centerScript, /renderLiveActionRoleTabs/);
  assert.match(centerScript, /data-role-select/);
  assert.match(centerScript, /基础信息/);
  assert.match(centerScript, /人物特征/);
  assert.match(centerScript, /固定造型/);
  assert.match(centerStyle, /grid-template-columns:230px minmax\(0,1fr\)/);
});

test('live action import payload carries the confirmed role contract', () => {
  const analysis = center.analyzeImportedScript('人物：林夏\n林夏：你好。');
  const contract = center.defaultManualCharacterContract(2);
  const payload = center.importProjectPayload({title:'真人短剧',source_text:'人物：林夏\n林夏：你好。'}, analysis, 'faithful', {
    content_type:'live_action', character_contract:contract
  });
  assert.equal(payload.content_type, 'live_action');
  assert.deepEqual(payload.character_contract, contract);
  assert.match(centerScript, /importProjectPayload\(liveActionForm,liveActionAnalysis,'faithful',\{content_type:'live_action',character_contract:contract\}\)/);
  assert.match(centerScript, /\{characters:contract\.map\(backendCharacterFromRole\),character_contract:contract\}/);
  const normalized = center.normalizeCharacterContract(contract);
  assert.equal(Object.hasOwn(normalized[0], 'reference_locked'), false);
  assert.equal(Object.hasOwn(normalized[0], 'reference_job_id'), false);
});

test('editing a persisted live action draft replaces it unless reference work exists', async () => {
  const draft = {id:'draft-1', revision:4, characters:[{
    character_key:'character_1', reference_job_id:null, reference_file:'',
    reference_url:'', reference_locked:false
  }]};
  assert.equal(center.liveActionProjectHasReferenceActivity(draft), false);
  let deleted = null;
  await center.discardPendingLiveActionProject({
    abandonLiveActionProject:async (project, key) => {
      deleted = {project, key}; return {deleted:true};
    }
  }, draft, false, 'stable-abandon-key');
  assert.equal(deleted.project.id, 'draft-1');
  assert.equal(deleted.key, 'stable-abandon-key');
  for (const marker of [
    {reference_job_id:9}, {reference_file:'role.png'},
    {reference_url:'https://example.test/role.png'}, {reference_version:1},
    {reference_locked:true}
  ]) {
    const protectedDraft = {...draft, characters:[{...draft.characters[0], ...marker}]};
    await assert.rejects(
      center.discardPendingLiveActionProject({abandonLiveActionProject:async () => {
        throw new Error('must not delete');
      }}, protectedDraft, false, 'protected-key'),
      /角色标准图/
    );
  }
  assert.equal(center.liveActionProjectHasReferenceActivity({...draft, spent_points:35}), true);
  await assert.rejects(
    center.discardPendingLiveActionProject({abandonLiveActionProject:async () => {
      throw new Error('delete failed');
    }}, draft, false, 'retry-key'),
    /delete failed/
  );
  await assert.rejects(
    center.discardPendingLiveActionProject({abandonLiveActionProject:async () => {
      throw new Error('must not delete');
    }}, draft, true, 'busy-key'),
    /正在生成/
  );
  const retryKeys = [];
  const uncertainClient = {abandonLiveActionProject:async (_project, key) => {
    retryKeys.push(key);
    if(retryKeys.length === 1)throw new Error('response lost after commit');
    return {deleted:true,replayed:true};
  }};
  await assert.rejects(
    center.discardPendingLiveActionProject(uncertainClient, draft, false, 'stable-retry-key'),
    /response lost/
  );
  await center.discardPendingLiveActionProject(
    uncertainClient, draft, false, 'stable-retry-key'
  );
  assert.deepEqual(retryKeys, ['stable-retry-key', 'stable-retry-key']);
  assert.match(centerScript, /if\(pendingLiveActionProject&&!pendingLiveActionDiscardKey\)pendingLiveActionDiscardKey=newLiveActionAbandonKey\(\)/);
  assert.match(centerScript, /discardPendingLiveActionProject\(client,pendingLiveActionProject,anyLiveActionReferenceBusy\(\),pendingLiveActionDiscardKey\)/);
  assert.match(centerScript, /pendingLiveActionProject=null;pendingLiveActionDiscardKey='';savedLiveActionRoleSignatures=\{\}/);
});

test('paid or locked live action roles remain editable but cannot be deleted', () => {
  const empty = {
    character_key:'character_1', reference_job_id:null, reference_file:'',
    reference_url:'', reference_version:0, reference_locked:false
  };
  assert.equal(center.liveActionRoleHasReferenceActivity(empty), false);
  for (const marker of [
    {reference_job_id:9}, {reference_file:'role.png'},
    {reference_url:'https://example.test/role.png'}, {reference_version:1},
    {reference_locked:true}
  ]) {
    assert.equal(
      center.liveActionRoleHasReferenceActivity({...empty, ...marker}), true
    );
  }
  assert.match(centerScript, /角色资料已变更，当前标准图仍会保留/);
  assert.match(centerScript, /liveActionRoleHasReferenceActivity\(removed\)/);
  assert.match(centerScript, /roleProtected=roleGenerating/);
  assert.doesNotMatch(centerScript, /if\(liveActionRoleHasReferenceActivity\(liveActionRoles\[activeLiveActionRole\]\)\)return showLiveActionError/);
});

test('live action roles only require fixed clothing when AI reference generation is selected', () => {
  assert.match(centerScript, /var fields=\['name','gender'\]/);
  assert.match(centerScript, /function validateLiveActionRole\(index\)/);
  assert.match(centerScript, /persistLiveActionRoles\(\{partial:true,roleIndex:activeLiveActionRole\}\)/);
  assert.match(centerScript, /AI 生成/);
  assert.match(centerScript, /35 点/);
  assert.match(centerScript, /reference_locked/);
  assert.match(centerScript, /confirm-character-reference/);
  assert.match(centerScript, /确认并锁定此标准图/);
  assert.match(html, /角色名称和性别为必填项/);
  assert.match(centerScript, /固定服装提示词 <small>AI 生图时必填<\/small>/);
  assert.match(centerScript, /使用 AI 生成标准图前，请先填写固定服装提示词/);
  assert.doesNotMatch(centerScript, /\{field:'fixed_clothing',label:'固定服装'\}/);
  assert.doesNotMatch(centerScript, /if\(!item\.age\)missing\.push/);
  assert.doesNotMatch(centerScript, /if\(!item\.appearance_prompt\)missing\.push/);
});

test('live action role captions keep required and optional controls aligned', () => {
  assert.match(centerScript, /short-drama-role-field-caption\">角色名称 <em class=\"short-drama-required\">\*<\/em><\/span><input data-role-field=\"name\"/);
  assert.match(centerScript, /short-drama-role-field-caption\">角色类型<\/span><select data-role-field=\"role_type\"/);
  assert.match(centerScript, /short-drama-role-field-caption\">性别 <em class=\"short-drama-required\">\*<\/em><\/span><select data-role-field=\"gender\"/);
  assert.match(centerScript, /short-drama-role-field-caption\">年龄<\/span><input data-role-field=\"age\"/);
  assert.match(centerStyle, /\.short-drama-role-field-caption\{display:inline-flex;min-height:12px;align-items:center;gap:3px\}/);
});

test('live action intro remains readable in the dark theme', () => {
  assert.match(centerStyle, /\.short-drama-live-action-intro\{[^}]*background:#fffaf0;[^}]*color:#172033/);
  assert.match(centerStyle, /\.short-drama-live-action-intro p\{color:#667085\}/);
});

test('live action role must be explicitly saved before reference generation', () => {
  assert.match(centerScript, /savedLiveActionRoleSignatures/);
  assert.match(centerScript, /function isLiveActionRoleSaved\(item\)/);
  assert.match(centerScript, /function focusLiveActionRoleError\(index\)/);
  assert.match(centerScript, /请先保存当前角色/);
  assert.match(centerScript, /请先保存角色资料/);
  const start = centerScript.indexOf('function generateLiveActionReference(index)');
  const end = centerScript.indexOf("roleTabs.addEventListener", start);
  const handler = centerScript.slice(start, end);
  assert.match(handler, /isLiveActionRoleSaved/);
  assert.doesNotMatch(handler, /persistLiveActionRoles/);
});

test('role type controls reference requirements and creation gate', () => {
  assert.match(centerScript, /function liveActionRoleReferenceRule\(item\)/);
  assert.match(centerScript, /主要角色必须锁定标准图/);
  assert.match(centerScript, /该次要角色有台词或多次出现，需要锁定标准图/);
  assert.match(centerScript, /群演无需单独设置标准图/);
  assert.match(centerScript, /liveActionRoleReferenceRule\(item\)\.required&&!\(item\.reference_url\|\|item\.reference_file\)/);
  assert.match(centerScript, /liveActionRoleReferenceRule\(item\)\.required&&!item\.reference_locked/);
});

test('character reference picker supports assets uploads and AI generation', async () => {
  assert.match(centerScript, /我的资产/);
  assert.match(centerScript, /本地上传/);
  assert.match(centerScript, /class="short-drama-reference-sources"/);
  assert.match(centerScript, /data-reference-source="asset"/);
  assert.match(centerScript, /data-reference-source="upload"/);
  assert.match(centerScript, /data-reference-source="ai"/);
  assert.doesNotMatch(centerScript, /aria-label="设置角色标准图"/);
  assert.match(centerScript, /image\/jpeg','image\/png','image\/webp/);
  assert.match(centerScript, /file\.size>10\*1024\*1024/);
  assert.match(centerScript, /真人且至少半身，无需三视图，不扣点/);
  assert.match(centerScript, /message==='请上传人物图'/);
  assert.match(centerStyle, /\.short-drama-reference-picker\{/);
  const calls = [];
  const client = center.createClient(async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>'{}'};
  });
  await client.listImageAssets(0, 60);
  await client.selectCharacterReference(
    {id:'project-1', revision:4}, {character_key:'character_1'},
    {source:'asset', asset_job_id:8, asset_url:'/api/gen/file/role.png', filename:'role.png'}
  );
  assert.equal(calls[0].url, '/api/gen/history?limit=60&offset=0&kind=image');
  assert.equal(calls[1].url, '/api/gen/short-drama/select-character-reference');
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    project_id:'project-1', revision:4, character_key:'character_1',
    source:'asset', asset_job_id:8, asset_url:'/api/gen/file/role.png',
    filename:'role.png', image_data:''
  });
  assert.match(centerScript, /className='short-drama-asset-library'/);
  assert.match(centerScript, /role="dialog" aria-modal="true" aria-label="选择图片资产"/);
  assert.match(centerScript, /加载更多图片/);
  assert.match(centerScript, /data-preview-asset-index/);
  assert.match(centerScript, /keepOpen:true/);
  assert.doesNotMatch(centerScript, /data-reference-picker-content/);
  assert.match(centerScript, /className='short-drama-upload-confirm'/);
  assert.match(centerScript, /aria-label="确认本地上传图片"/);
  assert.match(centerStyle, /\.short-drama-asset-library\{position:fixed;inset:0/);
  assert.match(centerStyle, /\.short-drama-asset-library-box\{/);
  assert.match(centerStyle, /\.short-drama-upload-confirm-box\{/);
});

test('generated character reference requires explicit confirmation before locking', async () => {
  const calls = [];
  const client = center.createClient(async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, text:async ()=>'{}'};
  });
  await client.confirmCharacterReference(
    {id:'project-1', revision:7},
    {character_key:'character_1', reference_version:3}
  );
  assert.equal(calls[0].url, '/api/gen/short-drama/confirm-character-reference');
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    project_id:'project-1', revision:7,
    character_key:'character_1', reference_version:3
  });
});

test('character reference generation remains recoverable after a long wait', () => {
  assert.match(centerScript, /timeoutError\.recoverable=true/);
  assert.match(centerScript, /角色标准图生成时间较长，任务仍保留，可刷新页面继续查看/);
  assert.match(centerScript, /系统会自动恢复查询/);
});

test('character reference generation is tracked per role and can resume polling', async () => {
  const calls = [];
  const created = [];
  const client = center.createClient(async (url, options) => {
    calls.push({url, options});
    const body = url.includes('generate-character-reference')
      ? {job_id:91}
      : {status:'done', result:{url:'/api/gen/file/role.png'}};
    return {ok:true, status:200, text:async () => JSON.stringify(body)};
  });
  const project = {id:'project-1', revision:7};
  const character = {character_key:'character_1'};
  await client.generateCharacterReference(project, character, {
    onCreated(jobId){ created.push(jobId); }
  });
  assert.deepEqual(created, [91]);
  assert.equal(calls[0].url, '/api/gen/short-drama/generate-character-reference');
  assert.equal(calls[1].url, '/api/gen/job/91');
  calls.length = 0;
  await client.generateCharacterReference(project, character, {job_id:91});
  assert.deepEqual(calls.map(call => call.url), ['/api/gen/job/91']);
  assert.match(centerScript, /liveActionReferenceTasks=\{\}/);
  assert.match(centerScript, /reference_tasks:JSON\.parse/);
  assert.match(centerScript, /角色标准图正在生成，请等待任务完成；你可以继续编辑其他角色/);
  assert.match(centerScript, /当前角色资料暂时锁定。你可以切换到其他角色继续编辑和保存/);
  assert.match(centerScript, /请等待全部角色标准图任务完成后再创建项目/);
  assert.match(centerScript, /图片服务繁忙，正在自动重试/);
  assert.match(centerScript, /无需重复提交/);
  assert.match(centerScript, /HTTP\\s\*\(\?:Error\\s\*\)\?429/);
  assert.match(centerScript, /if\(activeLiveActionRole===index\)renderLiveActionRoles\(\);else renderLiveActionRoleTabs\(\)/);
  assert.match(centerStyle, /\.short-drama-role-tab\.generating/);
});

test('character reference view guidance is grouped with the standard image', () => {
  assert.match(centerScript, /AI 标准图将包含/);
  assert.match(centerScript, /<b>正面全身<\/b><b>侧面全身<\/b><b>背面全身<\/b>/);
  assert.match(centerScript, /<span>图片要求<\/span><b>真人<\/b><b>至少半身<\/b><b>无需三视图<\/b>/);
  assert.doesNotMatch(centerScript, /正面半身/);
  assert.match(centerStyle, /\.short-drama-role-reference-content\{display:grid;gap:10px\}/);
});

test('legacy role drafts keep an explicit back-view migration warning', () => {
  assert.match(centerScript, /character_contract_migration/);
  assert.match(centerScript, /旧草稿缺少背面全身图，请生成并确认可信 AI 三视图标准图/);
  assert.match(centerScript, /普通上传或旧版半身参考图不能作为迁移证据/);
  assert.match(centerScript, /if\(migration\.required\)showLiveActionNotice/);
});

test('character reference validation keeps the selected image available for retry', () => {
  assert.match(centerScript, /正在检测并自动重试/);
  assert.match(centerScript, /重新检测此图片/);
  assert.match(centerScript, /重新检测所选资产/);
  assert.match(centerScript, /\^\(请上传\|人物检测\|人物图片检测\)/);
});

test('character reference image opens an accessible large preview', () => {
  assert.match(centerScript, /data-preview-role-reference aria-label="放大预览/);
  assert.match(centerScript, /createElement\('dialog'\)/);
  assert.match(centerScript, /aria-label','角色标准图大图预览/);
  assert.match(centerScript, /addEventListener\('cancel'.*closeLiveActionReferencePreview/);
  assert.match(centerScript, /preview\.showModal\(\)/);
  assert.match(centerStyle, /\.short-drama-image-preview\{position:fixed;inset:0;width:100vw;height:100vh/);
  assert.match(centerStyle, /\.short-drama-image-preview\[open\]\{display:grid;place-items:center\}/);
});

test('live action entry exposes unavailable content types without project creation', () => {
  assert.match(html, /data-content-type="live_action"/);
  assert.match(html, /data-content-type="comic"/);
  assert.match(html, /data-content-type="digital_presenter"/);
  assert.match(centerScript, /团队正在努力开发该功能/);
});
