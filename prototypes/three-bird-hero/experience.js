import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { animate, stagger } from 'animejs';
import 'animejs/adapters/three';

const root = document.documentElement;
const canvas = document.querySelector('[data-three-stage]');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const status = { ready: false, reducedMotion: reduced.matches, version: 'three-bird-1', meshes: 0, scrollProgress: 0 };
window.__threeBirdStatus = status;
window.__threeBirdCheck = () => status.ready && status.meshes >= 40 && canvas.width > 0;

let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: 'high-performance' });
} catch (error) {
  root.classList.add('webgl-fallback');
  console.warn('Three.js bird unavailable:', error);
}

if (renderer) {
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.22;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, innerWidth / innerHeight, .1, 40);
  camera.position.set(0, 0, 9);

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(innerWidth, innerHeight), .42, .58, .3);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  scene.add(new THREE.HemisphereLight(0x6d84ff, 0x08030f, 1.9));
  const key = new THREE.DirectionalLight(0xc6ddff, 4.4);
  key.position.set(-4, 5, 6);
  scene.add(key);
  const blueLight = new THREE.PointLight(0x245cff, 32, 14, 2);
  blueLight.position.set(2, -2, 4);
  scene.add(blueLight);
  const violetLight = new THREE.PointLight(0x8f35ff, 25, 12, 2);
  violetLight.position.set(2, 3, -2);
  scene.add(violetLight);
  const amberLight = new THREE.PointLight(0xffa12c, 20, 10, 2);
  amberLight.position.set(-3, -1, 3);
  scene.add(amberLight);

  const glass = new THREE.MeshPhysicalMaterial({
    color: 0x183bce,
    metalness: .08,
    roughness: .2,
    transmission: .3,
    thickness: 1.1,
    ior: 1.38,
    clearcoat: 1,
    clearcoatRoughness: .1,
    iridescence: 1,
    iridescenceIOR: 1.6,
    iridescenceThicknessRange: [120, 520],
    attenuationColor: new THREE.Color(0x2148ff),
    attenuationDistance: 1.6,
    transparent: true,
    opacity: .96,
    side: THREE.DoubleSide
  });
  const violetGlass = glass.clone();
  violetGlass.color.set(0x5422c8);
  violetGlass.attenuationColor.set(0x7b35ff);
  const amberGlass = glass.clone();
  amberGlass.color.set(0xbd5e13);
  amberGlass.attenuationColor.set(0xff9b22);
  amberGlass.transmission = .24;

  if (!reduced.matches) {
    [glass, violetGlass, amberGlass].forEach(material => { material.opacity = .06; });
    key.intensity = 0;
    bloom.strength = 0;
    animate([glass, violetGlass, amberGlass], { opacity: .96, duration: 1600, delay: stagger(130), ease: 'out(3)' });
    animate(key, { intensity: 4.4, duration: 1800, ease: 'outExpo' });
    animate(bloom, { strength: .42, duration: 1800, ease: 'outExpo' });
  }

  function featherGeometry(length, width) {
    const shape = new THREE.Shape();
    shape.moveTo(0, 0);
    shape.bezierCurveTo(width * .66, length * .22, width * .52, length * .7, 0, length);
    shape.bezierCurveTo(-width * .46, length * .72, -width * .58, length * .2, 0, 0);
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth: .055,
      bevelEnabled: true,
      bevelSegments: 2,
      bevelSize: .025,
      bevelThickness: .018,
      curveSegments: 6
    });
    geometry.translate(0, 0, -.0275);
    return geometry;
  }

  const bird = new THREE.Group();
  const body = new THREE.Mesh(new THREE.SphereGeometry(1, 48, 32), glass);
  body.scale.set(1.5, .5, .56);
  body.rotation.z = -.12;
  bird.add(body);

  const chest = new THREE.Mesh(new THREE.SphereGeometry(.56, 36, 24), violetGlass);
  chest.position.set(-.7, -.04, .04);
  chest.scale.set(1.3, .76, .84);
  bird.add(chest);

  const head = new THREE.Mesh(new THREE.SphereGeometry(.43, 36, 24), glass);
  head.position.set(-1.38, .18, .02);
  head.scale.set(1.04, .95, .96);
  bird.add(head);

  const beak = new THREE.Mesh(new THREE.ConeGeometry(.17, .7, 5), amberGlass);
  beak.position.set(-1.84, .13, .01);
  beak.rotation.z = Math.PI / 2;
  bird.add(beak);

  const eye = new THREE.Mesh(new THREE.SphereGeometry(.09, 18, 12), new THREE.MeshPhysicalMaterial({ color: 0x02030a, roughness: .08, clearcoat: 1 }));
  eye.position.set(-1.5, .29, .36);
  bird.add(eye);
  const iris = new THREE.Mesh(new THREE.SphereGeometry(.037, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffc35a }));
  iris.position.set(-1.525, .3, .43);
  bird.add(iris);

  function makeWing(mirrored = false) {
    const wing = new THREE.Group();
    for (let index = 0; index < 14; index += 1) {
      const amount = index / 13;
      const length = 1.25 + Math.sin(amount * Math.PI) * 1.3 + amount * .3;
      const feather = new THREE.Mesh(featherGeometry(length, .28 + amount * .05), index % 5 === 0 ? amberGlass : index % 2 ? violetGlass : glass);
      feather.position.set(.22 - amount * .66, .05 + amount * .08, index * .012);
      feather.rotation.z = THREE.MathUtils.lerp(-.55, .52, amount);
      feather.rotation.x = (amount - .5) * .12;
      wing.add(feather);
    }
    wing.position.set(.12, .12, mirrored ? -.34 : .2);
    wing.scale.set(1, mirrored ? -.74 : 1, mirrored ? .86 : 1);
    wing.rotation.x = mirrored ? -.18 : .08;
    return wing;
  }

  const upperWing = makeWing(false);
  const lowerWing = makeWing(true);
  upperWing.rotation.z = -.66;
  lowerWing.rotation.z = .56;
  bird.add(lowerWing, upperWing);

  const upperCover = new THREE.Mesh(new THREE.SphereGeometry(.78, 32, 20), glass);
  upperCover.position.set(.18, .34, .17);
  upperCover.scale.set(1.12, .5, .2);
  upperCover.rotation.z = -.06;
  bird.add(upperCover);
  const lowerCover = new THREE.Mesh(new THREE.SphereGeometry(.68, 32, 20), violetGlass);
  lowerCover.position.set(.18, -.28, -.2);
  lowerCover.scale.set(1.06, .48, .18);
  lowerCover.rotation.z = .08;
  bird.add(lowerCover);

  const tail = new THREE.Group();
  for (let index = 0; index < 9; index += 1) {
    const amount = index / 8;
    const feather = new THREE.Mesh(featherGeometry(1.35 + Math.sin(amount * Math.PI) * .42, .22), index === 4 ? amberGlass : index % 2 ? violetGlass : glass);
    feather.rotation.z = -Math.PI / 2 + THREE.MathUtils.lerp(-.28, .28, amount);
    feather.position.set(.8, -.18 + amount * .09, (amount - .5) * .18);
    tail.add(feather);
  }
  bird.add(tail);

  const flight = new THREE.Group();
  flight.add(bird);
  scene.add(flight);

  const glowTexture = (() => {
    const textureCanvas = document.createElement('canvas');
    textureCanvas.width = textureCanvas.height = 64;
    const context = textureCanvas.getContext('2d');
    const gradient = context.createRadialGradient(32, 32, 0, 32, 32, 32);
    gradient.addColorStop(0, 'rgba(255,255,255,1)');
    gradient.addColorStop(.22, 'rgba(98,135,255,.85)');
    gradient.addColorStop(1, 'rgba(40,70,255,0)');
    context.fillStyle = gradient;
    context.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(textureCanvas);
  })();

  const starsGeometry = new THREE.BufferGeometry();
  const stars = new Float32Array(1800 * 3);
  for (let index = 0; index < 1800; index += 1) {
    const seed = index * 12.9898;
    stars[index * 3] = (Math.sin(seed) * 43758.5453 % 1) * 18;
    stars[index * 3 + 1] = (Math.sin(seed + 3.1) * 24634.6345 % 1) * 11;
    stars[index * 3 + 2] = -3 - Math.abs(Math.sin(seed + 7.4) * 8);
  }
  starsGeometry.setAttribute('position', new THREE.BufferAttribute(stars, 3));
  const starField = new THREE.Points(starsGeometry, new THREE.PointsMaterial({ color: 0x6688ff, size: .085, map: glowTexture, transparent: true, opacity: .76, depthWrite: false, blending: THREE.AdditiveBlending }));
  scene.add(starField);

  for (let index = 0; index < 5; index += 1) {
    const curve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(1.1, (index - 2) * .18, -.5 + index * .08),
      new THREE.Vector3(2.5, (index - 2) * .3, -1),
      new THREE.Vector3(4.6, Math.sin(index) * .7, -2.2),
      new THREE.Vector3(7, (index - 2) * .55, -3.2)
    ]);
    const trail = new THREE.Mesh(new THREE.TubeGeometry(curve, 44, .012 + index * .005, 6, false), new THREE.MeshBasicMaterial({ color: index === 2 ? 0xffa33a : index % 2 ? 0x8d4dff : 0x2c6dff, transparent: true, opacity: .34, blending: THREE.AdditiveBlending, depthWrite: false }));
    scene.add(trail);
  }

  const scenes = [
    { at: 0, x: 1.72, y: -.12, z: 0, scale: .94, ry: -.16, rz: -.04 },
    { at: .2, x: -1.2, y: .45, z: -1, scale: .72, ry: .42, rz: -.08 },
    { at: .42, x: 1.1, y: -.25, z: -.3, scale: .82, ry: -.38, rz: .04 },
    { at: .64, x: -1.45, y: .2, z: -1.25, scale: .67, ry: .5, rz: -.08 },
    { at: .84, x: .8, y: -.35, z: -.45, scale: .84, ry: -.3, rz: .04 },
    { at: 1, x: -2.4, y: .7, z: -2.4, scale: .48, ry: .7, rz: -.1 }
  ];
  const lerp = THREE.MathUtils.lerp;
  let scrollTarget = 0;
  let scrollProgress = 0;
  let pointerTargetX = 0;
  let pointerTargetY = 0;
  let pointerX = 0;
  let pointerY = 0;
  let frame = 0;

  function applyScene(progress) {
    const end = scenes.findIndex(item => item.at >= progress);
    const nextIndex = end < 0 ? scenes.length - 1 : end;
    const previous = scenes[Math.max(0, nextIndex - 1)];
    const next = scenes[nextIndex];
    const amount = previous === next ? 0 : (progress - previous.at) / (next.at - previous.at);
    const compact = innerWidth < 700;
    const x = lerp(previous.x, next.x, amount);
    const y = lerp(previous.y, next.y, amount);
    flight.position.set(compact ? x * .2 + .25 : x, compact ? y - .52 : y, lerp(previous.z, next.z, amount));
    flight.scale.setScalar(lerp(previous.scale, next.scale, amount) * (compact ? .55 : 1));
    flight.rotation.y = lerp(previous.ry, next.ry, amount) + pointerX * .22;
    flight.rotation.z = lerp(previous.rz, next.rz, amount) - pointerY * .06;
    root.style.setProperty('--scene-hue', (progress * 18 - 7).toFixed(3));
    root.style.setProperty('--scene-glow', (1 + Math.sin(progress * Math.PI * 4) * .16).toFixed(3));
    status.scrollProgress = Number(progress.toFixed(4));
  }

  function updateScroll() {
    const range = Math.max(1, document.documentElement.scrollHeight - innerHeight);
    scrollTarget = Math.min(1, Math.max(0, scrollY / range));
  }

  const revealTargets = document.querySelectorAll('.story,.chapter,.closing');
  if (reduced.matches || !('IntersectionObserver' in window)) {
    revealTargets.forEach(target => target.classList.add('is-visible'));
  } else {
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('is-visible');
      observer.unobserve(entry.target);
    }), { threshold: .22 });
    revealTargets.forEach(target => observer.observe(target));
  }
  root.classList.add('motion-ready');

  if (!reduced.matches) {
    addEventListener('pointermove', event => {
      if (event.pointerType === 'touch') return;
      pointerTargetX = event.clientX / innerWidth * 2 - 1;
      pointerTargetY = 1 - event.clientY / innerHeight * 2;
    }, { passive: true });
  }
  addEventListener('scroll', updateScroll, { passive: true });

  function resize() {
    const pixelRatio = Math.min(devicePixelRatio || 1, innerWidth < 700 ? 1 : 1.5);
    renderer.setPixelRatio(pixelRatio);
    composer.setPixelRatio(pixelRatio);
    renderer.setSize(innerWidth, innerHeight, false);
    composer.setSize(innerWidth, innerHeight);
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
  }

  function render(now) {
    const time = reduced.matches ? 0 : now * .001;
    pointerX += (pointerTargetX - pointerX) * .045;
    pointerY += (pointerTargetY - pointerY) * .045;
    scrollProgress += (scrollTarget - scrollProgress) * (reduced.matches ? 1 : .055);
    applyScene(scrollProgress);
    camera.position.x = pointerX * .2;
    camera.position.y = pointerY * .14;
    camera.lookAt(0, 0, 0);
    upperWing.rotation.x = Math.sin(time * 1.25) * .12;
    upperWing.rotation.z = -.66 + Math.sin(time * 1.25) * .035;
    lowerWing.rotation.x = -Math.sin(time * 1.25) * .08;
    lowerWing.rotation.z = .56 - Math.sin(time * 1.25) * .028;
    bird.position.y = Math.sin(time * .85) * .055;
    starField.rotation.z = time * .006;
    blueLight.position.x = 2 + Math.sin(time * .7) * 1.5;
    violetLight.position.y = 3 + Math.cos(time * .6) * 1.2;
    composer.render();
    if (!status.ready) {
      status.meshes = scene.getObjectsByProperty('isMesh', true).length;
      status.ready = true;
      root.classList.add('three-ready');
    }
    frame = requestAnimationFrame(render);
  }

  addEventListener('resize', resize);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelAnimationFrame(frame);
    else frame = requestAnimationFrame(render);
  });
  reduced.addEventListener('change', event => { status.reducedMotion = event.matches; });
  updateScroll();
  resize();
  frame = requestAnimationFrame(render);
}
