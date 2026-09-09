// Synthetic, read-only page assertions; imported by check_editorial_layout.py.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

const [runtime, executablePath, fixturesPath] = process.argv.slice(2);
const requireRuntime = createRequire(path.join(runtime, 'package.json'));
const puppeteer = requireRuntime('puppeteer-core');
const output = path.dirname(fixturesPath);
const fixtures = JSON.parse(await fs.readFile(fixturesPath, 'utf8'));
const browser = await puppeteer.launch({executablePath, headless: true,
  userDataDir: path.join(output, 'browser-profile'),
  args: ['--no-sandbox', '--disable-dev-shm-usage']});
const records = [];
try {
  for (const fixture of fixtures) {
    const page = await browser.newPage();
    try {
      await page.setViewport({width: 720, height: 1280, deviceScaleFactor: 1});
      const errors = [];
      page.on('pageerror', error => {errors.push(String(error)); console.error(fixture.name, error);});
      await page.goto(pathToFileURL(path.join(fixture.workspace, 'index.html')).href);
      await page.waitForFunction(() => Boolean(window.__timelines?.main), {timeout: 30000});
      const measure = async at => page.evaluate(at => {
        window.__timelines.main.seek(at);
        const bounds = r => ({left:r.left, right:r.right, top:r.top, bottom:r.bottom,
          width:r.width, height:r.height});
        return [...document.querySelectorAll('.caption')].map(caption => {
          const en = caption.querySelector('.english');
          const range = document.createRange(); range.selectNodeContents(en);
          const chinese = document.createRange();
          chinese.selectNodeContents(caption.querySelector('.caption-text'));
          return {text: en.textContent, size: parseFloat(getComputedStyle(en).fontSize),
            caption: bounds(caption.getBoundingClientRect()), box: bounds(en.getBoundingClientRect()),
            textBounds: bounds(range.getBoundingClientRect()), chinese: bounds(chinese.getBoundingClientRect())};
        });
      }, at);
      const measured = await measure(3);
      await measure(1);
      assert.deepEqual(await measure(3), measured, fixture.name + ': layout changed after reverse seek');
      assert.deepEqual(errors, [], fixture.name + ': page errors');
      assert.equal(measured.length, 2, fixture.name + ': missing/extra captions');
      for (const [index, item] of measured.entries()) {
        assert.equal(item.text, fixture.english[index], 'Text changed/truncated');
        assert.ok(item.size >= 20 && item.size <= 26, 'Unreadable/unexpected font size');
        assert.ok(item.box.height <= 64 && item.box.height >= 32, 'More than two lines');
        assert.ok(item.textBounds.width <= 592.5, 'Text exceeds usable inner width');
        assert.ok(item.textBounds.left >= item.box.left - 2 &&
          item.textBounds.right <= item.box.right + 2, 'Text escapes caption');
        assert.ok(item.textBounds.top >= item.box.top - 3 &&
          item.textBounds.bottom <= item.box.bottom + 3, 'Glyphs vertically clipped');
        assert.ok(item.textBounds.left >= 0 && item.textBounds.right <= 720 &&
          item.textBounds.top >= 0 && item.textBounds.bottom <= 1280, 'Text escapes canvas');
        assert.ok(item.box.bottom <= item.caption.bottom, 'English escapes owning caption');
      }
      assert.ok(measured[0].textBounds.bottom < measured[1].chinese.top,
        'Upper English collides with the next Chinese caption');
      if (fixture.name === 'short-original') {
        assert.deepEqual(measured.map(item => item.size), [26, 26]);
        assert.deepEqual(measured.map(item => item.box.height), [32, 32]);
        assert.deepEqual(measured.map(item => item.caption.top), [798, 934]);
      }
      await page.screenshot({path: path.join(output, fixture.name + '.png')});
      records.push({name: fixture.name, measured});
    } finally { await page.close(); }
  }
} finally {
  await browser.close();
  await fs.writeFile(path.join(output, 'measurements.json'), JSON.stringify(records, null, 2));
}
console.log(JSON.stringify({cases: records.length, geometry: 'passed', reverse_seek: 'passed'}));
