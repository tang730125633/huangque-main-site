// 线上真实金线（#11 残留闭环）：全新空白会话，真实 LLM 走通 1→6 模块——
// 采集 → 报告初稿（三套方案）→ 选方案定稿 → 自动确认 → 选题 → 选选题 → 三版文案 → 修订。
// 全程只读对话/状态，不点任何付费按钮（报告/选题/文案均不扣点，出片才扣点，本脚本不碰出片）。
//
// 判定以服务端状态 API 为主（report.status: draft_ready→final，m5/m6.status: ready），
// 对话文本证据为辅。结尾校验历史顺序（第一条 user 必须是自我介绍——
// 串行队列修复上线后，连发的消息不再乱序抢答）。
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'https://huangquechuanmei.com/workbench/ip12/';
const errors = [];
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

// 收集对话全部可见文本 + 状态区
async function texts() {
  return page.evaluate(() => {
    // 真实气泡：排除状态气泡、打字指示器（#typing）与空文本气泡
    const real = (sel) => [...document.querySelectorAll(sel)]
      .filter((n) => !n.closest('#typing') && n.textContent.trim() !== '')
      .map((n) => n.textContent);
    const asst = real('.msg.assistant .bubble:not(.status-bubble)');
    const users = real('.msg.user .bubble:not(.status-bubble)');
    return {
      all: [...users, ...asst].join('\n'),
      tail: [...users, ...asst].slice(-8).join('\n'),
      userCount: [...document.querySelectorAll('.msg.user')].filter((n) => !n.closest('#typing')).length,
      asstRealCount: asst.length,
      lastAsst: asst[asst.length - 1] || '',
      lastUser: users[users.length - 1] || '',
    };
  });
}

// 服务端状态：report.status / confirmed / m5 / m6（经页面同源 fetch，避免本地 curl 走代理）
async function serverState() {
  return page.evaluate(async () => {
    const sid = localStorage.getItem('hq-v4-session-id');
    const r = await fetch('api/v4/restore/' + encodeURIComponent(sid));
    if (!r.ok) return { error: 'http ' + r.status };
    return r.json();
  });
}

async function waitState(check, timeout, label) {
  const t0 = Date.now();
  let last = null;
  while (true) {
    try { last = await serverState(); } catch (e) { last = { error: String(e) }; }
    if (check(last)) { log('✔', label, `(${((Date.now() - t0) / 1000).toFixed(0)}s)`); return last; }
    if (Date.now() - t0 > timeout) { log('✘', label, '超时', ((Date.now() - t0) / 1000).toFixed(0) + 's', JSON.stringify(last).slice(0, 160)); return null; }
    await page.waitForTimeout(3000);
  }
}

async function waitText(substrs, timeout, label) {
  const list = [].concat(substrs);
  const t0 = Date.now();
  while (true) {
    const t = await texts();
    if (list.some((s) => t.all.includes(s))) { log('✔', label, `(${((Date.now() - t0) / 1000).toFixed(0)}s)`); return t; }
    if (Date.now() - t0 > timeout) { log('✘', label, '超时', ((Date.now() - t0) / 1000).toFixed(0) + 's', 'tail:', t.tail.slice(0, 160)); return null; }
    await page.waitForTimeout(3000);
  }
}

async function send(msg, label) {
  log('→', label, msg.slice(0, 48));
  const before = await texts();
  await page.fill('#input', msg);
  await page.press('#input', 'Enter');
  // 1) 自己的消息先上屏（证明已发出）
  const t0 = Date.now();
  while (Date.now() - t0 < 10000) {
    const t = await texts();
    if (t.userCount > before.userCount) break;
    await page.waitForTimeout(500);
  }
  // 2) 等一条新的真实回复气泡（排除状态气泡；同会话轮次串行，每发必答）
  const t1 = Date.now();
  while (true) {
    const t = await texts();
    if (t.asstRealCount > before.asstRealCount) {
      log('  回复出现', ((Date.now() - t1) / 1000).toFixed(0) + 's', '|', t.lastAsst.slice(0, 40));
      return t;
    }
    if (Date.now() - t1 > 150000) { log('  ⚠ 等回复超时，继续下一步'); return null; }
    await page.waitForTimeout(3000);
  }
}

