const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const form=$('#form'), file=$('#file'), drop=$('#drop'), label=$('#fileLabel'), button=$('#convertButton');
let stageTimer=null, stage=0;

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

const stageText=['Inspecting source architecture...','Mapping functions, controls and assets...','Building canonical safe module...','Preserving source in legacy candidate...','Running compile checks and packaging ZIP...'];
function setStage(n){stage=n;const pct=[12,31,56,78,94][n];$('#progressPercent').textContent=String(pct).padStart(2,'0')+'%';$('#progressBar').style.width=pct+'%';$('.button-progress').style.width=pct+'%';$('#loadingText').textContent=stageText[n];$$('.stage-list>div').forEach((el,i)=>{el.classList.toggle('active',i===n);el.classList.toggle('done',i<n);el.querySelector('b').textContent=i<n?'DONE':i===n?'RUNNING':'WAITING'})}
function startProgress(){stage=0;setStage(0);stageTimer=setInterval(()=>{if(stage<4)setStage(stage+1)},900)}
function stopProgress(success){clearInterval(stageTimer);if(success){$('#progressPercent').textContent='100%';$('#progressBar').style.width='100%';$('.button-progress').style.width='100%';$$('.stage-list>div').forEach(el=>{el.classList.remove('active');el.classList.add('done');el.querySelector('b').textContent='DONE'})}}
function busy(on){button.disabled=on;$('.button-label').textContent=on?'CONVERTING...':'CONVERT SCRIPT';$('#loading').classList.toggle('hidden',!on);if(on){$('#results').classList.add('hidden');startProgress()}else{$('.button-progress').style.width='0'}}

window.bloodmoonConvert=async function(e){e?.preventDefault();clearError();if(!file.files[0]){showError('NO FILE SELECTED','Choose a Luau source file before starting conversion.');return false}if(!acceptFile(file.files[0]))return false;busy(true);try{const response=await fetch('/api/ai-convert',{method:'POST',body:new FormData(form)});let out;try{out=await response.json()}catch{throw new Error(`The server returned an invalid response (${response.status}).`)}if(!response.ok)throw new Error(out.detail||`Conversion failed with status ${response.status}.`);stopProgress(true);await new Promise(r=>setTimeout(r,450));render(out)}catch(error){stopProgress(false);showError('CONVERSION FAILED',error.message||'The conversion engine could not process this file.');$('#fileState').textContent='FAILED / CHECK ERROR'}finally{busy(false)}return false};

function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(out){const m=out.manifest||{},a=m.architecture||{},score=Math.round((m.readiness||0)*100);$('#results').classList.remove('hidden');$('#resultName').textContent=(m.source||file.files[0]?.name||'Bloodmoon project').split('/').pop();$('#architecture').textContent=(a.kind||'unknown').replaceAll('_',' ');$('#archConfidence').textContent=`${Math.round((a.confidence||0)*100)}% classification confidence`;$('#readiness').textContent=score+'%';$('.score-ring').style.background=`conic-gradient(#fff 0 ${score}%,#252728 ${score}% 100%)`;$('#functions').textContent=(m.roles||[]).length;$('#blockers').textContent=(m.blockers||[]).length;$('#keys').innerHTML=(m.keys||[]).map(k=>`<span class="chip">${esc(String(k).toUpperCase())}</span>`).join('')||'<span class="chip">NONE</span>';const ass=m.assets||{};$('#assets').innerHTML=`<b>${(ass.sounds||[]).length} SOUNDS</b><b>${(ass.meshes||[]).length} MESHES</b><b>${(ass.textures||[]).length} TEXTURES</b>`;$('#roles').innerHTML=(m.roles||[]).map(x=>`<div class="role"><div><b>${esc(x.name)}</b><span>Line ${x.line}</span></div><i>${esc(x.role)}</i></div>`).join('');$('#risks').innerHTML=(m.blockers||[]).map(x=>`<div class="risk ${esc(x.severity)}"><div><b>${esc(x.type)}</b><span>${esc(x.message||JSON.stringify(x.lines||x.items||[]))}</span></div><strong>${esc(x.severity)}</strong></div>`).join('')||'<div class="risk"><b>No static blockers detected.</b></div>';$('#overview').textContent=out.overview||'';$('#safeCode').textContent=out.safe_code||'';$('#legacyCode').textContent=out.legacy_code||'';$('#reports').textContent=(out.safe_report||'')+'\n\n--- LEGACY REPORT ---\n\n'+(out.legacy_report||'');let zipUrl=out.downloads.zip;if(out.zip_base64){const raw=atob(out.zip_base64),bytes=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);zipUrl=URL.createObjectURL(new Blob([bytes],{type:'application/zip'}))}$('#zip').href=zipUrl;$('#zip').download='bloodmoon-project.zip';$('#safeDownload').href=out.downloads.safe;$('#legacyDownload').href=out.downloads.legacy;$('#results').scrollIntoView({behavior:'smooth',block:'start'});setTimeout(()=>{const link=document.createElement('a');link.href=zipUrl;link.download='bloodmoon-project.zip';document.body.appendChild(link);link.click();link.remove()},700)}

$$('#tabs button').forEach(b=>b.addEventListener('click',()=>{$$('#tabs button').forEach(x=>x.classList.toggle('active',x===b));$$('.content').forEach(x=>x.classList.toggle('active',x.id===`tab-${b.dataset.tab}`))}));
$$('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('#'+b.dataset.copy).textContent);const old=b.textContent;b.textContent='COPIED';setTimeout(()=>b.textContent=old,1200)}catch{showError('COPY FAILED','Clipboard access was blocked by this browser.')}}));
