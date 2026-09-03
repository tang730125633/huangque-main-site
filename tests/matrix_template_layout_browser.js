const fs = require('fs');
const http = require('http');
const path = require('path');
const {chromium} = require('playwright');

const siteRoot = path.resolve(__dirname, '..', 'site');
const referenceIds = [
  'ref-01-chengdu-green-brush', 'ref-02-shenzhen-ai-orange',
  'ref-03-zhengzhou-blue-banner', 'ref-04-foshan-yellow-strip',
  'ref-05-changsha-white-red', 'ref-06-guangzhou-yellow-button',
  'ref-07-shenzhen-red-growth', 'ref-08-puyang-yellow-white',
  'ref-09-urumqi-soft-brush', 'ref-10-shenzhen-sisters',
  'ref-11-nansha-clean', 'ref-12-guangzhou-brush',
  'ref-13-shenzhen-green-location', 'ref-14-karamay-green',
  'ref-15-tianjin-monochrome', 'ref-16-shenzhen-opc',
  'ref-17-shenzhen-yellow-red',
];
const templates = [
  {id: 'full-overlay-bold', name: '沉浸强标题', description: '全屏素材与强标题', engine: 'ffmpeg', font_mode: 'selectable', font_selectable: true},
  {id: 'poster-split', name: '三段式活动海报', description: '上标题、中素材、下行动号召', engine: 'ffmpeg', font_mode: 'selectable', font_selectable: true},
  ...referenceIds.map((id, index) => ({
    id,
    name: `参考排版 ${String(index + 1).padStart(2, '0')}`,
    description: `固定字体与布局 ${String(index + 1).padStart(2, '0')}`,
    tags: ['HyperFrames', '内置字体'],
    engine: 'hyperframes',
    font_mode: 'template_locked',
    font_selectable: false,
    variant: `v${String(index + 1).padStart(2, '0')}`,
  })),
];
const visibleTemplateIds = referenceIds;

function serve(request, response) {
  if (request.url.startsWith('/api/gen/matrix-template/templates')) {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({
      templates,
      fonts: [
        {value: '', label: '自动搭配', source: 'automatic'},
        {value: 'Noto Sans SC', label: '思源黑体', source: 'bundled'},
        {value: 'YS HelloFont BangBangTi', label: '优设字由棒棒体', source: 'private'},
      ],
      default_template: 'native-bold', max_batch_size: 5,
      engine_concurrency: {ffmpeg: 5, hyperframes: 2}, cost: 5,
    }));
    return;
  }
  if (request.url.startsWith('/api/auth/me')) {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({user: {username: 'qa', points: 100}}));
    return;
  }
  const target = path.resolve(siteRoot, `.${decodeURIComponent(request.url.split('?')[0])}`);
  if (!target.startsWith(siteRoot) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    response.statusCode = 404;
    response.end('not found');
    return;
  }
  const types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.woff2': 'font/woff2'};
  response.setHeader('Content-Type', types[path.extname(target)] || 'application/octet-stream');
  fs.createReadStream(target).pipe(response);
}

function hasOverflow(box) {
  return box.scrollHeight > box.clientHeight || box.scrollWidth > box.clientWidth;
}

