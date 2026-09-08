const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..', 'site');
const batch = {
  id: 'creator_batch_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', project_id: 'a1b2c3d4e5f6',
  topic: '企业做 Agent 前先梳理流程', goal: '建立信任', status: 'quoted', revision: 1, created_at: 1787720000,
  plans: [
    { platform: 'douyin', platform_label: '抖音', top_text: '企业别急着做 Agent', bottom_text: '关注获取完整流程', template_id: 'native-bold', template_reason: '直接结论更适合快速停留', material_pack: { name: '抖音平台素材库' } },
    { platform: 'xiaohongshu', platform_label: '小红书', top_text: '做 Agent 前先看这份清单', bottom_text: '先收藏，下次直接照着做', template_id: 'minimal-headline', template_reason: '经验型表达更适合收藏', material_pack: { name: '小红书平台素材库' } },
  ],
  quote: { items: [{ platform: 'douyin', label: '抖音', cost: 5 }, { platform: 'xiaohongshu', label: '小红书', cost: 5 }], total_cost: 10, points: 1000, expires_in: 300, expires_at: Math.floor(Date.now() / 1000) + 300 },
  quote_expires_at: Math.floor(Date.now() / 1000) + 300,
  jobs: [
    { id: 'creator_job_1', platform: 'douyin', version: 1, status: 'quoted', result: {} },
    { id: 'creator_job_2', platform: 'xiaohongshu', version: 1, status: 'quoted', result: {} },
  ],
};
const data = {
  user: { username: 'qa', name: 'QA', points: 1000 },
  projects: [{ id: 'a1b2c3d4e5f6', title: '我的个人画像', updated: '2026-08-26 12:00', active: true }],
  project: {
    id: 'a1b2c3d4e5f6', title: '我的个人画像', display_name: '我的个人画像', revision: 9,
    progress: { current_module: 5, module_step: 0, completed_modules: [1, 2, 3, 4], foundation_status: 'confirmed', foundation_ready: true, profile_complete: true },
    profile_quality: { status: 'complete', issue_count: 0, issues: [] },
    harness_actions: [], reports: {}, deliverables: {}, artifacts: [],
    foundation_pdf_status: 'ready', foundation_pdf_error_code: '', foundation_pdf_retry_url: '',
    foundation_pdf_url: '/api/creator-agent/projects/a1b2c3d4e5f6/background.pdf',
  },
  workspace: { project_id: 'a1b2c3d4e5f6', alias: '我的个人画像', platforms: ['douyin', 'xiaohongshu'], template_video_preferences: { global: [], platforms: {} }, flow: { mode: 'template_review', batch_id: batch.id } },
  messages: [
    { id: 0, role: 'assistant', content: '先介绍一下你现在的身份、工作或正在做的事情。', public: { kind: 'profile_question', module: 1, module_name: '定位诊断', template: '我是___，目前主要在做___。', actions: [] }, created_at: 0 },
    { id: 1, role: 'assistant', content: '画像已完成，可以开始创作。', public: {}, created_at: 1 },
    { id: 2, role: 'assistant', content: '请核对各平台明细和总价。', public: { kind: 'video_quote', batch, actions: [{ intent: 'confirm_payment', label: '确认扣点并开始生成', primary: true }] }, created_at: 2 },
  ],
  quick_actions: [{ intent: 'start_video', label: '开始制作视频' }, { intent: 'topic_plan', label: '生成选题计划' }, { intent: 'modify_profile', label: '修改我的画像' }],
  platforms: [{ id: 'douyin', label: '抖音' }, { id: 'xiaohongshu', label: '小红书' }, { id: 'wechat_channels', label: '视频号' }],
  material_packs: [], batches: [batch], latest_batch: batch,
};
let messageBodies = [];
let dropMessageResponses = 0;
let refreshRequests = 0;
let requestPaths = [];
let pdfRequests = 0;

function json(response, status, value) {
  const raw = Buffer.from(JSON.stringify(value));
  response.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': raw.length });
  response.end(raw);
}

