<#
CLINIQ - one-shot Windows setup + external benchmarks (Chia + Gemini).

Run in PowerShell:
  powershell -ExecutionPolicy Bypass -File "$HOME\Downloads\run_cliniq.ps1"

What it does (safe to re-run; finished steps are skipped):
  1. Python 3.11+ (installs 3.12 with winget if missing)
  2. PostgreSQL (installs 16 with winget if missing; default superuser password 'postgres')
  3. Creates database user/database 'cliniq'
  4. Unpacks Downloads\cliniq.zip to Downloads\cliniq_app and installs it in a virtualenv
  5. Builds the dataset: migrate -> generate -> ingest -> dbt -> evidence index
  6. Downloads Chia (public, CC BY 4.0) from figshare and converts it
  7. Asks for your Gemini API key (hidden input, kept only in this window's memory)
  8. Runs the benchmarks and copies results to Downloads\cliniq_results

Your API key is never written to disk, logs or code; it is removed from the environment at the end.
#>
$ErrorActionPreference = "Continue"   # native tools report via exit codes; we check them explicitly
$DL   = Join-Path $HOME "Downloads"
$ROOT = Join-Path $DL "cliniq_app"
$APP  = Join-Path $ROOT "cliniq"
$EXT  = Join-Path $DL "cliniq_external"
$OUT  = Join-Path $DL "cliniq_results"

function Step($m)   { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Fail($m)   { Write-Host "`nERROR: $m" -ForegroundColor Red; Write-Host "Copy the text above into the chat with Claude (never your API key)."; if ($env:GEMINI_API_KEY) { Remove-Item Env:GEMINI_API_KEY }; exit 1 }
function Check($w)  { if ($LASTEXITCODE -ne 0) { Fail "$w (exit code $LASTEXITCODE)" } }
function RefreshPath { $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") }
function Plain($secure) { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)) }

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "winget not found. If Python 3.11+ and PostgreSQL are already installed this is fine; otherwise install 'App Installer' from the Microsoft Store." -ForegroundColor Yellow
}

# ------------------------------------------------------------------ 1. Python
Step "1/8  Python 3.11+"
$PyBase = $null
$candidates = @(
    @{ exe = "py"; args = @("-3.12") }, @{ exe = "py"; args = @("-3.13") }, @{ exe = "py"; args = @("-3.11") },
    @{ exe = "python"; args = @() }
)
foreach ($cand in $candidates) {
    $exe = $cand.exe; $args0 = $cand.args
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $out = & $exe @args0 -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out -and $out.Trim()) { $PyBase = $out.Trim(); break }
}
if (-not $PyBase) {
    Write-Host "Installing Python 3.12 with winget (approve the Windows prompt if asked)..."
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    Check "Python install"
    RefreshPath
    $PyBase = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
    if (-not $PyBase) { Fail "Python installed but not found; open a NEW PowerShell window and run this script again" }
}
Write-Host "Python: $PyBase"

# ------------------------------------------------------------------ 2. PostgreSQL
Step "2/8  PostgreSQL"
function Find-Psql { Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue |
    Sort-Object { $_.Directory.Parent.Name -as [double] } -Descending | Select-Object -First 1 }
