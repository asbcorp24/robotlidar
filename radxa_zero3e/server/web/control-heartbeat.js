// Keeps active motion commands fresh while the operator is intentionally
// holding/using controls. If the browser/network disappears, heartbeats stop
// and the Orange Pi / ESP32 watchdogs force a safe STOP.
let heartbeatDriveLeft=0;
let heartbeatDriveRight=0;
let heartbeatDriveLabel='';

const originalSendDrive=sendDrive;
sendDrive=async function(left,right,label=''){
  heartbeatDriveLeft=left;
  heartbeatDriveRight=right;
  heartbeatDriveLabel=label;
  return originalSendDrive(left,right,label);
};

const originalStopDrive=stopDrive;
stopDrive=async function(){
  heartbeatDriveLeft=0;
  heartbeatDriveRight=0;
  heartbeatDriveLabel='';
  return originalStopDrive();
};

// Radxa/HBVCAM sends SBS stereo. Orange Pi + MC500 is a mono camera, so in
// Cardboard the complete frame is duplicated for both eyes instead of being
// split into left/right halves.
const monoStyle=document.createElement('style');
monoStyle.textContent=`
.cardboard-view.mono-camera .cardboard-eye video{width:100%;height:100%;left:0!important;object-fit:contain}
`;
document.head.appendChild(monoStyle);

const originalEnterCardboard=enterCardboard;
enterCardboard=async function(){
  const mono=selected?.device_type==='orange_pi_ipcam'||selected?.device_type==='mono_ipcam';
  $('cardboardView').classList.toggle('mono-camera',!!mono);
  return originalEnterCardboard();
};

const originalExitCardboard=exitCardboard;
exitCardboard=function(){
  const r=originalExitCardboard();
  $('cardboardView').classList.remove('mono-camera');
  return r;
};

setInterval(()=>{
  if(!token||!selected?.online)return;
  if(driveActive){
    originalSendDrive(heartbeatDriveLeft,heartbeatDriveRight,heartbeatDriveLabel).catch(()=>{});
  }
  if(brushSpin!==0||brushLift!==0){
    sendBrush().catch(()=>{});
  }
},180);
