/* ============================================================
   黄雀 AI · Cloud Design 落地 — 共享脚本 cloud.js
   1) 金色粒子星网背景（.hq-bg canvas）
   2) 滚动入场（.reveal → .in）
   两者都尊重 prefers-reduced-motion
   ============================================================ */
(function(){
  "use strict";
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion:reduce)').matches;

  /* ---- 1. 金色粒子星网 ---- */
  function initParticles(){
    var cv = document.querySelector('.hq-bg canvas');
    if(!cv || !cv.getContext) return;
    var ctx = cv.getContext('2d');
    var w,h,dpr=Math.min(window.devicePixelRatio||1,2),ps=[],mouse={x:-9999,y:-9999},raf=0,running=false,lastFrame=0,lastMouseAt=0;
    var frameMs=33;
    function rs(){
      w=innerWidth; h=innerHeight; cv.width=w*dpr; cv.height=h*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
      var n=Math.min(64,Math.round(w*h/17000)); ps=[];
      for(var i=0;i<Math.max(16,n);i++)ps.push({x:Math.random()*w,y:Math.random()*h,vx:(Math.random()-.5)*.28,vy:(Math.random()-.5)*.28,r:Math.random()*1.4+.5});
    }
    function draw(){
      ctx.clearRect(0,0,w,h); ctx.globalAlpha=.55;
      var i,a,b;
      for(i=0;i<ps.length;i++){
        var p=ps[i];
        if(!reduced){
          p.x+=p.vx; p.y+=p.vy;
          var dx=p.x-mouse.x,dy=p.y-mouse.y,d2=dx*dx+dy*dy;
          if(d2<13000){var d=Math.sqrt(d2)||1,f=(114-d)/114*.55;p.x+=dx/d*f;p.y+=dy/d*f;}
          if(p.x<0||p.x>w)p.vx*=-1; if(p.y<0||p.y>h)p.vy*=-1;
        }
        ctx.beginPath(); ctx.fillStyle='rgba(231,178,76,.9)'; ctx.shadowColor='rgba(231,178,76,.9)'; ctx.shadowBlur=6;
        ctx.arc(p.x,p.y,p.r,0,6.283); ctx.fill();
      }
      ctx.shadowBlur=0;
      for(a=0;a<ps.length;a++)for(b=a+1;b<ps.length;b++){
        var A=ps[a],B=ps[b],qx=A.x-B.x,qy=A.y-B.y,dd=Math.sqrt(qx*qx+qy*qy);
        if(dd<128){ctx.globalAlpha=(1-dd/128)*.28;ctx.strokeStyle='rgba(231,178,76,1)';ctx.lineWidth=.6;ctx.beginPath();ctx.moveTo(A.x,A.y);ctx.lineTo(B.x,B.y);ctx.stroke();}
      }
    }
    function frame(ts){
      if(document.hidden){running=false;raf=0;return;}
      if(lastFrame && ts-lastFrame<frameMs){raf=requestAnimationFrame(frame);return;}
      lastFrame=ts||0; draw();
      if(running) raf=requestAnimationFrame(frame);
    }
    function start(){
      if(reduced||running||document.hidden) return;
      running=true; raf=requestAnimationFrame(frame);
    }
    function stop(){
      running=false; lastFrame=0;
      if(raf){cancelAnimationFrame(raf);raf=0;}
    }
    rs(); draw(); addEventListener('resize',function(){rs();draw();});
    if(!reduced){
      addEventListener('mousemove',function(e){
        var now=(typeof performance!=='undefined'&&performance.now)?performance.now():Date.now();
        if(now-lastMouseAt<50) return;
        lastMouseAt=now; mouse.x=e.clientX;mouse.y=e.clientY;
      },{passive:true});
      document.addEventListener('visibilitychange',function(){document.hidden?stop():start();});
      start();
    }
  }

  /* ---- 2. 滚动入场 ---- */
  function initReveal(){
    document.documentElement.classList.add('fx');
    var els=[].slice.call(document.querySelectorAll('.reveal'));
    els.forEach(function(el,i){ el.style.transitionDelay=(Math.min(i,8)*0.05)+'s'; });
    if(reduced || !('IntersectionObserver' in window)){ els.forEach(function(el){el.classList.add('in');}); return; }
    var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){en.target.classList.add('in');io.unobserve(en.target);}});},{threshold:.12});
    els.forEach(function(el){io.observe(el);});
  }

  function boot(){ try{ initParticles(); }catch(e){} try{ initReveal(); }catch(e){} }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',boot); else boot();
})();
