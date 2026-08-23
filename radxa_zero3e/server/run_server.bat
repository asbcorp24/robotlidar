@echo off
setlocal
cd /d "%~dp0"
echo Starting RobotLiDAR Go server...
go run .
pause
