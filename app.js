const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const form=$('#form'), file=$('#file'), drop=$('#drop'), label=$('#fileLabel'), button=$('#convertButton');
let stageTimer=null, stage=0;
const mode=$('#conversionMode');
mode.addEventListener('change',()=>$('#aiNotice').classList.toggle('hidden',mode.value!=='ai'));

if(matchMedia('(pointer:fine)').matches) addEventListener('mousemove',e=>{const g=$('#cursorGlow');g.style.left=e.clientX+'px';g.style.top=e.clientY+'px'});

function showError(title,message){$('#errorTitle').textContent=title;$('#errorMessage').textContent=message;$('#errorBox').classList.remove('hidden');$('#errorBox').scrollIntoView({behavior:'smooth',block:'center'})}
function clearError(){$('#errorBox').classList.add('hidden')}
$('#dismissError').onclick=clearError;

function resetIntake(){drop.classList.remove('has-file');label.textContent='Drop your script here';$('#fileState').textContent='WAITING FOR FILE'}
function acceptFile(f){clearError();if(!f)return false;const ext=(f.name.split('.').pop()||'').toLowerCase();if(!['lua','luau','txt'].includes(ext)){showError('UNSUPPORTED FILE','Choose a .lua, .luau, or .txt source file.');file.value='';resetIntake();return false}if(f.size>4*1024*1024){showError('FILE TOO LARGE','This file exceeds the 4 MB upload limit.');file.value='';resetIntake();return false}if(f.size===0){showError('EMPTY FILE','The selected file contains no source code.');file.value='';resetIntake();return false}label.textContent=f.name;drop.classList.add('has-file');const size=f.size<1024?`${f.size} B`:`${(f.size/1024).toFixed(1)} KB`;$('#fileState').textContent=`${size} / READY`;if(!$('#name').value)$('#name').value=f.name.replace(/\.(lua|luau|txt)$/i,'');return true}
let lastFileSignature='';
function handleNativeFile(){
  const selected=file.files&&file.files[0];
  if(!selected){return}
  const signature=`${selected.name}:${selected.size}:${selected.lastModified}`;
  if(signature===lastFileSignature&&drop.classList.contains('has-file'))return;
  lastFileSignature=signature;
  try{acceptFile(selected)}catch(error){console.error(error);showError('FILE LOAD FAILED',error?.message||'The selected file could not be read by the interface.')}
}
file.addEventListener('change',handleNativeFile);
file.addEventListener('input',handleNativeFile);
['dragenter','dragover'].forEach(type=>drop.addEventListener(type,e=>{e.preventDefault();drop.classList.add('drag')}));
['dragleave','drop'].forEach(type=>drop.addEventListener(type,e=>{e.preventDefault();drop.classList.remove('drag')}));
drop.addEventListener('drop',e=>{const f=e.dataTransfer.files[0];if(!f)return;const dt=new DataTransfer();dt.items.add(f);file.files=dt.files;acceptFile(f)});

const stageText=['Parsing Luau into an AST without executing it...','Building scoped provenance and dependency state...','Rewriting supported controls and R6 joints...','Compiling the emitted module with official Luau tools...','Preparing the moveset and fail-closed report...'];
function setStage(n){stage=n;const pct=[12,31,56,78,94][n];$('#progressPercent').textContent=String(pct).padStart(2,'0')+'%';$('#progressBar').style.width=pct+'%';$('.button-progress').style.width=pct+'%';$('#loadingText').textContent=stageText[n];$$('.stage-list>div').forEach((el,i)=>{el.classList.toggle('active',i===n);el.classList.toggle('done',i<n);el.querySelector('b').textContent=i<n?'DONE':i===n?'RUNNING':'WAITING'})}
function startProgress(){
  $('#progressPercent').textContent='WORKING'; $('#progressBar').style.width='35%';
  $('.button-progress').style.width='35%';
  $('#loadingText').textContent=mode.value==='ai'?'Sending source to the AI provider; waiting for analysis and static validation...':'Waiting for the server to finish static conversion...';
  $$('.stage-list>div').forEach(el=>{el.classList.remove('active','done');el.querySelector('b').textContent='WAITING'});
}
function stopProgress(success){if(success){$('#progressPercent').textContent='DONE';$('#progressBar').style.width='100%';$('.button-progress').style.width='100%'}}
function busy(on){button.disabled=on;$('.button-label').textContent=on?'CONVERTING...':'CONVERT SCRIPT';$('#loading').classList.toggle('hidden',!on);if(on){$('#results').classList.add('hidden');startProgress()}else{$('.button-progress').style.width='0'}}

