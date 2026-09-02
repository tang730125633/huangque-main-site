const fs = require('fs');
const path = require('path');
const vm = require('vm');

const page = fs.readFileSync(
  path.join(__dirname, '..', 'site', 'workbench', 'assets.html'),
  'utf8',
);
const start = page.indexOf('function freshUrl(url)');
const end = page.indexOf('// 缩略图：', start);
if (start < 0 || end < 0) throw new Error('video frame helpers are missing');

class Video {
  constructor() {
    this.dataset = {};
    this.src = '';
    this.preload = 'metadata';
    this.currentTime = 0;
    this.duration = 10;
    this.loads = 0;
    this.pauses = 0;
    this.listeners = {};
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  removeAttribute(name) {
    if (name === 'data-src') delete this.dataset.src;
    if (name === 'src') this.src = '';
  }
  load() { this.loads += 1; }
  pause() { this.pauses += 1; }
  fire(name) { if (this.listeners[name]) this.listeners[name](); }
}

class Observer {
  constructor(callback, options) {
    this.callback = callback;
    this.options = options;
    this.targets = new Set();
    this.disconnected = false;
    Observer.instances.push(this);
  }
  observe(target) { this.targets.add(target); }
  unobserve(target) { this.targets.delete(target); }
  disconnect() { this.disconnected = true; this.targets.clear(); }
  reveal(target) {
    this.callback([{ target, isIntersecting: true }], this);
  }
}
Observer.instances = [];

const context = {
  Date: { now: () => 1234 },
  IntersectionObserver: Observer,
  URL,
  window: { IntersectionObserver: Observer },
};
vm.createContext(context);
vm.runInContext(
  "var videoFrameObserver=null,activeVideoFrames=[];" +
  page.slice(start, end) +
  ";this.helpers={queueVideoFrame,resetVideoFrameObserver};",
  context,
);

function state(video) {
  return {
    src: video.src,
    preload: video.preload,
    dataSrc: video.dataset.src || '',
    currentTime: video.currentTime,
    loads: video.loads,
    pauses: video.pauses,
  };
}

const local = new Video();
context.helpers.queueVideoFrame(local, '/api/gen/file/video/local.mp4');
const localObserver = Observer.instances.at(-1);
localObserver.reveal(local);
local.fire('loadedmetadata');
const localBeforeCleanup = state(local);
context.helpers.resetVideoFrameObserver();
const localAfterCleanup = state(local);

const remote = new Video();
context.helpers.queueVideoFrame(remote, 'https://cdn.example/video.mp4');
const remoteObserver = Observer.instances.at(-1);
remoteObserver.reveal(remote);
remote.fire('loadedmetadata');
const remoteBeforeCleanup = state(remote);
context.helpers.resetVideoFrameObserver();

process.stdout.write(JSON.stringify({
  local: localBeforeCleanup,
  remote: remoteBeforeCleanup,
  localCleanup: localAfterCleanup,
  remoteCleanup: state(remote),
  observerDisconnected:
    localObserver.disconnected && remoteObserver.disconnected,
}));
