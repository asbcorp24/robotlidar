let token=localStorage.getItem('robotlidar_token')||'';
let devices=[];
let selected=null;
let currentPeer=null;
let cardboardActive=false;
let gyroBase=null;
let lastGyroSend=0;
let gyroPanBase=0;
let gyroTiltBase=0;
let brushSpin=0;
let brushLift=0;
let driveActive=false;
let videoDemand=null;
let videoReconnectTimer=null;
let talkActive=false;
let talkStarting=false;
let talkStream=null;
let talkContext=null;
let talkSource=null;
let talkProcessor=null;
let talkPending=new Uint8Array(0);
let talkSending=false;
let talkGestureToken=0;
let talkRecorder=null;
let talkRecordedChunks=[];
let talkRecordedMime='audio/webm';
let talkRecordedDeviceId='';
let talkResampleRate=0;
let talkResampleNeed=0;
let talkResampleAcc=0;
let talkResampleWeight=0;
let talkFilterState=new Float32Array(12);
let talkFilterPos=0;
const $=id=>document.getElementById(id);

function authHeaders(extra={}){return token?{...extra,Authorization:`Bearer ${token}`}:{...extra};}
async function api(url,opts={}){opts.headers=authHeaders(opts.headers||{});const r=await fetch(url,opts);if(r.status===401){logoutLocal();throw new Error('Требуется вход');}let data={};try{data=await r.json();}catch{}if(!r.ok)throw new Error(data.detail||`HTTP ${r.status}`);return data;}