(async () => {
  const server = http.createServer(serve);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const options = {headless: true};
  if (process.env.CHROME_PATH) options.executablePath = process.env.CHROME_PATH;
  const browser = await chromium.launch(options);
  const url = `http://127.0.0.1:${server.address().port}/workbench/matrix-template.html`;
  const report = {};
  try {
    for (const [name, viewport] of Object.entries({desktop: {width: 1440, height: 900}, mobile: {width: 390, height: 844}})) {
      const page = await browser.newPage({viewport});
      await page.goto(url, {waitUntil: 'networkidle'});
      const readBatchControl = () => page.evaluate(() => {
        const select = document.getElementById('batchCount');
        return {
          disabled: select.disabled,
          values: [...select.options].map(option => option.value),
          labels: [...select.options].map(option => option.textContent),
          hint: document.getElementById('batchHint').textContent,
        };
      });
      const hyperframesBatchControl = await readBatchControl();
      const initialAction = await page.locator('#generateBtn').evaluate(node => ({
        disabled: node.disabled,
        cursor: getComputedStyle(node).cursor,
        title: node.title,
      }));
      await page.locator('#generateBtn').click();
      const emptyReminder = await page.evaluate(() => ({
        status: document.getElementById('status').textContent,
        toast: document.getElementById('toast').textContent,
      }));
      const cardReport = await page.locator('.mt-template').evaluateAll(nodes => nodes.map(node => {
        const visual = node.querySelector('.mt-template-visual');
        const top = node.querySelector('.mt-template-top');
        const media = node.querySelector('.mt-template-media');
        const bottom = node.querySelector('.mt-template-bottom');
        const visualStyle = getComputedStyle(visual);
        const topStyle = getComputedStyle(top);
        const mediaStyle = getComputedStyle(media);
        const bottomStyle = getComputedStyle(bottom);
        return {
          label: node.querySelector('strong').textContent,
          variant: visual.dataset.variant || '',
          signature: [
            visualStyle.backgroundColor, visualStyle.color, visualStyle.gridTemplateRows,
            topStyle.color, topStyle.fontFamily, topStyle.textAlign,
            mediaStyle.backgroundImage, mediaStyle.borderColor, mediaStyle.borderRadius,
            bottomStyle.backgroundColor, bottomStyle.color, bottomStyle.borderRadius,
          ].join('|'),
        };
      }));
      if (process.env.MATRIX_QA_OUTPUT) {
        fs.mkdirSync(process.env.MATRIX_QA_OUTPUT, {recursive: true});
        const grid = page.locator('#templateGrid');
        await grid.evaluate(element => { element.scrollTop = 0; });
        await grid.screenshot({path: path.join(process.env.MATRIX_QA_OUTPUT, `matrix-${name}-catalog-start.png`)});
        await grid.evaluate(element => { element.scrollTop = element.scrollHeight; });
        await grid.screenshot({path: path.join(process.env.MATRIX_QA_OUTPUT, `matrix-${name}-catalog-end.png`)});
      }
      await page.fill('#topText', '标题'.repeat(30));
      await page.fill('#bottomText', '行动'.repeat(40));
      const readyAction = await page.locator('#generateBtn').evaluate(node => ({
        disabled: node.disabled,
        cursor: getComputedStyle(node).cursor,
        text: node.textContent,
      }));
      const overflow = [];
      for (let index = 0; index < visibleTemplateIds.length; index += 1) {
        await page.locator('.mt-template').nth(index).click();
        const boxes = await page.evaluate(() => ['liveTop', 'liveBottom'].map(id => {
          const element = document.getElementById(id);
          return {clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, clientHeight: element.clientHeight, scrollHeight: element.scrollHeight};
        }));
        if (boxes.some(hasOverflow)) overflow.push(visibleTemplateIds[index]);
      }
      const scroll = await page.evaluate(() => {
        const scroller = document.querySelector('.hq-main-scroll');
        const preview = document.getElementById('livePreview');
        scroller.scrollTop = scroller.scrollHeight;
        const rect = preview.getBoundingClientRect();
        return {scrollTop: scroller.scrollTop, scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight, top: rect.top, bottom: rect.bottom, viewport: innerHeight};
      });
      if (process.env.MATRIX_QA_OUTPUT) {
        await page.screenshot({path: path.join(process.env.MATRIX_QA_OUTPUT, `matrix-${name}-preview.png`), fullPage: true});
      }
      const references = cardReport.filter(item => item.variant);
      report[name] = {
        overflow,
        scroll,
        batchControl: {hyperframes: hyperframesBatchControl},
        action: {initial: initialAction, emptyReminder, ready: readyAction},
        cardCount: cardReport.length,
        cardLabels: cardReport.map(item => item.label),
        referenceCount: references.length,
        distinctReferencePreviews: new Set(references.map(item => item.signature)).size,
      };
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  if (report.desktop.overflow.length || report.mobile.overflow.length) throw new Error(`preview overflow: ${JSON.stringify(report)}`);
  for (const viewport of Object.values(report)) {
    if (viewport.cardCount !== 17 || viewport.referenceCount !== 17 || viewport.distinctReferencePreviews !== 17) throw new Error(`template cards are not distinct: ${JSON.stringify(report)}`);
    const expectedCardLabels = referenceIds.map((id, index) => `${index + 1}. 参考排版 ${String(index + 1).padStart(2, '0')}`);
    if (viewport.cardLabels.join('|') !== expectedCardLabels.join('|')) throw new Error(`template card numbering is inaccurate: ${JSON.stringify(viewport.cardLabels)}`);
    const hyperframes = viewport.batchControl.hyperframes;
    const expectedLabels = '1条,2条,3条,4条,5条';
    if (hyperframes.disabled || hyperframes.values.join(',') !== '1,2,3,4,5' || hyperframes.hint !== '最多5条' || hyperframes.labels.join(',') !== expectedLabels) throw new Error(`HyperFrames batch control is unavailable: ${JSON.stringify(hyperframes)}`);
    if (viewport.action.initial.disabled || viewport.action.initial.cursor !== 'pointer' || !viewport.action.initial.title.includes('顶部文案')) throw new Error(`empty-copy action state is inaccurate: ${JSON.stringify(viewport.action.initial)}`);
    if (!viewport.action.emptyReminder.status.includes('顶部文案和底部行动文案') || !viewport.action.emptyReminder.toast.includes('顶部文案和底部行动文案')) throw new Error(`empty-copy reminder is missing: ${JSON.stringify(viewport.action.emptyReminder)}`);
    if (viewport.action.ready.disabled || viewport.action.ready.cursor !== 'pointer' || viewport.action.ready.text !== '生成视频 · 5 点') throw new Error(`ready action state is inaccurate: ${JSON.stringify(viewport.action.ready)}`);
  }
  const mobile = report.mobile.scroll;
  if (mobile.scrollHeight <= mobile.clientHeight || mobile.scrollTop <= 0 || mobile.top >= mobile.viewport || mobile.bottom <= 0) throw new Error(`mobile preview is unreachable: ${JSON.stringify(mobile)}`);
  process.stdout.write(JSON.stringify(report));
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
