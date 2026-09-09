// Synthetic offline fixture; exact lock's Puppeteer deliberately uses detached
// Chrome and remote-debugging-port, just like HyperFrames 0.8.33.
import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
const [hfRoot, executablePath, outputDir, mode] = process.argv.slice(2);
const dependencies = path.dirname(hfRoot);
const versions = {};
for (const name of ['hyperframes', 'puppeteer-core', '@puppeteer/browsers']) {
  versions[name] = JSON.parse(fs.readFileSync(path.join(dependencies, name, 'package.json'))).version;
}
const {default: puppeteer} = await import(pathToFileURL(path.join(dependencies,
  'puppeteer-core/lib/puppeteer/puppeteer-core.js')));
const browser = await puppeteer.launch({executablePath, headless: true,
  userDataDir: path.join(outputDir, 'chrome-profile'), args: ['--no-sandbox']});
const page = await browser.newPage();
await page.goto('data:text/html,<h1>Isolated editorial cleanup fixture</h1>');
if (await page.$eval('h1', node => node.textContent) !== 'Isolated editorial cleanup fixture') {
  throw new Error('Real browser did not become usable');
}
const hang = 'process.on("SIGTERM",()=>{}); setInterval(()=>{},1000)';
const ordinary = spawn(process.execPath, ['-e', hang], {stdio: 'ignore'});
const detached = spawn(process.execPath, ['-e', hang], {detached: true, stdio: 'ignore'});
const ffmpeg = spawn('ffmpeg', ['-v', 'error', '-re', '-f', 'lavfi', '-i',
  'testsrc2=size=160x90:rate=10', '-f', 'null', '-'], {stdio: 'ignore'});
// The intermediary exits immediately; its detached grandchild must be adopted.
const grandchildFile = path.join(outputDir, 'grandchild.json');
const doubleFork = spawn(process.execPath, ['-e', `
  const cp=require('node:child_process'), fs=require('node:fs');
  const child=cp.spawn(process.execPath,['-e',${JSON.stringify(hang)}],{detached:true,stdio:'ignore'});
  fs.writeFileSync(${JSON.stringify(grandchildFile)},JSON.stringify({pid:child.pid}));
  child.unref();
`], {stdio: 'ignore'});
await new Promise(resolve => doubleFork.on('exit', resolve));
const grandchild = JSON.parse(fs.readFileSync(grandchildFile)).pid;
if (mode !== 'timeout-graceful') {
  // Force the supervisor fallback, including genuine Chrome left by Node exit.
  for (const signal of ['exit', 'SIGTERM', 'SIGINT', 'SIGHUP']) process.removeAllListeners(signal);
  process.on('SIGTERM', () => {});
}
fs.writeFileSync(path.join(outputDir, 'launch.json'), JSON.stringify({versions,
  node: process.pid, supervisor: process.ppid, chrome: browser.process().pid,
  ordinary: ordinary.pid, detached: detached.pid, ffmpeg: ffmpeg.pid, grandchild,
  chromeArgs: browser.process().spawnargs, mode}, null, 2));
// Let Python capture the actual live ownership tree before an intentional exit.
while (!fs.existsSync(path.join(outputDir, 'observed'))) {
  await new Promise(resolve => setTimeout(resolve, 20));
}
if (mode === 'exit-success') process.exit(0);
if (mode === 'exit-failure') process.exit(3);
await new Promise(() => {});
