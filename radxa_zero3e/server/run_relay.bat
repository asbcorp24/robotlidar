@echo off
setlocal
cd /d "%~dp0webrtc_relay"
echo Starting RobotLiDAR H.264 RTP -^> WebRTC relay...
go run .
pause
