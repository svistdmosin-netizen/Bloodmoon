(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fine = matchMedia('(pointer:fine)').matches;
  const canvas = document.getElementById('ashCanvas');
  const ctx = canvas?.getContext('2d');
  let w=0,h=0,dpr=1,particles=[];
  function resize(){if(!canvas)return;dpr=Math.min(devicePixelRatio||1,2);w=innerWidth;h=innerHeight;canvas.width=w*dpr;canvas.height=h*dpr;canvas.style.width=w+'px';canvas.style.height=h+'px';ctx.setTransform(dpr,0,0,dpr,0,0)}
  function seed(){particles=Array.from({length:Math.min(55,Math.floor(innerWidth/16))},()=>({x:Math.random()*w,y:Math.random()*h,r:.4+Math.random()*1.4,v:.08+Math.random()*.28,dx:(Math.random()-.5)*.08,a:.08+Math.random()*.32}))}
  function draw(){if(!ctx||reduce)return;ctx.clearRect(0,0,w,h);for(const p of particles){p.y-=p.v;p.x+=p.dx;if(p.y<-5){p.y=h+5;p.x=Math.random()*w}ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,7);ctx.fillStyle=`rgba(255,82,105,${p.a})`;ctx.fill()}requestAnimationFrame(draw)}
  resize();seed();draw();addEventListener('resize',()=>{resize();seed()},{passive:true});

  const moon=document.querySelector('.hero-art');
  addEventListener('scroll',()=>{if(!moon||reduce)return;const y=Math.min(scrollY,700);moon.style.transform=`translate3d(0,${y*.12}px,0) scale(${1+y*.00008})`},{passive:true});

  if(fine&&!reduce){
    const dot=document.getElementById('cursorDot'), ring=document.getElementById('cursorRing');let mx=0,my=0,rx=0,ry=0;
    addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY;dot.style.transform=`translate(${mx}px,${my}px)`});
    (function follow(){rx+=(mx-rx)*.13;ry+=(my-ry)*.13;ring.style.transform=`translate(${rx}px,${ry}px)`;requestAnimationFrame(follow)})();
    document.querySelectorAll('a,button,.drop').forEach(el=>{el.addEventListener('mouseenter',()=>ring.classList.add('hot'));el.addEventListener('mouseleave',()=>ring.classList.remove('hot'))});
  }

  const observer=new IntersectionObserver(entries=>entries.forEach(e=>{if(e.isIntersecting)e.target.classList.add('in-view')}),{threshold:.12});
  document.querySelectorAll('.maker-note,.section-title,.machine,.results,.creator-card').forEach(el=>{el.classList.add('observe');observer.observe(el)});

  document.querySelectorAll('.hero-button,.nav-cta,.convert-button,.download-main').forEach(el=>el.addEventListener('pointermove',e=>{if(!fine)return;const r=el.getBoundingClientRect();el.style.setProperty('--mx',`${e.clientX-r.left}px`);el.style.setProperty('--my',`${e.clientY-r.top}px`)}));

  if(!fine){
    document.querySelectorAll('a,button,.drop').forEach(el=>el.addEventListener('pointerup',()=>{
      setTimeout(()=>{ if(document.activeElement instanceof HTMLElement) document.activeElement.blur(); },80);
    }));
  }
})();
