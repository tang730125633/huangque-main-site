const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const rootDir = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(rootDir, 'site/assets/home/orbit-gallery.js'), 'utf8');

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `${name} must exist in orbit-gallery.js`);
  const brace = source.indexOf('{', start);
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = brace; index < source.length; index += 1) {
    const char = source[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === "'" || char === '"' || char === '`') quote = char;
    else if (char === '{') depth += 1;
    else if (char === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated function: ${name}`);
}

function loadFunction(name, scope) {
  const keys = Object.keys(scope);
  return new Function(...keys, `${extractFunction(name)}\nreturn ${name};`)(...keys.map(key => scope[key]));
}

function test(name, fn) {
  try {
    fn();
    process.stdout.write(`PASS ${name}\n`);
  } catch (error) {
    process.stderr.write(`FAIL ${name}\n${error.stack}\n`);
    process.exitCode = 1;
  }
}

test('pointer capture starts on pointerdown and external release clears drag state', () => {
  let now = 1000;
  const classes = new Set();
  const captures = new Set();
  const state = {
    tracking: false,
    dragging: false,
    moved: false,
    position: 0,
    velocity: 0,
    suppressClickUntil: 0,
  };
  const root = {
    classList: {
      add: value => classes.add(value),
      remove: value => classes.delete(value),
    },
    setPointerCapture: pointerId => captures.add(pointerId),
    hasPointerCapture: pointerId => captures.has(pointerId),
    releasePointerCapture: pointerId => captures.delete(pointerId),
  };
  const performance = { now: () => now };
  const beginDrag = loadFunction('beginDrag', { state, root, performance, isInteractive: () => true });
  const moveDrag = loadFunction('moveDrag', {
    state,
    root,
    performance,
    DRAG_SENSITIVITY: 0.0048,
    isInteractive: () => true,
    queueRender: () => {},
  });
  const finishDrag = loadFunction('finishDrag', {
    state,
    root,
    performance,
    ensureAnimationFrame: () => {},
  });

  beginDrag({ button: 0, pointerId: 7, clientX: 100 });
  assert.equal(captures.has(7), true, 'pointer must be captured immediately');
  now += 20;
  moveDrag({ pointerId: 7, clientX: 120, buttons: 1, preventDefault() {} });
  assert.equal(classes.has('is-dragging'), true);

  captures.delete(7);
  finishDrag({ pointerId: 7 });
  assert.equal(state.tracking, false);
  assert.equal(state.dragging, false);
  assert.equal(classes.has('is-dragging'), false);
});

test('pointer movement without a pressed button clears stale drag state', () => {
  const classes = new Set(['is-dragging']);
  const state = { tracking: true, dragging: true, moved: true, suppressClickUntil: 0 };
  const root = {
    classList: { add: value => classes.add(value), remove: value => classes.delete(value) },
    hasPointerCapture: () => false,
  };
  const performance = { now: () => 2000 };
  const finishDrag = loadFunction('finishDrag', {
    state,
    root,
    performance,
    ensureAnimationFrame: () => {},
  });
  const moveDrag = loadFunction('moveDrag', {
    state,
    root,
    performance,
    DRAG_SENSITIVITY: 0.0048,
    isInteractive: () => true,
    queueRender: () => {},
    finishDrag,
  });
  moveDrag({ pointerId: 9, buttons: 0 });
  assert.equal(state.tracking, false);
  assert.equal(state.dragging, false);
  assert.equal(classes.has('is-dragging'), false);
});

test('arrow navigation moves focus to the new active card', () => {
  let focusedIndex = -1;
  const state = {
    velocity: 1,
    position: 0,
    activeIndex: 0,
    items: [{ id: 0 }, { id: 1 }],
    cards: [0, 1].map(index => ({
      card: { focus: () => { focusedIndex = index; } },
    })),
  };
  const render = force => {
    assert.equal(force, true);
    state.activeIndex = 1;
  };
  const focusActiveCard = loadFunction('focusActiveCard', { state });
  const handleGalleryKeydown = loadFunction('handleGalleryKeydown', {
    state,
    isInteractive: () => true,
    render,
    focusActiveCard,
    openPreview: () => assert.fail('preview should not open for ArrowRight'),
  });
  let prevented = false;
  handleGalleryKeydown({ key: 'ArrowRight', preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(state.position, 1);
  assert.equal(focusedIndex, 1);
});

function fakeMedia(dataset) {
  const attributes = new Map();
  return {
    dataset,
    getAttribute: name => attributes.get(name) || null,
    removeAttribute: name => attributes.delete(name),
    set poster(value) { attributes.set('poster', value); },
    set src(value) { attributes.set('src', value); },
  };
}

test('Save-Data mounts only nearby visible media and unloads it offscreen', () => {
  const near = fakeMedia({ src: '/assets/near.webp' });
  const far = fakeMedia({ poster: '/assets/far.webp' });
  const state = {
    inViewport: false,
    cards: [
      { visible: true, item: { type: 'image' }, card: { querySelector: () => near } },
      { visible: true, item: { type: 'video' }, card: { querySelector: () => far } },
    ],
  };
  const syncCardMediaSources = loadFunction('syncCardMediaSources', {
    state,
    shouldLoadCardMedia: entry => (
      state.inViewport && entry.item.type === 'image'
    ),
    conserveResources: true,
    mountVideoPoster: (entry, media) => { media.poster = media.dataset.poster; },
    unmountVideoPoster: (entry, media) => media.removeAttribute('poster'),
  });

  syncCardMediaSources();
  assert.equal(near.getAttribute('src'), null, 'offscreen gallery must not mount media');
  state.inViewport = true;
  syncCardMediaSources();
  assert.equal(near.getAttribute('src'), '/assets/near.webp');
  assert.equal(far.getAttribute('poster'), null, 'distant card must remain unloaded in Save-Data mode');
  state.inViewport = false;
  syncCardMediaSources();
  assert.equal(near.getAttribute('src'), null, 'Save-Data media must unload after leaving the viewport');
});

test('gallery initialization waits until the section approaches the viewport', () => {
  let disconnected = false;
  let initialized = 0;
  const handleInitIntersection = loadFunction('handleInitIntersection', {
    initObserver: { disconnect: () => { disconnected = true; } },
    init: () => { initialized += 1; },
  });
  handleInitIntersection([{ isIntersecting: false }]);
  assert.equal(initialized, 0);
  handleInitIntersection([{ isIntersecting: true }]);
  assert.equal(disconnected, true);
  assert.equal(initialized, 1);
});

test('idle gallery does not advance without user-provided velocity', () => {
  const state = { position: 4, velocity: 0 };
  const advanceMotion = loadFunction('advanceMotion', { state });
  assert.equal(advanceMotion(5000), false);
  assert.equal(state.position, 4);
  state.velocity = 0.001;
  assert.equal(advanceMotion(16), true);
  assert.ok(state.position > 4);
});

test('preview autoplay is disabled for reduced-motion and resource conservation', () => {
  const reduced = loadFunction('previewAutoplayAllowed', {
    conserveResources: false,
    reducedMotion: { matches: true },
  });
  const conserved = loadFunction('previewAutoplayAllowed', {
    conserveResources: true,
    reducedMotion: { matches: false },
  });
  const standard = loadFunction('previewAutoplayAllowed', {
    conserveResources: false,
    reducedMotion: { matches: false },
  });
  assert.equal(reduced(), false);
  assert.equal(conserved(), false);
  assert.equal(standard(), true);
});

test('closing the preview restores focus to its triggering card', () => {
  let focused = false;
  let replaced = false;
  const trigger = {
    isConnected: true,
    focus: options => {
      assert.deepEqual(options, { preventScroll: true });
      focused = true;
    },
  };
  const state = { previewTrigger: trigger, cards: [], activeIndex: 0 };
  const previewStage = {
    querySelector: () => null,
    replaceChildren: () => { replaced = true; },
  };
  const previewTitle = { textContent: 'before' };
  const cleanupPreview = loadFunction('cleanupPreview', {
    state,
    previewStage,
    previewTitle,
    isInteractive: () => true,
    syncVideoPlayback: () => {},
  });
  cleanupPreview();
  assert.equal(replaced, true);
  assert.equal(previewTitle.textContent, '');
  assert.equal(focused, true);
  assert.equal(state.previewTrigger, null);
});

test('render frames are requested on demand and stop when idle', () => {
  const queued = { renderPending: false };
  let requested = 0;
  const queueRender = loadFunction('queueRender', {
    state: queued,
    ensureAnimationFrame: () => { requested += 1; },
  });
  queueRender();
  assert.equal(queued.renderPending, true);
  assert.equal(requested, 1);

  const state = {
    rafId: 19,
    lastFrameAt: 0,
    lastRenderAt: 0,
    inViewport: true,
    tracking: false,
    renderPending: false,
    velocity: 0,
  };
  let rescheduled = 0;
  const animate = loadFunction('animate', {
    state,
    preview: { open: false },
    document: { visibilityState: 'visible' },
    reducedMotion: { matches: false },
    conserveResources: false,
    advanceMotion: () => false,
    render: () => assert.fail('idle frame must not render'),
    FRAME_INTERVAL: 8,
    ensureAnimationFrame: () => { rescheduled += 1; },
  });
  animate(16);
  assert.equal(state.rafId, 0);
  assert.equal(rescheduled, 0, 'idle gallery must not schedule a perpetual RAF');
});

test('playing state is applied only after video.play succeeds', () => {
  const classes = new Set(['is-playing']);
  let shouldResolve = false;
  const video = {
    src: '/assets/video.mp4',
    dataset: { src: '/assets/video.mp4' },
    pause() {},
    load() {},
    removeAttribute() {},
    play: () => ({
      then(onFulfilled, onRejected) {
        if (shouldResolve) onFulfilled();
        else onRejected(new Error('blocked'));
      },
    }),
  };
  const card = {
    classList: {
      add: value => classes.add(value),
      remove: value => classes.delete(value),
    },
    querySelector: () => video,
  };
  const state = {
    cards: [{ item: { type: 'video' }, failed: false, card }],
  };
  const syncVideoPlayback = loadFunction('syncVideoPlayback', {
    state,
    galleryVideoCanPlay: () => true,
  });

  syncVideoPlayback();
  assert.equal(classes.has('is-playing'), false);
  shouldResolve = true;
  syncVideoPlayback();
  assert.equal(classes.has('is-playing'), true);
});

test('fallback state removes interactive semantics and dynamic cards', () => {
  const removed = [];
  const state = {
    ready: true,
    velocity: 1,
    tracking: true,
    dragging: true,
    renderPending: true,
    previewTrigger: {},
    rafId: 8,
    cards: [],
    items: [{ id: 1 }],
    activeIndex: 0,
  };
  const root = {
    dataset: { galleryState: 'ready' },
    classList: { remove() {} },
    removeAttribute: value => removed.push(value),
  };
  const status = {
    textContent: '',
    removeAttribute: value => removed.push(`status:${value}`),
  };
  let trackCleared = false;
  let instructionsHidden = false;
  const setFallbackState = loadFunction('setFallbackState', {
    state,
    root,
    cancelAnimationFrame: () => {},
    clearEntryMedia: () => {},
    track: { replaceChildren: () => { trackCleared = true; } },
    fallback: { removeAttribute: value => removed.push(`fallback:${value}`) },
    setInstructionsHidden: value => { instructionsHidden = value; },
    status,
  });
  setFallbackState('已切换静态样片。');
  assert.equal(state.ready, false);
  assert.equal(root.dataset.galleryState, 'fallback');
  assert.equal(trackCleared, true);
  assert.equal(instructionsHidden, true);
  assert.equal(status.textContent, '已切换静态样片。');
  assert.ok(removed.includes('aria-label'));
  assert.ok(removed.includes('aria-roledescription'));
});

test('too few valid media items restore the static fallback', () => {
  let fallbackMessage = '';
  const classes = new Set();
  const entry = {
    failed: false,
    visible: true,
    card: {
      hidden: false,
      classList: {
        add: value => classes.add(value),
        remove: (...values) => values.forEach(value => classes.delete(value)),
      },
      setAttribute() {},
      tabIndex: 0,
    },
  };
  const state = {
    ready: true,
    failedCount: 0,
    cards: [entry, ...Array.from({ length: 11 }, () => ({ failed: false }))],
  };
  const handleMediaFailure = loadFunction('handleMediaFailure', {
    state,
    clearEntryMedia: () => {},
    MIN_VALID_ITEMS: 12,
    setFallbackState: message => { fallbackMessage = message; },
    status: { textContent: '' },
    render: () => assert.fail('fallback should replace the gallery before render'),
  });
  handleMediaFailure(entry, '视频封面');
  assert.equal(entry.failed, true);
  assert.match(fallbackMessage, /视频封面样片加载失败/);
});
