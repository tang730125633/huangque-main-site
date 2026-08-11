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
    activePointerId: null,
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
  const beginDrag = loadFunction('beginDrag', {
    state,
    root,
    performance,
    isInteractive: () => true,
    cancelAutoAdvance: () => {},
  });
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
  assert.equal(state.activePointerId, null);
  assert.equal(classes.has('is-dragging'), false);
});

test('pointer movement without a pressed button clears stale drag state', () => {
  const classes = new Set(['is-dragging']);
  const state = { tracking: true, dragging: true, moved: true, suppressClickUntil: 0, activePointerId: 9 };
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

test('an unrelated pointer cannot replace or finish the active drag', () => {
  let now = 3000;
  const captures = new Set();
  const state = {
    activePointerId: null,
    tracking: false,
    dragging: false,
    moved: false,
    position: 2,
    velocity: 0,
    suppressClickUntil: 0,
  };
  const root = {
    classList: { add() {}, remove() {} },
    setPointerCapture: pointerId => captures.add(pointerId),
    hasPointerCapture: pointerId => captures.has(pointerId),
    releasePointerCapture: pointerId => captures.delete(pointerId),
  };
  const performance = { now: () => now };
  const beginDrag = loadFunction('beginDrag', {
    state,
    root,
    performance,
    isInteractive: () => true,
    cancelAutoAdvance: () => {},
  });
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

  beginDrag({ button: 0, pointerId: 11, clientX: 100 });
  beginDrag({ button: 0, pointerId: 12, clientX: 260 });
  assert.equal(state.activePointerId, 11);
  assert.equal(state.originX, 100, 'second pointer must not replace the drag origin');
  moveDrag({ pointerId: 12, clientX: 300, buttons: 1, preventDefault() {} });
  assert.equal(state.position, 2, 'second pointer must not move the gallery');
  finishDrag({ pointerId: 12 });
  assert.equal(state.tracking, true, 'second pointer must not finish the first drag');
  now += 20;
  moveDrag({ pointerId: 11, clientX: 130, buttons: 1, preventDefault() {} });
  assert.notEqual(state.position, 2, 'active pointer must continue dragging');
  finishDrag({ pointerId: 11 });
  assert.equal(state.activePointerId, null);
  assert.equal(state.tracking, false);
});

test('arrow navigation moves focus to the new active card', () => {
  let focusedIndex = -1;
  const state = {
    ready: true,
    velocity: 1,
    position: 0,
    activeIndex: 0,
    items: [{ id: 0 }, { id: 1 }],
    cards: [0, 1].map(index => ({
      failed: false,
      card: { focus: () => { focusedIndex = index; } },
    })),
  };
  const render = force => {
    assert.equal(force, true);
    state.activeIndex = 1;
  };
  const focusActiveCard = loadFunction('focusActiveCard', { state });
  const findNextAvailableIndex = loadFunction('findNextAvailableIndex', { state });
  const activateCard = loadFunction('activateCard', {
    state,
    isInteractive: () => true,
    render,
    focusActiveCard,
  });
  const handleGalleryKeydown = loadFunction('handleGalleryKeydown', {
    state,
    isInteractive: () => true,
    findNextAvailableIndex,
    activateCard,
    openPreview: () => assert.fail('preview should not open for ArrowRight'),
  });
  let prevented = false;
  handleGalleryKeydown({ key: 'ArrowRight', preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(state.position, 1);
  assert.equal(focusedIndex, 1);
});

test('arrow navigation skips failed cards in both directions', () => {
  let focusedIndex = -1;
  const state = {
    ready: true,
    velocity: 0,
    position: 0,
    activeIndex: 0,
    items: Array.from({ length: 5 }, (_, id) => ({ id })),
    cards: Array.from({ length: 5 }, (_, index) => ({
      failed: index === 1 || index === 2,
      card: { focus: () => { focusedIndex = index; } },
    })),
  };
  const render = force => {
    assert.equal(force, true);
    state.activeIndex = state.position;
  };
  const focusActiveCard = loadFunction('focusActiveCard', { state });
  const findNextAvailableIndex = loadFunction('findNextAvailableIndex', { state });
  const activateCard = loadFunction('activateCard', {
    state,
    isInteractive: () => true,
    render,
    focusActiveCard,
  });
  const handleGalleryKeydown = loadFunction('handleGalleryKeydown', {
    state,
    isInteractive: () => true,
    findNextAvailableIndex,
    activateCard,
    openPreview: () => assert.fail('preview should not open for arrow navigation'),
  });

  handleGalleryKeydown({ key: 'ArrowRight', preventDefault() {} });
  assert.equal(state.activeIndex, 3);
  assert.equal(focusedIndex, 3);
  handleGalleryKeydown({ key: 'ArrowLeft', preventDefault() {} });
  assert.equal(state.activeIndex, 0);
  assert.equal(focusedIndex, 0);
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

test('leaving Save-Data range cancels an in-flight poster probe without parallel duplicates', () => {
  let probesCreated = 0;
  const removedProbeAttributes = [];
  const probes = [];
  function Image() {
    probesCreated += 1;
    const probe = {
      onload: null,
      onerror: null,
      src: '',
      removeAttribute: name => removedProbeAttributes.push(name),
    };
    probes.push(probe);
    return probe;
  }
  const video = fakeMedia({ poster: '/assets/poster.webp' });
  const entry = { failed: false, posterLoading: false, posterProbe: null };
  const cancelPosterProbe = loadFunction('cancelPosterProbe', {});
  const mountVideoPoster = loadFunction('mountVideoPoster', {
    Image,
    shouldLoadCardMedia: () => true,
    handleMediaFailure: () => assert.fail('probe should not fail in this test'),
  });
  const unmountVideoPoster = loadFunction('unmountVideoPoster', { cancelPosterProbe });

  mountVideoPoster(entry, video);
  mountVideoPoster(entry, video);
  assert.equal(probesCreated, 1, 'an in-flight probe must be reused');
  const firstProbe = probes[0];
  unmountVideoPoster(entry, video);
  assert.deepEqual(removedProbeAttributes, ['src']);
  assert.equal(firstProbe.onload, null);
  assert.equal(firstProbe.onerror, null);
  assert.equal(entry.posterProbe, null);
  assert.equal(entry.posterLoading, false);

  mountVideoPoster(entry, video);
  assert.equal(probesCreated, 2, 'a new probe may start only after the prior request was cancelled');
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

test('idle motion schedules the next auto advance without keeping RAF alive', () => {
  const state = { position: 4, velocity: 0, autoTarget: null };
  let scheduled = 0;
  const advanceMotion = loadFunction('advanceMotion', {
    state,
    MOTION_DECAY_RATE: Math.log(0.93) / 16.67,
    scheduleAutoAdvance: () => { scheduled += 1; },
  });
  assert.equal(advanceMotion(5000), false);
  assert.equal(state.position, 4);
  assert.equal(scheduled, 1);
  state.velocity = 0.001;
  assert.equal(advanceMotion(16), true);
  assert.ok(state.position > 4);
});

test('auto advance timer launches one smooth target only when allowed', () => {
  const state = { autoTimerId: 0, autoTarget: null, position: 4.25, velocity: 0 };
  let timerCallback = null;
  let timerDelay = 0;
  let requestedFrames = 0;
  const scheduleAutoAdvance = loadFunction('scheduleAutoAdvance', {
    state,
    clearAutoAdvance: () => { state.autoTimerId = 0; },
    autoAdvanceAllowed: () => true,
    setTimeout: (callback, delay) => {
      timerCallback = callback;
      timerDelay = delay;
      return 41;
    },
    AUTO_ADVANCE_DELAY: 2600,
    AUTO_ADVANCE_IMPULSE: -Math.log(0.93) / 16.67,
    ensureAnimationFrame: () => { requestedFrames += 1; },
  });

  scheduleAutoAdvance();
  assert.equal(state.autoTimerId, 41);
  assert.equal(timerDelay, 2600);
  timerCallback();
  assert.equal(state.autoTimerId, 0);
  assert.equal(state.autoTarget, 5.25);
  assert.equal(state.velocity, -Math.log(0.93) / 16.67);
  assert.equal(requestedFrames, 1);
});

test('cancelling auto advance clears its timer target and velocity', () => {
  const cleared = [];
  const state = { autoTimerId: 19, autoTarget: 6, velocity: 0.0044 };
  const clearAutoAdvance = loadFunction('clearAutoAdvance', {
    state,
    clearTimeout: id => cleared.push(id),
  });
  const cancelAutoAdvance = loadFunction('cancelAutoAdvance', { state, clearAutoAdvance });
  cancelAutoAdvance();
  assert.deepEqual(cleared, [19]);
  assert.equal(state.autoTimerId, 0);
  assert.equal(state.autoTarget, null);
  assert.equal(state.velocity, 0);
});

test('auto advance targets one card and is stable across supported frame cadences', () => {
  function settle(frameMs) {
    const start = 7.25;
    const state = {
      position: start,
      velocity: -Math.log(0.93) / 16.67,
      autoTarget: start + 1,
    };
    let scheduled = 0;
    const advanceMotion = loadFunction('advanceMotion', {
      state,
      MOTION_DECAY_RATE: Math.log(0.93) / 16.67,
      scheduleAutoAdvance: () => { scheduled += 1; },
    });
    for (let frame = 0; state.velocity && frame < 1000; frame += 1) advanceMotion(frameMs);
    return { ...state, scheduled };
  }

  [16.67, 33.33, 50].forEach(frameMs => {
    const result = settle(frameMs);
    assert.equal(result.position, 8.25, `${frameMs}ms cadence must move exactly one card`);
    assert.equal(result.autoTarget, null);
    assert.equal(result.velocity, 0);
    assert.equal(result.scheduled, 1);
  });
});

test('motion integration is independent of frame partitioning', () => {
  function simulate(intervals) {
    const state = { position: 2.5, velocity: 0.0044, autoTarget: null };
    const advanceMotion = loadFunction('advanceMotion', {
      state,
      MOTION_DECAY_RATE: Math.log(0.93) / 16.67,
      scheduleAutoAdvance: () => {},
    });
    intervals.forEach(elapsed => advanceMotion(elapsed));
    return state;
  }

  const at60fps = simulate([...Array(59).fill(16.67), 16.47]);
  const at30fps = simulate([...Array(30).fill(33.33), 0.1]);
  const at20fps = simulate(Array(20).fill(50));
  assert.ok(Math.abs(at60fps.position - at30fps.position) < 1e-10);
  assert.ok(Math.abs(at30fps.position - at20fps.position) < 1e-10);
  assert.ok(Math.abs(at60fps.velocity - at20fps.velocity) < 1e-12);
});

test('autoplay pause control cancels motion and can resume scheduling', () => {
  const state = { ready: true, autoPaused: false, autoTarget: 6, velocity: 0.0044 };
  let cancelled = 0;
  let scheduled = 0;
  let updated = 0;
  const toggleAutoAdvance = loadFunction('toggleAutoAdvance', {
    state,
    isInteractive: () => true,
    cancelAutoAdvance: () => {
      cancelled += 1;
      state.autoTarget = null;
      state.velocity = 0;
    },
    scheduleAutoAdvance: () => { scheduled += 1; },
    updateAutoAdvanceControl: () => { updated += 1; },
  });

  toggleAutoAdvance();
  assert.equal(state.autoPaused, true);
  assert.equal(cancelled, 1);
  assert.equal(state.velocity, 0);
  toggleAutoAdvance();
  assert.equal(state.autoPaused, false);
  assert.equal(scheduled, 1);
  assert.equal(updated, 2);
});

test('autoplay pause control exposes its state and hides in reduced modes', () => {
  const attributes = new Map();
  const autoplayToggle = {
    hidden: true,
    textContent: '',
    setAttribute: (name, value) => attributes.set(name, value),
  };
  const state = { autoPaused: false };
  const reducedMotion = { matches: false };
  let interactive = true;
  const updateAutoAdvanceControl = loadFunction('updateAutoAdvanceControl', {
    state,
    autoplayToggle,
    reducedMotion,
    conserveResources: false,
    isInteractive: () => interactive,
  });

  updateAutoAdvanceControl();
  assert.equal(autoplayToggle.hidden, false);
  assert.equal(attributes.get('aria-pressed'), 'false');
  assert.equal(autoplayToggle.textContent, '暂停轮播');

  state.autoPaused = true;
  updateAutoAdvanceControl();
  assert.equal(attributes.get('aria-pressed'), 'true');
  assert.equal(autoplayToggle.textContent, '继续轮播');

  reducedMotion.matches = true;
  updateAutoAdvanceControl();
  assert.equal(autoplayToggle.hidden, true);

  reducedMotion.matches = false;
  interactive = false;
  updateAutoAdvanceControl();
  assert.equal(autoplayToggle.hidden, true);
});

test('auto advance is blocked offscreen, during interaction, pause, and reduced modes', () => {
  const state = { inViewport: true, tracking: false, autoPaused: false };
  const preview = { open: false };
  const reducedMotion = { matches: false };
  const document = { visibilityState: 'visible', activeElement: null };
  let focused = false;
  const root = { matches: () => focused };
  const autoplayToggle = {};
  const allowed = loadFunction('autoAdvanceAllowed', {
    state,
    root,
    autoplayToggle,
    preview,
    document,
    reducedMotion,
    conserveResources: false,
    isInteractive: () => true,
  });
  assert.equal(allowed(), true);
  state.inViewport = false;
  assert.equal(allowed(), false);
  state.inViewport = true;
  state.tracking = true;
  assert.equal(allowed(), false);
  state.tracking = false;
  state.autoPaused = true;
  assert.equal(allowed(), false);
  state.autoPaused = false;
  focused = true;
  assert.equal(allowed(), false);
  document.activeElement = autoplayToggle;
  assert.equal(allowed(), true, 'the visible pause control must not block resumed autoplay');
  focused = false;
  reducedMotion.matches = true;
  assert.equal(allowed(), false);

  const conserved = loadFunction('autoAdvanceAllowed', {
    state: { ...state, autoPaused: false },
    root,
    autoplayToggle,
    preview,
    document: { visibilityState: 'visible', activeElement: null },
    reducedMotion: { matches: false },
    conserveResources: true,
    isInteractive: () => true,
  });
  assert.equal(conserved(), false);
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
    hidden: false,
    disabled: false,
    getAttribute: () => null,
    focus: options => {
      assert.deepEqual(options, { preventScroll: true });
      focused = true;
    },
  };
  const state = { previewTrigger: trigger, cards: [{ card: trigger, failed: false }], activeIndex: 0 };
  const previewStage = {
    querySelector: () => null,
    replaceChildren: () => { replaced = true; },
  };
  const previewTitle = { textContent: 'before' };
  const cleanupPreview = loadFunction('cleanupPreview', {
    state,
    previewStage,
    previewTitle,
    cancelPreviewMedia: () => {},
    canRestoreGalleryFocus: loadFunction('canRestoreGalleryFocus', { state }),
    isInteractive: () => true,
    syncVideoPlayback: () => {},
    scheduleAutoAdvance: () => {},
  });
  cleanupPreview();
  assert.equal(replaced, true);
  assert.equal(previewTitle.textContent, '');
  assert.equal(focused, true);
  assert.equal(state.previewTrigger, null);
});

test('closing the preview skips a hidden failed trigger and focuses the active card', () => {
  let activeFocused = false;
  const failedTrigger = {
    isConnected: true,
    hidden: true,
    disabled: false,
    getAttribute: name => name === 'aria-hidden' ? 'true' : null,
    focus: () => assert.fail('hidden failed trigger must not receive focus'),
  };
  const activeCard = {
    isConnected: true,
    hidden: false,
    disabled: false,
    getAttribute: () => null,
    focus: options => {
      assert.deepEqual(options, { preventScroll: true });
      activeFocused = true;
    },
  };
  const state = {
    previewTrigger: failedTrigger,
    cards: [
      { card: failedTrigger, failed: true },
      { card: activeCard, failed: false },
    ],
    activeIndex: 1,
  };
  const cleanupPreview = loadFunction('cleanupPreview', {
    state,
    previewStage: { replaceChildren() {} },
    previewTitle: { textContent: 'old' },
    cancelPreviewMedia: () => {},
    canRestoreGalleryFocus: loadFunction('canRestoreGalleryFocus', { state }),
    isInteractive: () => true,
    syncVideoPlayback: () => {},
    scheduleAutoAdvance: () => {},
  });

  cleanupPreview();
  assert.equal(activeFocused, true);
  assert.equal(state.previewTrigger, null);
});

test('side-card preview becomes active before close and keyboard reopen', () => {
  const openedItems = [];
  let focusedIndex = -1;
  const cards = Array.from({ length: 3 }, (_, index) => ({
    isConnected: true,
    hidden: false,
    disabled: false,
    dataset: { galleryIndex: String(index) },
    getAttribute: () => null,
    focus: () => { focusedIndex = index; },
  }));
  const state = {
    ready: true,
    velocity: 0,
    position: 0,
    activeIndex: 0,
    previewTrigger: null,
    items: Array.from({ length: 3 }, (_, id) => ({ id })),
    cards: cards.map(card => ({ card, failed: false })),
  };
  const render = force => {
    assert.equal(force, true);
    state.activeIndex = state.position;
  };
  const focusActiveCard = loadFunction('focusActiveCard', { state });
  const activateCard = loadFunction('activateCard', {
    state,
    isInteractive: () => true,
    render,
    focusActiveCard,
  });
  const openPreview = (item, trigger) => {
    state.previewTrigger = trigger;
    openedItems.push(item.id);
  };
  const openCardPreview = loadFunction('openCardPreview', { state, activateCard, openPreview });

  openCardPreview(cards[2]);
  assert.equal(state.activeIndex, 2);
  assert.deepEqual(openedItems, [2]);

  const canRestoreGalleryFocus = loadFunction('canRestoreGalleryFocus', { state });
  const cleanupPreview = loadFunction('cleanupPreview', {
    state,
    previewStage: { replaceChildren() {} },
    previewTitle: { textContent: 'before' },
    cancelPreviewMedia: () => {},
    canRestoreGalleryFocus,
    isInteractive: () => true,
    syncVideoPlayback: () => {},
    scheduleAutoAdvance: () => {},
  });
  cleanupPreview();
  assert.equal(focusedIndex, 2);

  const handleGalleryKeydown = loadFunction('handleGalleryKeydown', {
    state,
    isInteractive: () => true,
    openPreview,
  });
  handleGalleryKeydown({ key: 'Enter', preventDefault() {} });
  assert.deepEqual(openedItems, [2, 2]);
});

test('preview cleanup detaches errors and cancels the old media request', () => {
  const removedAttributes = [];
  const errorHandler = () => {};
  let removedHandler = null;
  let paused = 0;
  let loaded = 0;
  const media = {
    removeEventListener(type, handler) {
      assert.equal(type, 'error');
      removedHandler = handler;
    },
    pause: () => { paused += 1; },
    removeAttribute: name => removedAttributes.push(name),
    load: () => { loaded += 1; },
  };
  const state = {
    previewMedia: media,
    previewMediaErrorHandler: errorHandler,
    previewToken: 4,
    previewTrigger: null,
    cards: [],
    activeIndex: 0,
  };
  const cancelPreviewMedia = loadFunction('cancelPreviewMedia', { state });
  const cleanupPreview = loadFunction('cleanupPreview', {
    state,
    previewStage: { replaceChildren() {} },
    previewTitle: { textContent: 'old' },
    cancelPreviewMedia,
    isInteractive: () => false,
    syncVideoPlayback: () => {},
    scheduleAutoAdvance: () => {},
  });

  cleanupPreview();
  assert.equal(removedHandler, errorHandler);
  assert.equal(paused, 1);
  assert.equal(loaded, 1);
  assert.deepEqual(removedAttributes, ['src', 'poster']);
  assert.equal(state.previewMedia, null);
  assert.equal(state.previewMediaErrorHandler, null);
  assert.equal(state.previewToken, 5);

  const imageAttributes = [];
  const image = {
    removeEventListener() {},
    removeAttribute: name => imageAttributes.push(name),
  };
  state.previewMedia = image;
  state.previewMediaErrorHandler = errorHandler;
  cancelPreviewMedia();
  assert.deepEqual(imageAttributes, ['src'], 'image requests must also be cancelled without video-only calls');
  assert.equal(state.previewToken, 6);
});

test('a stale preview error cannot replace the current preview', () => {
  const oldMedia = {};
  const currentMedia = {};
  const state = { previewMedia: currentMedia, previewToken: 9 };
  let cancelled = 0;
  let replacement = null;
  const document = {
    activeElement: null,
    createElement: () => ({ className: '', setAttribute() {}, textContent: '' }),
  };
  const showPreviewMediaError = loadFunction('showPreviewMediaError', {
    state,
    cancelPreviewMedia: () => { cancelled += 1; },
    document,
    previewClose: { focus: () => assert.fail('unfocused media errors must not steal focus') },
    previewStage: { replaceChildren: value => { replacement = value; } },
  });

  showPreviewMediaError(oldMedia, 8);
  assert.equal(cancelled, 0);
  assert.equal(replacement, null);
  showPreviewMediaError(currentMedia, 9);
  assert.equal(cancelled, 1);
  assert.equal(replacement.className, 'orbit-preview-error');
});

test('preview media errors move focused controls to the dialog close button', () => {
  const media = { contains: () => false };
  const state = { previewMedia: media, previewToken: 12 };
  let focused = false;
  const previewClose = {
    focus: options => {
      assert.deepEqual(options, { preventScroll: true });
      focused = true;
    },
  };
  const showPreviewMediaError = loadFunction('showPreviewMediaError', {
    state,
    cancelPreviewMedia: () => {},
    document: {
      activeElement: media,
      createElement: () => ({ className: '', setAttribute() {}, textContent: '' }),
    },
    previewClose,
    previewStage: { replaceChildren() {} },
  });

  showPreviewMediaError(media, 12);
  assert.equal(focused, true);
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
    document: { activeElement: null },
    root,
    cancelAutoAdvance: () => {},
    updateAutoAdvanceControl: () => {},
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

test('fallback moves focus from a removed gallery card to the status message', () => {
  const activeElement = {};
  const card = { contains: value => value === activeElement };
  const state = {
    ready: true,
    velocity: 0,
    tracking: false,
    dragging: false,
    activePointerId: null,
    renderPending: false,
    previewTrigger: null,
    rafId: 0,
    cards: [{ card }],
    items: [{ id: 1 }],
    activeIndex: 0,
  };
  let focused = false;
  const status = {
    textContent: '',
    tabIndex: 0,
    removeAttribute() {},
    focus: options => {
      assert.deepEqual(options, { preventScroll: true });
      focused = true;
    },
  };
  const setFallbackState = loadFunction('setFallbackState', {
    state,
    document: { activeElement },
    root: {
      dataset: {},
      classList: { remove() {} },
      removeAttribute() {},
    },
    cancelAnimationFrame: () => {},
    cancelAutoAdvance: () => {},
    updateAutoAdvanceControl: () => {},
    clearEntryMedia: () => {},
    track: { replaceChildren() {} },
    fallback: { removeAttribute() {} },
    setInstructionsHidden: () => {},
    status,
  });

  setFallbackState('已恢复静态样片。');
  assert.equal(status.tabIndex, -1);
  assert.equal(focused, true);
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
    document: { activeElement: null },
    clearEntryMedia: () => {},
    MIN_VALID_ITEMS: 12,
    setFallbackState: message => { fallbackMessage = message; },
    status: { textContent: '' },
    render: () => assert.fail('fallback should replace the gallery before render'),
    isInteractive: () => true,
  });
  handleMediaFailure(entry, '视频封面');
  assert.equal(entry.failed, true);
  assert.match(fallbackMessage, /视频封面样片加载失败/);
});

test('a continuous failed window recenters on the nearest surviving card', () => {
  const failedIndexes = new Set([17, 18, 19, 20, 0, 1, 2, 3, 4]);
  const makeCard = () => ({
    classList: { toggle() {} },
    setAttribute() {},
    style: { setProperty() {} },
    tabIndex: -1,
  });
  const state = {
    position: 0,
    activeIndex: 0,
    ready: true,
    failedCount: failedIndexes.size,
    items: Array.from({ length: 21 }, (_, id) => ({ id })),
    cards: Array.from({ length: 21 }, (_, index) => ({
      failed: failedIndexes.has(index),
      visible: null,
      card: makeCard(),
    })),
  };
  const shortestDelta = loadFunction('shortestDelta', { state });
  const normalizePosition = loadFunction('normalizePosition', { state });
  const nearestAvailableIndex = loadFunction('nearestAvailableIndex', { state, shortestDelta });
  const ensureAvailablePosition = loadFunction('ensureAvailablePosition', {
    state,
    nearestAvailableIndex,
    shortestDelta,
    VISIBLE_RANGE: 4.2,
  });
  let fallback = false;
  const render = loadFunction('render', {
    state,
    normalizePosition,
    ensureAvailablePosition,
    innerWidth: 1280,
    VISIBLE_RANGE: 4.2,
    shortestDelta,
    setFallbackState: () => { fallback = true; },
    status: { textContent: '' },
    syncCardMediaSources: () => {},
    syncVideoPlayback: () => {},
  });

  assert.equal(state.cards.filter(entry => !entry.failed).length, 12);
  render(true);
  assert.equal(fallback, false);
  assert.equal(state.position, 5);
  assert.equal(state.activeIndex, 5);
  assert.equal(state.cards[5].card.tabIndex, 0);
  const visibleSurvivors = state.cards.filter(entry => !entry.failed && entry.visible);
  assert.ok(visibleSurvivors.length > 0);
});

test('media failure transfers focus from the hidden card to the new active card', () => {
  let focused = false;
  const activeDescendant = {};
  const failedEntry = {
    failed: false,
    visible: true,
    item: { type: 'image' },
    card: {
      hidden: false,
      contains: value => value === activeDescendant,
      classList: { remove() {} },
      setAttribute() {},
      tabIndex: 0,
    },
  };
  const nextEntry = {
    failed: false,
    card: { focus: options => {
      assert.deepEqual(options, { preventScroll: true });
      focused = true;
    } },
  };
  const state = {
    ready: true,
    failedCount: 0,
    activeIndex: 0,
    cards: [failedEntry, nextEntry, ...Array.from({ length: 11 }, () => ({ failed: false }))],
  };
  const handleMediaFailure = loadFunction('handleMediaFailure', {
    state,
    document: { activeElement: activeDescendant },
    clearEntryMedia: () => {},
    MIN_VALID_ITEMS: 12,
    setFallbackState: () => assert.fail('12 valid items must keep the gallery interactive'),
    status: { textContent: '' },
    render: force => {
      assert.equal(force, true);
      state.activeIndex = 1;
    },
    isInteractive: () => true,
  });

  handleMediaFailure(failedEntry, '图片');
  assert.equal(failedEntry.card.hidden, true);
  assert.equal(focused, true);
});
