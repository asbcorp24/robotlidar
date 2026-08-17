const demoDevices = [
  {id:'cam-001',name:'Камера 01',location:'Устройство A',online:true,streamType:'webrtc',streamUrl:'',pan:0,tilt:0,fps:30,bitrateKbps:2050,latencyMs:68,ethernet:'1 Gbit/s',uptimeSec:128340},
  {id:'cam-002',name:'Камера 02',location:'Устройство B',online:true,streamType:'webrtc',streamUrl:'',pan:14.5,tilt:-5,fps:30,bitrateKbps:1980,latencyMs:74,ethernet:'1 Gbit/s',uptimeSec:93210},
  {id:'cam-003',name:'Камера 03',location:'Устройство C',online:false,streamType:'webrtc',streamUrl:'',pan:0,tilt:0,fps:0,bitrateKbps:0,latencyMs:0,ethernet:'offline',uptimeSec:0}
];

let devices=[];
let selected=null;
let currentPeer=null;

const $=id=>document.getElementById(id);

async function loadDevices(){
  try{
    const r=await fetch('/api/devices',{cache:'no-store'});
    if(!r.ok) throw new Error('api unavailable');
    devices=await r.json();
  }catch(e){devices=demoDevices.map(x=>({...x}));}
  renderDevices();
  if(!selected && devices.length) selectDevice(devices.find(d=>d.online)||devices[0]);
  else if(selected){const updated=devices.find(d=>d.id===selected.id); if(updated) selectDevice(updated,false);}
}

function renderDevices(){
  $('deviceCount').textContent=devices.length;
  $('deviceList').innerHTML='';
  devices.forEach(d=>{
    const el=document.createElement('div');
    el.className='device'+(selected?.id===d.id?' active':'');
    el.innerHTML=`<div class="device-top"><div><div class="device-name">${escapeHtml(d.name)}</div><div class="device-location">${escapeHtml(d.location||d.id)}</div></div><span class="device-state ${d.online?'online':'offline'}">${d.online?'online':'offline'}</span></div>`;
    el.onclick=()=>selectDevice(d);
    $('deviceList').appendChild(el);
  });
}

async function selectDevice(device,reconnect=true){
  selected={...device};
  renderDevices();
  $('cameraTitle').textContent=device.name;
  $('cameraSubtitle').textContent=`${device.location||device.id} · ${device.id}`;
  setOnline(device.online);
  $('streamType').textContent=(device.streamType||'—').toUpperCase();
  $('panRange').value=device.pan||0;
  $('tiltRange').value=device.tilt||0;
  updatePtzLabels();
  updateTelemetry(device);
  if(reconnect) await connectStream(device);
}

function setOnline(v){
  $('onlineDot').className='dot '+(v?'online':'offline');
  $('onlineText').textContent=v?'Online':'Offline';
}

function updateTelemetry(d){
  $('fpsMetric').textContent=d.fps?`${d.fps}`:'—';
  $('bitrateMetric').textContent=d.bitrateKbps?`${d.bitrateKbps} kbps`:'—';
  $('latencyMetric').textContent=d.latencyMs?`${d.latencyMs} ms`:'—';
  $('ethernetMetric').textContent=d.ethernet||'—';
  $('uptimeMetric').textContent=formatUptime(d.uptimeSec||0);
}

async function connectStream(device){
  const video=$('video');
  $('videoPlaceholder').style.display='flex';
  video.src='';
  if(currentPeer){currentPeer.close(); currentPeer=null;}
  if(!device.online) return;

  if(device.streamType==='hls' && device.streamUrl){
    video.src=device.streamUrl;
    try{await video.play(); $('videoPlaceholder').style.display='none';}catch(e){}
    return;
  }

  if(device.streamType==='mp4' && device.streamUrl){
    video.src=device.streamUrl;
    try{await video.play(); $('videoPlaceholder').style.display='none';}catch(e){}
    return;
  }

  if(device.streamType==='webrtc' && device.streamUrl){
    try{await connectWebRTC(device);}catch(e){console.warn('WebRTC:',e);}
  }
}

