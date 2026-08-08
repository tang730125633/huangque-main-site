(() => {
  const hero = document.querySelector('.hero');
  const canvas = document.querySelector('[data-hero-particles]');
  const finePointer = matchMedia('(pointer:fine)');
  const reducedMotion = matchMedia('(prefers-reduced-motion:reduce)');
  if (!hero || !canvas || !finePointer.matches || reducedMotion.matches) return;

  const context = canvas.getContext('2d');
  if (!context) return;

  const pointer = { x: -999, y: -999, targetX: -999, targetY: -999, energy: 0 };
  let points = null;
  let visible = true;
  let frame = 0;
  let width = 0;
  let height = 0;
  let ratio = 1;

  window.__homepageParticlesStatus = { ready: false, points: 0 };
  window.__homepageParticlesCheck = () => window.__homepageParticlesStatus.ready && canvas.width > 0;

  function resize() {
    const rect = hero.getBoundingClientRect();
    ratio = Math.min(devicePixelRatio || 1, 1.5);
    width = Math.round(rect.width);
    height = Math.round(rect.height);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function render(now) {
    frame = 0;
    if (!visible || !points) return;
    context.clearRect(0, 0, width, height);

    pointer.x += (pointer.targetX - pointer.x) * .16;
    pointer.y += (pointer.targetY - pointer.y) * .16;
    pointer.energy += ((pointer.targetX > -100 ? 1 : 0) - pointer.energy) * .1;

    const scale = Math.min(width, height) * .105 * (1 + Math.sin(now * .0007) * .008);
    const centerX = width * .82;
    const centerY = height * .53;
    const radius = Math.min(150, width * .12);

    if (pointer.energy > .01) {
      const glow = context.createRadialGradient(pointer.x, pointer.y, 0, pointer.x, pointer.y, radius);
      glow.addColorStop(0, `rgba(222,216,255,${.12 * pointer.energy})`);
      glow.addColorStop(.34, `rgba(167,139,250,${.055 * pointer.energy})`);
      glow.addColorStop(1, 'rgba(167,139,250,0)');
      context.fillStyle = glow;
      context.fillRect(pointer.x - radius, pointer.y - radius, radius * 2, radius * 2);
      context.strokeStyle = `rgba(224,218,255,${.2 * pointer.energy})`;
      context.lineWidth = 1;
      context.beginPath();
      context.arc(pointer.x, pointer.y, 25 + Math.sin(now * .004) * 3, 0, Math.PI * 2);
      context.stroke();
      context.strokeStyle = `rgba(167,139,250,${.1 * pointer.energy})`;
      context.beginPath();
      context.arc(pointer.x, pointer.y, 48 + Math.sin(now * .0025) * 6, 0, Math.PI * 2);
      context.stroke();
    }

    context.fillStyle = '#dce8ff';
    for (let index = 0; index < points.length; index += 3) {
      let x = centerX + points[index] * scale;
      let y = centerY - points[index + 1] * scale;
      const dx = x - pointer.x;
      const dy = y - pointer.y;
      const distance = Math.hypot(dx, dy);
      if (distance < radius) {
        const force = Math.pow(1 - distance / radius, 2) * 46 * pointer.energy;
        x += dx / Math.max(distance, 1) * force;
        y += dy / Math.max(distance, 1) * force;
      }
      context.globalAlpha = .25 + (points[index + 2] + 2.3) / 4.6 * .45;
      const size = index % 33 === 0 ? 1.7 : 1.05;
      context.fillRect(x, y, size, size);
    }

    context.globalAlpha = 1;
    frame = requestAnimationFrame(render);
  }

  function start() {
    resize();
    if (!frame) frame = requestAnimationFrame(render);
  }

  hero.addEventListener('pointermove', event => {
    const rect = hero.getBoundingClientRect();
    pointer.targetX = event.clientX - rect.left;
    pointer.targetY = event.clientY - rect.top;
  }, { passive: true });
  hero.addEventListener('pointerleave', () => {
    pointer.targetX = -999;
    pointer.targetY = -999;
  }, { passive: true });
  addEventListener('resize', resize);
  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    if (visible) start();
    else if (frame) { cancelAnimationFrame(frame); frame = 0; }
  }).observe(hero);

  fetch('/assets/home/bird-points-lite.bin')
    .then(response => {
      if (!response.ok) throw new Error(`particle asset ${response.status}`);
      return response.arrayBuffer();
    })
    .then(buffer => {
      points = new Float32Array(buffer);
      if (!points.length || points.length % 3) throw new Error('invalid particle asset');
      canvas.dataset.ready = 'true';
      canvas.dataset.points = String(points.length / 3);
      window.__homepageParticlesStatus = { ready: true, points: points.length / 3 };
      document.documentElement.classList.add('hero-particles-ready');
      start();
      console.assert(window.__homepageParticlesCheck(), 'Homepage particle layer is incomplete');
    })
    .catch(error => console.warn('Homepage particles unavailable:', error));
})();
