const assert = require('assert');
const agent = require('../site/workbench/script-agent.js');

function node(value, options) {
  options = options || {};
  const classes = new Set(options.classes || []);
  const result = {
    value: value || '', textContent: options.textContent || value || '', hidden: false,
    style: { display: options.display || '' }, disabled: !!options.disabled,
    checked: !!options.checked, files: options.files || [],
    attributes: options.attributes || {}, clicked: false, focused: false,
    classList: {
      add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if(force === true) classes.add(name);
        else if(force === false) classes.delete(name);
        else if(classes.has(name)) classes.delete(name); else classes.add(name);
      },
    },
    getAttribute(name) { return this.attributes[name] || null; },
    click() { this.clicked = true; }, focus() { this.focused = true; },
    scrollIntoView() {}, dispatchEvent() {},
  };
  if(options.options) result.options = options.options;
  if(options.noValue) delete result.value;
  return result;
}

function fixture(mode, breakdownTool) {
  mode = mode || 'write';
  breakdownTool = breakdownTool || 'scenes';
  const modeNodes = [
    node('AI写脚本', { attributes: {'data-mode': 'write'} }),
    node('文案成片', { attributes: {'data-mode': 'script_to_video'} }),
    node('拆解视频', { attributes: {'data-mode': 'breakdown'} }),
  ];
  const nodes = {
    panelBreakdown: node('', { display: mode === 'breakdown' ? '' : 'none' }), scTopic: node('夏日护肤'),
    scSell: node('清爽不黏腻'), scMeta: node('meta'), bdAnalysis: node('', { display: 'none' }),
    bdUrl: node(''), scGen: node(''), bdGen: node(''), scGenVideo: node(''),
    scGenAudio: node(''), scExport: node(''),
  };
  const breakdownToolNodes = [
    node('分解拆解', { attributes: {'data-bd-tool': 'scenes'} }),
    node('提示词反推', { attributes: {'data-bd-tool': 'reverse_prompt'} }),
  ];
  const options = {
    '#segStyle .on': node('口播'), '#segDur .on': node('30s'), '#platRow .on': node('抖音'),
    '#scModeTabs [data-mode].on': modeNodes.filter((item) => item.getAttribute('data-mode') === mode)[0],
    '#bdToolTabs [data-bd-tool].on': breakdownToolNodes.filter((item) => item.getAttribute('data-bd-tool') === breakdownTool)[0],
  };
  const lists = {
    '#scScenes .sc-card': [node('scene1'), node('scene2'), node('scene3')],
    '#segStyle .sc-opt': [options['#segStyle .on'], node('剧情'), node('种草'), node('口播技巧')],
    '#segDur .sc-opt': [node('15s'), options['#segDur .on'], node('60s')],
    '#platRow .sc-chip': [options['#platRow .on'], node('小红书'), node('视频号')],
    '#scModeTabs [data-mode]': modeNodes,
    '#bdToolTabs [data-bd-tool]': breakdownToolNodes,
  };
  const doc = {
    defaultView: { Event: function Event() {} },
    getElementById(id) { const value = nodes[id] || null; if(value) value.ownerDocument = doc; return value; },
    querySelector(selector) { return options[selector] || null; },
    querySelectorAll(selector) { return lists[selector] || []; },
  };
  return { doc, nodes, lists };
}