// 按追问内容给对应素材（避免同一句车轱辘话答所有问题）
const asked = new Set();
function answerFor(q) {
  if (/产品|工厂|做什么|卖什么|品类|供应链/.test(q)) return '我的工厂主要做收纳盒、整理箱这类家居收纳产品，义乌有自己车间，塑料和布艺两类都有，目前只做这一个品类，复购率高。';
  if (/变现|赚钱|涨粉|带货|盈利|模式|收入/.test(q)) return '变现预期是先涨粉再带货：前期靠干货涨到十万粉，再挂自己供应链的收纳产品链接，佣金自己可控。';
  if (/受众|观众|人群|粉丝|给谁|谁看|目标/.test(q)) return '目标人群是 25-40 岁想做副业或小生意的上班族，他们最关心怎么低成本起步、怎么避坑。';
  if (/频率|更新|一周|多久|节奏|时间/.test(q)) return '我一周能稳定出 2 条口播，手机+办公室就能拍，不需要额外设备。';
  if (/最难|低谷|状态|下决心|砍|亏/.test(q)) return '亏 200 万最难的时候是仓库压了 60 万货卖不动、连工资都发不出来；最后我盘了账，发现只有收纳这一个品类在赚钱，咬牙把其他全砍掉，两个月才缓过来。';
  if (/起来|关键|一步|爆|起量|怎么火|开头/.test(q)) return '那个收纳盒最开始是靠一条「3 块钱收纳盒的成本拆解」口播起的量：我把成本、利润、运费全摊开讲，评论区全是问链接的，当天就跑了 30 万播放。';
  if (/故事|成就|成功|经验|亮点|素材/.test(q)) return '最有成就感的是去年把一个 3 块钱的收纳盒单品做到月销 90 万，靠的是死磕复购率。';
  return '这方面我再补充一下：拍视频用手机+领夹麦，后期只会剪映基础剪辑；账号定位是「工厂老板讲选品和生意经」，内容走数字摊开讲的路子。';
}

function questionFrom(t) {
  const lines = t.lastAsst.split('\n').filter((l) => l.includes('？') || l.includes('?'));
  return (lines.pop() || '').trim();
}

