const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');

const ROOT = path.resolve(__dirname, '..');
const center = require(path.join(ROOT, 'site/workbench/short-drama-center.js'));
const html = fs.readFileSync(path.join(ROOT, 'site/workbench/short-drama.html'), 'utf8');
const shell = fs.readFileSync(path.join(ROOT, 'site/workbench/cloud-shell.js'), 'utf8');
const centerScript = fs.readFileSync(path.join(ROOT, 'site/workbench/short-drama-center.js'), 'utf8');
const centerStyle = fs.readFileSync(path.join(ROOT, 'site/workbench/short-drama-center.css'), 'utf8');

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

test('灵感助手逐步询问并输出三个可编辑方向', () => {
  assert.match(center.advisorStep([]).message, /哪一类内容/);
  assert.match(center.advisorStep(['家庭情感']).message, /看完是什么感受/);
  assert.match(center.advisorStep(['家庭情感', '温暖治愈']).message, /什么结局/);
  const result = center.advisorStep(['家庭情感', '温暖治愈', '合理反转']);
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
  assert.equal(center.plannerProgress(messages, direction, preview).score, 100);
  preview.shots[0].sound = 'CONFIRMED_SOUND_MARKER';
  preview.shots[0].transition = 'CONFIRMED_TRANSITION_MARKER';
  preview.shots[0].continuity = 'CONFIRMED_CONTINUITY_MARKER';
  const contract = center.plannerConfirmedContract(preview);
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

test('前置策划页面提供聊天、结构化卡片和人工确认入口', () => {
  for (const id of [
    'shortDramaIdeaChat', 'shortDramaRecommendations', 'shortDramaScriptPreview',
    'shortDramaPlannerBrief', 'shortDramaGeneratePreview', 'shortDramaConfirmScript'
  ]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(html, /确认剧本并创建项目/);
  assert.match(html, /保存设置并进入剧本策划/);
});

test('前置策划三栏使用包含内边距的自适应盒模型且不产生横向滚动', () => {
  assert.match(centerStyle, /\.short-drama-create-shell\{[^}]*box-sizing:border-box[^}]*overflow:hidden/);
  assert.match(centerStyle, /\.short-drama-planner-grid\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,1\.65fr\) minmax\(0,\.9fr\)[^}]*max-width:100%[^}]*overflow:hidden/);
  assert.match(centerStyle, /@media\(max-width:760px\)[^{]*\{[^}]*\.short-drama-create-dialog:has/);
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

test('浏览器运行时只使用模块内已定义的全局引用', () => {
  assert.match(centerScript, /var runtimeRoot=/);
  assert.doesNotMatch(centerScript, /\broot\.location\b/);
});