function digitalHumanFixture(mode, narrationMode) {
  mode = mode || 'photo';
  narrationMode = narrationMode || 'text';
  const photoTab = node('数字人一键生成', {
    attributes: {'data-dh-mode': 'photo'}, classes: mode === 'photo' ? ['on'] : [],
  });
  const videoTab = node('真人视频 Precision', {
    attributes: {'data-dh-mode': 'video'}, classes: mode === 'video' ? ['on'] : [],
  });
  const textMode = node('text', {checked: narrationMode === 'text', attributes: {value: 'text'}});
  const audioMode = node('audio', {checked: narrationMode === 'audio', attributes: {value: 'audio'}});
  const templates = [
    node('参考片高频剪辑', {attributes: {'data-template': 'viral-talking-head-v1'}, classes: ['on']}),
    node('专业讲解', {attributes: {'data-template': 'professional-explainer-v1'}}),
    node('纯净口播', {attributes: {'data-template': 'clean-talking-v1'}}),
  ];
  const nodes = {
    dhPhotoMode: node('', {display: mode === 'photo' ? '' : 'none'}),
    dhVideoMode: node('', {display: mode === 'video' ? '' : 'none'}),
    script: node('照片模式口播'), dhScript: node('真人视频新口播'),
    photo: node(''), photoName: node('portrait.png', {classes: ['file-ready']}),
    dhVideoFile: node(''), dhVideoName: node('真人视频 #8', {classes: ['file-ready']}),
    voiceSource: node('vip_personal'), voice: node(''), driveAudio: node(''),
    customerMaterialCount: node('', {textContent: '2 / 6', noValue: true}),
    consent: node('', {checked: false}), dhConsent: node('', {checked: false}),
    result: node(''), dhPrecisionResult: node(''), dhVoicePreview: node('', {disabled: mode !== 'video'}),
    photoDrop: node(''), voiceUploadDrop: node(''), customerMaterialsPicker: node(''),
    driveAudioDrop: node(''), dhDrop: node(''), analyze: node(''), start: node(''),
    dhAnalyze: node(''), dhStart: node(''),
  };
  const options = {
    '[data-dh-mode].on': mode === 'video' ? videoTab : photoTab,
    'input[name="narrationMode"]:checked': narrationMode === 'audio' ? audioMode : textMode,
    '.precision-template.on': templates[0], '.precision-source.on': null,
    '.step.failed': null, '.step.running': null,
  };
  const lists = {
    '[data-dh-mode]': [photoTab, videoTab],
    'input[name="narrationMode"]': [textMode, audioMode],
    '.precision-template': templates,
  };
  const doc = {
    body: node('', {attributes: {'data-director-guide-contract': 'digital-human-oneclick-guide-v1'}}),
    defaultView: {Event: function Event() {}},
    getElementById(id) { const value = nodes[id] || null; if(value) value.ownerDocument = doc; return value; },
    querySelector(selector) { return options[selector] || null; },
    querySelectorAll(selector) { return lists[selector] || []; },
  };
  return {doc, nodes, lists};
}

{
  const { doc } = fixture();
  const context = agent.createPageContext(doc);
  assert.equal(context.page, 'script');
  assert.equal(context.mode, 'write');
  assert.equal(context.topic, '夏日护肤');
  assert.equal(context.scene_count, 3);
  assert.equal(context.has_script, true);
  assert.equal(context.breakdown_tool, 'scenes');
  assert.equal(context.has_reverse_prompt, false);
  assert.match(agent.createPageSnapshot(doc).page_revision, /^[a-f0-9]{8}$/);
}
{
  const { doc, nodes, lists } = fixture();
  lists['#scScenes .sc-card'] = [node('示例分镜', { attributes: {'data-placeholder': '1'} })];
  nodes.scGenVideo.disabled = true;
  const context = agent.createPageContext(doc);
  assert.equal(context.scene_count, 0);
  assert.equal(context.has_script, false);
  assert.equal(context.active_job_status, 'running');
}
{
  const { doc } = fixture('breakdown', 'scenes');
  const context = agent.createPageContext(doc);
  assert.equal(context.has_breakdown, true);
  assert.equal(context.breakdown_scene_count, 3);
}
{
  const { doc, nodes } = fixture('breakdown', 'reverse_prompt');
  nodes.bdReversePromptText = node('电影感产品特写提示词');
  const context = agent.createPageContext(doc);
  assert.equal(context.breakdown_tool, 'reverse_prompt');
  assert.equal(context.has_reverse_prompt, true);
  assert.equal(context.has_breakdown, true);
  assert.equal(context.breakdown_scene_count, 0);
}
{
  const { doc } = fixture('script_to_video');
  const context = agent.createPageContext(doc);
  assert.equal(context.mode, 'script_to_video');
  assert.equal(context.has_script, true);
  assert.equal(context.scene_count, 3);
}

