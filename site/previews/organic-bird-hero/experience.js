(() => {
  'use strict';

  const root = document.documentElement;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const reveals = [...document.querySelectorAll('.reveal')];
  const status = { ready: true, reducedMotion: reduced.matches, version: 'organic-bird-1' };
  window.__organicBirdStatus = status;

  if (reduced.matches) {
    reveals.forEach(section => section.classList.add('is-visible'));
    return;
  }

  let targetX = 0;
  let targetY = 0;
  let pointerX = 0;
  let pointerY = 0;
  let frame = 0;

  addEventListener('pointermove', event => {
    if (event.pointerType === 'touch') return;
    targetX = event.clientX / innerWidth * 2 - 1;
    targetY = event.clientY / innerHeight * 2 - 1;
  }, { passive: true });

  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) entry.target.classList.add('is-visible');
    });
  }, { threshold: .16 });
  reveals.forEach(section => observer.observe(section));

  function render() {
    pointerX += (targetX - pointerX) * .055;
    pointerY += (targetY - pointerY) * .055;
    root.style.setProperty('--mx', pointerX.toFixed(4));
    root.style.setProperty('--my', pointerY.toFixed(4));
    frame = requestAnimationFrame(render);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelAnimationFrame(frame);
    else frame = requestAnimationFrame(render);
  });
  reduced.addEventListener('change', event => { status.reducedMotion = event.matches; });
  frame = requestAnimationFrame(render);
})();