function serve(request, response) {
  const pathname = decodeURIComponent(request.url.split('?')[0]);
  requestPaths.push(pathname);
  if (pathname === '/api/creator-agent/bootstrap') return json(response, 200, data);
  if (pathname === '/api/creator-agent/capability') return json(response, 200, { enabled: true, available: true });
  if (pathname === '/api/creator-agent/messages') {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
      messageBodies.push(body);
      if (body.intent === 'confirm_payment') {
        batch.status = 'running';
        batch.jobs.forEach((job) => { job.status = 'running'; });
      }
      if (dropMessageResponses > 0) {
        dropMessageResponses -= 1;
        return json(response, 503, { detail: 'temporary response loss', code: 'upstream_unavailable' });
      }
      json(response, 200, data);
    });
    return;
  }
  if (/^\/api\/creator-agent\/batches\/[^/]+\/refresh$/.test(pathname)) {
    refreshRequests += 1;
    return json(response, 200, { batch });
  }
  if (/^\/api\/creator-agent\/projects\/[^/]+\/background\.pdf$/.test(pathname)) {
    pdfRequests += 1;
    data.project.foundation_pdf_status = 'ready';
    data.project.foundation_pdf_error_code = '';
    data.project.foundation_pdf_url = pathname;
    data.project.foundation_pdf_retry_url = '';
    const pdf = Buffer.from('%PDF-1.4\n%%EOF\n');
    response.writeHead(200, { 'Content-Type': 'application/pdf', 'Content-Length': pdf.length });
    response.end(pdf); return;
  }
  if (pathname.startsWith('/api/creator-agent/')) return json(response, 200, data);
  if (pathname.startsWith('/api/gen/') || pathname.startsWith('/api/auth/')) return json(response, 200, { available: true, user: data.user });
  if (pathname.includes('/foundation-report/')) { response.statusCode = 204; response.end(); return; }
  const target = path.resolve(root, `.${pathname}`);
  if (!target.startsWith(root) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    response.statusCode = 404; response.end('not found'); return;
  }
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.woff2': 'font/woff2' };
  response.setHeader('Content-Type', types[path.extname(target)] || 'application/octet-stream');
  fs.createReadStream(target).pipe(response);
}

