# ─────────────────────────────────────────────────────
#  Microwave AI – Windows one-line installer
#
#  Usage:
#    irm https://raw.githubusercontent.com/robot-time/Microwave/main/install.ps1 | iex
#
#  With options (set env vars first):
#    $env:MICROWAVE_EXPERT_DOMAINS = "code,math"
#    irm https://raw.githubusercontent.com/robot-time/Microwave/main/install.ps1 | iex
# ─────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

$RepoUrl    = "https://github.com/robot-time/Microwave.git"
$InstallDir = if ($env:MICROWAVE_DIR) { $env:MICROWAVE_DIR } else { "$HOME\Microwave" }

function Write-Color($Color, $Text) {
    Write-Host $Text -ForegroundColor $Color -NoNewline
}

function Command-Exists([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Try-Install-WithWinget([string]$Id) {
    if (-not (Command-Exists "winget")) { return $false }
    try {
        winget install --id $Id --accept-package-agreements --accept-source-agreements --silent
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Try-Install-WithChoco([string]$Package) {
    if (-not (Command-Exists "choco")) { return $false }
    try {
        choco install $Package -y
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Run-Exe {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $false)][string]$WorkingDirectory = $null
    )
    $sp = @{
        FilePath    = $FilePath
        ArgumentList = $ArgumentList
        Wait        = $true
        NoNewWindow = $true
        PassThru    = $true
    }
    if ($WorkingDirectory) {
        $sp.WorkingDirectory = $WorkingDirectory
    }
    $p = Start-Process @sp
    if ($p.ExitCode -ne 0) {
        throw "$FilePath failed with exit code $($p.ExitCode)"
    }
}

Write-Host ""
Write-Host "     ________________"
Write-Host "    |.-----------.   |"
Write-Host "    ||   _____   |ooo|"
Write-Host "    ||  |     |  |ooo|"
Write-Host "    ||  |     |  | = |"
Write-Host "    ||  '-----'  | _ |"
Write-Host "    ||___________|[_]|"
Write-Host "    '----------------'"
Write-Host ""
Write-Color Cyan "Microwave AI"; Write-Host " - Windows installer"
Write-Host ""

# ── check prerequisites (auto-install where possible) ──────────────────────────

$Python = $null
$PythonBaseArgs = @()
foreach ($cmd in @("python3", "python")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($LASTEXITCODE -eq 0) { $Python = $cmd; $PythonBaseArgs = @(); break }
    } catch {}
}
if (-not $Python -and (Command-Exists "py")) {
    try {
        $ver = & py -3 --version 2>&1
        if ($LASTEXITCODE -eq 0) { $Python = "py"; $PythonBaseArgs = @("-3") }
    } catch {}
}

if (-not $Python) {
    Write-Host "Python not found. Attempting auto-install..." -ForegroundColor Yellow
    $ok = (Try-Install-WithWinget "Python.Python.3.12") -or (Try-Install-WithChoco "python")
    if ($ok) {
        foreach ($cmd in @("python3", "python")) {
            try {
                $ver = & $cmd --version 2>&1
                if ($LASTEXITCODE -eq 0) { $Python = $cmd; $PythonBaseArgs = @(); break }
            } catch {}
        }
        if (-not $Python -and (Command-Exists "py")) {
            try {
                $ver = & py -3 --version 2>&1
                if ($LASTEXITCODE -eq 0) { $Python = "py"; $PythonBaseArgs = @("-3") }
            } catch {}
        }
    }
    if (-not $Python) {
        Write-Host "Python not found. Install Python 3.10+: https://python.org" -ForegroundColor Red
        Write-Host "  Tip: 'winget install Python.Python.3.12' or install from Microsoft Store"
        exit 1
    }
    Write-Host "Python installed: $Python $($PythonBaseArgs -join ' ')" -ForegroundColor Green
}

$gitExists = Command-Exists "git"
if (-not $gitExists) {
    Write-Host "Git not found. Attempting auto-install..." -ForegroundColor Yellow
    $ok = (Try-Install-WithWinget "Git.Git") -or (Try-Install-WithChoco "git")
    $gitExists = Command-Exists "git"
    if (-not ($ok -and $gitExists)) {
        Write-Host "Git not found. Install git: https://git-scm.com" -ForegroundColor Red
        Write-Host "  Tip: 'winget install Git.Git'"
        exit 1
    }
    Write-Host "Git installed." -ForegroundColor Green
}

# ── clone or update ──────────────────────────────

if (Test-Path "$InstallDir\.git") {
    Write-Host "Updating existing install at $InstallDir..." -ForegroundColor DarkGray
    Run-Exe -FilePath "git" -ArgumentList @("pull", "--ff-only") -WorkingDirectory $InstallDir
} else {
    Write-Host "Cloning Microwave into " -NoNewline
    Write-Color Cyan $InstallDir; Write-Host "..."
    Run-Exe -FilePath "git" -ArgumentList @("clone", $RepoUrl, $InstallDir)
}

Set-Location $InstallDir

# ── python venv ──────────────────────────────────

Write-Host ""
Write-Host "[1/3] " -NoNewline; Write-Host "Python environment" -ForegroundColor White

if (-not (Test-Path ".venv")) {
    Run-Exe -FilePath $Python -ArgumentList ($PythonBaseArgs + @("-m", "venv", ".venv"))
}

$VenvBin = $null
if (Test-Path ".venv\Scripts") { $VenvBin = (Resolve-Path ".venv\Scripts").Path }
elseif (Test-Path ".venv\bin") { $VenvBin = (Resolve-Path ".venv\bin").Path }

if (-not $VenvBin) {
    Write-Host "venv creation failed. Delete .venv and retry." -ForegroundColor Red
    exit 1
}

$env:PATH = "$VenvBin;$env:PATH"
try {
    Run-Exe -FilePath $Python -ArgumentList ($PythonBaseArgs + @("-m", "pip", "install", "--upgrade", "pip", "-q"))
} catch {}
try {
    Run-Exe -FilePath $Python -ArgumentList ($PythonBaseArgs + @("-m", "pip", "install", "-e", ".", "-q"))
} catch {
    Run-Exe -FilePath $Python -ArgumentList ($PythonBaseArgs + @("-m", "pip", "install", "-e", "."))
}
Write-Host "  done" -ForegroundColor Green

# ── ollama ───────────────────────────────────────

Write-Host "[2/3] " -NoNewline; Write-Host "Ollama + models" -ForegroundColor White

$Model = if ($env:MICROWAVE_MODEL) { $env:MICROWAVE_MODEL } else { "llama3.2" }
$ollamaExists = Get-Command ollama -ErrorAction SilentlyContinue

if (-not $ollamaExists) {
    Write-Host "  Installing Ollama..." -ForegroundColor Yellow
    $ollamaInstaller = "$env:TEMP\OllamaSetup.exe"
    try {
        Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $ollamaInstaller -UseBasicParsing
        Start-Process -FilePath $ollamaInstaller -Args "/SILENT" -Wait
        $env:PATH = "$env:LOCALAPPDATA\Programs\Ollama;$env:PATH"
    } catch {
        Write-Host "  Auto-install failed. Install manually: https://ollama.com" -ForegroundColor Yellow
        Write-Host "  Then re-run this installer."
        exit 1
    }
}

$models = & ollama list 2>$null
if ($models -notmatch [regex]::Escape($Model)) {
    Write-Host "  Pulling $Model ..."
    & ollama pull $Model
}
Write-Host "  $Model ready" -ForegroundColor Green

# ── auto-detect location ────────────────────────

$Lat = if ($env:MICROWAVE_LAT) { $env:MICROWAVE_LAT } else { "0.0" }
$Lon = if ($env:MICROWAVE_LON) { $env:MICROWAVE_LON } else { "0.0" }

if ($Lat -eq "0.0" -and $Lon -eq "0.0") {
    try {
        $geo = Invoke-RestMethod -Uri "http://ip-api.com/json/?fields=lat,lon,status" -TimeoutSec 5 -ErrorAction SilentlyContinue
        if ($geo.status -eq "success") {
            $Lat = $geo.lat.ToString()
            $Lon = $geo.lon.ToString()
        }
    } catch {}
}

# ── build launch args ───────────────────────────

$GatewayUrl    = if ($env:MICROWAVE_GATEWAY_URL) { $env:MICROWAVE_GATEWAY_URL } else { "https://electricity-guzzler.tail7917c7.ts.net" }
$Region        = if ($env:MICROWAVE_REGION) { $env:MICROWAVE_REGION } else { "LAN" }
$ExpertDomains = if ($env:MICROWAVE_EXPERT_DOMAINS) { $env:MICROWAVE_EXPERT_DOMAINS } else { "general" }
$EngineType    = if ($env:MICROWAVE_ENGINE) { $env:MICROWAVE_ENGINE } else { "ollama" }

Write-Host ""
Write-Host "[3/3] " -NoNewline; Write-Host "Starting expert node" -ForegroundColor White
Write-Host "  Model    $Model" -ForegroundColor Cyan
Write-Host "  Domains  $ExpertDomains" -ForegroundColor Cyan
Write-Host "  Gateway  $GatewayUrl" -ForegroundColor Cyan
if ($Lat -ne "0.0") { Write-Host "  Location $Lat, $Lon" -ForegroundColor Cyan }

Write-Host ""
Write-Host "Setup complete. " -ForegroundColor Green -NoNewline
Write-Host "Connecting to network ..."
Write-Host "  Next time, run from install dir: " -NoNewline; Write-Host "$InstallDir" -ForegroundColor Cyan
Write-Host "    .\.venv\Scripts\microwave.exe run" -ForegroundColor DarkGray
Write-Host "    .\.venv\Scripts\microwave.exe status" -ForegroundColor DarkGray
Write-Host ""

$MicrowaveExe = Join-Path $VenvBin "microwave.exe"
if (-not (Test-Path $MicrowaveExe)) {
    $MicrowaveExe = Join-Path $VenvBin "microwave"
}
if (-not (Test-Path $MicrowaveExe)) {
    Write-Host "microwave CLI not found in venv. Try: $Python -m pip install -e ." -ForegroundColor Red
    exit 1
}

& $MicrowaveExe run `
    --gateway-url $GatewayUrl `
    --region $Region `
    --model $Model `
    --engine $EngineType `
    --latitude $Lat `
    --longitude $Lon `
    --expert-domains $ExpertDomains `
    --reverse
