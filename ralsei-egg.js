(() => {
  'use strict';
  const logo = document.querySelector('footer .footer-brand');
  if (!logo) return;
  let taps = 0, tapTimer, typed = '', timers = [], returnFocus = null;
  logo.setAttribute('role', 'button');
  logo.setAttribute('tabindex', '0');
  logo.setAttribute('aria-label', 'Bloodmoon footer logo');
  const style = document.createElement('style');
  style.textContent = `
  #bm-ralsei-egg{position:fixed;inset:0;z-index:2147483000;display:grid;place-items:center;padding:16px;box-sizing:border-box;background:rgba(0,0,0,.88);font-family:system-ui,sans-serif;color:#f7fff9}
  #bm-ralsei-egg *{box-sizing:border-box}
  #bm-ralsei-card{position:relative;width:min(680px,100%);background:#070b0a;border:1px solid #739988;box-shadow:0 0 0 5px #080b0a,0 0 0 6px #263b32,0 25px 90px #000;overflow:hidden}
  #bm-ralsei-top{height:42px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;border-bottom:1px solid #293b33;font:700 10px/1 monospace;letter-spacing:.15em;color:#a8dabc}
  #bm-ralsei-close{border:0;background:none;color:#c0dbca;cursor:pointer;font:26px/1 system-ui;padding:0 3px}
  #bm-stage{height:clamp(220px,42vh,340px);position:relative;overflow:hidden;isolation:isolate;background:radial-gradient(ellipse at 50% 80%,#16382f 0,#0a1714 42%,#050807 78%)}
  #bm-stage:before{content:'';position:absolute;inset:0;opacity:.65;background-image:radial-gradient(#a5ffc1 1px,transparent 1.4px),radial-gradient(#f0aaff 1px,transparent 1.3px);background-size:67px 59px,103px 83px;background-position:10px 6px,32px 19px;animation:bm-stars 3s ease-in-out infinite alternate}
  #bm-stage:after{content:'';position:absolute;bottom:-20px;left:0;right:0;height:40px;background:linear-gradient(180deg,#2a664b44,transparent);filter:blur(8px)}
  #bm-moon{position:absolute;width:100px;height:100px;left:50%;top:12%;transform:translateX(-50%);border-radius:50%;background:radial-gradient(circle at 35% 30%,#d3ffe6,#6bd096 50%,#153c2d 80%);opacity:.18;box-shadow:0 0 90px #55ffaf66}
  #bm-actor-wrap{position:absolute;bottom:17%;left:50%;width:0;height:0;transform:translateX(-50%)}
  #bm-actor{position:absolute;left:50%;bottom:0;width:69px;height:132px;transform:translateX(-50%);background:url('/static/ralsei-walk.png') 0 0/276px 132px;image-rendering:pixelated;filter:drop-shadow(0 0 13px #73ffc466);animation:bm-walk .8s steps(4) infinite}
  .bm-enter #bm-actor-wrap{animation:bm-arrive 2.8s ease-out both}
  .bm-magic #bm-actor{left:calc(50% + 70px);width:210px;height:144px;background-image:url('/static/ralsei-magic.png');background-size:1890px 144px;animation:bm-cast 3.3s steps(8,end) 1 forwards}
  #bm-aura{position:absolute;left:50%;bottom:10%;width:160px;height:160px;transform:translateX(-50%) scale(.1);border:2px solid #8dffb8;border-radius:50%;opacity:0;box-shadow:0 0 25px #70ffaf, inset 0 0 28px #70ffaf;pointer-events:none}
  .bm-magic #bm-aura{animation:bm-aura 3.8s ease-out forwards}
  #bm-heart{position:absolute;left:50%;top:24%;opacity:0;transform:translateX(-50%) scale(.1);font-size:33px;color:#ff84c4;text-shadow:0 0 12px #ff6ab4,0 0 30px #fa73be}
  .bm-heart #bm-heart{animation:bm-heart 1.4s cubic-bezier(.2,.8,.2,1) forwards}
  #bm-dialog{min-height:116px;margin:12px;padding:14px 15px 12px;border:2px solid #e4eee7;background:#0b0d0c;display:flex;gap:14px;align-items:flex-start}
  #bm-portrait{flex:0 0 46px;height:88px;background:url('/static/ralsei-walk.png') 0 0/184px 88px;image-rendering:pixelated}
  #bm-words{margin:2px 0;color:#fff;font:15px/1.45 ui-monospace,monospace;white-space:pre-wrap}
  #bm-words:before{content:'♥ ';color:#ff7eb8}
  #bm-ralsei-bottom{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:0 13px 13px;color:#83998b;font:10px/1.3 system-ui}
  #bm-replay{background:transparent;border:1px solid #4b735d;color:#b5ffd0;padding:7px 12px;cursor:pointer;font:700 10px monospace;letter-spacing:.08em}
  #bm-ralsei-bottom a{color:#9cd9af}
  @keyframes bm-walk{to{background-position:-276px 0}}
  @keyframes bm-cast{to{background-position:-1680px 0}}
  @keyframes bm-arrive{from{transform:translateX(calc(-50% - 190px));opacity:0}to{transform:translateX(-50%);opacity:1}}
  @keyframes bm-aura{0%,15%{transform:translateX(-50%) scale(.1);opacity:0}45%{opacity:.85}100%{transform:translateX(-50%) scale(1.8);opacity:0}}
  @keyframes bm-heart{0%{transform:translateX(-50%) scale(.1) translateY(20px);opacity:0}55%{transform:translateX(-50%) scale(1.2) translateY(-8px);opacity:1}100%{transform:translateX(-50%) scale(1) translateY(-15px);opacity:.9}}
  @keyframes bm-stars{from{opacity:.25}to{opacity:.8}}
  @media(max-width:420px){#bm-dialog{margin:9px;padding:11px;min-height:112px}#bm-words{font-size:13px}#bm-ralsei-bottom{font-size:9px}}
  @media(prefers-reduced-motion:reduce){#bm-ralsei-egg *,#bm-ralsei-egg *:before,#bm-ralsei-egg *:after{animation:none!important;transition:none!important}#bm-heart{opacity:.8;transform:translateX(-50%)}}
  `;
  document.head.appendChild(style);
  function clearTimers(){timers.forEach(clearTimeout);timers=[]}
  function schedule(ms,fn){timers.push(setTimeout(fn,ms))}
  function closeEgg(){clearTimers();const el=document.getElementById('bm-ralsei-egg');if(el)el.remove();document.removeEventListener('keydown',escapeEgg);if(returnFocus&&returnFocus.isConnected)returnFocus.focus({preventScroll:true})}
  function escapeEgg(e){if(e.key==='Escape')closeEgg()}
  function setWords(el, text){el.textContent=text}
  function play(egg){
    clearTimers();const stage=egg.querySelector('#bm-stage'), words=egg.querySelector('#bm-words');
    stage.className='bm-enter';setWords(words,'* A warm light appears in the dark...');
    schedule(2800,()=>{stage.className='';setWords(words,'* Oh! Hello there.');});
    schedule(5000,()=>{stage.className='bm-magic';setWords(words,'* Let me cast a little spell for you!');});
    schedule(8300,()=>{stage.className='bm-heart';setWords(words,'* You have been filled with kindness.');});
    schedule(11400,()=>{setWords(words,'* Take a breath. You are doing great!');});
    schedule(14800,()=>{setWords(words,'* See you next time, friend!');});
  }
  function openEgg(){
    if(document.getElementById('bm-ralsei-egg'))return;
    returnFocus=document.activeElement;
    const egg=document.createElement('div');egg.id='bm-ralsei-egg';
    egg.innerHTML=`<div id="bm-ralsei-card" role="dialog" aria-modal="true" aria-label="Ralsei secret scene"><div id="bm-ralsei-top"><span>✦ SECRET ENCOUNTER / BLOODMOON</span><button id="bm-ralsei-close" type="button" aria-label="Close">×</button></div><div id="bm-stage" class="bm-enter"><div id="bm-moon"></div><div id="bm-aura"></div><div id="bm-heart">♥</div><div id="bm-actor-wrap"><div id="bm-actor" role="img" aria-label="Ralsei animation"></div></div></div><div id="bm-dialog"><div id="bm-portrait" aria-hidden="true"></div><p id="bm-words" aria-live="polite"></p></div><div id="bm-ralsei-bottom"><span>Unofficial fan scene · DELTARUNE © Toby Fox · <a href="https://www.spriters-resource.com/pc_computer/deltarune/asset/110469/" target="_blank" rel="noopener noreferrer">Sprite source</a></span><button id="bm-replay" type="button">REPLAY ↻</button></div></div>`;
    document.body.appendChild(egg);egg.querySelector('#bm-ralsei-close').addEventListener('click',closeEgg);egg.querySelector('#bm-replay').addEventListener('click',()=>play(egg));egg.addEventListener('click',e=>{if(e.target===egg)closeEgg()});document.addEventListener('keydown',escapeEgg);egg.querySelector('#bm-ralsei-close').focus();play(egg);
  }
  function tap(){clearTimeout(tapTimer);if(++taps>=5){taps=0;openEgg()}else tapTimer=setTimeout(()=>{taps=0},2500)}
  logo.addEventListener('click',tap);
  logo.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();tap()}});
  document.addEventListener('keydown',e=>{if((e.target instanceof Element&&e.target.closest('input,textarea,select,[contenteditable="true"]'))||e.ctrlKey||e.altKey||e.metaKey)return;if(e.key.length===1){typed=(typed+e.key.toLowerCase()).slice(-6);if(typed==='ralsei'){typed='';openEgg()}}});
})();
