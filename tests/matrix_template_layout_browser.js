const fs = require('fs');
const http = require('http');
const path = require('path');
const {chromium} = require('playwright');

const siteRoot = path.resolve(__dirname, '..', 'site');
const templateIds = [
  'native-bold', 'video-diary', 'minimal-headline', 'airy-blush',
  'yellow-blue-pop', 'business-black', 'black-gold-premium', 'data-compare',
  'chinese-title', 'torn-magazine', 'vlog-journal', 'bilingual-split',
  'portrait-quote',
];

function serve(request, response) {
  if (request.url.startsWith('/api/gen/matrix-template/templates')) {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({
      templates: templateIds.map(id => ({id, name: id, tags: ['QA']})),
      fonts: [
        {value: '', label: '自动搭配', source: 'automatic'},
        {value: 'Noto Sans SC', label: '思源黑体', source: 'bundled'},
        {value: 'YS HelloFont BangBangTi', label: '优设字由棒棒体', source: 'private'},
      ],
      default_template: 'native-bold', cost: 5,
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
      await page.fill('#topText', '标题'.repeat(30));
      await page.fill('#bottomText', '行动'.repeat(40));
      const overflow = [];
      for (let index = 0; index < templateIds.length; index += 1) {
        await page.locator('.mt-template').nth(index).click();
        const boxes = await page.evaluate(() => ['liveTop', 'liveBottom'].map(id => {
          const element = document.getElementById(id);
          return {clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, clientHeight: element.clientHeight, scrollHeight: element.scrollHeight};
        }));
        if (boxes.some(hasOverflow)) overflow.push(templateIds[index]);
      }
      const scroll = await page.evaluate(() => {
        const scroller = document.querySelector('.hq-main-scroll');
        const preview = document.getElementById('livePreview');
        scroller.scrollTop = scroller.scrollHeight;
        const rect = preview.getBoundingClientRect();
        return {scrollTop: scroller.scrollTop, scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight, top: rect.top, bottom: rect.bottom, viewport: innerHeight};
      });
      if (process.env.MATRIX_QA_OUTPUT) {
        fs.mkdirSync(process.env.MATRIX_QA_OUTPUT, {recursive: true});
        await page.screenshot({path: path.join(process.env.MATRIX_QA_OUTPUT, `matrix-${name}.png`), fullPage: true});
      }
      report[name] = {overflow, scroll};
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  if (report.desktop.overflow.length || report.mobile.overflow.length) throw new Error(`preview overflow: ${JSON.stringify(report)}`);
  const mobile = report.mobile.scroll;
  if (mobile.scrollHeight <= mobile.clientHeight || mobile.scrollTop <= 0 || mobile.top >= mobile.viewport || mobile.bottom <= 0) throw new Error(`mobile preview is unreachable: ${JSON.stringify(mobile)}`);
  process.stdout.write(JSON.stringify(report));
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
