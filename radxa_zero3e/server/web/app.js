let token=localStorage.getItem('robotlidar_token')||'';
let devices=[];
let selected=null;
let currentPeer=null;
const $=id=>document.getElementById(id);

function authHeaders(extra={}){return token?{...extra,Authorization:`Bearer ${token}`}:{...extra};}
async function api(url,opts={}){opts.headers=authHeaders(opts.headers||{});const r=await fetch(url,opts);if(r.status===401){logoutLocal();throw new Error('Требуется вход');}let data={};try{data=await r.json();}catch{}if(!r.ok)throw new Error(data.detail||`HTTP ${r.status}`);return data;}

async function login(register=false){$('authError').textContent='';try{const data=await api(`/api/auth/${register?'register':'login'}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('loginName').value.trim(),password:$('loginPassword').value})});token=data.token;localStorage.setItem('robotlidar_token',token);await enterApp();}catch(e){$('authError').textContent=e.message;}}
async function enterApp(){try{const me=await api('/api/auth/me');$('usernameLabel').textContent=me.username;$('authScreen').classList.add('hidden');$('appShell').classList.remove('hidden');await Promise.all([loadDevices(),loadLinked()]);}catch(e){logoutLocal();}}
function logoutLocal(){closePeer();token='';localStorage.removeItem('robotlidar_token');$('appShell').classList.add('hidden');$('authScreen').classList.remove('hidden');}
async function logout(){try{await api('/api/auth/logout',{method:'POST'});}catch{}logoutLocal();}

async function loadDevices(){try{const data=await api('/api/devices',{cache:'no-store'});devices=data.devices||[];}catch(e){return;}renderDevices();if(!selected&&devices.length)selectDevice(devices.find(d=>d.online)||devices[0]);else if(selected){const d=devices.find(x=>x.id===selected.id);if(d)selectDevice(d,false);else{selected=null;closePeer();}}}
function renderDevices(){$('deviceCount').textContent=devices.length;$('deviceList').innerHTML='';if(!devices.length){$('deviceList').innerHTML='<div class="empty-note">Нет привязанных тракторов.<br>Добавьте ID в настройках.</div>';return;}devices.forEach(d=>{const el=document.createElement('div');el.className='device'+(selected?.id===d.id?' active':'');el.innerHTML=`<div class="device-top"><div><div class="device-name">${escapeHtml(d.name)}</div><div class="device-location">${escapeHtml(d.id)}</div></div><span class="device-state ${d.online?'online':'offline'}">${d.online?'online':'offline'}</span></div>`;el.onclick=()=>selectDevice(d);$('deviceList').appendChild(el);});}
function selectDevice(d,reconnect=true){selected={...d};renderDevices();$('cameraTitle').textContent=d.name;$('cameraSubtitle').textContent=d.id;setOnline(d.online);$('panRange').value=d.pan||0;$('tiltRange').value=d.tilt||0;updatePtzLabels();updateTelemetry(d);if(reconnect)connectStream(d);}
function setOnline(v){$('onlineDot').className='dot '+(v?'online':'offline');$('onlineText').textContent=v?'Online':'Offline';}
function updateTelemetry(d){$('fpsMetric').textContent=d.fps||'—';$('bitrateMetric').textContent=d.bitrateKbps?`${d.bitrateKbps} kbps`:'—';$('ethernetMetric').textContent=d.ethernet||'—';$('uptimeMetric').textContent=formatUptime(d.uptimeSec||0);}

function closePeer(){if(currentPeer){try{currentPeer.close();}catch{}currentPeer=null;}const v=$('video');v.srcObject=null;}
function waitIce(pc){return new Promise(resolve=>{if(pc.iceGatheringState==='complete')return resolve();const done=()=>{if(pc.iceGatheringState==='complete'){pc.removeEventListener('icegatheringstatechange',done);resolve();}};pc.addEventListener('icegatheringstatechange',done);setTimeout(resolve,2500);});}

function preferH264(transceiver){
  try{
    if(!RTCRtpReceiver.getCapabilities||!transceiver.setCodecPreferences)return true;
    const caps=RTCRtpReceiver.getCapabilities('video');
    const h264=(caps?.codecs||[]).filter(c=>String(c.mimeType).toLowerCase()==='video/h264');
    if(!h264.length)return false;
    h264.sort((a,b)=>{
      const ap=String(a.sdpFmtpLine||'').includes('packetization-mode=1')?0:1;
      const bp=String(b.sdpFmtpLine||'').includes('packetization-mode=1')?0:1;
      return ap-bp;
    });
    transceiver.setCodecPreferences(h264);
    return true;
  }catch(e){console.warn('H264 codec preference',e);return true;}
}

async function connectStream(d){
  const video=$('video');
  closePeer();
  $('videoPlaceholder').style.display='flex';
  $('videoPlaceholder').querySelector('div:nth-child(2)').textContent=d.online?(d.video_online?'Подключение H.264 WebRTC...':'Видеопоток пока не поступает'):'Трактор offline';
  if(!d.online||!d.video_online)return;

  const pc=new RTCPeerConnection();
  currentPeer=pc;
  const transceiver=pc.addTransceiver('video',{direction:'recvonly'});
  if(!preferH264(transceiver)){
    closePeer();
    $('videoPlaceholder').querySelector('div:nth-child(2)').textContent='Браузер не поддерживает H.264 WebRTC';
    return;
  }
  pc.ontrack=e=>{
    video.srcObject=e.streams[0]||new MediaStream([e.track]);
    video.play().catch(()=>{});
    $('videoPlaceholder').style.display='none';
  };
  pc.onconnectionstatechange=()=>{
    if(pc!==currentPeer)return;
    if(['failed','disconnected','closed'].includes(pc.connectionState)){
      $('videoPlaceholder').style.display='flex';
      $('videoPlaceholder').querySelector('div:nth-child(2)').textContent=`WebRTC: ${pc.connectionState}`;
    }
  };

  try{
    const offer=await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitIce(pc);
    const answer=await api(`/api/devices/${encodeURIComponent(d.id)}/webrtc`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type})
    });
    if(pc!==currentPeer)return;
    await pc.setRemoteDescription(answer);
  }catch(e){
    console.error('WebRTC',e);
    if(pc===currentPeer){
      closePeer();
      $('videoPlaceholder').style.display='flex';
      $('videoPlaceholder').querySelector('div:nth-child(2)').textContent=`Ошибка видео: ${e.message}`;
    }
  }
}

function updatePtzLabels(){$('panValue').textContent=`${Number($('panRange').value).toFixed(1)}°`;$('tiltValue').textContent=`${Number($('tiltRange').value).toFixed(1)}°`;$('speedValue').textContent=`${$('speedRange').value}°/с`;}
async function sendAbsolutePtz(){if(!selected?.online)return;const body={pan_cdeg:Math.round(Number($('panRange').value)*100),tilt_cdeg:Math.round(Number($('tiltRange').value)*100),speed_cdeg_s:Math.round(Number($('speedRange').value)*100)};try{await api(`/api/devices/${encodeURIComponent(selected.id)}/ptz`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(e){console.warn(e);}updatePtzLabels();}
async function move(dir){if(!selected?.online)return;let p=Number($('panRange').value),t=Number($('tiltRange').value);if(dir==='left')p-=5;if(dir==='right')p+=5;if(dir==='up')t+=5;if(dir==='down')t-=5;$('panRange').value=Math.max(-90,Math.min(90,p));$('tiltRange').value=Math.max(-45,Math.min(45,t));await sendAbsolutePtz();}
async function center(){if(!selected?.online)return;$('panRange').value=0;$('tiltRange').value=0;updatePtzLabels();await api(`/api/devices/${encodeURIComponent(selected.id)}/center`,{method:'POST'});}
async function requestIdr(){if(selected?.online)await api(`/api/devices/${encodeURIComponent(selected.id)}/request-idr`,{method:'POST'});}

async function loadLinked(){try{const data=await api('/api/settings/devices');const list=data.devices||[];$('linkedDevices').innerHTML=list.length?'':'<div class="empty-note">Тракторы пока не добавлены.</div>';list.forEach(d=>{const row=document.createElement('div');row.className='linked-row';row.innerHTML=`<div><strong>${escapeHtml(d.alias||d.device_id)}</strong><span>${escapeHtml(d.device_id)}</span></div><button class="btn danger">Удалить</button>`;row.querySelector('button').onclick=()=>detach(d.device_id);$('linkedDevices').appendChild(row);});}catch(e){}}
async function attach(){const id=$('attachId').value.trim(),alias=$('attachAlias').value.trim();$('attachError').textContent='';try{await api('/api/settings/devices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:id,alias:alias||null})});$('attachId').value='';$('attachAlias').value='';await Promise.all([loadLinked(),loadDevices()]);}catch(e){$('attachError').textContent=e.message;}}
async function detach(id){try{await api(`/api/settings/devices/${encodeURIComponent(id)}`,{method:'DELETE'});if(selected?.id===id){selected=null;closePeer();}await Promise.all([loadLinked(),loadDevices()]);}catch(e){alert(e.message);}}
function switchView(name){document.querySelectorAll('.nav-tab').forEach(b=>b.classList.toggle('active',b.dataset.view===name));$('cameraView').classList.toggle('hidden',name!=='cameras');$('settingsView').classList.toggle('hidden',name!=='settings');$('cameraNav').classList.toggle('hidden',name!=='cameras');if(name==='settings')loadLinked();}
function formatUptime(sec){if(!sec)return '—';const d=Math.floor(sec/86400),h=Math.floor((sec%86400)/3600),m=Math.floor((sec%3600)/60);return d?`${d}д ${h}ч`:`${h}ч ${m}м`;}
function escapeHtml(s){return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}

document.querySelectorAll('[data-move]').forEach(b=>b.onclick=()=>move(b.dataset.move));
document.querySelectorAll('.nav-tab').forEach(b=>b.onclick=()=>switchView(b.dataset.view));
$('loginBtn').onclick=()=>login(false);$('registerBtn').onclick=()=>login(true);$('logoutBtn').onclick=logout;$('attachBtn').onclick=attach;$('centerBtn').onclick=center;$('idrBtn').onclick=requestIdr;$('refreshBtn').onclick=loadDevices;
$('panRange').oninput=updatePtzLabels;$('tiltRange').oninput=updatePtzLabels;$('speedRange').oninput=updatePtzLabels;$('panRange').onchange=sendAbsolutePtz;$('tiltRange').onchange=sendAbsolutePtz;
document.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName))return;const map={arrowup:'up',w:'up',arrowdown:'down',s:'down',arrowleft:'left',a:'left',arrowright:'right',d:'right'};const k=e.key.toLowerCase();if(map[k]){e.preventDefault();move(map[k]);}});
if(token)enterApp();
setInterval(()=>{if(token)loadDevices();},3000);
