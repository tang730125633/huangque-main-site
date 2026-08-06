(() => {
  'use strict';

  const root = document.documentElement;
  const canvas = document.querySelector('[data-light-field]');
  const wake = [...document.querySelectorAll('[data-cursor-wake] span')];
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const gl = canvas?.getContext('webgl', { alpha: true, antialias: false, powerPreference: 'high-performance' });
  const status = { ready: false, reducedMotion: reduced.matches, version: 'liquid-bird-1' };
  window.__liquidBirdStatus = status;
  window.__liquidBirdCheck = () => status.ready && canvas.width > 0 && canvas.height > 0;

  let pointerX = 0;
  let pointerY = 0;
  let targetX = 0;
  let targetY = 0;
  let cursorX = innerWidth * .5;
  let cursorY = innerHeight * .5;
  let cursorActive = 0;
  let cursorActiveTarget = 0;
  const wakeX = wake.map(() => cursorX);
  const wakeY = wake.map(() => cursorY);
  const wakeDamping = [.24, .13, .075, .045];
  let scrollProgress = 0;
  let frame = 0;

  function updatePageState() {
    const range = Math.max(1, innerHeight * .9);
    scrollProgress = Math.min(1, Math.max(0, scrollY / range));
    root.style.setProperty('--flight', scrollProgress.toFixed(4));
  }

  if (!reduced.matches) {
    addEventListener('pointermove', event => {
      if (event.pointerType === 'touch') return;
      targetX = event.clientX / innerWidth * 2 - 1;
      targetY = event.clientY / innerHeight * 2 - 1;
      cursorX = event.clientX;
      cursorY = event.clientY;
      cursorActiveTarget = 1;
      root.classList.add('cursor-active');
    }, { passive: true });
    addEventListener('mouseout', event => {
      if (event.relatedTarget) return;
      cursorActiveTarget = 0;
      root.classList.remove('cursor-active');
    });
  }
  addEventListener('scroll', updatePageState, { passive: true });
  updatePageState();

  if (!gl) {
    root.classList.add('webgl-fallback');
    return;
  }

  const vertexSource = `
    attribute vec2 aPosition;
    void main(){ gl_Position=vec4(aPosition,0.,1.); }
  `;
  const fragmentSource = `
    precision highp float;
    uniform vec2 uResolution;
    uniform vec2 uPointer;
    uniform float uTime;
    uniform float uScroll;
    uniform float uPointerActive;

    float hash(vec2 p){
      p=fract(p*vec2(123.34,345.45));
      p+=dot(p,p+34.345);
      return fract(p.x*p.y);
    }
    float noise(vec2 p){
      vec2 i=floor(p),f=fract(p); f=f*f*(3.-2.*f);
      return mix(mix(hash(i),hash(i+vec2(1.,0.)),f.x),mix(hash(i+vec2(0.,1.)),hash(i+1.),f.x),f.y);
    }
    float filament(vec2 p,float offset,float speed,float width){
      float wave=.2*sin(p.x*1.5+offset+uTime*speed)+.07*sin(p.x*4.-offset*1.7);
      wave+=(noise(vec2(p.x*.72+offset,uTime*.05))-.5)*.22;
      return width/(abs(p.y-wave)+width);
    }
    void main(){
      vec2 baseUv=(2.*gl_FragCoord.xy-uResolution.xy)/min(uResolution.x,uResolution.y);
      vec2 cursorUv=baseUv-uPointer*vec2(uResolution.x,uResolution.y)/min(uResolution.x,uResolution.y);
      vec2 uv=baseUv;
      uv+=uPointer*vec2(.055,.035);
      uv.x-=.52-uScroll*.18;
      uv.y+=.08-uScroll*.15;
      vec3 color=vec3(0.);
      vec3 blue=vec3(.07,.22,1.);
      vec3 violet=vec3(.34,.08,.92);
      vec3 amber=vec3(1.,.48,.09);
      float f1=filament(uv,0.,.16,.0032);
      float f2=filament(uv,1.7,-.11,.0021);
      float f3=filament(uv,3.4,.09,.0014);
      float taper=smoothstep(2.1,.18,abs(uv.x));
      color+=(blue*f1*.12+violet*f2*.09+amber*f3*.05)*taper;
      float haze=exp(-1.8*length((uv-vec2(.45,.05))*vec2(.8,1.6)));
      color+=mix(blue,violet,.45)*haze*.026;
      float cursorDistance=length(cursorUv*vec2(.78,1.));
      float cursorLens=exp(-18.*cursorDistance*cursorDistance);
      float cursorRing=exp(-95.*abs(cursorDistance-.12));
      color+=(mix(blue,amber,.28)*cursorLens*.04+mix(violet,blue,.5)*cursorRing*.022)*uPointerActive;
      float grain=hash(gl_FragCoord.xy+floor(uTime*9.));
      color+=(grain-.5)*.008;
      gl_FragColor=vec4(color,1.);
    }
  `;

  function compile(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
    console.warn('Liquid bird shader unavailable:', gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }

  const vertex = compile(gl.VERTEX_SHADER, vertexSource);
  const fragment = compile(gl.FRAGMENT_SHADER, fragmentSource);
  if (!vertex || !fragment) return;
  const program = gl.createProgram();
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return;
  gl.useProgram(program);

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,3,-1,-1,3]), gl.STATIC_DRAW);
  const position = gl.getAttribLocation(program, 'aPosition');
  gl.enableVertexAttribArray(position);
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
  const resolution = gl.getUniformLocation(program, 'uResolution');
  const pointer = gl.getUniformLocation(program, 'uPointer');
  const time = gl.getUniformLocation(program, 'uTime');
  const scroll = gl.getUniformLocation(program, 'uScroll');
  const pointerActive = gl.getUniformLocation(program, 'uPointerActive');

  function resize() {
    const dpr = Math.min(devicePixelRatio || 1, innerWidth < 700 ? 1 : 1.35);
    const width = Math.round(innerWidth * dpr);
    const height = Math.round(innerHeight * dpr);
    if (canvas.width === width && canvas.height === height) return;
    canvas.width = width;
    canvas.height = height;
    gl.viewport(0, 0, width, height);
  }

  function render(now) {
    resize();
    pointerX += (targetX - pointerX) * .055;
    pointerY += (targetY - pointerY) * .055;
    cursorActive += (cursorActiveTarget - cursorActive) * .09;
    for (let index = 0; index < wake.length; index += 1) {
      wakeX[index] += (cursorX - wakeX[index]) * wakeDamping[index];
      wakeY[index] += (cursorY - wakeY[index]) * wakeDamping[index];
      const stretch = 1 + Math.min(.42, Math.hypot(cursorX - wakeX[index], cursorY - wakeY[index]) / 230);
      wake[index].style.transform = `translate3d(${wakeX[index].toFixed(1)}px,${wakeY[index].toFixed(1)}px,0) rotate(-14deg) scaleX(${stretch.toFixed(3)})`;
    }
    root.style.setProperty('--px', pointerX.toFixed(4));
    root.style.setProperty('--py', pointerY.toFixed(4));
    gl.uniform2f(resolution, canvas.width, canvas.height);
    gl.uniform2f(pointer, pointerX, pointerY);
    gl.uniform1f(time, reduced.matches ? 0 : now * .001);
    gl.uniform1f(scroll, scrollProgress);
    gl.uniform1f(pointerActive, cursorActive);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    if (!status.ready) status.ready = true;
    frame = requestAnimationFrame(render);
  }

  addEventListener('resize', resize);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelAnimationFrame(frame);
    else frame = requestAnimationFrame(render);
  });
  reduced.addEventListener('change', event => { status.reducedMotion = event.matches; });
  frame = requestAnimationFrame(render);
})();
