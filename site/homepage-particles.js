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
  const mix = (from, to, amount) => from + (to - from) * amount;
  const ease = value => value * value * (3 - 2 * value);

  for (let index = 0; index < 1400; index += 1) {
    const bird = index < 980 ? makeBirdPoint() : null;
    particles.push({ x: random(), y: random(), depth: random(), phase: random() * Math.PI * 2, bird });
  }

  window.__homepageParticlesStatus = { ready: false, points: particles.length, birdPoints: 980, scroll: 0, birdMix: 0, birdPose: 'stars' };
  window.__homepageParticlesCheck = () => window.__homepageParticlesStatus.ready && canvas.width > 0;

  function makeBirdPoint() {
    const pick = random();
    if (pick < .6) return { part: 'wing', side: random() > .5 ? 1 : -1, u: Math.pow(random(), .72), v: random(), grain: random() - .5 };
    if (pick < .79) return { part: 'body', angle: random() * Math.PI * 2, radius: Math.sqrt(random()) };
    if (pick < .9) return { part: 'tail', side: random() > .5 ? 1 : -1, u: random(), v: random() };
    if (pick < .98) return { part: 'head', angle: random() * Math.PI * 2, radius: Math.sqrt(random()) };
    return { part: 'beak', u: random(), v: random() };
  }

  function birdPointAt(point, pose) {
    if (point.part === 'wing') {
      const span = [ .39, .245, .13 ][pose];
      const sweep = [ .18, .37, .42 ][pose];
      const taper = Math.sin(point.u * Math.PI);
      return {
        x: .08 - sweep * point.u + (point.v - .5) * .13 * taper + point.grain * .012,
        y: point.side * (.045 + span * point.u + (point.v - .5) * .09 * taper)
      };
    }
    if (point.part === 'body') return { x: .01 + Math.cos(point.angle) * .27 * point.radius, y: Math.sin(point.angle) * .072 * point.radius };
    if (point.part === 'tail') return { x: -.24 - point.u * .23, y: point.side * (.025 + point.u * mix(.16, .07, pose / 2) * point.v) };
    if (point.part === 'head') return { x: .29 + Math.cos(point.angle) * .068 * point.radius, y: Math.sin(point.angle) * .058 * point.radius };
    return { x: .35 + point.u * .13, y: (point.v - .5) * .035 * (1 - point.u) };
  }

  function birdTarget(point, progress) {
    const phase = Math.min(2.999, progress * 3);
    const order = [0, 1, 2, 0];
    const step = Math.floor(phase);
    const amount = ease(phase - step);
    const from = birdPointAt(point, order[step]);
    const to = birdPointAt(point, order[step + 1]);
    const scale = Math.min(width * .78, height * .96);
    const centerX = width * (.61 - Math.sin(progress * Math.PI) * .08);
    const centerY = height * (.52 + Math.cos(progress * Math.PI * 2) * .035);
    return { x: centerX + mix(from.x, to.x, amount) * scale, y: centerY + mix(from.y, to.y, amount) * scale };
  }

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
    const birdIn = ease(Math.min(1, Math.max(0, (scroll - .055) / .085)));
    const birdOut = 1 - ease(Math.min(1, Math.max(0, (scroll - .84) / .12)));
    const birdMix = birdIn * birdOut;
    const birdProgress = Math.min(1, Math.max(0, (scroll - .11) / .69));
    context.fillStyle = '#e8edf4';
    for (const particle of particles) {
      let x = (particle.x * width + scrollFlow * (.22 + particle.depth)) % (width + 80) - 40;
      let y = particle.y * height + Math.sin(particle.phase + scroll * 20 + now * (.00008 + particle.depth * .00009)) * (16 + particle.depth * 54);
      y = (y + height) % height;
      if (particle.bird && birdMix > .001) {
        const target = birdTarget(particle.bird, birdProgress);
        x = mix(x, target.x, birdMix);
        y = mix(y, target.y, birdMix);
      }
      const dx = x - pointer.x;
      const dy = y - pointer.y;
      const distance = Math.hypot(dx, dy);
      if (distance < radius) {
        const force = Math.pow(1 - distance / radius, 2) * 54 * pointer.energy;
        x += dx / Math.max(distance, 1) * force;
        y += dy / Math.max(distance, 1) * force;
      }
      context.globalAlpha = .08 + particle.depth * .34 + (particle.bird ? birdMix * .34 : 0);
      const size = .55 + particle.depth * 1.25 + (particle.bird ? birdMix * .42 : 0);
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
    window.__homepageParticlesStatus.birdMix = Number(birdMix.toFixed(3));
    window.__homepageParticlesStatus.birdPose = birdMix < .05 ? 'stars' : ['spread', 'glide', 'fold', 'spread'][Math.min(3, Math.floor(birdProgress * 4))];
    canvas.dataset.scroll = String(window.__homepageParticlesStatus.scroll);
    canvas.dataset.bird = window.__homepageParticlesStatus.birdPose;
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
