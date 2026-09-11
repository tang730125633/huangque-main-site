(() => {
  'use strict';

  const canvas = document.querySelector('[data-agent-wave]');
  const story = document.querySelector('[data-wave-story]');
  const beats = [...document.querySelectorAll('[data-beat]')];
  const steps = [...document.querySelectorAll('[data-step]')];
  const indexes = [...document.querySelectorAll('[data-index]')];
  const result = document.querySelector('.result-card');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const context = canvas?.getContext('2d', { alpha: true });
  const status = { ready: false, progress: 0, chapter: 0, points: 0, reducedMotion: reduced.matches };
  window.__agentWaveStatus = status;
  window.__agentWaveCheck = () => status.ready && status.points > 0 && canvas.width > 0;

  if (!canvas || !story || !context) return;

  let width = 0;
  let height = 0;
  let dpr = 1;
  let columns = 0;
  let rows = 0;
  let targetProgress = 0;
  let progress = 0;
  let targetPointerX = .78;
  let targetPointerY = .54;
  let pointerX = targetPointerX;
  let pointerY = targetPointerY;
  let frame = 0;

  const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, value));
  const smooth = value => value * value * (3 - 2 * value);
  const hash = (x, y) => {
    const value = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
    return value - Math.floor(value);
  };

  function resize() {
    dpr = Math.min(devicePixelRatio || 1, innerWidth < 700 ? 1 : 1.45);
    width = innerWidth;
    height = innerHeight;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    columns = innerWidth < 700 ? 68 : 112;
    rows = innerWidth < 700 ? 32 : 48;
    status.points = columns * rows;
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

  function updateInterface() {
    const chapter = progress < .32 ? 0 : progress < .67 ? 1 : 2;
    const activeStep = Math.min(3, Math.floor(progress * 4.15));
    setChapter(chapter);
    steps.forEach((step, index) => step.classList.toggle('is-active', index <= activeStep));
    result?.classList.toggle('is-visible', progress > .68);
    document.documentElement.style.setProperty('--progress', progress.toFixed(4));
    status.progress = Number(progress.toFixed(4));
  }

  function drawWave(now) {
    context.clearRect(0, 0, width, height);
    const time = reduced.matches ? 0 : now * .00032;
    const formation = smooth(clamp((progress - .03) / .45));
    const goldAmount = smooth(clamp((progress - .5) / .5));
    const centerX = width * (innerWidth < 900 ? .6 : .73);
    const centerY = height * (innerWidth < 900 ? .58 : .54);
    const pointerRadius = Math.min(width, height) * .2;

    for (let row = 0; row < rows; row += 1) {
      const depth = row / Math.max(1, rows - 1);
      const perspective = .42 + depth * .72;
      const horizon = (depth - .5) * height * .58;

      for (let column = 0; column < columns; column += 1) {
        const seed = hash(column, row);
        const x = column / Math.max(1, columns - 1) * 2 - 1;
        const primary = Math.sin(x * 4.15 + depth * 7.4 + time * 2.1);
        const detail = Math.sin(x * 11.5 - depth * 5.7 - time * 1.25) * .28;
        const wave = (primary + detail) * height * (.105 - depth * .035);
        const scatterX = (seed - .5) * width * .92 * (1 - formation);
        const scatterY = (hash(row, column + 9) - .5) * height * .72 * (1 - formation);
        let xPos = centerX + x * width * .58 * perspective + scatterX;
        let yPos = centerY + horizon + wave * formation + scatterY;

        const dx = xPos - pointerX * width;
        const dy = yPos - pointerY * height;
        const distance = Math.hypot(dx, dy);
        if (!reduced.matches && distance < pointerRadius) {
          const force = (1 - distance / pointerRadius) ** 2 * 24;
          xPos += dx / Math.max(distance, 1) * force;
          yPos += dy / Math.max(distance, 1) * force;
        }

        if (xPos < -4 || xPos > width + 4 || yPos < -4 || yPos > height + 4) continue;
        const edgeFade = clamp(1 - Math.abs(x) * .72);
        const textFade = xPos < width * .45 ? .28 : 1;
        context.globalAlpha = (.18 + seed * .62) * edgeFade * textFade;
        context.fillStyle = seed < goldAmount * .62 ? '#f2c84b' : '#e9e9e3';
        const size = (.65 + seed * 1.55 + depth * .8) * (innerWidth < 700 ? .76 : 1);
        context.fillRect(xPos, yPos, size, size);
      }
    }
    context.globalAlpha = 1;
  }

  function render(now = 0) {
    progress += (targetProgress - progress) * (reduced.matches ? 1 : .075);
    pointerX += (targetPointerX - pointerX) * .055;
    pointerY += (targetPointerY - pointerY) * .055;
    updateInterface();
    drawWave(now);
    if (!status.ready) {
      status.ready = true;
      console.assert(window.__agentWaveCheck(), 'Agent Wave canvas is incomplete');
    }
    frame = requestAnimationFrame(render);
  }

  addEventListener('pointermove', event => {
    if (event.pointerType === 'touch' || reduced.matches) return;
    targetPointerX = event.clientX / innerWidth;
    targetPointerY = event.clientY / innerHeight;
  }, { passive: true });
  addEventListener('scroll', updateScroll, { passive: true });
  addEventListener('resize', resize);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelAnimationFrame(frame);
    else frame = requestAnimationFrame(render);
  });
  reduced.addEventListener('change', event => { status.reducedMotion = event.matches; });

  resize();
  updateScroll();
  frame = requestAnimationFrame(render);
})();