async function login(register=false){$('authError').textContent='';try{const data=await api(`/api/auth/${register?'register':'login'}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('loginName').value.trim(),password:$('loginPassword').value})});token=data.token;localStorage.setItem('robotlidar_token',token);await enterApp();}catch(e){$('authError').textContent=e.message;}}
async function enterApp(){try{const me=await api('/api/auth/me');$('usernameLabel').textContent=me.username;$('authScreen').classList.add('hidden');$('appShell').classList.remove('hidden');await Promise.all([loadDevices(),loadLinked()]);}catch(e){logoutLocal();}}
function logoutLocal(){emergencyStop();stopTalkback();exitCardboard();closePeer();token='';localStorage.removeItem('robotlidar_token');$('appShell').classList.add('hidden');$('authScreen').classList.remove('hidden');}
async function logout(){try{await emergencyStop();await api('/api/auth/logout',{method:'POST'});}catch{}logoutLocal();}

async function loadDevices(){try{const data=await api('/api/devices',{cache:'no-store'});devices=data.devices||[];}catch(e){return;}renderDevices();if(!selected&&devices.length)selectDevice(devices.find(d=>d.online)||devices[0]);else if(selected){const d=devices.find(x=>x.id===selected.id);if(d)selectDevice(d,false);else{selected=null;emergencyStop();exitCardboard();closePeer();}}}
function renderDevices(){$('deviceCount').textContent=devices.length;$('deviceList').innerHTML='';if(!devices.length){$('deviceList').innerHTML='<div class="empty-note">Нет привязанных тракторов.<br>Добавьте ID в настройках.</div>';return;}devices.forEach(d=>{const el=document.createElement('div');el.className='device'+(selected?.id===d.id?' active':'');el.innerHTML=`<div class="device-top"><div><div class="device-name">${escapeHtml(d.name)}</div><div class="device-location">${escapeHtml(d.id)}</div></div><span class="device-state ${d.online?'online':'offline'}">${d.online?'online':'offline'}</span></div>`;el.onclick=()=>selectDevice(d);$('deviceList').appendChild(el);});}
function selectDevice(d,reconnect=true){if(selected&&selected.id!==d.id){emergencyStop();stopTalkback();}if(cardboardActive)exitCardboard();selected={...d};brushSpin=0;brushLift=0;driveActive=false;renderDevices();$('cameraTitle').textContent=d.name;$('cameraSubtitle').textContent=d.id;setOnline(d.online);$('panRange').value=d.pan||0;$('tiltRange').value=d.tilt||0;updatePtzLabels();updateTelemetry(d);updateCameraUi(d);updateDeviceCapabilities(d);updateDriveUi();updateBrushUi();if(reconnect)connectStream(d);}
function setOnline(v){$('onlineDot').className='dot '+(v?'online':'offline');$('onlineText').textContent=v?'Online':'Offline';}
function isCameraOnlyDevice(d=selected){
  const t=String(d?.device_type||'').toLowerCase();
  return t.startsWith('orange_pi_zero_camera')
    || t==='orange_pi_ipcam'
    || t==='raspberry_robotlidar'
    || t.startsWith('raspberry_pi_zero')
    || t==='rpz'
    || t.startsWith('rpz_');
}
function updateDeviceCapabilities(d=selected){
  const cameraOnly=isCameraOnlyDevice(d);
  document.querySelectorAll('.tractor-panel,.brush-panel').forEach(el=>el.classList.toggle('hidden',cameraOnly));
  $('talkbackPanel')?.classList.toggle('hidden',!d?.talkback);
  $('gpsBtn')?.classList.toggle('hidden',!d?.gps?.valid);
  if($('talkBtn'))$('talkBtn').disabled=!d?.online||!d?.talkback;
  if(cameraOnly){
    driveActive=false;
    brushSpin=0;
    brushLift=0;
  }
  if(!d?.talkback&&talkActive)stopTalkback();
  if(!d?.gps?.valid&&!$('gpsModal')?.classList.contains('hidden'))closeGpsModal();
}
function updateTelemetry(d){$('fpsMetric').textContent=d.fps||'—';$('bitrateMetric').textContent=d.bitrateKbps?`${d.bitrateKbps} kbps`:'—';$('ethernetMetric').textContent=d.ethernet||'—';$('uptimeMetric').textContent=formatUptime(d.uptimeSec||0);}

function openGpsModal(){
  const gps=selected?.gps;
  if(!gps?.valid)return;
  const lat=Number(gps.latitude),lon=Number(gps.longitude);
  if(!Number.isFinite(lat)||!Number.isFinite(lon))return;
  const delta=.006;
  const bbox=[lon-delta,lat-delta,lon+delta,lat+delta].join(',');
  $('gpsTitle').textContent=selected?.name||selected?.id||'Трактор';
  $('gpsCoords').textContent=`${lat.toFixed(6)}, ${lon.toFixed(6)}`;
  const updated=Number(gps.updated_ms||0);
  $('gpsUpdated').textContent=updated?`Последняя координата: ${new Date(updated).toLocaleString('ru-RU')}`:'';
  $('gpsMap').src='https://www.openstreetmap.org/export/embed.html?bbox='+encodeURIComponent(bbox)+'&layer=mapnik&marker='+encodeURIComponent(lat+','+lon);
  $('gpsModal').classList.remove('hidden');
  $('gpsModal').setAttribute('aria-hidden','false');
}
function closeGpsModal(){
  $('gpsModal')?.classList.add('hidden');
  $('gpsModal')?.setAttribute('aria-hidden','true');
  if($('gpsMap'))$('gpsMap').src='about:blank';
}

function stopVideoDemand(){const d=videoDemand;if(!d)return;videoDemand=null;if(d.timer)clearInterval(d.timer);api(`/api/devices/${encodeURIComponent(d.deviceId)}/video-demand`,{method:'POST',keepalive:true,headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:d.sessionId,active:false})}).catch(()=>{});}
function closePeer(){if(videoReconnectTimer){clearTimeout(videoReconnectTimer);videoReconnectTimer=null;}stopVideoDemand();if(currentPeer){try{currentPeer.close();}catch{}currentPeer=null;}const v=$('video');v.srcObject=null;syncVrStreams(null);}
function scheduleVideoReconnect(delay=1800){if(videoReconnectTimer||document.hidden||!selected?.online)return;const id=selected.id;videoReconnectTimer=setTimeout(()=>{videoReconnectTimer=null;if(!document.hidden&&selected?.online&&selected.id===id)connectStream({...selected});},delay);}
function demandSessionId(){try{return crypto.randomUUID();}catch{return 'viewer-'+Date.now()+'-'+Math.random().toString(16).slice(2);}}
async function startVideoDemand(deviceId){stopVideoDemand();const sessionId=demandSessionId();const send=()=>api(`/api/devices/${encodeURIComponent(deviceId)}/video-demand`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sessionId,active:true})});await send();const timer=setInterval(()=>send().catch(()=>{}),5000);videoDemand={deviceId,sessionId,timer};}
async function waitVideoOnline(deviceId,baselinePackets=0,timeoutMs=25000){const end=Date.now()+timeoutMs;while(Date.now()<end){const data=await api('/api/devices',{cache:'no-store'});const d=(data.devices||[]).find(x=>x.id===deviceId);if(d?.video_online&&Number(d.video_packets||0)>Number(baselinePackets||0))return d;await new Promise(r=>setTimeout(r,650));}throw new Error('Камера не запустила видеопоток');}
function waitIce(pc){return new Promise(resolve=>{if(pc.iceGatheringState==='complete')return resolve();const done=()=>{if(pc.iceGatheringState==='complete'){pc.removeEventListener('icegatheringstatechange',done);resolve();}};pc.addEventListener('icegatheringstatechange',done);setTimeout(resolve,2500);});}
function preferH264(transceiver){try{if(!RTCRtpReceiver.getCapabilities||!transceiver.setCodecPreferences)return true;const caps=RTCRtpReceiver.getCapabilities('video');const h264=(caps?.codecs||[]).filter(c=>String(c.mimeType).toLowerCase()==='video/h264');if(!h264.length)return false;h264.sort((a,b)=>(String(a.sdpFmtpLine||'').includes('packetization-mode=1')?0:1)-(String(b.sdpFmtpLine||'').includes('packetization-mode=1')?0:1));transceiver.setCodecPreferences(h264);return true;}catch(e){console.warn('H264 codec preference',e);return true;}}
function syncVrStreams(stream){for(const id of ['vrVideoLeft','vrVideoRight']){const v=$(id);v.srcObject=stream;if(stream)v.play().catch(()=>{});}}

async function connectStream(d){const video=$('video');closePeer();$('videoPlaceholder').style.display='flex';$('videoPlaceholder').querySelector('div:nth-child(2)').textContent=d.online?'Запрос видеопотока...':'Трактор offline';if(!d.online)return;try{await startVideoDemand(d.id);$('videoPlaceholder').querySelector('div:nth-child(2)').textContent='Запуск SRT...';const live=await waitVideoOnline(d.id,d.video_packets||0);if(!selected||selected.id!==d.id){stopVideoDemand();return;}const pc=new RTCPeerConnection();currentPeer=pc;const transceiver=pc.addTransceiver('video',{direction:'recvonly'});if(!preferH264(transceiver)){closePeer();$('videoPlaceholder').querySelector('div:nth-child(2)').textContent='Браузер не поддерживает H.264 WebRTC';return;}pc.ontrack=e=>{const stream=e.streams[0]||new MediaStream([e.track]);video.srcObject=stream;syncVrStreams(stream);video.play().catch(()=>{});$('videoPlaceholder').style.display='none';};pc.onconnectionstatechange=()=>{if(pc!==currentPeer)return;if(['failed','disconnected','closed'].includes(pc.connectionState)){$('videoPlaceholder').style.display='flex';$('videoPlaceholder').querySelector('div:nth-child(2)').textContent=`WebRTC: ${pc.connectionState}`;if(pc.connectionState!=='disconnected')stopVideoDemand();if(pc.connectionState!=='closed')scheduleVideoReconnect(pc.connectionState==='disconnected'?2500:1200);}};const offer=await pc.createOffer();await pc.setLocalDescription(offer);await waitIce(pc);const answer=await api(`/api/devices/${encodeURIComponent(live.id)}/webrtc`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type})});if(pc!==currentPeer)return;await pc.setRemoteDescription(answer);}catch(e){console.error('WebRTC',e);if(selected?.id===d.id){closePeer();$('videoPlaceholder').style.display='flex';$('videoPlaceholder').querySelector('div:nth-child(2)').textContent=`Ошибка видео: ${e.message}. Повторное подключение…`;scheduleVideoReconnect(1800);}}}

function fullscreenElement(){return document.fullscreenElement||document.webkitFullscreenElement||null;}
async function toggleVideoFullscreen(){
  const card=$('videoCard');
  if(!card)return;
  try{
    if(fullscreenElement()){
      if(document.exitFullscreen)await document.exitFullscreen();
      else if(document.webkitExitFullscreen)document.webkitExitFullscreen();
    }else{
      if(card.requestFullscreen)await card.requestFullscreen();
      else if(card.webkitRequestFullscreen)card.webkitRequestFullscreen();
      else if($('video')?.webkitEnterFullscreen)$('video').webkitEnterFullscreen();
    }
  }catch(e){console.warn('fullscreen',e);}
}
function updateFullscreenButton(){
  const b=$('fullscreenBtn');
  if(!b)return;
  const active=fullscreenElement()===$('videoCard');
  b.textContent=active?'⛶ Свернуть':'⛶ На весь экран';
}

function updatePtzLabels(){$('panValue').textContent=`${Number($('panRange').value).toFixed(1)}°`;$('tiltValue').textContent=`${Number($('tiltRange').value).toFixed(1)}°`;$('speedValue').textContent=`${$('speedRange').value}°/с`;}
async function sendAbsolutePtz(){if(!selected?.online)return;const body={pan_cdeg:Math.round(Number($('panRange').value)*100),tilt_cdeg:Math.round(Number($('tiltRange').value)*100),speed_cdeg_s:Math.round(Number($('speedRange').value)*100)};try{await api(`/api/devices/${encodeURIComponent(selected.id)}/ptz`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(e){console.warn(e);}updatePtzLabels();}
async function move(dir){if(!selected?.online)return;let p=Number($('panRange').value),t=Number($('tiltRange').value);if(dir==='left')p-=5;if(dir==='right')p+=5;if(dir==='up')t+=5;if(dir==='down')t-=5;$('panRange').value=Math.max(-90,Math.min(90,p));$('tiltRange').value=Math.max(-45,Math.min(45,t));await sendAbsolutePtz();}
async function center(){if(!selected?.online)return;$('panRange').value=0;$('tiltRange').value=0;updatePtzLabels();await api(`/api/devices/${encodeURIComponent(selected.id)}/center`,{method:'POST'});}
async function requestIdr(){if(selected?.online)await api(`/api/devices/${encodeURIComponent(selected.id)}/request-idr`,{method:'POST'});}

function updateCameraUi(d=selected){const n=Number(d?.active_camera||1)===2?2:1;$('activeCameraState').textContent=`Активная: Camera ${n}`;$('camera1Btn').className='btn '+(n===1?'primary':'secondary');$('camera2Btn').className='btn '+(n===2?'primary':'secondary');}
async function switchCamera(camera){if(!selected?.online)return;camera=camera===2?2:1;const b=camera===2?$('camera2Btn'):$('camera1Btn');b.disabled=true;try{await api(`/api/devices/${encodeURIComponent(selected.id)}/camera`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera})});selected.active_camera=camera;updateCameraUi(selected);$('activeCameraState').textContent=`Переключение на Camera ${camera}...`;setTimeout(async()=>{await loadDevices();if(selected?.online)connectStream(selected);},900);}catch(e){alert(e.message);}finally{b.disabled=false;}}

function driveSpeed(){return Math.round(Number($('driveSpeedRange').value)*10);}
function updateDriveUi(){$('driveSpeedValue').textContent=`${$('driveSpeedRange').value}%`;if(!driveActive)$('driveState').textContent='Гусеницы: STOP';}
async function sendDrive(left,right,label=''){if(!selected?.online)return;driveActive=left!==0||right!==0;$('driveState').textContent=driveActive?`Гусеницы: ${label} · L ${Math.round(left/10)}% · R ${Math.round(right/10)}%`:'Гусеницы: STOP';try{await api(`/api/devices/${encodeURIComponent(selected.id)}/drive`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({left,right})});}catch(e){console.warn('drive',e);}}
function startDrive(dir){const s=driveSpeed();if(dir==='forward')sendDrive(s,s,'ВПЕРЁД');if(dir==='backward')sendDrive(-s,-s,'НАЗАД');if(dir==='left')sendDrive(-s,s,'ВЛЕВО');if(dir==='right')sendDrive(s,-s,'ВПРАВО');}
async function stopDrive(){driveActive=false;updateDriveUi();if(!selected?.online)return;try{await api(`/api/devices/${encodeURIComponent(selected.id)}/drive-stop`,{method:'POST'});}catch(e){console.warn('drive stop',e);}}

function brushSpeed(){return Math.round(Number($('brushSpeedRange').value)*10);}
function updateBrushUi(){$('brushSpeedValue').textContent=`${$('brushSpeedRange').value}%`;const spin=brushSpin>0?'вперёд':brushSpin<0?'реверс':'остановлена';const lift=brushLift>0?'ПОДЪЁМ':brushLift<0?'ОПУСКАНИЕ':'STOP';$('brushState').textContent=`Щётка: ${spin} · подъём: ${lift}`;}
async function sendBrush(){updateBrushUi();if(!selected?.online)return;try{await api(`/api/devices/${encodeURIComponent(selected.id)}/brush`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({spin:brushSpin,lift:brushLift})});}catch(e){console.warn('brush',e);}}
function setBrushSpin(dir){brushSpin=dir==='forward'?brushSpeed():dir==='reverse'?-brushSpeed():0;sendBrush();}
function refreshBrushSpin(){if(brushSpin>0)brushSpin=brushSpeed();else if(brushSpin<0)brushSpin=-brushSpeed();sendBrush();}
function startBrushLift(dir){brushLift=dir==='up'?1000:-1000;sendBrush();}
function stopBrushLift(){if(brushLift===0)return;brushLift=0;sendBrush();}
async function emergencyStop(){const hadSelected=selected?.online;driveActive=false;brushLift=0;updateDriveUi();updateBrushUi();if(!hadSelected)return;try{await Promise.allSettled([api(`/api/devices/${encodeURIComponent(selected.id)}/drive-stop`,{method:'POST'}),api(`/api/devices/${encodeURIComponent(selected.id)}/brush`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({spin:brushSpin,lift:0})})]);}catch{}}

function talkSetState(text,active=false){
  if($('talkState'))$('talkState').textContent=text;
  if($('talkBtn')){
    $('talkBtn').classList.toggle('talking',active);
    $('talkBtn').textContent=active?'🔴 Говорите…':'🎙 Удерживайте, чтобы говорить';
  }
}
function concatBytes(a,b){
  const out=new Uint8Array(a.length+b.length);
  out.set(a,0);out.set(b,a.length);return out;
}
function resetTalkResampler(inputRate=0){
  talkResampleRate=Number(inputRate)||0;
  talkResampleNeed=talkResampleRate>0?talkResampleRate/8000:0;
  talkResampleAcc=0;
  talkResampleWeight=0;
  talkFilterState=new Float32Array(12);
  talkFilterPos=0;
}
function lowpassTalkSample(x){
  // Simple 12-tap moving-average low-pass before 44.1/48 kHz -> 8 kHz
  // downsampling. Speech bandwidth is limited anyway by Tapo PCMA/8000.
  talkFilterState[talkFilterPos]=x;
  talkFilterPos=(talkFilterPos+1)%talkFilterState.length;
  let sum=0;
  for(let i=0;i<talkFilterState.length;i++)sum+=talkFilterState[i];
  return sum/talkFilterState.length;
}
function resampleTo8k(input,inputRate){
  if(!input?.length||!inputRate)return new Int16Array(0);
  if(talkResampleRate!==inputRate||talkResampleNeed<=0)resetTalkResampler(inputRate);

  // Stateful area/box resampler. Unlike sampling each ScriptProcessor block
  // independently, this keeps fractional phase across block boundaries and
  // low-pass filters before reducing 44.1/48 kHz microphone audio to 8 kHz.
  const ratio=inputRate/8000;
  const tmp=[];
  let need=talkResampleNeed;
  let acc=talkResampleAcc;
  let weight=talkResampleWeight;

  for(let i=0;i<input.length;i++){
    let remaining=1;
    const raw=Number.isFinite(input[i])?input[i]:0;
    const x=lowpassTalkSample(raw);
    while(remaining>1e-9){
      const take=Math.min(remaining,need);
      acc+=x*take;
      weight+=take;
      need-=take;
      remaining-=take;
      if(need<=1e-9){
        let sample=weight>0?acc/weight:0;
        // Mild gain with soft limiting. Keep speech level healthy without
        // hard clipping before G.711 A-law encoding.
        sample*=1.15;
        sample=Math.tanh(sample);
        sample=Math.max(-0.98,Math.min(0.98,sample));
        tmp.push(Math.round(sample*32767));
        acc=0;
        weight=0;
        need=ratio;
      }
    }
  }

  talkResampleNeed=need;
  talkResampleAcc=acc;
  talkResampleWeight=weight;
  return Int16Array.from(tmp);
}
function pcm16leBytes(samples){
  const bytes=new Uint8Array(samples.length*2);
  const view=new DataView(bytes.buffer);
  for(let i=0;i<samples.length;i++)view.setInt16(i*2,samples[i],true);
  return bytes;
}
function queueTalkPcm(samples){
  if(!talkActive||!samples?.length)return;
  const bytes=pcm16leBytes(samples);
  talkPending=concatBytes(talkPending,bytes);
  const maxBuffered=640*6;
  if(talkPending.length>maxBuffered)talkPending=talkPending.slice(talkPending.length-maxBuffered);
  flushTalkPcm();
}
async function flushTalkPcm(){
  if(talkSending||!talkActive)return;
  if(talkPending.length<640)return;
  talkSending=true;
  try{
    while(talkActive&&talkPending.length>=640){
      const chunk=talkPending.slice(0,640);
      talkPending=talkPending.slice(640);
      const response=await fetch(`/api/devices/${encodeURIComponent(selected.id)}/talk?action=data`,{
        method:'POST',
        headers:authHeaders({'Content-Type':'application/octet-stream'}),
        body:chunk,
        cache:'no-store',
      });
      if(!response.ok){
        let detail='HTTP '+response.status;
        try{const d=await response.json();detail=d.detail||detail;}catch{}
        throw new Error(detail);
      }
    }
  }catch(e){
    console.warn('talkback',e);
    talkSetState('Ошибка: '+e.message,false);
    stopTalkback();
  }finally{
    talkSending=false;
    if(talkActive&&talkPending.length>=640)flushTalkPcm();
  }
}
async function startTalkback(){
  if(talkActive||talkStarting||!selected?.online||!selected?.talkback)return;
  const gesture=++talkGestureToken;
  talkStarting=true;
  talkSetState('Запрашиваю микрофон…',false);
  try{
    if(typeof MediaRecorder==='undefined')throw new Error('Браузер не поддерживает запись микрофона');
    talkStream=await navigator.mediaDevices.getUserMedia({
      audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true,sampleRate:{ideal:48000}},
      video:false
    });
    if(gesture!==talkGestureToken){talkStream.getTracks().forEach(t=>t.stop());talkStream=null;return;}

    const candidates=[
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/ogg'
    ];
    talkRecordedMime=candidates.find(t=>MediaRecorder.isTypeSupported?.(t))||'';
    const opts=talkRecordedMime?{mimeType:talkRecordedMime,audioBitsPerSecond:64000}:{audioBitsPerSecond:64000};
    talkRecorder=new MediaRecorder(talkStream,opts);
    talkRecordedChunks=[];
    talkRecordedDeviceId=selected.id;
    talkRecorder.ondataavailable=e=>{
      if(e.data&&e.data.size>0)talkRecordedChunks.push(e.data);
    };
    talkRecorder.onerror=e=>{
      console.warn('talkback recorder',e);
      talkSetState('Ошибка записи микрофона',false);
    };
    talkRecorder.start();
    talkActive=true;
    talkSetState('Идёт запись — отпустите кнопку для отправки',true);
  }catch(e){
    console.warn('talkback start',e);
    talkSetState('Не удалось включить: '+e.message,false);
    try{talkStream?.getTracks().forEach(t=>t.stop());}catch{}
    talkRecorder=null;talkStream=null;talkRecordedChunks=[];talkRecordedDeviceId='';
  }finally{
    talkStarting=false;
  }
}
async function stopTalkback(sendClip=true){
  ++talkGestureToken;
  const recorder=talkRecorder;
  const stream=talkStream;
  const deviceId=talkRecordedDeviceId;
  const hadRecording=!!recorder&&recorder.state!=='inactive';

  talkActive=false;
  talkStarting=false;

  try{
    if(hadRecording){
      await new Promise(resolve=>{
        const done=()=>resolve();
        recorder.addEventListener('stop',done,{once:true});
        try{recorder.stop();}catch{resolve();}
      });
    }
  }catch(e){console.warn('talkback recorder stop',e);}

  try{stream?.getTracks().forEach(t=>t.stop());}catch{}
  talkRecorder=null;talkStream=null;

  const chunks=talkRecordedChunks;
  talkRecordedChunks=[];
  talkRecordedDeviceId='';

  if(!sendClip||!deviceId||!chunks.length){
    talkSetState('Микрофон выключен',false);
    return;
  }

  try{
    const type=recorder?.mimeType||talkRecordedMime||chunks[0]?.type||'audio/webm';
    const blob=new Blob(chunks,{type});
    if(blob.size<=0)throw new Error('Пустая запись');
    if(blob.size>4*1024*1024)throw new Error('Фраза слишком длинная — максимум 4 МБ');

    talkSetState('Отправка записи на камеру…',false);
    const response=await fetch(`/api/devices/${encodeURIComponent(deviceId)}/talk?action=clip`,{
      method:'POST',
      headers:authHeaders({'Content-Type':blob.type||'audio/webm'}),
      body:blob,
      cache:'no-store',
    });
    if(!response.ok){
      let detail='HTTP '+response.status;
      try{const d=await response.json();detail=d.detail||detail;}catch{}
      throw new Error(detail);
    }
    talkSetState('Фраза отправлена в камеру',false);
  }catch(e){
    console.warn('talkback clip',e);
    talkSetState('Ошибка отправки: '+e.message,false);
  }
}

function deg2rad(v){return v*Math.PI/180;}
function quatFromDevice(alpha,beta,gamma,orient){const x=deg2rad(beta||0),y=deg2rad(gamma||0),z=deg2rad(alpha||0),o=deg2rad(orient||0);const cX=Math.cos(x/2),cY=Math.cos(y/2),cZ=Math.cos(z/2),sX=Math.sin(x/2),sY=Math.sin(y/2),sZ=Math.sin(z/2);let q={x:sX*cY*cZ-cX*sY*sZ,y:cX*sY*cZ+sX*cY*sZ,z:cX*cY*sZ+sX*sY*cZ,w:cX*cY*cZ-sX*sY*sZ};const so=Math.sin(-o/2),co=Math.cos(-o/2);q={x:q.x*co-q.y*so,y:q.x*so+q.y*co,z:q.z*co+q.w*so,w:-q.z*so+q.w*co};return q;}
function quatInv(q){return{x:-q.x,y:-q.y,z:-q.z,w:q.w};}
function quatMul(a,b){return{x:a.w*b.x+a.x*b.w+a.y*b.z-a.z*b.y,y:a.w*b.y-a.x*b.z+a.y*b.w+a.z*b.x,z:a.w*b.z+a.x*b.y-a.y*b.x+a.z*b.w,w:a.w*b.w-a.x*b.x-a.y*b.y-a.z*b.z};}
function quatYawPitch(q){const siny=2*(q.w*q.z+q.x*q.y),cosy=1-2*(q.y*q.y+q.z*q.z);const yaw=Math.atan2(siny,cosy)*180/Math.PI;const sinp=2*(q.w*q.x-q.z*q.y);const pitch=Math.asin(Math.max(-1,Math.min(1,sinp)))*180/Math.PI;return{yaw,pitch};}
function screenAngle(){return screen.orientation?.angle??window.orientation??0;}
async function requestOrientationPermission(){try{if(typeof DeviceOrientationEvent==='undefined')throw new Error('На устройстве нет датчика ориентации');if(typeof DeviceOrientationEvent.requestPermission==='function'){const result=await DeviceOrientationEvent.requestPermission();if(result!=='granted')throw new Error('Доступ к гироскопу не разрешён');}return true;}catch(e){alert(e.message);return false;}}
function recenterGyro(){gyroBase=null;gyroPanBase=Number($('panRange').value)||0;gyroTiltBase=Number($('tiltRange').value)||0;$('vrStatus').textContent='Смотрите прямо — центр зафиксируется';}
async function enterCardboard(){if(!selected?.online){alert('Сначала выберите подключённый трактор');return;}if(!$('video').srcObject){alert('Сначала дождитесь видеопотока');return;}if(!await requestOrientationPermission())return;await stopDrive();cardboardActive=true;gyroPanBase=Number($('panRange').value)||0;gyroTiltBase=Number($('tiltRange').value)||0;gyroBase=null;syncVrStreams($('video').srcObject);$('cardboardView').classList.remove('hidden');document.body.classList.add('cardboard-active');window.addEventListener('deviceorientation',onDeviceOrientation,true);try{await screen.orientation?.lock?.('landscape');}catch{}try{await $('cardboardView').requestFullscreen?.();}catch{}$('vrStatus').textContent='Смотрите прямо — центр зафиксируется';}
function exitCardboard(){if(!cardboardActive)return;cardboardActive=false;window.removeEventListener('deviceorientation',onDeviceOrientation,true);$('cardboardView').classList.add('hidden');document.body.classList.remove('cardboard-active');gyroBase=null;try{if(document.fullscreenElement)document.exitFullscreen();}catch{}try{screen.orientation?.unlock?.();}catch{}}
function onDeviceOrientation(e){if(!cardboardActive||!selected?.online)return;const q=quatFromDevice(e.alpha,e.beta,e.gamma,screenAngle());if(!gyroBase){gyroBase=q;$('vrStatus').textContent='Гироскоп активен';return;}const rel=quatMul(quatInv(gyroBase),q);const a=quatYawPitch(rel);const pan=Math.max(-90,Math.min(90,gyroPanBase-a.yaw));const tilt=Math.max(-45,Math.min(45,gyroTiltBase-a.pitch));$('panRange').value=pan;$('tiltRange').value=tilt;updatePtzLabels();const now=performance.now();if(now-lastGyroSend<100)return;lastGyroSend=now;sendAbsolutePtz();$('vrStatus').textContent=`PAN ${pan.toFixed(0)}°  TILT ${tilt.toFixed(0)}°`;}

async function loadLinked(){try{const data=await api('/api/settings/devices');const list=data.devices||[];$('linkedDevices').innerHTML=list.length?'':'<div class="empty-note">Тракторы пока не добавлены.</div>';list.forEach(d=>{const row=document.createElement('div');row.className='linked-row';row.innerHTML=`<div><strong>${escapeHtml(d.alias||d.device_id)}</strong><span>${escapeHtml(d.device_id)}</span></div><button class="btn danger">Удалить</button>`;row.querySelector('button').onclick=()=>detach(d.device_id);$('linkedDevices').appendChild(row);});}catch(e){}}
async function attach(){const id=$('attachId').value.trim(),alias=$('attachAlias').value.trim();$('attachError').textContent='';try{await api('/api/settings/devices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:id,alias:alias||null})});$('attachId').value='';$('attachAlias').value='';await Promise.all([loadLinked(),loadDevices()]);}catch(e){$('attachError').textContent=e.message;}}
async function detach(id){try{await emergencyStop();await api(`/api/settings/devices/${encodeURIComponent(id)}`,{method:'DELETE'});if(selected?.id===id){selected=null;exitCardboard();closePeer();}await Promise.all([loadLinked(),loadDevices()]);}catch(e){alert(e.message);}}
function switchView(name){emergencyStop();stopTalkback();if(cardboardActive)exitCardboard();document.querySelectorAll('.nav-tab').forEach(b=>b.classList.toggle('active',b.dataset.view===name));$('cameraView').classList.toggle('hidden',name!=='cameras');$('settingsView').classList.toggle('hidden',name!=='settings');$('cameraNav').classList.toggle('hidden',name!=='cameras');if(name==='settings'){closePeer();loadLinked();}else if(name==='cameras'&&selected)connectStream(selected);}
function formatUptime(sec){if(!sec)return '—';const d=Math.floor(sec/86400),h=Math.floor((sec%86400)/3600),m=Math.floor((sec%3600)/60);return d?`${d}д ${h}ч`:`${h}ч ${m}м`;}
function escapeHtml(s){return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}

function bindHold(button,onStart,onStop){button.addEventListener('pointerdown',e=>{e.preventDefault();button.setPointerCapture?.(e.pointerId);onStart();});for(const ev of ['pointerup','pointercancel','lostpointercapture'])button.addEventListener(ev,e=>{e.preventDefault();onStop();});}
document.querySelectorAll('[data-move]').forEach(b=>b.onclick=()=>move(b.dataset.move));
document.querySelectorAll('.drive-btn').forEach(b=>bindHold(b,()=>startDrive(b.dataset.drive),stopDrive));
document.querySelectorAll('.brush-lift-btn').forEach(b=>bindHold(b,()=>startBrushLift(b.dataset.brushLift),stopBrushLift));
document.querySelectorAll('.brush-spin-btn').forEach(b=>b.onclick=()=>setBrushSpin(b.dataset.brushSpin));
document.querySelectorAll('.nav-tab').forEach(b=>b.onclick=()=>switchView(b.dataset.view));
bindHold($('talkBtn'),startTalkback,()=>stopTalkback());
$('driveStopBtn').onclick=stopDrive;$('brushSpinStopBtn').onclick=()=>setBrushSpin('stop');
$('loginBtn').onclick=()=>login(false);$('registerBtn').onclick=()=>login(true);$('logoutBtn').onclick=logout;$('fullscreenBtn').onclick=toggleVideoFullscreen;$('attachBtn').onclick=attach;$('centerBtn').onclick=center;$('idrBtn').onclick=requestIdr;$('camera1Btn').onclick=()=>switchCamera(1);$('camera2Btn').onclick=()=>switchCamera(2);$('refreshBtn').onclick=loadDevices;$('cardboardBtn').onclick=enterCardboard;$('vrExitBtn').onclick=exitCardboard;$('vrRecenterBtn').onclick=recenterGyro;$('gpsBtn').onclick=openGpsModal;$('gpsCloseBtn').onclick=closeGpsModal;
$('panRange').oninput=updatePtzLabels;$('tiltRange').oninput=updatePtzLabels;$('speedRange').oninput=updatePtzLabels;$('panRange').onchange=sendAbsolutePtz;$('tiltRange').onchange=sendAbsolutePtz;
$('driveSpeedRange').oninput=updateDriveUi;$('brushSpeedRange').oninput=updateBrushUi;$('brushSpeedRange').onchange=refreshBrushSpin;
$('gpsModal').addEventListener('click',e=>{if(e.target===$('gpsModal'))closeGpsModal();});
document.addEventListener('fullscreenchange',()=>{updateFullscreenButton();if(cardboardActive&&!document.fullscreenElement)exitCardboard();});document.addEventListener('webkitfullscreenchange',updateFullscreenButton);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&cardboardActive){exitCardboard();return;}if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName))return;const k=String(e.key||'').toLowerCase();const driveMap={w:'forward',s:'backward',a:'left',d:'right'};const cameraMap={arrowup:'up',arrowdown:'down',arrowleft:'left',arrowright:'right'};if(k===' '){e.preventDefault();stopDrive();return;}if(driveMap[k]&&!e.repeat){e.preventDefault();startDrive(driveMap[k]);return;}if(cameraMap[k]){e.preventDefault();move(cameraMap[k]);}});
document.addEventListener('keyup',e=>{const k=String(e.key||'').toLowerCase();if(['w','a','s','d'].includes(k)){e.preventDefault();stopDrive();}});
window.addEventListener('blur',()=>{stopDrive();stopBrushLift();stopTalkback();});
document.addEventListener('visibilitychange',()=>{if(document.hidden){stopDrive();stopBrushLift();stopTalkback();return;}if(selected?.online&&(!videoDemand||!currentPeer||['failed','closed','disconnected'].includes(currentPeer.connectionState)))scheduleVideoReconnect(250);});window.addEventListener('pagehide',()=>{stopVideoDemand();stopTalkback();});window.addEventListener('pageshow',()=>{if(selected?.online)scheduleVideoReconnect(250);});
updateDriveUi();updateBrushUi();
if(token)enterApp();
setInterval(()=>{if(token&&!cardboardActive)loadDevices();},3000);
