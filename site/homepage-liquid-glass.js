(() => {
  const videos=[...document.querySelectorAll('.hero-media video')];
  const reducedMotion=matchMedia('(prefers-reduced-motion:reduce)');
  if(!videos.length||reducedMotion.matches)return;

  const vertexSource='attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}';
  const fragmentSource=`
    precision highp float;
    uniform sampler2D image;
    uniform vec2 resolution,videoSize,mouse;
    uniform vec4 shape;
    uniform float radius,dpr;
    float roundedBox(vec2 p,vec2 halfSize,float r){vec2 q=abs(p)-halfSize+r;return min(max(q.x,q.y),0.0)+length(max(q,0.0))-r;}
    float field(vec2 p){float d=roundedBox(p-shape.xy,shape.zw,radius);float h=clamp(-d/24.0,0.0,1.0);return h*h*(3.0-2.0*h);}
    vec2 coverUV(vec2 p){
      vec2 uv=p/resolution;float screenAspect=resolution.x/resolution.y;float sourceAspect=videoSize.x/videoSize.y;
      if(sourceAspect>screenAspect)uv.x=(uv.x-.5)*screenAspect/sourceAspect+.5;else uv.y=(uv.y-.5)*sourceAspect/screenAspect+.5;
      return clamp(uv,vec2(.003),vec2(.997));
    }
    vec3 backdrop(vec2 p){vec3 color=texture2D(image,coverUV(p)).rgb;float luma=dot(color,vec3(.2126,.7152,.0722));color=mix(vec3(luma),color,.78);return ((color-.5)*1.05+.5)*.8;}
    void main(){
      vec2 p=gl_FragCoord.xy/dpr;float d=roundedBox(p-shape.xy,shape.zw,radius);if(d>1.5)discard;
      float depth=-d;float e=1.25;
      vec2 gradient=vec2(field(p+vec2(e,0.0))-field(p-vec2(e,0.0)),field(p+vec2(0.0,e))-field(p-vec2(0.0,e)))/(2.0*e);
      vec2 n=normalize(gradient+vec2(.0001));vec3 N=normalize(vec3(-gradient*13.0,1.0));
      float outer=1.0-smoothstep(0.0,4.5,depth);float inner=smoothstep(2.0,9.0,depth)*(1.0-smoothstep(16.0,29.0,depth));
      float bevel=1.0-smoothstep(0.0,26.0,depth);float cavity=smoothstep(3.0,9.0,depth)*(1.0-smoothstep(14.0,25.0,depth));
      vec2 facePoint=shape.xy+(p-shape.xy)*.965;vec2 refractedPoint=facePoint-n*(14.0+48.0*inner);vec2 outsidePoint=p+n*(10.0+18.0*outer);vec2 dispersion=n*(2.2+2.4*inner);
      vec3 refracted=vec3(backdrop(refractedPoint+dispersion).r,backdrop(refractedPoint).g,backdrop(refractedPoint-dispersion).b);
      vec3 color=mix(backdrop(facePoint),refracted,inner*.98);color=mix(color,backdrop(outsidePoint),outer*.84);
      vec3 lightDir=normalize(vec3((mouse-shape.xy)/resolution*2.2,.72));float spec=pow(max(dot(N,lightDir),0.0),13.0)*bevel;float fresnel=pow(1.0-max(N.z,0.0),1.8)*bevel;
      vec3 reflected=reflect(vec3(0.0,0.0,-1.0),N);float envBand=pow(max(1.0-abs(reflected.x*.7+reflected.y*.5-.12),0.0),9.0)*bevel;
      color*=1.0-cavity*.22;color=mix(color,vec3(.98,1.0,1.0),envBand*.48);color+=vec3(.78,.92,1.0)*(spec*.62+fresnel*.3);
      color+=outer*(vec3(.08,.22,.34)*max(n.x,0.0)+vec3(.34,.07,.015)*max(-n.x,0.0));gl_FragColor=vec4(color,smoothstep(1.5,-1.5,d));
    }`;

  const pointer={x:innerWidth*.35,y:innerHeight*.7};
  let visible=true;
  const hero=document.querySelector('.hero');
  let navOverHero=true;
  const syncNavBackdrop=()=>{
    navOverHero=(hero?.getBoundingClientRect().bottom||0)>(document.querySelector('.site-header')?.offsetHeight||0);
    document.documentElement.classList.toggle('nav-current-backdrop',!navOverHero);
  };
  addEventListener('pointermove',event=>{pointer.x=event.clientX;pointer.y=event.clientY;},{passive:true});
  addEventListener('scroll',syncNavBackdrop,{passive:true});addEventListener('resize',syncNavBackdrop);syncNavBackdrop();
  document.addEventListener('visibilitychange',()=>{visible=!document.hidden;});
  const status=window.__homepageLiquidGlassStatus={supported:true,hero:false,nav:false,active:false};

  function createLens(canvas,target,key,readyClass,maxDpr){
    const gl=canvas?.getContext('webgl',{alpha:true,antialias:false,premultipliedAlpha:true});
    if(!canvas||!target||!gl){status.supported=false;return;}
    const compile=(type,source)=>{const shader=gl.createShader(type);gl.shaderSource(shader,source);gl.compileShader(shader);return gl.getShaderParameter(shader,gl.COMPILE_STATUS)?shader:null;};
    const vertex=compile(gl.VERTEX_SHADER,vertexSource),fragment=compile(gl.FRAGMENT_SHADER,fragmentSource);if(!vertex||!fragment){status.supported=false;return;}
    const program=gl.createProgram();gl.attachShader(program,vertex);gl.attachShader(program,fragment);gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS)){status.supported=false;return;}gl.useProgram(program);
    const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);
    const position=gl.getAttribLocation(program,'p');gl.enableVertexAttribArray(position);gl.vertexAttribPointer(position,2,gl.FLOAT,false,0,0);
    const texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,texture);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,true);
    const uniform=Object.fromEntries(['dpr','radius','resolution','videoSize','mouse','shape'].map(name=>[name,gl.getUniformLocation(program,name)]));
    function render(){
      const video=document.querySelector('.hero-media video.is-active');const canvasRect=canvas.getBoundingClientRect();const targetRect=target.getBoundingClientRect();const ratio=Math.min(devicePixelRatio||1,maxDpr);
      const width=Math.round(canvasRect.width*ratio),height=Math.round(canvasRect.height*ratio);if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;gl.viewport(0,0,width,height);}
      gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);
      if(key==='nav'&&!navOverHero){status.navBackdrop='current';requestAnimationFrame(render);return;}
      if(visible&&video?.readyState>=2&&video.videoWidth&&width&&height){
        if(key==='nav')status.navBackdrop='video';
        gl.uniform1f(uniform.dpr,ratio);gl.uniform1f(uniform.radius,targetRect.height/2);gl.uniform2f(uniform.resolution,canvasRect.width,canvasRect.height);gl.uniform2f(uniform.videoSize,video.videoWidth,video.videoHeight);
        gl.uniform2f(uniform.mouse,pointer.x-canvasRect.left,canvasRect.bottom-pointer.y);gl.uniform4f(uniform.shape,targetRect.left-canvasRect.left+targetRect.width/2,canvasRect.bottom-targetRect.top-targetRect.height/2,targetRect.width/2,targetRect.height/2);
        gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,video);gl.drawArrays(gl.TRIANGLES,0,6);
        if(!status[key]){status[key]=true;document.documentElement.classList.add(readyClass);status.active=status.hero&&status.nav;}
      }
      requestAnimationFrame(render);
    }
    requestAnimationFrame(render);
  }

  createLens(document.querySelector('[data-hero-liquid-glass]'),document.querySelector('.hero .button-primary'),'hero','hero-liquid-glass-ready',1.5);
  createLens(document.querySelector('[data-nav-liquid-glass]'),document.querySelector('.nav-shell'),'nav','nav-liquid-glass-ready',1.25);
  window.__homepageLiquidGlassCheck=()=>status.active&&document.querySelector('[data-hero-liquid-glass]')?.width>0&&document.querySelector('[data-nav-liquid-glass]')?.width>0;
})();
