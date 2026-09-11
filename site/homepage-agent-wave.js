(() => {
  'use strict';

  const video = document.querySelector('[data-hero-scrub]');
  const story = document.querySelector('[data-agent-wave-story]');
  const overlay = document.querySelector('[data-agent-wave-overlay]');
  const overlayContext = overlay?.getContext('2d');
  const beats = [...document.querySelectorAll('[data-agent-beat]')];
  const steps = [...document.querySelectorAll('[data-agent-step]')];
  const indexes = [...document.querySelectorAll('[data-agent-index]')];
  const result = document.querySelector('.agent-wave-result-card');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const anchors = [[0, 0], [.32, 3.5], [.67, 7.8], [1, 12.1]];
  const status = { ready: false, mode: 'ambient-loop', progress: 0, videoTime: 0, chapter: 0, overlayPoints: 0 };
  window.__homepageAgentWaveStatus = status;
  window.__homepageAgentWaveCheck = () => status.ready && video?.readyState >= 1 && video.autoplay && overlay?.width > 0 && status.overlayPoints > 0;

  if (!video || !story) return;

  let targetProgress = 0;
  let progress = 0;
  let frame = 0;
  let scrubbing = false;
  let overlayWidth = 0;
  let overlayHeight = 0;
  const clamp = value => Math.min(1, Math.max(0, value));

  function resizeOverlay() {
    if (!overlay || !overlayContext) return;
    const ratio = Math.min(devicePixelRatio || 1, innerWidth < 700 ? 1 : 1.4);
    overlayWidth = innerWidth;
    overlayHeight = innerHeight;
    overlay.width = Math.round(overlayWidth * ratio);
    overlay.height = Math.round(overlayHeight * ratio);
    overlayContext.setTransform(ratio, 0, 0, ratio, 0, 0);
    status.overlayPoints = 3 * (innerWidth < 880 ? 80 : 128);
  }

  function drawOverlay(now) {
    if (!overlayContext || !overlayWidth || !overlayHeight) return;
    overlayContext.clearRect(0, 0, overlayWidth, overlayHeight);
    overlayContext.globalCompositeOperation = 'screen';
    const time = reduced.matches ? 0 : now * .001;
    const mobile = innerWidth < 880;
    const left = overlayWidth * (mobile ? .18 : .42);
    const span = overlayWidth * (mobile ? .94 : .66);
    const count = mobile ? 80 : 128;
    const waves = [
      { y: .28, amp: 22, freq: 1.7, speed: .24, phase: 0, color: '220,225,224', alpha: .26, drift: -12 },
      { y: .5, amp: 18, freq: 2.4, speed: -.18, phase: 1.7, color: '216,164,91', alpha: .2 + progress * .4, drift: 18 },
      { y: .66, amp: 32, freq: 1.25, speed: .12, phase: 3.1, color: '174,180,178', alpha: .18, drift: 30 },
    ];

    waves.forEach((wave, waveIndex) => {
      overlayContext.beginPath();
      for (let index = 0; index < count; index += 1) {
        const amount = index / (count - 1);
        const fade = Math.sin(amount * Math.PI);
        const x = left + amount * span + Math.sin(time * .16 + amount * 7 + wave.phase) * 8;
        const y = overlayHeight * wave.y
          + Math.sin(amount * Math.PI * 2 * wave.freq + time * wave.speed + wave.phase + progress * 2.5) * wave.amp
          + Math.sin(amount * 19 - time * .12) * 4
          + wave.drift * progress;
        if (!index) overlayContext.moveTo(x, y);
        else overlayContext.lineTo(x, y);
        overlayContext.fillStyle = `rgba(${wave.color},${wave.alpha * fade})`;
        const size = 1 + ((index * 17 + waveIndex * 13) % 7) / 5;
        const jitter = (((index * 13 + waveIndex * 19) % 11) - 5) * .7;
        overlayContext.fillRect(x, y + jitter, size, size);
      }
      overlayContext.strokeStyle = `rgba(${wave.color},${wave.alpha * .1})`;
      overlayContext.lineWidth = 1;
      overlayContext.stroke();
    });
    overlayContext.globalCompositeOperation = 'source-over';
    status.overlayPoints = waves.length * count;
  }

  function timeAt(value) {
    for (let index = 1; index < anchors.length; index += 1) {
      const [endProgress, endTime] = anchors[index];
      if (value > endProgress) continue;
      const [startProgress, startTime] = anchors[index - 1];
      return startTime + (endTime - startTime) * (value - startProgress) / (endProgress - startProgress);
    }
    return anchors[anchors.length - 1][1];
  }

  function updateScroll() {
    const rect = story.getBoundingClientRect();
    const travel = Math.max(1, story.offsetHeight - innerHeight);
    targetProgress = clamp(-rect.top / travel);
    if (reduced.matches) {
      scrubbing = true;
      status.mode = 'reduced-motion';
      progress = targetProgress;
      video.pause();
      return;
    }
    if (!scrubbing && targetProgress > .012) {
      scrubbing = true;
      status.mode = 'scroll-scrub';
      video.pause();
    } else if (scrubbing && targetProgress < .002) {
      scrubbing = false;
      status.mode = 'ambient-loop';
      video.play().catch(() => { status.autoplayBlocked = true; });
    }
  }

  function setChapter(chapter) {
    if (status.chapter === chapter && beats[chapter]?.classList.contains('is-active')) return;
    status.chapter = chapter;
    beats.forEach((beat, index) => beat.classList.toggle('is-active', index === chapter));
    indexes.forEach((item, index) => {
      item.classList.toggle('is-active', index === chapter);
      if (index === chapter) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
  }

  function sync() {
    const chapter = progress < .32 ? 0 : progress < .67 ? 1 : 2;
    const activeStep = Math.min(3, Math.floor(progress * 4.15));
    const duration = Number.isFinite(video.duration) ? video.duration : anchors[anchors.length - 1][1];
    const targetTime = Math.min(timeAt(progress), Math.max(0, duration - .04));

    setChapter(chapter);
    steps.forEach((step, index) => step.classList.toggle('is-active', index <= activeStep));
    result?.classList.toggle('is-visible', progress > .68);
    document.documentElement.style.setProperty('--agent-wave-progress', progress.toFixed(4));
    document.documentElement.dataset.agentWaveProgress = progress.toFixed(4);
    if (scrubbing && video.readyState >= 1 && !video.seeking && Math.abs(video.currentTime - targetTime) > .03) video.currentTime = targetTime;
    video.dataset.scrubTime = targetTime.toFixed(3);
    status.progress = Number(progress.toFixed(4));
    status.videoTime = Number(targetTime.toFixed(3));
  }

  function render(now = 0) {
    progress += (targetProgress - progress) * (reduced.matches ? 1 : .09);
    sync();
    drawOverlay(now);
    frame = requestAnimationFrame(render);
  }

  video.autoplay = true;
  video.loop = true;
  const markReady = () => {
    sync();
    status.ready = true;
    document.documentElement.dataset.agentWaveReady = 'true';
    if (!scrubbing && !reduced.matches) video.play().catch(() => { status.autoplayBlocked = true; });
    console.assert(window.__homepageAgentWaveCheck(), 'Homepage Agent Wave scrubbing is incomplete');
  };
  resizeOverlay();
  updateScroll();
  if (video.readyState >= 1) markReady();
  else video.addEventListener('loadedmetadata', markReady, { once: true });

  addEventListener('scroll', updateScroll, { passive: true });
  addEventListener('resize', () => { updateScroll(); resizeOverlay(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      cancelAnimationFrame(frame);
      video.pause();
    } else {
      frame = requestAnimationFrame(render);
      if (!scrubbing && !reduced.matches) video.play().catch(() => { status.autoplayBlocked = true; });
    }
  });
  frame = requestAnimationFrame(render);
})();
