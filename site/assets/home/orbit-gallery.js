(() => {
  const root = document.querySelector('[data-orbit-gallery]');
  const track = root?.querySelector('[data-gallery-track]');
  const status = root?.querySelector('[data-gallery-status]');
  const fallback = root?.querySelector('[data-gallery-fallback]');
  const preview = document.querySelector('[data-gallery-preview]');
  const previewStage = preview?.querySelector('[data-gallery-preview-stage]');
  const previewTitle = preview?.querySelector('[data-gallery-preview-title]');
  const previewClose = preview?.querySelector('[data-gallery-preview-close]');
  if (!root || !track || !status || !preview || !previewStage || !previewTitle || !previewClose) return;
  const instructions = root.querySelectorAll('[data-gallery-instruction]');

  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  const saveData = Boolean(connection?.saveData);
  const lowMemory = Number.isFinite(navigator.deviceMemory) && navigator.deviceMemory <= 4;
  const conserveResources = saveData || lowMemory;
  const allowedImages = /\.(?:avif|gif|jpe?g|png|webp)$/i;
  const allowedVideos = /\.(?:m4v|mov|mp4|webm)$/i;
  const DRAG_SENSITIVITY = 0.0048;
  const FRAME_INTERVAL = conserveResources ? 32 : 8;
  const VISIBLE_RANGE = conserveResources ? 4.2 : 5.2;
  const MEDIA_LOAD_RANGE = conserveResources ? 2.2 : VISIBLE_RANGE;
  const MIN_VALID_ITEMS = 12;

  root.dataset.galleryMode = conserveResources ? 'lite' : 'full';

  const state = {
    items: [],
    cards: [],
    position: 0,
    velocity: 0,
    tracking: false,
    dragging: false,
    moved: false,
    inViewport: false,
    initialized: false,
    ready: false,
    originX: 0,
    startPosition: 0,
    lastPointerX: 0,
    lastPointerAt: 0,
    suppressClickUntil: 0,
    activeIndex: -1,
    lastFrameAt: 0,
    lastRenderAt: 0,
    renderPending: false,
    rafId: 0,
    previewTrigger: null,
    failedCount: 0
  };

  function isInteractive() {
    return state.ready && state.items.length > 0;
  }

  function setInstructionsHidden(hidden) {
    instructions.forEach(instruction => {
      instruction.hidden = hidden;
    });
  }

  function setLoadingState() {
    root.dataset.galleryState = 'loading';
    root.removeAttribute('aria-label');
    status.setAttribute('aria-live', 'polite');
    status.textContent = '正在载入创作样片…';
    setInstructionsHidden(true);
  }

  function clearEntryMedia(entry) {
    if (entry.posterProbe) {
      entry.posterProbe.onload = null;
      entry.posterProbe.onerror = null;
      entry.posterProbe = null;
    }
    entry.posterLoading = false;
    const media = entry.card.querySelector(entry.item.type === 'video' ? 'video' : 'img');
    if (!media) return;
    if (entry.item.type === 'video') {
      media.pause();
      media.removeAttribute('src');
      media.removeAttribute('poster');
      media.load();
    } else {
      media.removeAttribute('src');
    }
  }

  function setFallbackState(message) {
    state.ready = false;
    state.velocity = 0;
    state.tracking = false;
    state.dragging = false;
    state.renderPending = false;
    state.previewTrigger = null;
    root.classList.remove('is-dragging');
    if (state.rafId) cancelAnimationFrame(state.rafId);
    state.rafId = 0;
    state.cards.forEach(clearEntryMedia);
    state.cards = [];
    state.items = [];
    state.activeIndex = -1;
    track.replaceChildren();
    root.dataset.galleryState = 'fallback';
    root.removeAttribute('aria-label');
    root.removeAttribute('aria-roledescription');
    fallback?.removeAttribute('aria-hidden');
    setInstructionsHidden(true);
    status.removeAttribute('aria-live');
    status.textContent = message || '动态画廊暂不可用，已显示静态创作样片。';
  }

  function assetPath(value, type) {
    if (typeof value !== 'string' || !value.startsWith('/assets/')) return null;
    let url;
    try {
      url = new URL(value, location.origin);
    } catch (_) {
      return null;
    }
    if (url.origin !== location.origin || !url.pathname.startsWith('/assets/')) return null;
    const allowed = type === 'video' ? allowedVideos : allowedImages;
    return allowed.test(url.pathname) ? `${url.pathname}${url.search}` : null;
  }

  function normalizeItem(raw, index, seen) {
    if (!raw || (raw.type !== 'image' && raw.type !== 'video')) return null;
    const id = typeof raw.id === 'string' ? raw.id.trim() : `gallery-media-${index + 1}`;
    if (!id || seen.has(id)) return null;
    const src = assetPath(raw.src, raw.type);
    const poster = raw.type === 'video' ? assetPath(raw.poster, 'image') : null;
    if (!src || (raw.type === 'video' && !poster)) return null;
    seen.add(id);
    return {
      id,
      type: raw.type,
      src,
      poster,
      alt: typeof raw.alt === 'string' && raw.alt.trim()
        ? raw.alt.trim().slice(0, 120)
        : `黄雀创作样片 ${index + 1}`
    };
  }

  async function loadItems() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(root.dataset.galleryManifest, {
        credentials: 'same-origin',
        signal: controller.signal
      });
      if (!response.ok) throw new Error(`manifest ${response.status}`);
      const payload = await response.json();
      const seen = new Set();
      const items = Array.isArray(payload.items)
        ? payload.items.slice(0, 32).map((item, index) => normalizeItem(item, index, seen)).filter(Boolean)
        : [];
      if (items.length < MIN_VALID_ITEMS) throw new Error('at least 12 valid gallery items are required');
      return items;
    } finally {
      clearTimeout(timeout);
    }
  }

  function createCard(item, index) {
    const card = document.createElement('button');
    const entry = {
      card,
      item,
      index,
      visible: null,
      failed: false,
      posterProbe: null,
      posterLoading: false
    };
    card.type = 'button';
    card.className = `orbit-gallery-card is-${item.type}`;
    card.dataset.galleryIndex = String(index);
    card.setAttribute('aria-label', `预览${item.type === 'video' ? '视频' : '图片'}：${item.alt}`);

    if (item.type === 'video') {
      const video = document.createElement('video');
      video.dataset.src = item.src;
      video.dataset.poster = item.poster;
      video.muted = true;
      video.defaultMuted = true;
      video.loop = true;
      video.playsInline = true;
      video.preload = 'none';
      video.disablePictureInPicture = true;
      video.setAttribute('muted', '');
      video.setAttribute('playsinline', '');
      video.addEventListener('error', () => handleMediaFailure(entry, '视频'));
      card.append(video);

      const badge = document.createElement('span');
      badge.className = 'orbit-gallery-badge';
      badge.setAttribute('aria-hidden', 'true');
      badge.textContent = 'VIDEO';
      card.append(badge);
    } else {
      const image = new Image();
      image.dataset.src = item.src;
      image.alt = '';
      image.decoding = 'async';
      image.loading = 'lazy';
      image.addEventListener('error', () => handleMediaFailure(entry, '图片'));
      card.append(image);
    }
    track.append(card);
    return entry;
  }

  function handleMediaFailure(entry, mediaLabel) {
    if (!state.ready || entry.failed) return;
    entry.failed = true;
    entry.visible = false;
    state.failedCount += 1;
    entry.card.hidden = true;
    entry.card.classList.remove('is-visible', 'is-active', 'is-playing');
    entry.card.setAttribute('aria-hidden', 'true');
    entry.card.tabIndex = -1;
    clearEntryMedia(entry);
    const remaining = state.cards.filter(cardEntry => !cardEntry.failed).length;
    if (remaining < MIN_VALID_ITEMS) {
      setFallbackState(`${mediaLabel}样片加载失败，已恢复静态创作样片。`);
      return;
    }
    status.textContent = `${remaining} 个创作样片可用 · ${state.failedCount} 个暂不可用`;
    render(true);
  }

  function shouldLoadCardMedia(entry) {
    return isInteractive()
      && state.inViewport
      && entry.visible
      && !entry.failed
      && Math.abs(shortestDelta(entry.index)) <= MEDIA_LOAD_RANGE;
  }

  function unmountVideoPoster(entry, video) {
    if (entry.posterProbe) {
      entry.posterProbe.onload = null;
      entry.posterProbe.onerror = null;
      entry.posterProbe = null;
    }
    entry.posterLoading = false;
    video.removeAttribute('poster');
  }

  function mountVideoPoster(entry, video) {
    if (video.getAttribute('poster') || entry.posterLoading || entry.failed) return;
    entry.posterLoading = true;
    const probe = new Image();
    entry.posterProbe = probe;
    probe.onload = () => {
      entry.posterLoading = false;
      entry.posterProbe = null;
      if (!entry.failed && shouldLoadCardMedia(entry)) video.poster = video.dataset.poster;
    };
    probe.onerror = () => {
      entry.posterLoading = false;
      entry.posterProbe = null;
      handleMediaFailure(entry, '视频封面');
    };
    probe.src = video.dataset.poster;
  }

  function shortestDelta(index) {
    const count = state.items.length;
    let delta = index - state.position;
    return ((delta + count / 2) % count + count) % count - count / 2;
  }

  function normalizePosition() {
    const count = state.items.length;
    if (count) state.position = ((state.position % count) + count) % count;
  }

  function galleryVideoCanPlay(index) {
    return isInteractive()
      && state.inViewport
      && document.visibilityState === 'visible'
      && !preview.open
      && !reducedMotion.matches
      && !conserveResources
      && index === state.activeIndex;
  }

  function syncVideoPlayback() {
    state.cards.forEach((entry, index) => {
      if (entry.item.type !== 'video' || entry.failed) return;
      const video = entry.card.querySelector('video');
      const shouldPlay = galleryVideoCanPlay(index);
      entry.card.classList.remove('is-playing');
      if (!shouldPlay) {
        video.pause();
        if (video.src) {
          video.removeAttribute('src');
          video.load();
        }
        return;
      }
      if (!video.src) {
        video.src = video.dataset.src;
        video.load();
      }
      try {
        const playAttempt = video.play();
        if (playAttempt?.then) {
          playAttempt.then(
            () => {
              if (galleryVideoCanPlay(index) && video.src) entry.card.classList.add('is-playing');
            },
            () => entry.card.classList.remove('is-playing')
          );
        } else if (galleryVideoCanPlay(index)) {
          entry.card.classList.add('is-playing');
        }
      } catch (_) {
        entry.card.classList.remove('is-playing');
      }
    });
  }

  function syncCardMediaSources() {
    state.cards.forEach(entry => {
      if (entry.failed) return;
      const shouldLoad = shouldLoadCardMedia(entry);
      const media = entry.card.querySelector(entry.item.type === 'video' ? 'video' : 'img');
      if (!media) return;
      if (entry.item.type === 'video') {
        if (shouldLoad) mountVideoPoster(entry, media);
        else if (!shouldLoad && conserveResources) unmountVideoPoster(entry, media);
      } else if (shouldLoad && !media.getAttribute('src')) {
        media.src = media.dataset.src;
      } else if (!shouldLoad && conserveResources) {
        media.removeAttribute('src');
      }
    });
  }

  function render(force = false) {
    if (!state.items.length) return;
    normalizePosition();
    const compact = innerWidth < 720;
    const radiusX = Math.min(compact ? innerWidth * 0.94 : innerWidth * 0.64, compact ? 540 : 800);
    const curveDepth = Math.min(compact ? 420 : 820, Math.max(compact ? 320 : 640, innerWidth * 0.52));
    const centerDepth = -curveDepth * (compact ? 0.92 : 0.78);
    const depthExpansion = compact ? 1.65 : 1.85;
    const angleStep = compact ? 0.4 : 0.34;
    const arcLift = compact
      ? 168
      : Math.min(680, Math.max(470, innerWidth * 0.34));
    let activeIndex = 0;
    let activeDistance = Infinity;

    state.cards.forEach((entry, index) => {
      if (entry.failed) return;
      const delta = shortestDelta(index);
      const distance = Math.abs(delta);
      const visible = distance <= VISIBLE_RANGE;
      if (entry.visible !== visible) {
        entry.visible = visible;
        entry.card.classList.toggle('is-visible', visible);
        entry.card.setAttribute('aria-hidden', visible ? 'false' : 'true');
        if (!visible) entry.card.style.opacity = '0';
      }
      if (!visible) return;

      const angle = delta * angleStep;
      const centerWeight = Math.max(0, Math.cos(Math.min(Math.PI / 2, Math.abs(angle))));
      const spacingBoost = 1 + centerWeight * 0.48;
      const x = Math.sin(angle) * radiusX * spacingBoost;
      const curve = 1 - Math.cos(angle);
      const verticalCurve = Math.pow(curve, compact ? 0.68 : 0.5);
      const z = centerDepth + curve * curveDepth * depthExpansion;
      const y = -verticalCurve * arcLift;
      const rotateY = -Math.sign(angle) * Math.min(82, Math.abs(angle) * (compact ? 94 : 112));
      const scale = (compact ? 0.72 : 0.86) + centerWeight * (compact ? 0.42 : 0.64);
      const opacity = 0.28 + centerWeight * 0.72;
      entry.card.style.transform = `translate3d(calc(-50% + ${x}px), calc(-50% + ${y}px), ${z}px) rotateY(${rotateY}deg) scale(${scale})`;
      entry.card.style.opacity = String(opacity);
      entry.card.style.zIndex = String(1000 + Math.round(z));
      entry.card.style.setProperty('--depth-shade', String(1 - centerWeight));
      entry.card.style.setProperty('--media-saturation', String(0.78 + centerWeight * 0.22));
      entry.card.style.setProperty('--media-brightness', String(0.58 + centerWeight * 0.42));

      if (distance < activeDistance) {
        activeDistance = distance;
        activeIndex = index;
      }
    });

    if (activeIndex !== state.activeIndex || force) {
      state.activeIndex = activeIndex;
      state.cards.forEach((entry, index) => {
        const active = !entry.failed && index === activeIndex;
        entry.card.classList.toggle('is-active', active);
        entry.card.tabIndex = state.ready && active ? 0 : -1;
      });
      const unavailable = state.failedCount ? ` · ${state.failedCount} 个暂不可用` : '';
      status.textContent = `${activeIndex + 1} / ${state.items.length} · ${state.items[activeIndex].alt}${unavailable}`;
      syncCardMediaSources();
      syncVideoPlayback();
    }
  }

  function cleanupPreview() {
    const video = previewStage.querySelector('video');
    if (video) video.pause();
    previewStage.replaceChildren();
    previewTitle.textContent = '';
    const trigger = state.previewTrigger;
    state.previewTrigger = null;
    if (isInteractive()) {
      const focusTarget = trigger?.isConnected ? trigger : state.cards[state.activeIndex]?.card;
      focusTarget?.focus({ preventScroll: true });
    }
    syncVideoPlayback();
  }

  function closePreview() {
    if (preview.open && typeof preview.close === 'function') preview.close();
    else {
      preview.removeAttribute('open');
      cleanupPreview();
    }
  }

  function previewAutoplayAllowed() {
    return !conserveResources && !reducedMotion.matches;
  }

  function showPreviewMediaError() {
    const message = document.createElement('p');
    message.className = 'orbit-preview-error';
    message.setAttribute('role', 'status');
    message.textContent = '此作品暂时无法载入，请稍后重试。';
    previewStage.replaceChildren(message);
  }

  function openPreview(item, trigger) {
    if (!isInteractive() || !item) return;
    state.previewTrigger = trigger?.closest?.('[data-gallery-index]') || state.cards[state.activeIndex]?.card || null;
    previewStage.replaceChildren();
    previewTitle.textContent = item.alt;
    let media;
    if (item.type === 'video') {
      media = document.createElement('video');
      media.src = item.src;
      media.poster = item.poster;
      media.controls = true;
      media.playsInline = true;
      media.autoplay = previewAutoplayAllowed();
    } else {
      media = new Image();
      media.src = item.src;
      media.alt = item.alt;
      media.decoding = 'async';
    }
    media.addEventListener('error', showPreviewMediaError, { once: true });
    previewStage.append(media);
    if (typeof preview.showModal === 'function') preview.showModal();
    else preview.setAttribute('open', '');
    syncVideoPlayback();
  }

  function queueRender() {
    state.renderPending = true;
    ensureAnimationFrame();
  }

  function beginDrag(event) {
    if (!isInteractive() || event.button !== 0) return;
    state.tracking = true;
    state.dragging = false;
    state.moved = false;
    state.originX = event.clientX;
    state.startPosition = state.position;
    state.lastPointerX = event.clientX;
    state.lastPointerAt = performance.now();
    state.velocity = 0;
    if (typeof root.setPointerCapture === 'function') root.setPointerCapture(event.pointerId);
  }

  function moveDrag(event) {
    if (!isInteractive() || !state.tracking) return;
    if ((event.buttons & 1) === 0) {
      finishDrag(event);
      return;
    }
    const totalX = event.clientX - state.originX;
    if (!state.dragging && Math.abs(totalX) < 6) return;
    if (!state.dragging) {
      state.dragging = true;
      state.moved = true;
      root.classList.add('is-dragging');
    }
    const now = performance.now();
    const elapsed = Math.max(8, now - state.lastPointerAt);
    const pointerDelta = event.clientX - state.lastPointerX;
    state.position = state.startPosition - totalX * DRAG_SENSITIVITY;
    state.velocity = state.velocity * 0.58 - (pointerDelta * DRAG_SENSITIVITY / elapsed) * 0.42;
    state.lastPointerX = event.clientX;
    state.lastPointerAt = now;
    queueRender();
    event.preventDefault();
  }

  function finishDrag(event) {
    const wasTracking = state.tracking;
    state.tracking = false;
    state.dragging = false;
    root.classList.remove('is-dragging');
    if (typeof root.hasPointerCapture === 'function' && root.hasPointerCapture(event.pointerId)) {
      root.releasePointerCapture(event.pointerId);
    }
    if (!wasTracking) return;
    if (state.moved) state.suppressClickUntil = performance.now() + 380;
    ensureAnimationFrame();
  }

  root.addEventListener('pointerdown', beginDrag);
  root.addEventListener('pointermove', moveDrag);
  root.addEventListener('pointerup', finishDrag);
  root.addEventListener('pointercancel', finishDrag);
  root.addEventListener('lostpointercapture', finishDrag);
  addEventListener('pointerup', finishDrag, true);
  addEventListener('pointercancel', finishDrag, true);

  root.addEventListener('wheel', event => {
    if (!isInteractive() || preview.open || (Math.abs(event.deltaX) <= Math.abs(event.deltaY) && !event.shiftKey)) return;
    const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    state.position += delta * 0.0016;
    state.velocity = delta * 0.000035;
    queueRender();
    event.preventDefault();
  }, { passive: false });

  root.addEventListener('click', event => {
    if (!isInteractive()) return;
    const card = event.target.closest('[data-gallery-index]');
    if (!card || performance.now() < state.suppressClickUntil) return;
    const item = state.items[Number(card.dataset.galleryIndex)];
    if (item) openPreview(item, card);
  });

  function focusActiveCard() {
    state.cards[state.activeIndex]?.card.focus({ preventScroll: true });
  }

  function handleGalleryKeydown(event) {
    if (!isInteractive()) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      state.velocity = 0;
      state.position += event.key === 'ArrowRight' ? 1 : -1;
      render(true);
      focusActiveCard();
    } else if ((event.key === 'Enter' || event.key === ' ') && state.items[state.activeIndex]) {
      event.preventDefault();
      openPreview(state.items[state.activeIndex], state.cards[state.activeIndex]?.card);
    }
  }

  root.addEventListener('keydown', handleGalleryKeydown);

  previewClose.addEventListener('click', closePreview);
  preview.addEventListener('click', event => {
    if (event.target === preview) closePreview();
  });
  preview.addEventListener('close', cleanupPreview);

  function stopAnimationFrame() {
    if (state.rafId) cancelAnimationFrame(state.rafId);
    state.rafId = 0;
  }

  function handleDocumentVisibility() {
    if (document.visibilityState !== 'visible') {
      state.velocity = 0;
      state.renderPending = false;
      stopAnimationFrame();
    } else {
      ensureAnimationFrame();
    }
    syncVideoPlayback();
  }

  document.addEventListener('visibilitychange', handleDocumentVisibility);

  if (typeof IntersectionObserver !== 'function') {
    setFallbackState('当前浏览器不支持动态画廊，已显示静态创作样片。');
    return;
  }

  const visibilityObserver = new IntersectionObserver(entries => {
    state.inViewport = Boolean(entries[0]?.isIntersecting);
    if (!state.inViewport) {
      state.velocity = 0;
      state.renderPending = false;
      stopAnimationFrame();
    } else if (isInteractive()) {
      render(true);
      ensureAnimationFrame();
    }
    syncCardMediaSources();
    syncVideoPlayback();
  }, { rootMargin: '12% 0px' });
  visibilityObserver.observe(root);

  function advanceMotion(elapsed) {
    if (Math.abs(state.velocity) <= 0.00002) {
      state.velocity = 0;
      return false;
    }
    state.position += state.velocity * elapsed;
    state.velocity *= Math.pow(0.93, elapsed / 16.67);
    if (Math.abs(state.velocity) <= 0.00002) state.velocity = 0;
    return true;
  }

  function ensureAnimationFrame() {
    if (
      state.rafId
      || !isInteractive()
      || !state.inViewport
      || document.visibilityState !== 'visible'
    ) return;
    state.lastFrameAt = performance.now();
    state.rafId = requestAnimationFrame(animate);
  }

  function animate(now) {
    state.rafId = 0;
    const elapsed = Math.min(50, now - state.lastFrameAt);
    state.lastFrameAt = now;
    const canMove = state.inViewport
      && !state.tracking
      && !preview.open
      && document.visibilityState === 'visible'
      && !reducedMotion.matches
      && !conserveResources;
    let positionChanged = false;
    if (canMove) positionChanged = advanceMotion(elapsed);
    if ((positionChanged || state.renderPending) && now - state.lastRenderAt >= FRAME_INTERVAL) {
      state.lastRenderAt = now;
      state.renderPending = false;
      render();
    }
    if (state.renderPending || (canMove && Math.abs(state.velocity) > 0.00002)) {
      ensureAnimationFrame();
    }
  }

  async function init() {
    if (state.initialized) return;
    state.initialized = true;
    setLoadingState();
    try {
      state.items = await loadItems();
      state.cards = state.items.map(createCard);
      state.ready = true;
      state.failedCount = 0;
      fallback?.setAttribute('aria-hidden', 'true');
      root.dataset.galleryState = 'ready';
      root.setAttribute('aria-label', '可拖动的黄雀图片与视频环形画廊，使用左右方向键切换，按回车放大预览');
      root.setAttribute('aria-roledescription', '环形画廊');
      setInstructionsHidden(false);
      render(true);
    } catch (_) {
      setFallbackState('动态画廊暂不可用，已显示静态创作样片。');
    }
  }

  function handleInitIntersection(entries) {
    if (!entries[0]?.isIntersecting) return;
    initObserver.disconnect();
    init();
  }

  const initObserver = new IntersectionObserver(handleInitIntersection, { rootMargin: '40% 0px' });
  initObserver.observe(root);

  addEventListener('resize', () => {
    if (isInteractive()) render(true);
  });
  reducedMotion.addEventListener?.('change', () => {
    state.velocity = 0;
    state.renderPending = false;
    stopAnimationFrame();
    if (isInteractive()) render(true);
    syncVideoPlayback();
  });
})();