{
  const {doc} = digitalHumanFixture('photo', 'text');
  const context = agent.createPageContext(doc);
  assert.equal(context.page, 'digital_human_oneclick');
  assert.equal(context.guide_contract, 'digital-human-oneclick-guide-v1');
  assert.equal(context.mode, 'photo');
  assert.equal(context.narration_mode, 'text');
  assert.equal(context.script_text, '照片模式口播');
  assert.equal(context.script_length, 6);
  assert.equal(context.has_portrait, true);
  assert.equal(context.has_voice_source, true);
  assert.equal(context.customer_material_count, 2);
  assert.equal(context.consent_confirmed, false);
  assert.equal(context.active_job_status, 'idle');
  doc.getElementById('voiceSource').value = '__clone__';
  assert.equal(agent.createPageContext(doc).has_voice_source, false);
  const body = agent.buildPayload('把文案填进去', doc, {messages: []}, {
    getItem() { return 'digital_session_123'; }, setItem() {},
  });
  assert.equal(body.source_page, 'digital_human_oneclick');
  assert.equal(body.page_context.page, 'digital_human_oneclick');
}
{
  const {doc} = digitalHumanFixture('video', 'text');
  const context = agent.createPageContext(doc);
  assert.equal(context.mode, 'video');
  assert.equal(context.script_text, '真人视频新口播');
  assert.equal(context.has_video_source, true);
  assert.equal(context.has_voice_source, true);
  assert.equal(context.precision_template, 'viral-talking-head-v1');
}

{
  const { doc, nodes, lists } = fixture();
  agent.applyAction({type:'fill_field',field:'selling_points',value:'三秒吸收',label:'填入卖点'}, doc, {});
  assert.equal(nodes.scSell.value, '三秒吸收');
  agent.applyAction({type:'choose_option',field:'style',value:'剧情',label:'选剧情'}, doc, {});
  assert.equal(lists['#segStyle .sc-opt'][1].clicked, true);
  agent.applyAction({type:'focus',target:'generate_video',label:'看视频按钮'}, doc, {});
  agent.applyAction({type:'choose_option',field:'style',value:'口播',label:'选口播'}, doc, {});
  assert.equal(lists['#segStyle .sc-opt'][0].clicked, true);
  assert.equal(lists['#segStyle .sc-opt'][3].clicked, false);
  lists['#segStyle .sc-opt'][0].clicked = false;
  lists['#segStyle .sc-opt'][3].clicked = false;
  agent.applyAction({type:'choose_option',field:'style',value:'口',label:'模糊选口播'}, doc, {});
  assert.equal(lists['#segStyle .sc-opt'][0].clicked, true);
  assert.equal(lists['#segStyle .sc-opt'][3].clicked, false);
  agent.applyAction({type:'choose_option',field:'breakdown_tool',value:'reverse_prompt',label:'切换提示词反推'}, doc, {});
  assert.equal(lists['#bdToolTabs [data-bd-tool]'][1].clicked, true);
  assert.equal(nodes.scGenVideo.focused, true);
  const win = { location: { href: '' } };
  agent.applyAction({type:'navigate',target:'assets',label:'去素材库'}, doc, win);
  assert.equal(win.location.href, '/workbench/assets.html');
  assert.throws(() => agent.applyAction({type:'navigate',target:'https://evil.example',label:'外链'}, doc, win), /站内目标无效/);
}
{
  const {doc, nodes, lists} = digitalHumanFixture('photo', 'text');
  agent.applyAction({type:'fill_field',field:'digital_human_script',value:'顾客发来的新文案',label:'填入口播'}, doc, {});
  assert.equal(nodes.script.value, '顾客发来的新文案');
  agent.applyAction({type:'choose_option',field:'narration_mode',value:'audio',label:'使用完整录音'}, doc, {});
  assert.equal(lists['input[name="narrationMode"]'][1].clicked, true);
  agent.applyAction({type:'choose_option',field:'precision_template',value:'professional-explainer-v1',label:'选择专业讲解'}, doc, {});
  assert.equal(lists['.precision-template'][1].clicked, true);
  agent.applyAction({type:'switch_mode',mode:'video',label:'切换真人视频'}, doc, {});
  assert.equal(lists['[data-dh-mode]'][1].clicked, true);
  agent.applyAction({type:'focus',target:'photo_upload',label:'上传照片'}, doc, {});
  assert.equal(nodes.photoDrop.focused, true);
  agent.applyAction({type:'focus',target:'generate_photo_video',label:'查看生成按钮'}, doc, {});
  assert.equal(nodes.start.focused, true);
  assert.equal(nodes.start.clicked, false);
}

