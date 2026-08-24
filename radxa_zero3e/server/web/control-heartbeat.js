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

setInterval(()=>{
  if(!token||!selected?.online)return;
  if(driveActive){
    originalSendDrive(heartbeatDriveLeft,heartbeatDriveRight,heartbeatDriveLabel).catch(()=>{});
  }
  if(brushSpin!==0||brushLift!==0){
    sendBrush().catch(()=>{});
  }
},180);
