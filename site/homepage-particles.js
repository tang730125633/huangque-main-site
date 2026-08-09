import * as THREE from './vendor/three.module.min.js';

const canvas = document.querySelector('[data-hero-particles]');
const particleStory = document.querySelector('[data-particle-story]');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const isMobile = matchMedia('(max-width: 700px)').matches;
const status = { ready: false, points: 0, reducedMotion: reducedMotion.matches, timeline: 0, scene: 'scatter', pointerStrength: 0 };
window.__homepageParticlesStatus = status;
window.__homepageParticlesCheck = () => status.ready && status.points > 0 && canvas.width > 0;

if (!canvas || !particleStory || reducedMotion.matches) {
  document.documentElement.classList.add('particle-story-fallback');
} else {
  start().catch((error) => {
    document.documentElement.classList.add('particle-story-fallback');
    status.error = String(error);
    console.error('Particle bird unavailable:', error);
  });
}

async function start() {
  const response = await fetch('/assets/home/bird-points.bin');
  if (!response.ok) throw new Error(`point asset ${response.status}`);
  const bird = new Float32Array(await response.arrayBuffer());
  if (!bird.length || bird.length % 3) throw new Error('invalid point asset');

  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: false, powerPreference: 'high-performance' });
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, innerWidth / innerHeight, .1, 40);
  camera.position.set(0, 0, 8.8);

  const count = bird.length / 3;
  const scatter = new Float32Array(bird.length);
  const seeds = new Float32Array(count);
  let randomState = 0x48515145;
  const random = () => ((randomState = (1664525 * randomState + 1013904223) >>> 0) / 0x100000000);

  for (let index = 0; index < count; index += 1) {
    const angle = random() * Math.PI * 2;
    const radius = 3.2 + random() * 5.8;
    scatter[index * 3] = Math.cos(angle) * radius + (random() - .5) * 2;
    scatter[index * 3 + 1] = Math.sin(angle) * radius * .62 + (random() - .5) * 4;
    scatter[index * 3 + 2] = (random() - .5) * 7;
    seeds[index] = random();
  }

  const feather = makeFeatherTarget(count, random);
  const flow = makeFlowTarget(count, random);
  const flock = makeFlockTarget(bird, seeds);
  const logo = makeLogoTarget(count, seeds, random);

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(bird, 3));
  geometry.setAttribute('aScatter', new THREE.BufferAttribute(scatter, 3));
  geometry.setAttribute('aFeather', new THREE.BufferAttribute(feather, 3));
  geometry.setAttribute('aFlow', new THREE.BufferAttribute(flow, 3));
  geometry.setAttribute('aFlock', new THREE.BufferAttribute(flock, 3));
  geometry.setAttribute('aLogo', new THREE.BufferAttribute(logo, 3));
  geometry.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));
  geometry.setDrawRange(0, isMobile ? Math.min(24576, count) : count);

  const uniforms = {
    uTime: { value: 0 },
    uTimeline: { value: reducedMotion.matches ? 2 : 0 },
    uPointer: { value: new THREE.Vector3(99, 99, 0) },
    uPointerStrength: { value: 0 },
    uPixelRatio: { value: 1 },
    uTextMask: { value: isMobile ? 0 : 1 },
  };

  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader: `
      uniform float uTime;
      uniform float uTimeline;
      uniform float uPointerStrength;
      uniform float uPixelRatio;
      uniform float uTextMask;
      uniform vec3 uPointer;
      attribute vec3 aScatter;
      attribute vec3 aFeather;
      attribute vec3 aFlow;
      attribute vec3 aFlock;
      attribute vec3 aLogo;
      attribute float aSeed;
      varying float vAlpha;
      varying float vGold;
      varying float vEnergy;

      void main() {
        float rawStage = min(uTimeline, 4.999);
        float stage = floor(rawStage);
        float localProgress = uTimeline >= 5.0 ? 1.0 : fract(rawStage);
        vec3 currentTarget = aScatter;
        vec3 nextTarget = aFeather;
        if (stage > .5) { currentTarget = aFeather; nextTarget = position; }
        if (stage > 1.5) { currentTarget = position; nextTarget = aFlow; }
        if (stage > 2.5) { currentTarget = aFlow; nextTarget = aFlock; }
        if (stage > 3.5) { currentTarget = aFlock; nextTarget = aLogo; }

        float stagger = aSeed * .1;
        float shapeBlend = smoothstep(stagger, .9 + stagger, localProgress);
        vec3 transformed = mix(currentTarget, nextTarget, shapeBlend);
        float formed = smoothstep(.05, .45, uTimeline);
        float flowEnergy = 1.0 - smoothstep(.65, 1.25, abs(uTimeline - 3.0));
        float flockEnergy = 1.0 - smoothstep(.65, 1.2, abs(uTimeline - 4.0));
        transformed.y += sin(uTime * .72 + aSeed * 18.0) * .012 * formed;
        transformed.x += sin(uTime * .45 + aSeed * 24.0) * .055 * flowEnergy;
        transformed.z += cos(uTime * .55 + aSeed * 16.0) * .045 * (flowEnergy + flockEnergy);

        vec2 delta = transformed.xy - uPointer.xy;
        float distanceToPointer = length(delta);
        float repel = pow(smoothstep(.28, .01, distanceToPointer), 2.0) * uPointerStrength;
        vec2 direction = normalize(delta + vec2(.001));
        vec2 tangent = vec2(-direction.y, direction.x);
        transformed.xy += direction * repel * (.018 + aSeed * .014);
        transformed.xy += tangent * repel * (aSeed - .5) * .028;
        transformed.z += (aSeed - .5) * repel * .035;

        vec4 modelPosition = modelMatrix * vec4(transformed, 1.0);
        vec4 viewPosition = viewMatrix * modelPosition;
        gl_Position = projectionMatrix * viewPosition;
        gl_PointSize = (1.25 + aSeed * 2.35 + formed * .85 + repel * 1.1) * uPixelRatio * (8.0 / -viewPosition.z);
        vAlpha = mix(.3, 1.0, formed) * (.62 + aSeed * .38);
        float textSafety = smoothstep(-2.2, -.7, transformed.x);
        vAlpha *= mix(1.0, mix(.06, 1.0, textSafety), uTextMask);
        vGold = max(smoothstep(.72, .98, aSeed + position.y * .055), smoothstep(4.55, 5.0, uTimeline) * .72);
        vEnergy = repel;
      }
    `,
    fragmentShader: `
      varying float vAlpha;
      varying float vGold;
      varying float vEnergy;
      void main() {
        float d = distance(gl_PointCoord, vec2(.5));
        float core = 1.0 - smoothstep(.08, .48, d);
        float halo = 1.0 - smoothstep(.18, .5, d);
        vec3 blue = mix(vec3(.08, .25, .95), vec3(.37, .63, 1.0), core);
        vec3 gold = vec3(1.0, .62, .16);
        vec3 color = mix(blue, gold, vGold * .72);
        color = mix(color, vec3(.72, .9, 1.0), vEnergy * .75);
        gl_FragColor = vec4(color, (core * .82 + halo * .22) * vAlpha);
      }
    `,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });

  const birdPoints = new THREE.Points(geometry, material);
  const visualPath = isMobile ? {
    x: [.2, .12, .38, 0, .08, .12],
    scale: [.66, .65, .72, .62, .62, .55],
  } : {
    x: [1.1, 1.45, 1.25, .65, 1.15, 1.55],
    scale: [.88, .88, .88, .78, .82, .82],
  };
  const rotationYPath = [0, -.12, -.72, 0, -.42, 0];
  const rotationXPath = [0, -.03, -.04, 0, -.02, 0];
  birdPoints.position.set(visualPath.x[0], .05, 0);
  birdPoints.scale.setScalar(visualPath.scale[0]);
  scene.add(birdPoints);

  const stars = makeStars(isMobile ? 420 : 1100, random);
  scene.add(stars);

  const raycaster = new THREE.Raycaster();
  const pointerNdc = new THREE.Vector2();
  const pointerTarget = new THREE.Vector3(99, 99, 0);
  const pointerCurrent = pointerTarget.clone();
  const interactionPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
  let pointerStrengthTarget = 0;
  let scrollTarget = 0;
  let timeline = scrollTarget;
  let opacityTarget = 0;
  let opacity = 0;
  let frame = 0;
  let nearSection = false;
  let pageVisible = !document.hidden;

  addEventListener('pointermove', (event) => {
    if (reducedMotion.matches || event.pointerType === 'touch') return;
    pointerNdc.set(event.clientX / innerWidth * 2 - 1, 1 - event.clientY / innerHeight * 2);
    raycaster.setFromCamera(pointerNdc, camera);
    if (!raycaster.ray.intersectPlane(interactionPlane, pointerTarget)) return;
    birdPoints.worldToLocal(pointerTarget);
    pointerStrengthTarget = 1;
  }, { passive: true });
  addEventListener('pointerout', () => { pointerStrengthTarget = 0; }, { passive: true });
  addEventListener('scroll', updateScroll, { passive: true });
  addEventListener('resize', resize);

  const sectionObserver = new IntersectionObserver(([entry]) => {
    nearSection = entry.isIntersecting;
    if (nearSection) startRendering();
    else canvas.style.opacity = '0';
  }, { rootMargin: '80% 0px' });
  sectionObserver.observe(particleStory);

  document.addEventListener('visibilitychange', () => {
    pageVisible = !document.hidden;
    if (pageVisible) startRendering();
    else if (frame) { cancelAnimationFrame(frame); frame = 0; }
  });

  function updateScroll() {
    const sectionTop = scrollY + particleStory.getBoundingClientRect().top;
    const travel = Math.max(1, particleStory.offsetHeight - innerHeight);
    const rawProgress = (scrollY - sectionTop) / travel;
    const progress = Math.min(1, Math.max(0, rawProgress));
    scrollTarget = progress * 5;
    opacityTarget = smoothstep(-.08, .05, rawProgress) * (1 - smoothstep(.9, 1.03, rawProgress));
  }

  function resize() {
    const ratio = Math.min(devicePixelRatio || 1, isMobile ? 1 : 1.5);
    renderer.setPixelRatio(ratio);
    renderer.setSize(innerWidth, innerHeight, false);
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
    uniforms.uPixelRatio.value = ratio;
  }

  function render(now) {
    frame = 0;
    if (!nearSection || !pageVisible) return;
    timeline += (scrollTarget - timeline) * (reducedMotion.matches ? 1 : .065);
    opacity += (opacityTarget - opacity) * .12;
    canvas.style.opacity = String(opacity * .94);
    pointerCurrent.lerp(pointerTarget, .16);
    uniforms.uPointer.value.copy(pointerCurrent);
    uniforms.uPointerStrength.value += (pointerStrengthTarget - uniforms.uPointerStrength.value) * .12;
    uniforms.uTime.value = reducedMotion.matches ? 0 : now * .001;
    uniforms.uTimeline.value = timeline;
    birdPoints.position.x = samplePath(visualPath.x, timeline);
    birdPoints.scale.setScalar(samplePath(visualPath.scale, timeline));
    birdPoints.rotation.y = samplePath(rotationYPath, timeline) + pointerNdc.x * .014;
    birdPoints.rotation.x = samplePath(rotationXPath, timeline) - pointerNdc.y * .01;
    stars.rotation.z = now * .000004;
    status.timeline = Number(timeline.toFixed(3));
    status.scene = ['scatter', 'feather', 'bird', 'flow', 'flock', 'logo'][Math.min(5, Math.round(timeline))];
    status.pointerStrength = Number(uniforms.uPointerStrength.value.toFixed(3));
    canvas.setAttribute('data-particle-scene', status.scene);
    renderer.render(scene, camera);
    frame = requestAnimationFrame(render);
  }

  function startRendering() {
    updateScroll();
    if (!frame && pageVisible && nearSection) frame = requestAnimationFrame(render);
  }

  status.ready = true;
  status.points = geometry.drawRange.count;
  canvas.dataset.ready = 'true';
  canvas.dataset.points = String(status.points);
  document.documentElement.classList.add('page-particles-ready');
  updateScroll();
  resize();
  console.assert(window.__homepageParticlesCheck(), 'Homepage particle point cloud is incomplete');
}

