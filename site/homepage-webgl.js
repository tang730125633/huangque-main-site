(() => {
  'use strict';

  const canvas = document.querySelector('[data-webgl-stage]');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const gl = canvas?.getContext('webgl', { alpha: true, antialias: false, powerPreference: 'high-performance' });
  const status = { ready: false, version: 'story1', reducedMotion: reducedMotion.matches };
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
    float noise(vec3 p){
      vec3 i=floor(p),f=fract(p); f=f*f*(3.-2.*f);
      float n=dot(i,vec3(1.,57.,113.));
      return mix(mix(mix(hash21(vec2(n,n+1.)),hash21(vec2(n+1.,n+2.)),f.x),mix(hash21(vec2(n+57.,n+58.)),hash21(vec2(n+58.,n+59.)),f.x),f.y),mix(mix(hash21(vec2(n+113.,n+114.)),hash21(vec2(n+114.,n+115.)),f.x),mix(hash21(vec2(n+170.,n+171.)),hash21(vec2(n+171.,n+172.)),f.x),f.y),f.z);
    }
    mat2 rot(float a){ float c=cos(a),s=sin(a); return mat2(c,-s,s,c); }
    vec2 mapScene(vec3 p){
      p.xz*=rot(.18+uScroll*1.65);
      p.xy*=rot(-.12+uScroll*.48);
      vec3 moon=p-vec3(.42-.84*uScroll,.08*sin(uScroll*PI),0.);
      float crater=(noise(moon*5.8)-.5)*.055;
      float sphere=length(moon)-1.02-crater;
      vec3 ring=p; ring.xy*=rot(.56+uScroll*.42); ring.yz*=rot(1.04);
      float torus=length(vec2(length(ring.xz)-1.48,ring.y))-.038;
      return sphere<torus?vec2(sphere,1.):vec2(torus,2.);
    }
    vec3 normalAt(vec3 p){
      vec2 e=vec2(.002,0.); float d=mapScene(p).x;
      return normalize(vec3(d-mapScene(p-e.xyy).x,d-mapScene(p-e.yxy).x,d-mapScene(p-e.yyx).x));
    }
    void main(){
      vec2 uv=(2.*gl_FragCoord.xy-uResolution.xy)/min(uResolution.x,uResolution.y);
      uv+=uPointer*vec2(.12,.08);
      vec3 gold=vec3(.94,.65,.23), violet=vec3(.48,.27,.9);
      vec3 bg=vec3(.008,.009,.014);
      bg+=mix(violet,gold,uScroll)*(.025+.035*max(0.,1.-length(uv-vec2(.15,0.))));
      vec2 cell=floor((uv+10.)*180.);
      float star=step(.9965,hash21(cell))*pow(hash21(cell+7.3),9.);
      bg+=vec3(star)*(.28+.72*hash21(cell+2.))*(.78+.22*sin(uTime*.7+hash21(cell)*PI*2.));

      vec3 ro=vec3(0.,0.,4.35+.28*sin(uScroll*PI));
      vec3 rd=normalize(vec3(uv,-2.25));
      ro.x+=mix(.16,-.2,uScroll);
      float distanceTravelled=0., material=0., glow=0.;
      vec3 p=ro;
      for(int i=0;i<62;i++){
        p=ro+rd*distanceTravelled;
        vec2 scene=mapScene(p);
        glow+=.00072/max(scene.x*scene.x,.00035)*(scene.y>1.5?1.:0.);
        if(scene.x<.0015){ material=scene.y; break; }
        distanceTravelled+=max(scene.x*.72,.008);
        if(distanceTravelled>8.) break;
      }

      vec3 color=bg+mix(violet,gold,.62+.28*sin(uScroll*PI))*min(glow,.42);
      if(material>0.){
        vec3 n=normalAt(p);
        vec3 light=normalize(vec3(-.7,.8,1.2));
        float diffuse=max(dot(n,light),0.);
        float rim=pow(1.-max(dot(n,-rd),0.),3.);
        if(material<1.5){
          float stone=noise(p*7.)*.13+noise(p*21.)*.04;
          vec3 moonColor=mix(vec3(.10,.095,.09),vec3(.72,.64,.48),diffuse*.72+stone);
          color=mix(moonColor,mix(gold,violet,uScroll),rim*.42);
          color+=pow(max(dot(reflect(-light,n),-rd),0.),18.)*.18;
        }else{
          color=gold*(1.5+diffuse)+vec3(1.)*.45;
        }
      }
      float vignette=1.-smoothstep(.65,1.55,length(uv));
      color*=.42+.58*vignette;
      color=pow(color,vec3(.82));
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

  const resolutionLocation = gl.getUniformLocation(program, 'uResolution');
  const pointerLocation = gl.getUniformLocation(program, 'uPointer');
  const timeLocation = gl.getUniformLocation(program, 'uTime');
  const scrollLocation = gl.getUniformLocation(program, 'uScroll');
  const pointerTarget = [0, 0];
  const pointer = [0, 0];
  let frame = 0;
  let visible = true;

  function resize() {
    const dpr = Math.min(devicePixelRatio || 1, innerWidth < 700 ? 1.1 : 1.45);
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
    const scrollRange = Math.max(1, document.documentElement.scrollHeight - innerHeight);
    const progress = Math.min(1, Math.max(0, scrollY / scrollRange));
    if (!reducedMotion.matches) {
      pointer[0] += (pointerTarget[0] - pointer[0]) * .055;
      pointer[1] += (pointerTarget[1] - pointer[1]) * .055;
    }
    gl.uniform2f(resolutionLocation, canvas.width, canvas.height);
    gl.uniform2f(pointerLocation, pointer[0], pointer[1]);
    gl.uniform1f(timeLocation, reducedMotion.matches ? 0 : now * .001);
    gl.uniform1f(scrollLocation, progress);
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

  addEventListener('pointermove', event => {
    pointerTarget[0] = event.clientX / innerWidth * 2 - 1;
    pointerTarget[1] = 1 - event.clientY / innerHeight * 2;
    schedule();
  }, { passive: true });
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule);
  reducedMotion.addEventListener('change', event => { status.reducedMotion = event.matches; schedule(); });
  document.addEventListener('visibilitychange', () => {
    visible = !document.hidden;
    if (visible) schedule();
  });
  canvas.addEventListener('webglcontextlost', event => {
    event.preventDefault();
    status.ready = false;
    document.documentElement.classList.remove('webgl-ready');
    document.documentElement.classList.add('webgl-fallback');
  });

  schedule();
})();