window.bloodmoonConvert=async function(e){e?.preventDefault();clearError();if(!file.files[0]){showError('NO FILE SELECTED','Choose a Luau source file before starting conversion.');return false}if(!acceptFile(file.files[0]))return false;busy(true);const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),mode.value==='ai'?300000:180000);try{const response=await fetch(mode.value==='ai'?'/api/ai-convert':'/api/deterministic-convert',{method:'POST',body:new FormData(form),signal:controller.signal});let out;try{out=await response.json()}catch{throw new Error(`The server returned an invalid response (${response.status}).`)}if(!response.ok)throw new Error(out.detail||`Conversion failed with status ${response.status}.`);stopProgress(true);await new Promise(r=>setTimeout(r,450));render(out)}catch(error){stopProgress(false);const message=error.name==='AbortError'?'The conversion timed out. Try a smaller source file or retry later.':error.message;showError('CONVERSION FAILED',message||'The conversion engine could not process this file.');$('#fileState').textContent='FAILED / CHECK ERROR'}finally{clearTimeout(timeout);busy(false)}return false};

function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(out){
  const m=out.manifest||{},a=m.architecture||{},score=Math.round((m.readiness||0)*100);
  const runtimeCode=(out.safe_code||'').replace(/\r?\n/g,'\n');
  const diagnosticCode=(out.diagnostic_code||out.legacy_code||'').replace(/\r?\n/g,'\n');
  const reportText=out.safe_report||JSON.stringify(out.report||{},null,2);
  const runtimeReady=m.status==='RUNTIME_TEST_REQUIRED'&&m.output_kind==='runtime_candidate';
  const aiDraft=m.status==='AI_DRAFT_REVIEW_REQUIRED'&&m.output_kind==='ai_draft';
  $('#results').classList.remove('hidden');
  $('#resultStatus').textContent=runtimeReady?'STATIC CHECKS PASSED':aiDraft?'AI DRAFT — NOT VERIFIED':'REVIEW REQUIRED';
  $('#resultMessage').textContent=runtimeReady?'Static checks passed. Runtime testing is still required.':aiDraft?'Experimental AI draft. Some behavior may be missing or invented. Read the report and test in Roblox.':'Conversion is incomplete or validation failed. Diagnostic output, if any, is not runtime-ready.';
  $('#resultName').textContent=(m.source||file.files[0]?.name||'Bloodmoon project').split('/').pop();
  $('#architecture').textContent=(a.kind||'unknown').replaceAll('_',' ');
  $('#archConfidence').textContent=`${Math.round((a.confidence||0)*100)}% classification confidence`;
  $('#readiness').textContent=(aiDraft||m.status==='AI_VALIDATION_FAILED')?'N/A':score+'%';
  $('.score-ring').style.background=`conic-gradient(#fff 0 ${score}%,#252728 ${score}% 100%)`;
  $('#functions').textContent=(m.roles||[]).length;
  $('#blockers').textContent=(m.blockers||[]).length;
  $('#keys').innerHTML=(m.keys||[]).map(k=>`<span class="chip">${esc(String(k).toUpperCase())}</span>`).join('')||'<span class="chip">NONE</span>';
  const ass=m.assets||{};
  if(Array.isArray(ass.ids)||Array.isArray(ass.urls)){
    $('#assets').innerHTML=`<b>${(ass.ids||[]).length} ASSET IDS</b><b>${(ass.urls||[]).length} URLS</b>`;
  }else{
    $('#assets').innerHTML=`<b>${(ass.sounds||[]).length} SOUNDS</b><b>${(ass.meshes||[]).length} MESHES</b><b>${(ass.textures||[]).length} TEXTURES</b>`;
  }
  $('#roles').innerHTML=(m.roles||[]).map(x=>`<div class="role"><div><b>${esc(x.name)}</b><span>Line ${x.line}</span></div><i>${esc(x.role)}</i></div>`).join('');
  $('#risks').innerHTML=(m.blockers||[]).map(x=>`<div class="risk ${esc(x.severity)}"><div><b>${esc(x.type)}</b><span>${esc(x.message||JSON.stringify(x.lines||x.items||[]))}</span></div><strong>${esc(x.severity)}</strong></div>`).join('')||'<div class="risk"><b>No static blockers detected.</b></div>';
  $('#overview').textContent=out.overview||'';
  $('#safeCode').textContent=runtimeCode||'-- No runtime-candidate output was emitted. Open BLOCKERS or REPORT.';
  $('#legacyCode').textContent=diagnosticCode||'-- No diagnostic output.';
  $('#reports').textContent=reportText;
  const save=(text,filename,type='text/plain;charset=utf-8')=>{if(!text.trim()){showError('NO OUTPUT',`${filename} was not emitted. Check BLOCKERS and REPORT.`);return}const blob=new Blob([text.endsWith('\n')?text:text+'\n'],{type});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=filename;link.style.display='none';document.body.appendChild(link);link.click();setTimeout(()=>{link.remove();URL.revokeObjectURL(url)},30000)};
  const wire=(el,text,filename,type)=>{el.removeAttribute('href');el.setAttribute('role','button');el.onclick=e=>{e.preventDefault();save(text,filename,type)}};
  wire($('#safeDownload'),runtimeCode,'converted_moveset.lua');
  $('#safeDownload').textContent=runtimeCode.trim()?'DOWNLOAD':'NO SAFE OUTPUT — BLOCKED';
  wire($('#legacyDownload'),diagnosticCode,aiDraft?'ai_moveset_draft.lua':'diagnostic_only.lua');
  $('#legacyDownload').textContent=diagnosticCode.trim()?(aiDraft?'DOWNLOAD AI DRAFT':'DOWNLOAD DIAGNOSTIC'):'NO OUTPUT';
  if(runtimeCode.trim()){wire($('#zip'),runtimeCode,'converted_moveset.lua');$('#zip').innerHTML='DOWNLOAD CONVERTED_MOVESET.LUA <b>↓</b>'}
  else if(diagnosticCode.trim()){wire($('#zip'),diagnosticCode,aiDraft?'ai_moveset_draft.lua':'diagnostic_only.lua');$('#zip').innerHTML=aiDraft?'DOWNLOAD AI DRAFT (UNVERIFIED) <b>↓</b>':'DOWNLOAD DIAGNOSTIC_ONLY.LUA <b>↓</b>'}
  else{wire($('#zip'),reportText,'conversion.report.json','application/json');$('#zip').innerHTML='NO LUA OUTPUT — DOWNLOAD REPORT <b>↓</b>'}
  $('#results').scrollIntoView({behavior:'smooth',block:'start'});
  if(runtimeReady)setTimeout(()=>save(runtimeCode,'converted_moveset.lua'),700);
}

$$('#tabs button').forEach(b=>b.addEventListener('click',()=>{$$('#tabs button').forEach(x=>x.classList.toggle('active',x===b));$$('.content').forEach(x=>x.classList.toggle('active',x.id===`tab-${b.dataset.tab}`))}));
$$('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('#'+b.dataset.copy).textContent);const old=b.textContent;b.textContent='COPIED';setTimeout(()=>b.textContent=old,1200)}catch{showError('COPY FAILED','Clipboard access was blocked by this browser.')}}));