{
  const { doc } = fixture();
  const snapshot = agent.createPageSnapshot(doc);
  assert.equal(agent.validatePlan({page_revision:snapshot.page_revision,actions:[]}, doc), true);
  assert.throws(() => agent.validatePlan({page_revision:'deadbeef',actions:[]}, doc), /页面内容已变化/);
}

{
  const values = {};
  const storage = {
    getItem(key){ return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null; },
    setItem(key,value){ values[key] = String(value); },
  };
  const pending = agent.createPendingRequest(
    {prompt:'resume me',page_revision:'a1b2c3d4',page_context:{mode:'write'}},
    'director-agent-recovery123',
    'resume me',
    12345
  );
  assert.equal(pending.summary.prompt, 'resume me');
  assert.equal(pending.job_id, null);
  agent.saveState(storage,{messages:[{role:'user',content:'resume me'}],open:true,pending_request:pending});
  const restored = agent.readState(storage);
  assert.equal(restored.pending_request.key, 'director-agent-recovery123');
  assert.equal(restored.pending_request.body.page_revision, 'a1b2c3d4');
  assert.equal(restored.open, true);
  assert.equal(agent.autoResumeKind(restored), '');
  restored.pending_request.job_id = '77';
  assert.equal(agent.autoResumeKind(restored), 'request');
  assert.equal(agent.validPendingRequest({
    key:'bad',body:{},job_id:null,created_at:1
  }), null);
}

const source = require('fs').readFileSync(require('path').join(__dirname, '../site/workbench/script-agent.js'), 'utf8');
const digitalHumanPage = require('fs').readFileSync(require('path').join(__dirname, '../site/workbench/digital-human-oneclick.html'), 'utf8');
assert.ok(source.includes('currentPlan.actions.map(function(action){return applyAction(action,doc,win);}'));
assert.ok(source.includes('health.director_agent_enabled!==true'));
assert.ok(source.includes("button.textContent='确认生产并扣 '+offer.expected_cost+' 点'"));
assert.ok(source.includes("'/api/gen/director_agent/produce'"));
assert.ok(source.indexOf('state.pending_request=record; persist();') <
  source.indexOf('runPending(record,false);'));
assert.ok(source.includes("if(resumeKind==='request') runPending(state.pending_request,true);"));
assert.ok(source.includes("showRecovery('request')"));
assert.ok(!source.includes("setTimeout(function(){resolve(accepted());},1400)"));
assert.ok(!source.includes("setTimeout(function(){resolve(accepted());},1500)"));
assert.ok(!source.includes('private_domain_video'));
assert.ok(digitalHumanPage.includes('src="script-agent.js?'));
assert.ok(digitalHumanPage.includes('data-director-guide-contract="digital-human-oneclick-guide-v1"'));
assert.equal(agent.retainProductionOfferAfterError({status:402,data:{code:'insufficient_points'}}), true);
assert.equal(agent.retainProductionOfferAfterError({status:400,data:{code:'director_offer_expired'}}), false);
assert.equal(agent.retainProductionOfferAfterError({status:409,data:{code:'director_offer_refreshed'}}), false);
for(const id of ['photoDrop','voiceUploadDrop','customerMaterialsPicker','driveAudioDrop']) {
  assert.ok(digitalHumanPage.includes('id="'+id+'"'));
}