(async () => {
  const server = http.createServer(serve);
  const requestedPort = Number(process.env.CREATOR_AGENT_PREVIEW_PORT || 0);
  await new Promise((resolve) => server.listen(requestedPort, '127.0.0.1', resolve));
  if (process.argv.includes('--serve')) {
    console.log(`http://127.0.0.1:${server.address().port}/workbench/creator-agent.html`);
    return;
  }
  const options = { headless: true };
  if (process.env.CHROME_PATH) options.executablePath = process.env.CHROME_PATH;
  const browser = await chromium.launch(options);
  const report = {};
  try {
    for (const [name, viewport] of Object.entries({ desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } })) {
      batch.status = 'quoted';
      batch.jobs.forEach((job) => { job.status = 'quoted'; });
      messageBodies = [];
      dropMessageResponses = 0;
      refreshRequests = 0;
      requestPaths = [];
      const page = await browser.newPage({ viewport });
      await page.goto(`http://127.0.0.1:${server.address().port}/workbench/creator-agent.html`, { waitUntil: 'networkidle' });
      await page.waitForSelector('[data-intent="confirm_payment"]');
      if (name === 'mobile') {
        await page.click('[data-mobile-view="output"]');
        await page.waitForFunction(() => document.getElementById('caShell').classList.contains('show-output'));
        if (process.env.CREATOR_AGENT_QA_OUTPUT) {
          fs.mkdirSync(process.env.CREATOR_AGENT_QA_OUTPUT, { recursive: true });
          await page.screenshot({ path: path.join(process.env.CREATOR_AGENT_QA_OUTPUT, 'creator-agent-mobile-output.png'), fullPage: true });
        }
        await page.click('[data-mobile-view="chat"]');
      }
      const metrics = await page.evaluate(() => ({
        width: document.documentElement.scrollWidth,
        viewport: innerWidth,
        messages: document.querySelectorAll('.ca-message').length,
        plans: document.querySelectorAll('.ca-plan-card').length,
        total: document.querySelector('.ca-quote-total b').textContent,
        quoteCountdown: document.querySelector('.ca-quote-expiry')?.textContent || '',
        profileTemplate: document.querySelector('.ca-foundation')?.textContent || '',
        confirm: !!document.querySelector('[data-intent="confirm_payment"]'),
        tabs: document.querySelectorAll('.ca-tab').length,
        aiEntry: document.querySelector('.hq-side-ai-entry')?.getAttribute('href') || '',
        aiLabel: document.querySelector('.hq-side-ai-entry')?.textContent.trim() || '',
        pdfLinks: document.querySelectorAll('a[href*="background.pdf"]').length,
        pdfFrame: document.querySelector('.ca-pdf')?.getAttribute('src') || '',
      }));
      if (name === 'desktop') {
        dropMessageResponses = 1;
        await page.fill('#messageInput', '查看我的偏好');
        await page.click('#sendButton');
        await page.waitForFunction(() => !localStorage.getItem('hq-creator-agent-pending-v2'), null, { timeout: 12000 });
        metrics.messageReplayStable = messageBodies.length === 2
          && JSON.stringify(messageBodies[0]) === JSON.stringify(messageBodies[1])
          && messageBodies[0].project_id === data.project.id;

        messageBodies = [];
        dropMessageResponses = 1;
        refreshRequests = 0;
        page.on('dialog', (dialog) => dialog.accept());
        await page.click('[data-intent="confirm_payment"]');
        await page.waitForFunction(() => !localStorage.getItem('hq-creator-agent-pending-v2'), null, { timeout: 12000 });
        metrics.confirmRecovered = messageBodies.length === 1
          && refreshRequests > 0
          && /^creator-confirm-/.test(messageBodies[0].payload.confirmation_id || '')
          && messageBodies[0].payload.expected_revision === batch.revision;
        metrics.confirmRequests = messageBodies.length;
        metrics.refreshRequests = refreshRequests;
        metrics.confirmRevision = messageBodies[0]?.payload?.expected_revision;
        metrics.confirmQuoteExpiresAt = messageBodies[0]?.payload?.expected_quote_expires_at;
        metrics.confirmIntent = messageBodies[0]?.intent;
        metrics.confirmBatchId = messageBodies[0]?.payload?.batch_id;
        metrics.pathsAfterConfirm = requestPaths.slice(-8);
      } else {
        metrics.messageReplayStable = true;
        metrics.confirmRecovered = true;
        await page.evaluate(() => {
          document.querySelector('.ca-quote-expiry').dataset.quoteExpires = '1';
        });
        await page.waitForFunction(() => document.querySelector('[data-original-intent="confirm_payment"]')?.dataset.intent === 'confirm_plan');
        metrics.expiredQuoteRequotes = await page.evaluate(() => document.querySelector('[data-original-intent="confirm_payment"]')?.textContent === '重新报价');
      }
      report[name] = metrics;
      if (process.env.CREATOR_AGENT_QA_OUTPUT) {
        fs.mkdirSync(process.env.CREATOR_AGENT_QA_OUTPUT, { recursive: true });
        await page.screenshot({ path: path.join(process.env.CREATOR_AGENT_QA_OUTPUT, `creator-agent-${name}.png`), fullPage: true });
      }
      await page.close();
    }
    data.project.foundation_pdf_status = 'failed';
    data.project.foundation_pdf_error_code = 'profile_pdf_failed';
    data.project.foundation_pdf_url = '';
    data.project.foundation_pdf_retry_url = '/api/creator-agent/projects/a1b2c3d4e5f6/background.pdf';
    data.project.deliverables = {};
    pdfRequests = 0;
    const failurePage = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    await failurePage.goto(`http://127.0.0.1:${server.address().port}/workbench/creator-agent.html`, { waitUntil: 'networkidle' });
    await failurePage.click('[data-tab="content"]');
    await failurePage.waitForSelector('[data-pdf-retry]');
    const beforeRetry = await failurePage.evaluate(() => ({
      badge: document.querySelector('[data-pdf-retry]')?.closest('.ca-artifact')?.querySelector('.ca-badge')?.textContent || '',
      retryLabel: document.querySelector('[data-pdf-retry]')?.textContent || '',
      frames: document.querySelectorAll('.ca-pdf').length,
      pdfLinks: document.querySelectorAll('a[href*="background.pdf"]').length,
      titleCount: Array.from(document.querySelectorAll('.ca-artifact header b')).filter((item) => item.textContent === 'IP人设定位背景档案').length,
    }));
    if (process.env.CREATOR_AGENT_QA_OUTPUT) {
      fs.mkdirSync(process.env.CREATOR_AGENT_QA_OUTPUT, { recursive: true });
      await failurePage.screenshot({ path: path.join(process.env.CREATOR_AGENT_QA_OUTPUT, 'creator-agent-pdf-failed.png'), fullPage: true });
    }
    await failurePage.click('[data-pdf-retry]');
    await failurePage.waitForSelector('.ca-pdf');
    const afterRetry = await failurePage.evaluate(() => ({
      badge: Array.from(document.querySelectorAll('.ca-artifact header b')).find((item) => item.textContent === 'IP人设定位背景档案')?.parentElement?.querySelector('.ca-badge')?.textContent || '',
      frames: document.querySelectorAll('.ca-pdf').length,
      pdfLinks: document.querySelectorAll('a[href*="background.pdf"]').length,
    }));
    report.pdfFailure = { beforeRetry, afterRetry, pdfRequests };
    if (process.env.CREATOR_AGENT_QA_OUTPUT) {
      fs.mkdirSync(process.env.CREATOR_AGENT_QA_OUTPUT, { recursive: true });
      await failurePage.screenshot({ path: path.join(process.env.CREATOR_AGENT_QA_OUTPUT, 'creator-agent-pdf-retry.png'), fullPage: true });
    }
    await failurePage.close();
    data.project.profile_quality = {
      status: 'needs_review', issue_count: 6,
      issues: [{ module: 1, key: 'career_identity', reason: '信息不足' }],
    };
    data.quick_actions = [
      { intent: 'repair_profile', label: '完善画像' },
      { intent: 'modify_profile', label: '修改我的画像' },
    ];
    data.project.foundation_pdf_status = 'blocked';
    data.project.foundation_pdf_url = '';
    data.project.foundation_pdf_retry_url = '';
    const qualityPage = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    await qualityPage.goto(`http://127.0.0.1:${server.address().port}/workbench/creator-agent.html`, { waitUntil: 'networkidle' });
    report.profileQuality = await qualityPage.evaluate(() => ({
      title: document.getElementById('progressTitle')?.textContent || '',
      detail: document.getElementById('progressDetail')?.textContent || '',
      repair: !!document.querySelector('[data-quick-intent="repair_profile"]'),
      start: !!document.querySelector('[data-quick-intent="start_video"]'),
      pdfBlocked: document.getElementById('contentPanel')?.textContent.includes('画像待完善') || false,
      pdfFrame: document.querySelectorAll('.ca-pdf').length,
    }));
    if (process.env.CREATOR_AGENT_QA_OUTPUT) {
      fs.mkdirSync(process.env.CREATOR_AGENT_QA_OUTPUT, { recursive: true });
      await qualityPage.screenshot({ path: path.join(process.env.CREATOR_AGENT_QA_OUTPUT, 'creator-agent-profile-quality.png'), fullPage: true });
    }
    await qualityPage.close();
  } finally {
    await browser.close(); server.close();
  }
  console.log(JSON.stringify(report));
  const viewports = [report.desktop, report.mobile];
  const pdfFailure = report.pdfFailure || {};
  const profileQuality = report.profileQuality || {};
  if (viewports.some((item) => item.width > item.viewport || item.messages < 3 || item.plans < 2 || !item.confirm || item.total !== '10 点' || !item.quoteCountdown.startsWith('报价剩余') || !item.profileTemplate.includes('回答参考') || item.tabs !== 3 || item.aiEntry !== 'creator-agent.html' || item.aiLabel !== 'AI 创作助手' || item.pdfLinks !== 2 || !item.pdfFrame.includes('/background.pdf') || !item.messageReplayStable || !item.confirmRecovered || (item.confirmQuoteExpiresAt !== undefined && item.confirmQuoteExpiresAt !== batch.quote_expires_at) || (item.expiredQuoteRequotes === false)) || pdfFailure.beforeRetry?.badge !== 'PDF 生成失败' || pdfFailure.beforeRetry?.retryLabel !== '重新生成' || pdfFailure.beforeRetry?.frames !== 0 || pdfFailure.beforeRetry?.pdfLinks !== 0 || pdfFailure.beforeRetry?.titleCount !== 1 || pdfFailure.afterRetry?.badge !== 'PDF 已生成' || pdfFailure.afterRetry?.frames !== 1 || pdfFailure.afterRetry?.pdfLinks !== 2 || pdfFailure.pdfRequests < 1 || profileQuality.title !== '画像需要完善' || !profileQuality.detail.includes('6 项') || !profileQuality.repair || profileQuality.start || !profileQuality.pdfBlocked || profileQuality.pdfFrame !== 0) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exitCode = 1; });
