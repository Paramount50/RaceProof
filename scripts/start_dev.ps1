param(
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

& python -m uvicorn raceproof.api:app `
    --reload `
    --reload-dir (Join-Path $projectRoot 'src') `
    --reload-dir (Join-Path $projectRoot 'examples') `
    --port $Port
