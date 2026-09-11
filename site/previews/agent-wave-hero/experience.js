(() => {
  'use strict';

  const video = document.querySelector('[data-scrub-video]');
  const story = document.querySelector('[data-wave-story]');
  const beats = [...document.querySelectorAll('[data-beat]')];
  const steps = [...document.querySelectorAll('[data-step]')];
  const indexes = [...document.querySelectorAll('[data-index]')];
  const result = document.querySelector('.result-card');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const anchors = [[0, 0], [.32, 3.5], [.67, 7.8], [1, 12.1]];
  const status = { ready: false, mode: 'scroll-scrub', progress: 0, videoTime: 0, chapter: 0 };
  window.__agentWaveStatus = status;
  window.__agentWaveCheck = () => status.ready && video.readyState >= 1 && !video.autoplay;

  if (!video || !story) return;

  let targetProgress = 0;
  let progress = 0;
  let frame = 0;

  const clamp = value => Math.min(1, Math.max(0, value));

  function timeAt(value) {
    for (let index = 1; index < anchors.length; index += 1) {
      const [endProgress, endTime] = anchors[index];
      if (value > endProgress) continue;
      const [startProgress, startTime] = anchors[index - 1];
      const amount = (value - startProgress) / (endProgress - startProgress);
      return startTime + (endTime - startTime) * amount;
    }
    return anchors.at(-1)[1];
  }

  function updateScroll() {
    const rect = story.getBoundingClientRect();
    const travel = Math.max(1, story.offsetHeight - innerHeight);
    targetProgress = clamp(-rect.top / travel);
    if (reduced.matches) progress = targetProgress;
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
    const duration = Number.isFinite(video.duration) ? video.duration : anchors.at(-1)[1];
    const targetTime = Math.min(timeAt(progress), Math.max(0, duration - .04));

    setChapter(chapter);
    steps.forEach((step, index) => step.classList.toggle('is-active', index <= activeStep));
    result?.classList.toggle('is-visible', progress > .68);
    document.documentElement.style.setProperty('--progress', progress.toFixed(4));
    document.documentElement.dataset.scrubProgress = progress.toFixed(4);

    if (video.readyState >= 1 && !video.seeking && Math.abs(video.currentTime - targetTime) > .03) video.currentTime = targetTime;
    video.dataset.scrubTime = targetTime.toFixed(3);
    status.progress = Number(progress.toFixed(4));
    status.videoTime = Number(targetTime.toFixed(3));
  }

  function render() {
    progress += (targetProgress - progress) * (reduced.matches ? 1 : .09);
    sync();
    frame = requestAnimationFrame(render);
  }

  video.autoplay = false;
  video.loop = false;
  video.pause();
  video.addEventListener('play', () => video.pause());
  video.addEventListener('loadedmetadata', () => {
    video.pause();
    sync();
    status.ready = true;
    document.documentElement.dataset.scrubReady = 'true';
    console.assert(window.__agentWaveCheck(), 'Agent Wave video scrubbing is incomplete');
  }, { once: true });
  video.addEventListener('error', () => document.documentElement.classList.add('video-fallback'), { once: true });

  addEventListener('scroll', updateScroll, { passive: true });
  addEventListener('resize', updateScroll);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelAnimationFrame(frame);
    else frame = requestAnimationFrame(render);
  });

  updateScroll();
  frame = requestAnimationFrame(render);
})();
