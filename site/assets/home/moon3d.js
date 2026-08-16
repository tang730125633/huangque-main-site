/* 首页 3D 月球:原生 WebGL 球体 + 4K 等距柱状月面纹理,无第三方依赖。
   纹理来源与许可见 moon_texture_LICENSE.txt；低规格 GPU/省流量模式回退 2K。
   WebGL 不可用时保留静态 <img> 兜底;prefers-reduced-motion 只渲染单帧。 */
(() => {
  const wrap = document.querySelector('.hero-moon');
  const canvas = wrap && wrap.querySelector('canvas.moon-3d');
  if (!wrap || !canvas) return;

  const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: false, antialias: true });
  if (!gl) { canvas.remove(); return; }

  const supports4K = gl.getParameter(gl.MAX_TEXTURE_SIZE) >= 4096;
  const saveData = Boolean(navigator.connection && navigator.connection.saveData);
  const texture4K = '/assets/home/moon_map.webp?v=20260808-4k';
  const texture2K = '/assets/home/moon_map_2k.webp?v=20260808-2k';
  const textureSrc = supports4K && !saveData ? texture4K : texture2K;
  wrap.dataset.moonTexture = textureSrc === texture4K ? '4k' : '2k';

  const VERT = `
attribute vec3 aPos; attribute vec3 aNormal; attribute vec2 aUV;
uniform mat4 uMVP; uniform mat4 uModel;
varying vec3 vNormal; varying vec2 vUV;
void main() {
  vNormal = mat3(uModel) * aNormal;
  vUV = aUV;
  gl_Position = uMVP * vec4(aPos, 1.0);
}`;

  const FRAG = `
precision mediump float;
uniform sampler2D uTex; uniform vec3 uLight;
varying vec3 vNormal; varying vec2 vUV;
void main() {
  vec3 n = normalize(vNormal);
  float diff = max(dot(n, normalize(uLight)), 0.0);
  vec3 tex = texture2D(uTex, vUV).rgb;
  // 主光 + 环境光提亮,曲线拉通透,边缘补一圈冷色轮廓光
  float shade = 0.14 + 1.02 * diff;
  float rim = pow(1.0 - max(dot(n, vec3(0.0, 0.0, 1.0)), 0.0), 2.6) * 0.22;
  vec3 color = tex * shade * vec3(0.96, 0.98, 1.06) + vec3(0.62, 0.72, 0.95) * rim;
  color = pow(color, vec3(0.88));
  gl_FragColor = vec4(color, 1.0);
}`;

  function compile(type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }

  let prog;
  try {
    prog = gl.createProgram();
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
  } catch (e) { canvas.remove(); return; }
  gl.useProgram(prog);

  // UV 球体网格
  const LAT = 128, LON = 128;
  const positions = [], normals = [], uvs = [], indices = [];
  for (let lat = 0; lat <= LAT; lat++) {
    const theta = lat * Math.PI / LAT;
    const sinT = Math.sin(theta), cosT = Math.cos(theta);
    for (let lon = 0; lon <= LON; lon++) {
      const phi = lon * 2 * Math.PI / LON;
      const x = Math.cos(phi) * sinT, y = cosT, z = Math.sin(phi) * sinT;
      positions.push(x, y, z); normals.push(x, y, z);
      uvs.push(1 - lon / LON, 1 - lat / LAT);
    }
  }
  for (let lat = 0; lat < LAT; lat++) {
    for (let lon = 0; lon < LON; lon++) {
      const a = lat * (LON + 1) + lon, b = a + LON + 1;
      indices.push(a, b, a + 1, b, b + 1, a + 1);
    }
  }

  function buffer(data, loc, size) {
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.STATIC_DRAW);
    const attr = gl.getAttribLocation(prog, loc);
    gl.enableVertexAttribArray(attr);
    gl.vertexAttribPointer(attr, size, gl.FLOAT, false, 0, 0);
  }
  buffer(positions, 'aPos', 3);
  buffer(normals, 'aNormal', 3);
  buffer(uvs, 'aUV', 2);
  const ibo = gl.createBuffer();
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(indices), gl.STATIC_DRAW);

  // 纹理
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
    new Uint8Array([30, 30, 32, 255])); // 加载完成前的占位色
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  let ready = false;
  const img = new Image();
  img.onload = () => {
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.generateMipmap(gl.TEXTURE_2D);
    const anisotropy = gl.getExtension('EXT_texture_filter_anisotropic') ||
      gl.getExtension('WEBKIT_EXT_texture_filter_anisotropic') ||
      gl.getExtension('MOZ_EXT_texture_filter_anisotropic');
    if (anisotropy) {
      const max = gl.getParameter(anisotropy.MAX_TEXTURE_MAX_ANISOTROPY_EXT);
      gl.texParameterf(gl.TEXTURE_2D, anisotropy.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(8, max));
    }
    ready = true;
    wrap.classList.add('gl-on');
    render(performance.now());
  };
  img.onerror = () => { canvas.remove(); };
  img.src = textureSrc;

  // 矩阵工具(列主序)
  const multiply = (a, b) => {
    const out = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++)
      out[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    return out;
  };
  const rotY = a => new Float32Array([Math.cos(a), 0, -Math.sin(a), 0, 0, 1, 0, 0, Math.sin(a), 0, Math.cos(a), 0, 0, 0, 0, 1]);
  const rotZ = a => new Float32Array([Math.cos(a), Math.sin(a), 0, 0, -Math.sin(a), Math.cos(a), 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
  const perspective = (fov, aspect, near, far) => {
    const f = 1 / Math.tan(fov / 2), nf = 1 / (near - far);
    return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
  };
  const translate = (x, y, z) => new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]);

  const uMVP = gl.getUniformLocation(prog, 'uMVP');
  const uModel = gl.getUniformLocation(prog, 'uModel');
  gl.uniform3f(gl.getUniformLocation(prog, 'uLight'), -0.55, 0.38, 0.74);
  gl.enable(gl.DEPTH_TEST);
  gl.clearColor(0, 0, 0, 0);

  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const TILT = -0.14;          // 轻微轴倾角,增强立体感
  const SPEED = 2 * Math.PI / 90000; // 90 秒一圈

  function resize() {
    const dpr = Math.min(devicePixelRatio || 1, 2.5);
    const w = Math.round(canvas.clientWidth * dpr), h = Math.round(canvas.clientHeight * dpr);
    if (w && h && (canvas.width !== w || canvas.height !== h)) { canvas.width = w; canvas.height = h; }
  }

  let inView = true, t0 = performance.now(), base = 0.6;
  function render(now) {
    resize();
    const angle = reduced.matches ? base : base + (now - t0) * SPEED;
    const aspect = canvas.width / Math.max(1, canvas.height);
    const model = multiply(rotZ(TILT), rotY(angle));
    const view = translate(0, 0, -3.15);
    const proj = perspective(0.62, aspect, 0.1, 10);
    const mvp = multiply(proj, multiply(view, model));
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.uniformMatrix4fv(uModel, false, model);
    gl.uniformMatrix4fv(uMVP, false, mvp);
    gl.drawElements(gl.TRIANGLES, indices.length, gl.UNSIGNED_SHORT, 0);
  }

  function loop(now) {
    if (ready && inView && !reduced.matches) render(now);
    requestAnimationFrame(loop);
  }

  new IntersectionObserver(entries => {
    inView = entries[0] ? entries[0].isIntersecting : true;
  }, { threshold: 0.02 }).observe(wrap);
  reduced.addEventListener('change', () => render(performance.now()));
  addEventListener('resize', () => { if (!reduced.matches) render(performance.now()); });

  requestAnimationFrame(loop);
})();