async function connectWebRTC(device){
  const pc=new RTCPeerConnection(); currentPeer=pc;
  pc.addTransceiver('video',{direction:'recvonly'});
  pc.ontrack=e=>{ $('video').srcObject=e.streams[0]; $('videoPlaceholder').style.display='none'; };
  const offer=await pc.createOffer(); await pc.setLocalDescription(offer);
  await waitIce(pc);
  const r=await fetch(device.streamUrl,{method:'POST',headers:{'Content-Type':'application/sdp'},body:pc.localDescription.sdp});
  if(!r.ok) throw new Error(`WebRTC endpoint ${r.status}`);
  const answer=await r.text();
  await pc.setRemoteDescription({type:'answer',sdp:answer});
}

function waitIce(pc){return new Promise(resolve=>{if(pc.iceGatheringState==='complete')return resolve(); const f=()=>{if(pc.iceGatheringState==='complete'){pc.removeEventListener('icegatheringstatechange',f);resolve();}};pc.addEventListener('icegatheringstatechange',f);setTimeout(resolve,1200);});}

function updatePtzLabels(){
  $('panValue').textContent=`${Number($('panRange').value).toFixed(1)}°`;
  $('tiltValue').textContent=`${Number($('tiltRange').value).toFixed(1)}°`;
  $('speedValue').textContent=`${$('speedRange').value}°/с`;
}

async function sendAbsolutePtz(){
  if(!selected?.online) return;
  const body={pan:Number($('panRange').value),tilt:Number($('tiltRange').value),speed:Number($('speedRange').value)};
  selected.pan=body.pan; selected.tilt=body.tilt; updatePtzLabels();
  await apiPost(`/api/devices/${encodeURIComponent(selected.id)}/ptz`,body);
}

async function move(dir){
  if(!selected?.online) return;
  const step=5;
  let pan=Number($('panRange').value),tilt=Number($('tiltRange').value);
  if(dir==='left')pan-=step;if(dir==='right')pan+=step;if(dir==='up')tilt+=step;if(dir==='down')tilt-=step;
  pan=Math.max(-90,Math.min(90,pan)); tilt=Math.max(-45,Math.min(45,tilt));
  $('panRange').value=pan;$('tiltRange').value=tilt;await sendAbsolutePtz();
}

async function center(){if(!selected?.online)return;$('panRange').value=0;$('tiltRange').value=0;updatePtzLabels();await apiPost(`/api/devices/${encodeURIComponent(selected.id)}/center`,{});}
async function requestIdr(){if(selected?.online)await apiPost(`/api/devices/${encodeURIComponent(selected.id)}/request-idr`,{});}
async function apiPost(url,body){try{await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(e){console.debug('demo mode',url,body);}}

function formatUptime(sec){if(!sec)return '—';const d=Math.floor(sec/86400),h=Math.floor((sec%86400)/3600),m=Math.floor((sec%3600)/60);return d?`${d}д ${h}ч`:`${h}ч ${m}м`;}
function escapeHtml(s){return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}

document.querySelectorAll('[data-move]').forEach(b=>b.addEventListener('click',()=>move(b.dataset.move)));
$('centerBtn').addEventListener('click',center);
$('idrBtn').addEventListener('click',requestIdr);
$('refreshBtn').addEventListener('click',loadDevices);
$('panRange').addEventListener('input',updatePtzLabels);
$('tiltRange').addEventListener('input',updatePtzLabels);
$('speedRange').addEventListener('input',updatePtzLabels);
$('panRange').addEventListener('change',sendAbsolutePtz);
$('tiltRange').addEventListener('change',sendAbsolutePtz);

document.addEventListener('keydown',e=>{
  if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName))return;
  const k=e.key.toLowerCase(); const map={arrowup:'up',w:'up',arrowdown:'down',s:'down',arrowleft:'left',a:'left',arrowright:'right',d:'right'};
  if(map[k]){e.preventDefault();move(map[k]);} if(k==='c'){e.preventDefault();center();}
});

loadDevices();
setInterval(loadDevices,5000);
