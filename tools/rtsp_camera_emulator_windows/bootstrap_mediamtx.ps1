$ErrorActionPreference = 'Stop'

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtime = Join-Path $base 'runtime'
$exe = Join-Path $runtime 'mediamtx.exe'

if (Test-Path $exe) {
    Write-Host "MediaMTX already installed: $exe"
    exit 0
}

New-Item -ItemType Directory -Force -Path $runtime | Out-Null

Write-Host 'MediaMTX not found. Downloading latest Windows x64 release through Windows TLS...'

# GitHub requires TLS 1.2+ on older PowerShell/.NET versions.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {}

$headers = @{ 'User-Agent' = 'RobotLiDAR-RTSP-Emulator' }
$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/bluenviron/mediamtx/releases/latest' -Headers $headers -UseBasicParsing
$asset = $release.assets | Where-Object { $_.name -match 'windows_amd64.*\.zip$' } | Select-Object -First 1
if (-not $asset) {
    throw 'Latest MediaMTX release has no Windows amd64 ZIP asset.'
}

$zip = Join-Path $runtime $asset.name
Write-Host ("Downloading {0}" -f $asset.name)
Invoke-WebRequest -Uri $asset.browser_download_url -Headers $headers -OutFile $zip -UseBasicParsing

Write-Host 'Extracting MediaMTX...'
Expand-Archive -Path $zip -DestinationPath $runtime -Force
Remove-Item $zip -Force

if (-not (Test-Path $exe)) {
    throw "mediamtx.exe was not found after extraction: $exe"
}

Write-Host "MediaMTX installed: $exe"
exit 0
