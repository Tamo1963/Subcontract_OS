<#
.SYNOPSIS
  Subcontract OS - create a PostgreSQL role and database with a TEMPORARY password, write .env,
  apply migrations and (optionally) load the demo.
.USAGE
  cd D:\SubcontractorOS
  powershell -ExecutionPolicy Bypass -File scripts\setup_postgres.ps1
  Options: -AppUser scos_app -Database subcontract_os -ValidDays 30 -PgHost localhost -Port 5432 -SuperUser postgres -NoDemo
#>
param(
  [string]$AppUser = "scos_app",
  [string]$Database = "subcontract_os",
  [int]$ValidDays = 30,
  [string]$PgHost = "localhost",
  [int]$Port = 5432,
  [string]$SuperUser = "postgres",
  [switch]$NoDemo
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# 1. Locate psql
$psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
if (-not $psql) {
  $psql = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue |
          Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $psql) {
  Write-Host "PostgreSQL client (psql) not found." -ForegroundColor Red
  Write-Host "Install PostgreSQL 16 first, for example:  winget install PostgreSQL.PostgreSQL.16"
  Write-Host "or the installer from https://www.postgresql.org/download/windows/ , then re-run this script."
  exit 1
}
Write-Host "Using $psql"

# 2. Superuser password (used once, not stored)
$sec = Read-Host "Password for PostgreSQL superuser '$SuperUser' (set during PostgreSQL installation)" -AsSecureString
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))

# 3. Temporary application password (alphanumeric, 21 chars)
$chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789".ToCharArray()
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 18; $rng.GetBytes($bytes)
$AppPassword = "Tmp" + (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] }))

# 4. Create role and database
& $psql -h $PgHost -p $Port -U $SuperUser -d postgres -q `
  -v app_user=$AppUser -v app_password=$AppPassword -v app_db=$Database -v valid_days=$ValidDays `
  -f "db\setup_postgres.sql"
if ($LASTEXITCODE -ne 0) { Remove-Item Env:PGPASSWORD; Write-Host "Database setup failed (see above)." -ForegroundColor Red; exit 1 }
Remove-Item Env:PGPASSWORD

# 5. Write .env (backup any existing one)
if (Test-Path ".env") { Copy-Item ".env" ".env.bak" -Force; Write-Host "Existing .env backed up to .env.bak" }
@"
# Written by scripts/setup_postgres.ps1 on $(Get-Date -Format s). Do not commit.
SCOS_DATABASE_URL=postgresql+psycopg://${AppUser}:${AppPassword}@${PgHost}:${Port}/${Database}
SCOS_AUTO_MIGRATE=1
SCOS_SESSION_HOURS=8
SCOS_ALLOW_DEV_AUTH=0
"@ | Set-Content -Path ".env" -Encoding ascii

# 6. Python environment, migrations, demo
$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { py -3 -m venv .venv; if (-not (Test-Path $py)) { python -m venv .venv } }
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements.txt reportlab
& $py -m app.cli migrate
if (-not $NoDemo) {
  & $py -m seed.make_samples
  & $py -m seed.demo_seed
  New-Item -ItemType File -Force -Path "var\.demo_loaded" | Out-Null
} else {
  & $py -m app.cli create-admin admin
}

Write-Host ""
Write-Host "PostgreSQL is ready." -ForegroundColor Green
Write-Host "  Database : $Database on ${PgHost}:$Port"
Write-Host "  Username : $AppUser"
Write-Host "  Password : $AppPassword   (TEMPORARY - expires in $ValidDays days; saved in .env)"
Write-Host "Start the app with start_windows.bat, then sign in with the temporary app password printed above."