function makeFeatherTarget(count, random) {
  const target = new Float32Array(count * 3);
  for (let index = 0; index < count; index += 1) {
    const offset = index * 3;
    const along = Math.floor(random() * 180) / 179;
    const y = -2.0 + along * 4.2;
    if (random() < .14) {
      target[offset] = (random() - .5) * .055;
      target[offset + 1] = -2.82 + random() * 5.1;
    } else {
      const side = random() < .5 ? -1 : 1;
      const asymmetry = side < 0 ? 1 : .56 + along * .1;
      const reach = Math.pow(Math.sin(Math.PI * along), .68) * 1.42 * asymmetry * (.84 + random() * .16);
      const distance = .08 + Math.pow(random(), .78) * .92;
      const curve = Math.sin(along * Math.PI) * .2;
      target[offset] = curve + side * reach * distance;
      target[offset + 1] = y - distance * (.38 + (1 - along) * .22) + (random() - .5) * .025;
    }
    target[offset + 2] = (random() - .5) * .12;
  }
  return target;
}

function makeFlowTarget(count, random) {
  const target = new Float32Array(count * 3);
  for (let index = 0; index < count; index += 1) {
    const offset = index * 3;
    const band = index % 4;
    const x = (random() - .5) * 11.5;
    target[offset] = x;
    target[offset + 1] = (band - 1.5) * .92 + Math.sin(x * .72 + band * 1.7) * .2 + (random() - .5) * .16;
    target[offset + 2] = (random() - .5) * .75 + band * .08;
  }
  return target;
}