const stage = { ok: {} };
try {
  // ---- 全新空白会话 ----
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => !!localStorage.getItem('hq-v4-session-id'), null, { timeout: 30000 });
  const sid = await page.evaluate(() => localStorage.getItem('hq-v4-session-id'));
  log('新会话 sid:', sid);
  stage.ok.start = true;

  // 等开场白气泡渲染完再发第一条（避免把开场白误当成对首条消息的回复）
  {
    const t0 = Date.now();
    while (Date.now() - t0 < 120000) {
      const t = await texts();
      if (t.asstRealCount >= 1) { log('✔ 开场白出现', t.lastAsst.slice(0, 30)); break; }
      await page.waitForTimeout(3000);
    }
  }

  // ---- 1. 采集：一次性自我介绍覆盖核心字段 ----
  await send('你好！我想做我的 IP 人设定位。我叫周远志，35 岁，在义乌做了 8 年跨境电商，主要做家居小百货，'
    + '自己的公司团队 12 个人。我最有成就感的事是去年把一个 3 块钱的收纳盒单品做到月销 90 万。'
    + '我踩过最大的坑是 2023 年盲目扩品类亏了 200 万，后来砍到只做收纳这一个品类才缓过来。'
    + '我的目标人群是 25-40 岁想做副业或小生意的上班族。我性格比较实诚、直给，不太会包装自己。'
    + '我希望通过短视频帮这些人避坑，顺便给自己的供应链带货。我平时能拍口播，有手机、有办公室，一周能出 2 条。'
    + '我最讨厌虚头巴脑的营销话术，喜欢把成本、利润这些数字摊开讲。', '自我介绍（覆盖核心字段）');

  // ---- 2. 追问应答：按问题给素材，最多 6 轮；报告状态出现即停 ----
  for (let i = 0; i < 6; i++) {
    const st = await serverState().catch(() => ({}));
    if (st.report && st.report.status === 'draft_ready') break;
    const t = await texts();
    const q = questionFrom(t);
    if (!q) break; // Agent 不再追问（可能正在生成报告）
    const key = q.slice(0, 20);
    if (asked.has(key)) {
      // 同一问题又问了一遍：换个角度补充，不空手回应（轮次上限兜底防死循环）
      log('追问重复，换角度补答:', q.slice(0, 40));
      await send('我换个角度说：' + answerFor(q) + '；再补充一点，我出内容最看重真诚和数字，最反感夸大。', '补充回答第 ' + (i + 1) + ' 轮');
      continue;
    }
    asked.add(key);
    log('追问检测:', q.slice(0, 50));
    await send(answerFor(q), '补充回答第 ' + (i + 1) + ' 轮');
  }

  // ---- 3. 报告初稿：三套方案（状态 + 文本双证据）----
  stage.ok.reportDraft = !!(await waitState((s) => s.report && s.report.status === 'draft_ready', 420000, '报告初稿三套方案就绪（draft_ready）'));
  if (stage.ok.reportDraft) await waitText(['方案'], 60000, '方案文本出现在对话');
  if (!stage.ok.reportDraft) {
    // 信息若还不够，直说“信息够了，帮我出报告”，再等一轮
    await send('信息应该够了吧？帮我生成我的 IP 人设定位报告', '催促生成报告');
    stage.ok.reportDraft = !!(await waitState((s) => s.report && s.report.status === 'draft_ready', 420000, '（催促后）报告初稿就绪'));
  }

  // ---- 4. 选方案定稿 ----
  const t4 = await texts();
  const choice = t4.all.match(/方案\s*([ABC])/i) ? t4.all.match(/方案\s*([ABC])/i)[1] : 'A';
  await send('我选方案 ' + choice + '，就按这个来', '选定方案 ' + choice);
  stage.ok.finalize = !!(await waitState((s) => s.report && s.report.status === 'final', 420000, '报告定稿（final）'));

  // ---- 4b. UI 确认（若自动确认没生效，帮用户点确认按钮）----
  stage.ok.confirmed = !!(await waitState((s) => s.report && s.report.confirmed, 120000, '报告已确认（confirmed）'));
  if (!stage.ok.confirmed) {
    const confirmBtn = await page.$('button:has-text("确认")');
    if (confirmBtn) {
      await confirmBtn.click();
      log('点击确认按钮');
      stage.ok.confirmed = !!(await waitState((s) => s.report && s.report.confirmed, 120000, '（点击后）报告已确认'));
    }
  }

  // ---- 5. 选题生成（确认后主 Agent 自动启动 m5）----
  stage.ok.topics = !!(await waitState((s) => s.report && s.report.m5 && s.report.m5.status === 'ready', 600000, '选题清单生成（m5 ready）'));
  if (stage.ok.topics) await waitText(['选题'], 60000, '选题清单出现在对话');
  // 从对话里提取一个选题标题（「**标题**」格式；等文本完整渲染再取，最长等 30s）
  let topic = null;
  const t5a = Date.now();
  while (!topic && Date.now() - t5a < 30000) {
    const t = await texts();
    const m = t.all.match(/\*\*([^*]{4,60})\*\*/);
    if (m) topic = m[1].replace(/^[\s⭐★0-9.、\-：:]+/, '').trim();
    if (!topic) await page.waitForTimeout(2000);
  }
  log('提取选题:', topic);

  // ---- 6. 选定选题 → 三版文案 ----
  if (topic) {
    await send('就选《' + topic + '》这个选题，帮我写文案', '选定选题');
  } else {
    await send('就选你重点推荐的第一个选题，帮我写文案', '选定选题（兜底）');
  }
  stage.ok.scripts = !!(await waitState((s) => s.report && s.report.m6 && s.report.m6.status === 'ready', 600000, '三版文案生成（m6 ready）'));
  if (stage.ok.scripts) await waitText(['共情', '文案'], 60000, '文案文本出现在对话');

  // ---- 7. 修订 ----
  await send('金句再有力一点，中段节奏再紧凑一些', '提修订意见');
  stage.ok.revise = !!(await waitText(['修订', '已按', '改好'], 420000, '修订完成'));

  // ---- 汇总 + 历史顺序校验 ----
  const st = await serverState();
  const hist = st.history || [];
  const roles = hist.map((x) => x.role);
  const firstUser = (hist.find((x) => x.role === 'user') || {}).content || '';
  // 顺序健康标准：开场白(assistant)开头、最后一条是回复；用户消息严格按发送顺序、
  // 且没有任何两条用户消息相邻（相邻=中间丢了一条回复）。
  // 连续的 assistant 是正常的——后台任务完成通知（报告/选题/文案出炉）就是纯助手消息。
  const userIdx = [];
  hist.forEach((m, i) => { if (m.role === 'user') userIdx.push(i); });
  const noAdjacentUsers = userIdx.every((v, i) => i === 0 || v > userIdx[i - 1] + 1);
  const pairOk = roles.length >= 2 && roles[0] === 'assistant'
    && roles[roles.length - 1] === 'assistant' && noAdjacentUsers;
  stage.history = {
    len: hist.length,
    firstUserIsIntro: firstUser.includes('周远志'),
    alternating: pairOk,
    reportStatus: (st.report || {}).status,
    confirmed: (st.report || {}).confirmed,
    m5: ((st.report || {}).m5 || {}).status,
    m6: ((st.report || {}).m6 || {}).status,
  };
  stage.ok.historyOrder = stage.history.firstUserIsIntro && stage.history.alternating;
  const fin = await texts();
  stage.final = { userCount: fin.userCount, asstRealCount: fin.asstRealCount, tail: fin.tail.slice(0, 300) };
  stage.sid = await page.evaluate(() => localStorage.getItem('hq-v4-session-id'));
  await page.screenshot({ path: '/tmp/hq-golden-run.png', fullPage: false });
  log('== 金线结果 ==');
  console.log(JSON.stringify(stage, null, 2));
} catch (e) {
  log('脚本异常:', e.message);
  await page.screenshot({ path: '/tmp/hq-golden-run-err.png', fullPage: false });
  process.exitCode = 1;
}

const pass = Object.values(stage.ok).filter(Boolean).length;
console.log('GOLDEN_STAGES:', pass, '/', Object.keys(stage.ok).length);
console.log('PAGE_ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(Object.values(stage.ok).every(Boolean) && errors.length === 0 ? 0 : 1);
