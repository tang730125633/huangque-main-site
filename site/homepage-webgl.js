(() => {
  'use strict';

  const canvas = document.querySelector('[data-webgl-stage]');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const gl = canvas?.getContext('webgl', { alpha: false, antialias: false, powerPreference: 'high-performance' });
  const status = { ready: false, version: 'flight1', reducedMotion: reducedMotion.matches };
  window.__homepageWebglStatus = status;
  window.__homepageWebglCheck = () => status.ready && canvas.width > 0 && canvas.height > 0;

  if (!gl) {
    document.documentElement.classList.add('webgl-fallback');
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

    #define PI 3.14159265359

    float hash21(vec2 p){
      p=fract(p*vec2(123.34,456.21));
      p+=dot(p,p+45.32);
      return fract(p.x*p.y);
    }
    float noise(vec2 p){
      vec2 i=floor(p),f=fract(p); f=f*f*(3.-2.*f);
      return mix(mix(hash21(i),hash21(i+vec2(1.,0.)),f.x),mix(hash21(i+vec2(0.,1.)),hash21(i+1.),f.x),f.y);
    }
    float fbm(vec2 p){
      float value=0.,amplitude=.5;
      for(int i=0;i<5;i++){
        value+=noise(p)*amplitude;
        p=mat2(1.6,-1.2,1.2,1.6)*p+1.7;
        amplitude*=.5;
      }
      return value;
    }
    mat2 rot(float a){ float c=cos(a),s=sin(a); return mat2(c,-s,s,c); }

    void main(){
      float aspect=uResolution.x/uResolution.y;
      vec2 uv=(2.*gl_FragCoord.xy-uResolution.xy)/min(uResolution.x,uResolution.y);
      vec2 pointer=uPointer*vec2(.08,.05);
      uv+=pointer;

      float t=uTime*.16;
      float journey=uScroll*4.;
      vec3 black=vec3(.006,.007,.009);
      vec3 gold=vec3(.92,.63,.18);
      vec3 ivory=vec3(.96,.91,.72);
      vec3 violet=vec3(.45,.23,.92);
      vec3 cyan=vec3(.13,.55,.62);
      vec3 palette=mix(gold,violet,smoothstep(.2,.72,uScroll));
      palette=mix(palette,cyan,smoothstep(.72,1.,uScroll)*.55);

      vec2 p=uv;
      p.x+=mix(.48,-.32,uScroll)*step(1.15,aspect);
      p*=rot(-.08+.16*sin(uScroll*PI));
      float warp=fbm(p*.82+vec2(journey*.13,t*.12));
      float detail=fbm(p*1.65-vec2(t*.08,journey*.09));

      vec3 color=black;
      color+=palette*.018*(1.-smoothstep(.1,2.2,length(p-vec2(.35,0.))));

      for(int i=0;i<7;i++){
        float fi=float(i);
        float depth=fi/6.;
        float wave=.28*sin(p.x*(1.05+depth*.7)+fi*.72+t*(.4-depth*.22));
        wave+=.105*sin(p.x*3.1-fi*.9-journey*.34);
        wave+=(depth-.5)*.22+(warp-.5)*(.34-depth*.12);
        float distanceToFlight=abs(p.y-wave);
        float taper=smoothstep(2.3,.15,abs(p.x-.18));
        float line=.0018/(distanceToFlight+.0025)*taper;
        vec3 lineColor=mix(palette,ivory,pow(depth,2.)*.5);
        color+=lineColor*line*(.07+detail*.085)*(1.-depth*.36);
      }

      vec2 dustUv=uv*vec2(88.,54.);
      dustUv.x+=uScroll*18.+uTime*.22;
      vec2 cell=floor(dustUv);
      vec2 local=fract(dustUv)-.5;
      float seed=hash21(cell);
      vec2 offset=vec2(hash21(cell+3.1),hash21(cell+8.7))-.5;
      float point=smoothstep(.075,.005,length(local-offset*.62));
      float flightBand=exp(-2.1*abs(uv.y-.24*sin(uv.x*1.45+seed*2.+t)-(warp-.5)*.5));
      point*=step(.76,seed)*(.18+.82*flightBand);
      color+=mix(ivory,palette,seed)*point*(.24+.76*hash21(cell+1.));

      float horizon=exp(-4.2*abs(uv.y+1.05-uScroll*.72));
      color+=palette*horizon*.025*(.4+.6*noise(vec2(uv.x*5.+t,journey)));
      float vignette=1.-smoothstep(.65,1.9,length(uv*vec2(.72,1.)));
      color*=.38+.62*vignette;
      color=pow(color,vec3(.84));
      gl_FragColor=vec4(color,1.);
    }
  `;

  function compile(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
    console.warn('Homepage WebGL shader unavailable:', gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }

  const vertex = compile(gl.VERTEX_SHADER, vertexSource);
  const fragment = compile(gl.FRAGMENT_SHADER, fragmentSource);
  if (!vertex || !fragment) {
    document.documentElement.classList.add('webgl-fallback');
    return;
  }

  const program = gl.createProgram();
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.warn('Homepage WebGL program unavailable:', gl.getProgramInfoLog(program));
    document.documentElement.classList.add('webgl-fallback');
    return;
  }

  gl.useProgram(program);
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const position = gl.getAttribLocation(program, 'aPosition');
  gl.enableVertexAttribArray(position);
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

  const uniforms = {
    resolution: gl.getUniformLocation(program, 'uResolution'),
    pointer: gl.getUniformLocation(program, 'uPointer'),
    time: gl.getUniformLocation(program, 'uTime'),
    scroll: gl.getUniformLocation(program, 'uScroll'),
  };
  const pointerTarget = [0, 0];
  const pointer = [0, 0];
  let frame = 0;
  let visible = true;

  function resize() {
    const dpr = Math.min(devicePixelRatio || 1, innerWidth < 700 ? 1.05 : 1.4);
    const width = Math.round(innerWidth * dpr);
    const height = Math.round(innerHeight * dpr);
    if (canvas.width === width && canvas.height === height) return;
    canvas.width = width;
    canvas.height = height;
    gl.viewport(0, 0, width, height);
  }

  function render(now = 0) {
    frame = 0;
    resize();
    const range = Math.max(1, document.documentElement.scrollHeight - innerHeight);
    const progress = Math.min(1, Math.max(0, scrollY / range));
    if (!reducedMotion.matches) {
      pointer[0] += (pointerTarget[0] - pointer[0]) * .055;
      pointer[1] += (pointerTarget[1] - pointer[1]) * .055;
    }
    gl.uniform2f(uniforms.resolution, canvas.width, canvas.height);
    gl.uniform2f(uniforms.pointer, pointer[0], pointer[1]);
    gl.uniform1f(uniforms.time, reducedMotion.matches ? 0 : now * .001);
    gl.uniform1f(uniforms.scroll, progress);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    if (!status.ready) {
      status.ready = true;
      document.documentElement.classList.add('webgl-ready');
    }
    if (visible && !reducedMotion.matches) frame = requestAnimationFrame(render);
  }

  function schedule() {
    if (!frame && visible) frame = requestAnimationFrame(render);
  }

  if (!reducedMotion.matches && matchMedia('(pointer:fine)').matches) {
    addEventListener('pointermove', event => {
      pointerTarget[0] = event.clientX / innerWidth * 2 - 1;
      pointerTarget[1] = 1 - event.clientY / innerHeight * 2;
      schedule();
    }, { passive: true });
  }
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule);
  reducedMotion.addEventListener('change', event => { status.reducedMotion = event.matches; schedule(); });
  document.addEventListener('visibilitychange', () => { visible = !document.hidden; if (visible) schedule(); });
  canvas.addEventListener('webglcontextlost', event => {
    event.preventDefault();
    status.ready = false;
    document.documentElement.classList.remove('webgl-ready');
    document.documentElement.classList.add('webgl-fallback');
  });

  schedule();
})();