function makeFlockTarget(bird, seeds) {
  const target = new Float32Array(bird.length);
  const placements = [
    [-3.2, 1.2, -1.2, .2], [-1.7, 1.75, -.4, .24], [0, 1.2, .4, .28],
    [1.8, 1.65, -.2, .22], [3.3, .8, -1.1, .18], [-2.45, -.45, .1, .24],
    [-.65, -.2, .7, .2], [1.05, -.55, .35, .24], [2.7, -.35, -.6, .18],
  ];
  for (let index = 0; index < seeds.length; index += 1) {
    const offset = index * 3;
    const placement = placements[Math.min(placements.length - 1, Math.floor(seeds[index] * placements.length))];
    target[offset] = bird[offset] * placement[3] + placement[0];
    target[offset + 1] = bird[offset + 1] * placement[3] + placement[1];
    target[offset + 2] = bird[offset + 2] * placement[3] + placement[2];
  }
  return target;
}

function makeLogoTarget(count, seeds, random) {
  const canvas = document.createElement('canvas');
  canvas.width = 320;
  canvas.height = 320;
  const context = canvas.getContext('2d', { willReadFrequently: true });
  if (!context) throw new Error('2d canvas unavailable');
  context.fillStyle = '#fff';
  context.font = '700 230px "Songti SC", "Noto Serif CJK SC", serif';
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.fillText('雀', 160, 168);
  const pixels = context.getImageData(0, 0, 320, 320).data;
  const ink = [];
  for (let y = 28; y < 292; y += 2) {
    for (let x = 28; x < 292; x += 2) {
      if (pixels[(y * 320 + x) * 4 + 3] > 96) ink.push([x, y]);
    }
  }
  if (!ink.length) throw new Error('logo target unavailable');

  const target = new Float32Array(count * 3);
  for (let index = 0; index < count; index += 1) {
    const offset = index * 3;
    if (seeds[index] < .18) {
      const angle = random() * Math.PI * 2;
      const radius = 2.28 + (random() - .5) * .06;
      target[offset] = Math.cos(angle) * radius;
      target[offset + 1] = Math.sin(angle) * radius;
    } else {
      const point = ink[Math.floor(random() * ink.length)];
      target[offset] = (point[0] / 320 - .5) * 3.55 + (random() - .5) * .025;
      target[offset + 1] = (.5 - point[1] / 320) * 3.55 + (random() - .5) * .025;
    }
    target[offset + 2] = (random() - .5) * .09;
  }
  return target;
}

function makeStars(count, random) {
  const positions = new Float32Array(count * 3);
  for (let index = 0; index < count; index += 1) {
    positions[index * 3] = (random() - .5) * 20;
    positions[index * 3 + 1] = (random() - .5) * 12;
    positions[index * 3 + 2] = -2 - random() * 9;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  return new THREE.Points(geometry, new THREE.PointsMaterial({ color: 0x4068d8, size: .025, transparent: true, opacity: .52, depthWrite: false }));
}

function samplePath(values, value) {
  const index = Math.min(values.length - 2, Math.floor(Math.max(0, value)));
  const progress = Math.min(1, Math.max(0, value - index));
  return values[index] + (values[index + 1] - values[index]) * smoothstep(0, 1, progress);
}

function smoothstep(edge0, edge1, value) {
  const x = Math.min(1, Math.max(0, (value - edge0) / (edge1 - edge0)));
  return x * x * (3 - 2 * x);
}