(async function(){
  let mounted = 0;
  const healthDoc = {
    getElementById(id){ return id === 'scTopic' ? {} : null; },
  };
  function healthResponse(data, ok) {
    return {
      ok: ok !== false, status: ok === false ? 503 : 200,
      text(){ return Promise.resolve(JSON.stringify(data)); },
    };
  }
  const disabled = await agent.bootstrap(healthDoc, {
    fetch(){ return Promise.resolve(healthResponse({director_agent_enabled:false})); },
  }, function(){ mounted += 1; });
  assert.equal(disabled, null);
  assert.equal(mounted, 0);
  const enabled = await agent.bootstrap(healthDoc, {
    fetch(){ return Promise.resolve(healthResponse({director_agent_enabled:true})); },
  }, function(){ mounted += 1; return 'mounted'; });
  assert.equal(enabled, 'mounted');
  assert.equal(mounted, 1);
  const unavailable = await agent.bootstrap(healthDoc, {
    fetch(){ return Promise.resolve(healthResponse({detail:'maintenance'}, false)); },
  }, function(){ mounted += 1; });
  assert.equal(unavailable, null);
  assert.equal(mounted, 1);
  const digitalHealthDoc = {
    getElementById(id){ return id === 'dhPhotoMode' ? {} : null; },
  };
  const digitalEnabled = await agent.bootstrap(digitalHealthDoc, {
    fetch(){ return Promise.resolve(healthResponse({director_agent_enabled:true})); },
  }, function(){ mounted += 1; return 'digital-mounted'; });
  assert.equal(digitalEnabled, 'digital-mounted');
  assert.equal(mounted, 2);
  let calls = 0;
  const win = {fetch(){
    calls += 1;
    if(calls === 1) return Promise.reject(new Error('temporary network failure'));
    return Promise.resolve({
      ok:true,status:200,
      text(){return Promise.resolve(JSON.stringify({status:'done',result:{content:'ok'}}));},
    });
  }};
  const result = await agent.pollJob(win, 'job_123');
  assert.equal(result.content, 'ok');
  assert.equal(calls, 2);
  const postKeys = [];
  let postCalls = 0;
  let persistedJob = '';
  const recoveryRecord = agent.createPendingRequest(
    {prompt:'same request',page_revision:'a1b2c3d4',page_context:{mode:'write'}},
    'director-agent-response-lost-123',
    'same request',
    54321
  );
  const recoveryWin = {fetch(url, options){
    if(url === '/api/gen/director_agent'){
      postCalls += 1;
      postKeys.push(options.headers['Idempotency-Key']);
      assert.equal(options.body, JSON.stringify(recoveryRecord.body));
      if(postCalls === 1) return Promise.reject(new Error('response lost'));
      return Promise.resolve({
        ok:true,status:200,
        text(){return Promise.resolve(JSON.stringify({job_id:77}));},
      });
    }
    assert.equal(url, '/api/gen/job/77');
    return Promise.resolve({
      ok:true,status:200,
      text(){return Promise.resolve(JSON.stringify({
        status:'done',result:{content:'recovered'}
      }));},
    });
  }};
  await assert.rejects(agent.resumeRequest(
    recoveryWin, recoveryRecord,
    function(updated){ persistedJob = updated.job_id; }
  ), /response lost/);
  assert.equal(postCalls, 1);
  const recovered = await agent.resumeRequest(
    recoveryWin, recoveryRecord,
    function(updated){ persistedJob = updated.job_id; }
  );
  assert.equal(recovered.content, 'recovered');
  assert.equal(postCalls, 2);
  assert.deepEqual(postKeys, [
    'director-agent-response-lost-123',
    'director-agent-response-lost-123'
  ]);
  assert.equal(persistedJob, '77');

  const pollingRecord = agent.createPendingRequest(
    {prompt:'poll existing',page_revision:'a1b2c3d4',page_context:{mode:'write'}},
    'director-agent-existing-job-123',
    'poll existing',
    67890
  );
  pollingRecord.job_id = '88';
  let existingCalls = 0;
  const existing = await agent.resumeRequest({fetch(url){
    existingCalls += 1;
    assert.equal(url, '/api/gen/job/88');
    return Promise.resolve({
      ok:true,status:200,
      text(){return Promise.resolve(JSON.stringify({status:'done',result:{content:'continued'}}));},
    });
  }}, pollingRecord);
  assert.equal(existing.content, 'continued');
  assert.equal(existingCalls, 1);
  console.log('director agent frontend tests passed');
})().catch(function(error){

  console.error(error);
  process.exitCode = 1;
});
