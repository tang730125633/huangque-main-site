(() => {
  const canvas = document.querySelector('[data-hero-particles]');
  const reducedMotion = matchMedia('(prefers-reduced-motion:reduce)');
  const compact = matchMedia('(max-width:880px)');
  if (!canvas || reducedMotion.matches || compact.matches) return;

  const context = canvas.getContext('2d');
  if (!context) return;

  const pointer = { x: -999, y: -999, targetX: -999, targetY: -999, energy: 0 };
  const particles = [];
  let randomState = 0x48515145;
  let width = 0;
  let height = 0;
  let ratio = 1;
  let scroll = 0;
  let scrollTarget = 0;
  let frame = 0;
  let visible = !document.hidden;
  const random = () => ((randomState = (1664525 * randomState + 1013904223) >>> 0) / 0x100000000);

  for (let index = 0; index < 1400; index += 1) {
    particles.push({ x: random(), y: random(), depth: random(), phase: random() * Math.PI * 2 });
  }

  window.__homepageParticlesStatus = { ready: false, points: particles.length, scroll: 0 };
  window.__homepageParticlesCheck = () => window.__homepageParticlesStatus.ready && canvas.width > 0;

  function resize() {
    ratio = Math.min(devicePixelRatio || 1, 1.5);
    width = innerWidth;
    height = innerHeight;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function updateScroll() {
    const range = Math.max(1, document.documentElement.scrollHeight - innerHeight);
    scrollTarget = Math.min(1, Math.max(0, scrollY / range));
  }

  function render(now) {
    frame = 0;
    if (!visible) return;
    context.clearRect(0, 0, width, height);
    scroll += (scrollTarget - scroll) * .075;
    pointer.x += (pointer.targetX - pointer.x) * .16;
    pointer.y += (pointer.targetY - pointer.y) * .16;
    pointer.energy += ((pointer.targetX > -100 ? 1 : 0) - pointer.energy) * .1;

    const radius = Math.min(160, width * .12);
    const scrollFlow = scroll * width * 2.4;
    context.fillStyle = '#e8edf4';
    for (const particle of particles) {
      let x = (particle.x * width + scrollFlow * (.22 + particle.depth)) % (width + 80) - 40;
      let y = particle.y * height + Math.sin(particle.phase + scroll * 20 + now * (.00008 + particle.depth * .00009)) * (16 + particle.depth * 54);
      y = (y + height) % height;
      const dx = x - pointer.x;
      const dy = y - pointer.y;
      const distance = Math.hypot(dx, dy);
      if (distance < radius) {
        const force = Math.pow(1 - distance / radius, 2) * 54 * pointer.energy;
        x += dx / Math.max(distance, 1) * force;
        y += dy / Math.max(distance, 1) * force;
      }
      context.globalAlpha = .08 + particle.depth * .34;
      const size = .55 + particle.depth * 1.25;
      context.fillRect(x, y, size, size);
    }

    if (pointer.energy > .01) {
      const glow = context.createRadialGradient(pointer.x, pointer.y, 0, pointer.x, pointer.y, radius);
      glow.addColorStop(0, `rgba(245,248,252,${.11 * pointer.energy})`);
      glow.addColorStop(.36, `rgba(204,220,235,${.045 * pointer.energy})`);
      glow.addColorStop(1, 'rgba(204,220,235,0)');
      context.globalAlpha = 1;
      context.fillStyle = glow;
      context.fillRect(pointer.x - radius, pointer.y - radius, radius * 2, radius * 2);
      context.lineWidth = 1;
      context.strokeStyle = `rgba(245,248,252,${.2 * pointer.energy})`;
      context.beginPath();
      context.arc(pointer.x, pointer.y, 26 + Math.sin(now * .004) * 3, 0, Math.PI * 2);
      context.stroke();
      context.strokeStyle = `rgba(204,220,235,${.09 * pointer.energy})`;
      context.beginPath();
      context.arc(pointer.x, pointer.y, 50 + Math.sin(now * .0025) * 6, 0, Math.PI * 2);
      context.stroke();
    }

    context.globalAlpha = 1;
    window.__homepageParticlesStatus.scroll = Number(scroll.toFixed(3));
    canvas.dataset.scroll = String(window.__homepageParticlesStatus.scroll);
    frame = requestAnimationFrame(render);
  }

  function start() {
    if (!frame && visible) frame = requestAnimationFrame(render);
  }

  addEventListener('pointermove', event => {
    if (event.pointerType === 'touch') return;
    pointer.targetX = event.clientX;
    pointer.targetY = event.clientY;
  }, { passive: true });
  document.documentElement.addEventListener('pointerleave', () => { pointer.targetX = pointer.targetY = -999; }, { passive: true });
  addEventListener('scroll', updateScroll, { passive: true });
  addEventListener('resize', resize);
  document.addEventListener('visibilitychange', () => {
    visible = !document.hidden;
    if (visible) start();
    else if (frame) { cancelAnimationFrame(frame); frame = 0; }
  });

  resize();
  updateScroll();
  canvas.dataset.ready = 'true';
  canvas.dataset.points = String(particles.length);
  window.__homepageParticlesStatus.ready = true;
  document.documentElement.classList.add('page-particles-ready');
  console.assert(window.__homepageParticlesCheck(), 'Homepage particle background is incomplete');
  start();
})();
