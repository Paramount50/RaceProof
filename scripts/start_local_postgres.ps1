param(
    [int]$Port = 55432
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.raceproof\postgres'
$dataDir = Join-Path $runtimeRoot 'data'
$logPath = Join-Path $runtimeRoot 'postgres.log'
$postgresBin = 'C:\Program Files\PostgreSQL\17\bin'

if (-not (Test-Path -LiteralPath (Join-Path $postgresBin 'initdb.exe'))) {
    throw 'PostgreSQL 17 binaries were not found. Use docker compose up -d postgres instead.'
}

New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $dataDir 'PG_VERSION'))) {
    & (Join-Path $postgresBin 'initdb.exe') -D $dataDir --username=raceproof --auth=trust --encoding=UTF8 --no-locale
    if ($LASTEXITCODE -ne 0) { throw 'initdb failed' }
}

& (Join-Path $postgresBin 'pg_ctl.exe') -D $dataDir status 2>$null
if ($LASTEXITCODE -ne 0) {
    & (Join-Path $postgresBin 'pg_ctl.exe') -D $dataDir -l $logPath -o "-p $Port -h 127.0.0.1" start
    if ($LASTEXITCODE -ne 0) { throw 'pg_ctl start failed' }
}

$databaseExists = & (Join-Path $postgresBin 'psql.exe') -h 127.0.0.1 -p $Port -U raceproof -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='raceproof'"
if ($databaseExists -ne '1') {
    & (Join-Path $postgresBin 'createdb.exe') -h 127.0.0.1 -p $Port -U raceproof raceproof
    if ($LASTEXITCODE -ne 0) { throw 'createdb failed' }
}

Write-Output "RACEPROOF_DATABASE_URL=postgresql+psycopg://raceproof@127.0.0.1:$Port/raceproof"