$psql = Find-Psql
if (-not $psql) {
    Write-Host "Installing PostgreSQL 16 with winget (approve the Windows prompt; takes a few minutes)..."
    winget install -e --id PostgreSQL.PostgreSQL.16 --accept-package-agreements --accept-source-agreements `
        --override "--mode unattended --unattendedmodeui none --superpassword postgres --serverport 5432"
    Check "PostgreSQL install"
    $psql = Find-Psql
    if (-not $psql) { Fail "PostgreSQL installed but psql.exe not found under C:\Program Files\PostgreSQL" }
}
$PSQL = $psql.FullName
$env:Path = "$($psql.Directory.FullName);$env:Path"
Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Where-Object Status -ne "Running" | ForEach-Object {
    try { Start-Service $_ -ErrorAction Stop } catch { Write-Host "Could not start $($_.Name); run PowerShell as Administrator or start it in services.msc" -ForegroundColor Yellow }
}
Write-Host "psql: $PSQL"

# ------------------------------------------------------------------ 3. database user + db
Step "3/8  Database user 'cliniq'"
$env:PGPASSWORD = "postgres"
& $PSQL -U postgres -h localhost -tAc "select 1" *> $null
if ($LASTEXITCODE -ne 0) {
    $env:PGPASSWORD = Plain (Read-Host "Password of the PostgreSQL 'postgres' superuser (hidden)" -AsSecureString)
    & $PSQL -U postgres -h localhost -tAc "select 1" *> $null
    Check "Connecting to PostgreSQL as 'postgres' (wrong password, or service not running)"
}
$hasUser = (& $PSQL -U postgres -h localhost -tAc "select 1 from pg_roles where rolname='cliniq'" | Out-String).Trim()
if ($hasUser -ne "1") { & $PSQL -U postgres -h localhost -c "CREATE USER cliniq WITH PASSWORD 'cliniq' SUPERUSER"; Check "Creating user cliniq" }
$hasDb = (& $PSQL -U postgres -h localhost -tAc "select 1 from pg_database where datname='cliniq'" | Out-String).Trim()
if ($hasDb -ne "1") { & $PSQL -U postgres -h localhost -c "CREATE DATABASE cliniq OWNER cliniq"; Check "Creating database cliniq" }
Remove-Item Env:PGPASSWORD
Write-Host "Database ready."

# ------------------------------------------------------------------ 4. unpack + install
Step "4/8  Unpack and install CLINIQ (first run downloads packages; a few minutes)"
$zip = Join-Path $DL "cliniq.zip"
if (-not (Test-Path $zip)) { Fail "cliniq.zip not found in $DL" }
Expand-Archive -Path $zip -DestinationPath $ROOT -Force -ErrorAction Stop
$venv = Join-Path $APP ".venv"
$PY = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $PY)) { & $PyBase -m venv $venv; Check "Creating virtualenv" }
$env:Path = "$(Join-Path $venv 'Scripts');$env:Path"
& $PY -m pip install --upgrade pip --quiet; Check "pip upgrade"
& $PY -m pip install -e "$APP\backend[dbt]" --quiet; Check "Installing CLINIQ"

$env:PYTHONUTF8 = "1"
$env:CLINIQ_ENV = "dev"
$env:CLINIQ_DATABASE_URL = "postgresql://cliniq:cliniq@localhost:5432/cliniq"
$env:CLINIQ_READER_DATABASE_URL = "postgresql://cliniq_reader:cliniq_reader@localhost:5432/cliniq"
Set-Location $APP

# ------------------------------------------------------------------ 5. build dataset
Step "5/8  Build dataset (migrate -> generate -> ingest -> dbt -> index; about 1 minute)"
Write-Host "Running... (output hidden)"
& $PY -m cliniq.cli bootstrap | Out-Null; Check "cliniq bootstrap"
Write-Host "Dataset built."

# ------------------------------------------------------------------ 6. Chia
Step "6/8  Chia corpus (figshare, CC BY 4.0)"
$chiaJsonl = Join-Path $EXT "chia.jsonl"
if (-not (Test-Path $chiaJsonl)) {
    $manual = Get-ChildItem $DL -Filter "*.zip" | Where-Object { $_.Name -match "11855817|chia" -and $_.Name -notmatch "cliniq" } | Select-Object -First 1
    if ($manual) { & $PY scripts\fetch_external.py --skip-trec --out $EXT --chia-zip $manual.FullName }
    else         { & $PY scripts\fetch_external.py --skip-trec --out $EXT }
}
$haveChia = Test-Path $chiaJsonl
if ($haveChia) { Write-Host "Chia ready: $chiaJsonl" }
else { Write-Host "Chia could not be downloaded automatically. Download it in a browser from https://doi.org/10.6084/m9.figshare.11855817.v2 ('Download all') and re-run this script. Continuing without Chia." -ForegroundColor Yellow }

# ------------------------------------------------------------------ 7. Gemini key
Step "7/8  Gemini API key"
Write-Host "Get one at https://aistudio.google.com/apikey . Paste it below; nothing will appear while you paste - that is normal."
$env:GEMINI_API_KEY = Plain (Read-Host "Gemini API key (hidden)" -AsSecureString)
if (-not $env:GEMINI_API_KEY) { Fail "No API key entered" }
$env:CLINIQ_GEMINI_DELAY_S = "5"   # free tier allows roughly 10-15 requests/minute
$modelFile = Join-Path $env:TEMP "cliniq_gemini_model.txt"
Remove-Item $modelFile -ErrorAction SilentlyContinue
Write-Host "Testing the key and finding a Gemini model that still has free quota..."
& $PY -c "from cliniq.extraction.gemini import get_api_key, pick_working_model; m = pick_working_model(get_api_key()); open(r'$modelFile', 'w').write(m); print('Using model:', m)"
$geminiOk = ($LASTEXITCODE -eq 0) -and (Test-Path $modelFile)
if ($geminiOk) { $env:CLINIQ_GEMINI_MODEL = (Get-Content $modelFile -Raw).Trim() }
else { Write-Host "Gemini is not usable with this key right now (see the message above). Rules benchmarks will still run." -ForegroundColor Yellow }

# ------------------------------------------------------------------ 8. benchmarks
Step "8/8  Benchmarks"
$warnings = @()
try {
    & $PY -m cliniq.cli eval --extractor rules-v1 --extractor rules-v2 | Out-Null; Check "Internal benchmark (rules)"
    if ($haveChia) { & $PY -m cliniq.cli eval --chia $chiaJsonl --extractor rules-v1 --extractor rules-v2 | Out-Null; Check "Chia benchmark (rules)" }
    if ($geminiOk) {
        Write-Host "Gemini internal benchmark (125 requests, ~15-25 min at free-tier pace)..."
        & $PY -m cliniq.cli eval --extractor gemini | Out-Null
        if ($LASTEXITCODE -ne 0) { $warnings += "Gemini internal benchmark stopped early (quota/rate limit) - see message above" }
        elseif ($haveChia) {
            Write-Host "Gemini Chia benchmark (150 criteria)..."
            & $PY -m cliniq.cli eval --chia $chiaJsonl --extractor gemini --limit 150 | Out-Null
            if ($LASTEXITCODE -ne 0) { $warnings += "Gemini Chia benchmark stopped early (quota/rate limit)" }
        }
    }
    & $PY scripts\make_eval_report.py | Out-Null
} finally {
    Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $OUT | Out-Null
Copy-Item (Join-Path $APP "eval\reports\*") $OUT -Force
Copy-Item (Join-Path $APP "docs\evaluation_report.md") $OUT -Force
foreach ($w in $warnings) { Write-Host "WARNING: $w" -ForegroundColor Yellow }
Write-Host "`nDONE. Results are in $OUT" -ForegroundColor Green
Write-Host "Tell Claude: 'results are in Downloads'."
