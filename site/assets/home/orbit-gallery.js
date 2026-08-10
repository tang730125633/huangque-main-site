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

  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  const saveData = Boolean(connection?.saveData);
  const lowMemory = Number.isFinite(navigator.deviceMemory) && navigator.deviceMemory <= 4;
  const conserveResources = saveData || lowMemory;
  const allowedImages = /\.(?:avif|gif|jpe?g|png|webp)$/i;
  const allowedVideos = /\.(?:m4v|mov|mp4|webm)$/i;
  const AUTO_SPEED = 0.00016;
  const DRAG_SENSITIVITY = 0.0048;
  const FRAME_INTERVAL = conserveResources ? 32 : 8;
  const VISIBLE_RANGE = conserveResources ? 4.2 : 5.2;

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
    originX: 0,
    startPosition: 0,
    lastPointerX: 0,
    lastPointerAt: 0,
    suppressClickUntil: 0,
    autoResumeAt: 0,
    activeIndex: -1,
    lastFrameAt: performance.now(),
    lastRenderAt: 0,
    renderPending: false
  };

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
      if (items.length < 12) throw new Error('at least 12 valid gallery items are required');
      return items;
    } finally {
      clearTimeout(timeout);
    }
  }

  function createCard(item, index) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `orbit-gallery-card is-${item.type}`;
    card.dataset.galleryIndex = String(index);
    card.setAttribute('aria-label', `预览${item.type === 'video' ? '视频' : '图片'}：${item.alt}`);

    if (item.type === 'video') {
      const video = document.createElement('video');
      video.dataset.src = item.src;
      video.poster = item.poster;
      video.muted = true;
      video.defaultMuted = true;
      video.loop = true;
      video.playsInline = true;
      video.preload = 'none';
      video.disablePictureInPicture = true;
      video.setAttribute('muted', '');
      video.setAttribute('playsinline', '');
      card.append(video);

      const badge = document.createElement('span');
      badge.className = 'orbit-gallery-badge';
      badge.setAttribute('aria-hidden', 'true');
      badge.textContent = 'VIDEO';
      card.append(badge);
    } else {
      const image = new Image();
      image.src = item.src;
      image.alt = '';
      image.decoding = 'async';
      image.loading = index < 6 ? 'eager' : 'lazy';
      card.append(image);
    }
    track.append(card);
    return { card, item, visible: null };
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

  function syncVideoPlayback() {
    const pageCanPlay = state.inViewport
      && document.visibilityState === 'visible'
      && !preview.open
      && !reducedMotion.matches
      && !conserveResources;

    state.cards.forEach((entry, index) => {
      if (entry.item.type !== 'video') return;
      const video = entry.card.querySelector('video');
      const shouldPlay = pageCanPlay && index === state.activeIndex;
      entry.card.classList.toggle('is-playing', shouldPlay);
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
      video.play().catch(() => {});
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
        const active = index === activeIndex;
        entry.card.classList.toggle('is-active', active);
        entry.card.tabIndex = active ? 0 : -1;
      });
      status.textContent = `${activeIndex + 1} / ${state.items.length} · ${state.items[activeIndex].alt}`;
      syncVideoPlayback();
    }
  }

  function cleanupPreview() {
    const video = previewStage.querySelector('video');
    if (video) video.pause();
    previewStage.replaceChildren();
    previewTitle.textContent = '';
    root.focus({ preventScroll: true });
    state.autoResumeAt = performance.now() + 1400;
    syncVideoPlayback();
  }

  function closePreview() {
    if (preview.open && typeof preview.close === 'function') preview.close();
    else {
      preview.removeAttribute('open');
      cleanupPreview();
    }
  }

  function openPreview(item) {
    previewStage.replaceChildren();
    previewTitle.textContent = item.alt;
    let media;
    if (item.type === 'video') {
      media = document.createElement('video');
      media.src = item.src;
      media.poster = item.poster;
      media.controls = true;
      media.playsInline = true;
      media.autoplay = !saveData;
    } else {
      media = new Image();
      media.src = item.src;
      media.alt = item.alt;
      media.decoding = 'async';
    }
    previewStage.append(media);
    if (typeof preview.showModal === 'function') preview.showModal();
    else preview.setAttribute('open', '');
    syncVideoPlayback();
    if (item.type === 'video' && !saveData) media.play().catch(() => {});
  }

  function queueRender() {
    state.renderPending = true;
  }

  root.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    state.tracking = true;
    state.dragging = false;
    state.moved = false;
    state.originX = event.clientX;
    state.startPosition = state.position;
    state.lastPointerX = event.clientX;
    state.lastPointerAt = performance.now();
    state.velocity = 0;
    state.autoResumeAt = performance.now() + 3200;
  });

  root.addEventListener('pointermove', event => {
    if (!state.tracking) return;
    const totalX = event.clientX - state.originX;
    if (!state.dragging && Math.abs(totalX) < 6) return;
    if (!state.dragging) {
      state.dragging = true;
      state.moved = true;
      root.classList.add('is-dragging');
      root.setPointerCapture(event.pointerId);
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
  });

  function finishDrag(event) {
    if (!state.tracking) return;
    state.tracking = false;
    if (!state.dragging) return;
    state.dragging = false;
    root.classList.remove('is-dragging');
    if (root.hasPointerCapture(event.pointerId)) root.releasePointerCapture(event.pointerId);
    if (state.moved) state.suppressClickUntil = performance.now() + 380;
    state.autoResumeAt = performance.now() + 1600;
  }

  root.addEventListener('pointerup', finishDrag);
  root.addEventListener('pointercancel', finishDrag);

  root.addEventListener('wheel', event => {
    if (preview.open || (Math.abs(event.deltaX) <= Math.abs(event.deltaY) && !event.shiftKey)) return;
    const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    state.position += delta * 0.0016;
    state.velocity = delta * 0.000035;
    state.autoResumeAt = performance.now() + 1800;
    queueRender();
    event.preventDefault();
  }, { passive: false });

  root.addEventListener('click', event => {
    const card = event.target.closest('[data-gallery-index]');
    if (!card || performance.now() < state.suppressClickUntil) return;
    const item = state.items[Number(card.dataset.galleryIndex)];
    if (item) openPreview(item);
  });

  root.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      state.velocity = 0;
      state.position += event.key === 'ArrowRight' ? 1 : -1;
      state.autoResumeAt = performance.now() + 1800;
      render(true);
    } else if ((event.key === 'Enter' || event.key === ' ') && state.items[state.activeIndex]) {
      event.preventDefault();
      openPreview(state.items[state.activeIndex]);
    }
  });

  previewClose.addEventListener('click', closePreview);
  preview.addEventListener('click', event => {
    if (event.target === preview) closePreview();
  });
  preview.addEventListener('close', cleanupPreview);
  document.addEventListener('visibilitychange', syncVideoPlayback);

  const visibilityObserver = new IntersectionObserver(entries => {
    state.inViewport = Boolean(entries[0]?.isIntersecting);
    syncVideoPlayback();
  }, { rootMargin: '12% 0px' });
  visibilityObserver.observe(root);

  function animate(now) {
    const elapsed = Math.min(50, now - state.lastFrameAt);
    state.lastFrameAt = now;
    const canMove = state.inViewport
      && !state.tracking
      && !preview.open
      && document.visibilityState === 'visible'
      && !reducedMotion.matches
      && !conserveResources;
    let positionChanged = false;
    if (canMove) {
      if (Math.abs(state.velocity) > 0.00002) {
        state.position += state.velocity * elapsed;
        state.velocity *= Math.pow(0.93, elapsed / 16.67);
        if (Math.abs(state.velocity) <= 0.00002) state.velocity = 0;
        positionChanged = true;
      } else if (now >= state.autoResumeAt) {
        state.position += AUTO_SPEED * elapsed;
        positionChanged = true;
      }
    }
    if ((positionChanged || state.renderPending) && now - state.lastRenderAt >= FRAME_INTERVAL) {
      state.lastRenderAt = now;
      state.renderPending = false;
      render();
    }
    requestAnimationFrame(animate);
  }

  async function init() {
    try {
      state.items = await loadItems();
      state.cards = state.items.map(createCard);
      fallback?.setAttribute('aria-hidden', 'true');
      root.dataset.galleryState = 'ready';
      render(true);
      requestAnimationFrame(animate);
    } catch (_) {
      root.dataset.galleryState = 'fallback';
      status.textContent = '动态画廊暂不可用，已显示静态创作样片。';
    }
  }

  addEventListener('resize', () => render(true));
  reducedMotion.addEventListener?.('change', () => {
    state.velocity = 0;
    render(true);
    syncVideoPlayback();
  });
  init();
})();
