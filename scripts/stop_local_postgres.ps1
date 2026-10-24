$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $projectRoot '.raceproof\postgres\data'
$pgCtl = 'C:\Program Files\PostgreSQL\17\bin\pg_ctl.exe'
if (Test-Path -LiteralPath (Join-Path $dataDir 'PG_VERSION')) {
    & $pgCtl -D $dataDir stop -m fast
}

