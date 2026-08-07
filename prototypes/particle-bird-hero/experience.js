import * as THREE from 'three';

const canvas = document.querySelector('[data-particle-stage]');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const isMobile = matchMedia('(max-width: 700px)').matches;
const status = { ready: false, points: 0, reducedMotion: reducedMotion.matches, morph: 0, pointerStrength: 0 };
window.__particleBirdStatus = status;
window.__particleBirdCheck = () => status.ready && status.points > 0 && canvas.width > 0;

start().catch((error) => {
  document.body.classList.add('webgl-fallback');
  document.querySelector('.load-message').textContent = '粒子暂时沉睡，正文仍可阅读。';
  status.error = String(error);
  console.error('Particle bird unavailable:', error);
});

async function start() {
  const response = await fetch('./public/assets/bird-points.bin');
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

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(bird, 3));
  geometry.setAttribute('aScatter', new THREE.BufferAttribute(scatter, 3));
  geometry.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));
  geometry.setDrawRange(0, isMobile ? Math.min(24576, count) : count);

  const uniforms = {
    uTime: { value: 0 },
    uMorph: { value: reducedMotion.matches ? 1 : 0 },
    uPointer: { value: new THREE.Vector3(99, 99, 0) },
    uPointerStrength: { value: 0 },
    uPixelRatio: { value: 1 },
  };

  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader: `
      uniform float uTime;
      uniform float uMorph;
      uniform float uPointerStrength;
      uniform float uPixelRatio;
      uniform vec3 uPointer;
      attribute vec3 aScatter;
      attribute float aSeed;
      varying float vAlpha;
      varying float vGold;
      varying float vEnergy;

      void main() {
        float stagger = aSeed * .17;
        float formation = smoothstep(stagger, .82 + stagger, uMorph);
        vec3 bird = position;
        bird.y += sin(uTime * .72 + aSeed * 18.0) * .012 * formation;
        vec3 transformed = mix(aScatter, bird, formation);

        vec2 delta = transformed.xy - uPointer.xy;
        float distanceToPointer = length(delta);
        float repel = pow(smoothstep(.28, .01, distanceToPointer), 2.0) * uPointerStrength * formation;
        vec2 direction = normalize(delta + vec2(.001));
        vec2 tangent = vec2(-direction.y, direction.x);
        transformed.xy += direction * repel * (.018 + aSeed * .014);
        transformed.xy += tangent * repel * (aSeed - .5) * .028;
        transformed.z += (aSeed - .5) * repel * .035;

        vec4 modelPosition = modelMatrix * vec4(transformed, 1.0);
        vec4 viewPosition = viewMatrix * modelPosition;
        gl_Position = projectionMatrix * viewPosition;
        gl_PointSize = (1.25 + aSeed * 2.35 + formation * .85 + repel * 1.1) * uPixelRatio * (8.0 / -viewPosition.z);
        vAlpha = mix(.3, 1.0, formation) * (.62 + aSeed * .38);
        vGold = smoothstep(.72, .98, aSeed + position.y * .055);
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
  birdPoints.rotation.set(-.04, -.72, -.03);
  birdPoints.scale.setScalar(isMobile ? .72 : .88);
  birdPoints.position.set(isMobile ? .48 : 1.25, .05, 0);
  scene.add(birdPoints);

  const stars = makeStars(isMobile ? 420 : 1100, random);
  scene.add(stars);

  const raycaster = new THREE.Raycaster();
  const pointerNdc = new THREE.Vector2();
  const pointerTarget = new THREE.Vector3(99, 99, 0);
  const pointerCurrent = pointerTarget.clone();
  const interactionPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
  let pointerStrengthTarget = 0;
  let scrollTarget = reducedMotion.matches ? 1 : 0;
  let morph = scrollTarget;

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

  function updateScroll() {
    const range = Math.max(1, document.documentElement.scrollHeight - innerHeight);
    const progress = Math.min(1, Math.max(0, scrollY / range));
    scrollTarget = reducedMotion.matches ? 1 : smoothstep(.04, .78, progress);
    document.body.classList.toggle('has-scrolled', scrollY > 20);
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
    morph += (scrollTarget - morph) * (reducedMotion.matches ? 1 : .065);
    pointerCurrent.lerp(pointerTarget, .16);
    uniforms.uPointer.value.copy(pointerCurrent);
    uniforms.uPointerStrength.value += (pointerStrengthTarget - uniforms.uPointerStrength.value) * .12;
    uniforms.uTime.value = reducedMotion.matches ? 0 : now * .001;
    uniforms.uMorph.value = morph;
    birdPoints.rotation.y = -.72 + morph * .12 + pointerNdc.x * .018;
    birdPoints.rotation.x = -.04 - pointerNdc.y * .012;
    stars.rotation.z = now * .000004;
    status.morph = Number(morph.toFixed(3));
    status.pointerStrength = Number(uniforms.uPointerStrength.value.toFixed(3));
    renderer.render(scene, camera);
    requestAnimationFrame(render);
  }

  status.ready = true;
  status.points = geometry.drawRange.count;
  document.body.classList.add('is-ready');
  document.querySelector('.load-message').textContent = '微光已醒来';
  updateScroll();
  resize();
  requestAnimationFrame(render);
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

function smoothstep(edge0, edge1, value) {
  const x = Math.min(1, Math.max(0, (value - edge0) / (edge1 - edge0)));
  return x * x * (3 - 2 * x);
}
