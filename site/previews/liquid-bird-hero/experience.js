(() => {
  'use strict';

  const root = document.documentElement;
  const canvas = document.querySelector('[data-light-field]');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const gl = canvas?.getContext('webgl', { alpha: true, antialias: false, powerPreference: 'high-performance' });
  const status = { ready: false, reducedMotion: reduced.matches, version: 'liquid-bird-2', scrollProgress: 0, sceneIndex: 0, pointerSpeed: 0 };
  window.__liquidBirdStatus = status;
  window.__liquidBirdCheck = () => status.ready && canvas.width > 0 && canvas.height > 0;

  let pointerX = 0;
  let pointerY = 0;
  let targetX = 0;
  let targetY = 0;
  let cursorActive = 0;
  let cursorActiveTarget = 0;
  let scrollProgress = 0;
  let scrollTarget = 0;
  let frame = 0;

  const scenes = [
    { at: 0, x: 0, y: 0, scale: 1, rotate: 0, opacity: 1, hue: 0, glow: 1 },
    { at: .2, x: -24, y: 7, scale: .72, rotate: -3, opacity: .34, hue: 7, glow: .78 },
    { at: .42, x: -8, y: -8, scale: .8, rotate: 1.5, opacity: .43, hue: -8, glow: 1.08 },
    { at: .64, x: -27, y: 9, scale: .66, rotate: -2.5, opacity: .34, hue: 10, glow: .72 },
    { at: .84, x: -7, y: -9, scale: .81, rotate: 1, opacity: .42, hue: -5, glow: 1.04 },
    { at: 1, x: -38, y: -14, scale: .5, rotate: -4, opacity: .08, hue: 14, glow: .62 }
  ];

  const lerp = (start, end, amount) => start + (end - start) * amount;

  function applyScene(progress) {
    const end = scenes.findIndex(scene => scene.at >= progress);
    const nextIndex = end < 0 ? scenes.length - 1 : end;
    const previousIndex = Math.max(0, nextIndex - 1);
    const previous = scenes[previousIndex];
    const next = scenes[nextIndex];
    const amount = previous === next ? 0 : (progress - previous.at) / (next.at - previous.at);
    ['x', 'y', 'scale', 'rotate', 'opacity', 'hue', 'glow'].forEach(key => {
      const name = key === 'x' || key === 'y' || key === 'scale' || key === 'rotate' || key === 'opacity'
        ? `--orbit-${key}`.replace('--orbit-opacity', '--bird-opacity')
        : `--scene-${key}`;
      root.style.setProperty(name, lerp(previous[key], next[key], amount).toFixed(4));
    });
    status.scrollProgress = Number(progress.toFixed(4));
    status.sceneIndex = previousIndex;
  }

  function updatePageState() {
    const range = Math.max(1, document.documentElement.scrollHeight - innerHeight);
    scrollTarget = Math.min(1, Math.max(0, scrollY / range));
    if (!gl || reduced.matches) {
      scrollProgress = scrollTarget;
      applyScene(scrollProgress);
    }
  }

  if (!reduced.matches) {
    addEventListener('pointermove', event => {
      if (event.pointerType === 'touch') return;
      targetX = event.clientX / innerWidth * 2 - 1;
      targetY = 1 - event.clientY / innerHeight * 2;
      cursorActiveTarget = 1;
    }, { passive: true });
    addEventListener('mouseout', event => {
      if (event.relatedTarget) return;
      cursorActiveTarget = 0;
    });
  }
  addEventListener('scroll', updatePageState, { passive: true });
  updatePageState();

  const revealTargets = document.querySelectorAll('.story,.chapter,.closing');
  if (reduced.matches || !('IntersectionObserver' in window)) {
    revealTargets.forEach(target => target.classList.add('is-visible'));
  } else {
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, { threshold: .22 });
    revealTargets.forEach(target => observer.observe(target));
  }
  root.classList.add('motion-ready');

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
    uniform vec2 uPointerTarget;
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
    float segmentDistance(vec2 point,vec2 start,vec2 end){
      vec2 segment=end-start;
      float amount=clamp(dot(point-start,segment)/max(dot(segment,segment),.0001),0.,1.);
      return length(point-start-segment*amount);
    }
    float starField(vec2 point,vec2 cursor,float velocity){
      float density=8.;
      vec2 base=floor(point*density);
      float light=0.;
      for(int y=-1;y<=1;y++){
        for(int x=-1;x<=1;x++){
          vec2 cell=base+vec2(float(x),float(y));
          float seed=hash(cell);
          if(seed<.45) continue;
          vec2 home=(cell+vec2(seed,hash(cell+19.17)))/density;
          vec2 away=home-cursor;
          vec2 radial=normalize(away+vec2(.0001));
          vec2 tangent=vec2(-radial.y,radial.x);
          float force=smoothstep(.72,.05,length(away))*uPointerActive;
          vec2 star=home+radial*force*(.035+velocity*.21)+tangent*force*velocity*.16;
          float radius=mix(.0045,.011,hash(cell+7.31));
          float twinkle=.58+.42*sin(uTime*(1.2+seed)+seed*18.);
          float distanceToStar=length(point-star);
          float core=1.-smoothstep(0.,radius,distanceToStar);
          float halo=1.-smoothstep(radius,radius*4.,distanceToStar);
          light+=(core+halo*.2)*twinkle;
        }
      }
      return light;
    }
    void main(){
      vec2 baseUv=(2.*gl_FragCoord.xy-uResolution.xy)/min(uResolution.x,uResolution.y);
      vec2 cursorScale=vec2(uResolution.x,uResolution.y)/min(uResolution.x,uResolution.y);
      vec2 cursorNow=uPointer*cursorScale;
      vec2 cursorTarget=uPointerTarget*cursorScale;
      vec2 fieldCursor=mix(cursorNow,cursorTarget,.7);
      vec2 uv=baseUv;
      uv+=uPointer*vec2(.055,.035);
      uv.x-=.52-uScroll*.18;
      uv.y+=.08-uScroll*.15;
      vec3 color=vec3(0.);
      vec3 blue=mix(vec3(.07,.22,1.),vec3(.04,.42,.92),smoothstep(.15,.75,uScroll));
      vec3 violet=mix(vec3(.34,.08,.92),vec3(.47,.12,.78),uScroll);
      vec3 amber=vec3(1.,.48,.09);
      float f1=filament(uv,0.,.16,.0032);
      float f2=filament(uv,1.7,-.11,.0021);
      float f3=filament(uv,3.4,.09,.0014);
      float taper=smoothstep(2.1,.18,abs(uv.x));
      color+=(blue*f1*.12+violet*f2*.09+amber*f3*.05)*taper;
      float haze=exp(-1.8*length((uv-vec2(.45,.05))*vec2(.8,1.6)));
      color+=mix(blue,violet,.45)*haze*.026;
      float cursorSpeed=clamp(length(cursorTarget-cursorNow)*4.6,0.,1.);
      float streakDistance=segmentDistance(baseUv,cursorNow,cursorTarget);
      float streakHalo=exp(-18.*streakDistance)*cursorSpeed;
      float streakCore=exp(-65.*streakDistance)*cursorSpeed;
      float cursorHead=exp(-34.*dot(baseUv-cursorTarget,baseUv-cursorTarget));
      color+=(blue*streakHalo*.055+mix(violet,amber,.24)*streakCore*.105+amber*cursorHead*.02)*uPointerActive;
      float stars=starField(baseUv,fieldCursor,cursorSpeed);
      color+=mix(vec3(.26,.46,1.),vec3(1.,.62,.24),.18)*stars*.72;
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
  const pointerTarget = gl.getUniformLocation(program, 'uPointerTarget');
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
    pointerX += (targetX - pointerX) * .035;
    pointerY += (targetY - pointerY) * .035;
    cursorActive += (cursorActiveTarget - cursorActive) * .09;
    scrollProgress += (scrollTarget - scrollProgress) * (reduced.matches ? 1 : .055);
    applyScene(scrollProgress);
    status.pointerSpeed = Number(Math.min(1, Math.hypot(targetX - pointerX, targetY - pointerY) * 4.6).toFixed(4));
    root.style.setProperty('--px', pointerX.toFixed(4));
    root.style.setProperty('--py', pointerY.toFixed(4));
    gl.uniform2f(resolution, canvas.width, canvas.height);
    gl.uniform2f(pointer, pointerX, pointerY);
    gl.uniform2f(pointerTarget, targetX, targetY);
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
