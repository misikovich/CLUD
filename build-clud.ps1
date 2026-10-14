# Builds a portable Claude Code bundle ("CLUD") into -Target.
# Downloads portable Node.js (LTS) and PortableGit, then installs Claude Code from npm.
# Re-running it skips parts that are already there; use -Force to rebuild everything.
param(
    [string]$Target = (Join-Path $PSScriptRoot 'CLUD'),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$src = Join-Path $PSScriptRoot 'src'
$dl  = Join-Path $PSScriptRoot '.downloads'
New-Item -ItemType Directory -Force $Target, $dl | Out-Null

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- Node.js (latest LTS, win-x64 zip) ---------------------------------------
$nodeDir = Join-Path $Target 'node'
if ($Force -or -not (Test-Path (Join-Path $nodeDir 'node.exe'))) {
    $lts = (Invoke-RestMethod 'https://nodejs.org/dist/index.json') | Where-Object { $_.lts } | Select-Object -First 1
    $name = "node-$($lts.version)-win-x64"
    $zip = Join-Path $dl "$name.zip"
    Step "Downloading Node.js $($lts.version) LTS"
    if (-not (Test-Path $zip)) { Invoke-WebRequest "https://nodejs.org/dist/$($lts.version)/$name.zip" -OutFile $zip }
    Step 'Extracting Node.js'
    if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
    $tmp = Join-Path $dl 'node-extract'
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive $zip $tmp
    Move-Item (Join-Path $tmp $name) $nodeDir
    Remove-Item -Recurse -Force $tmp
} else { Step 'Node.js already present' }

# --- PortableGit (provides git + the bash.exe Claude Code needs on Windows) ---
$gitDir = Join-Path $Target 'git'
if ($Force -or -not (Test-Path (Join-Path $gitDir 'bin\bash.exe'))) {
    $rel = Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' -Headers @{ 'User-Agent' = 'clud-build' }
    $asset = $rel.assets | Where-Object { $_.name -like 'PortableGit-*-64-bit.7z.exe' } | Select-Object -First 1
    $exe = Join-Path $dl $asset.name
    Step "Downloading $($asset.name)"
    if (-not (Test-Path $exe)) { Invoke-WebRequest $asset.browser_download_url -OutFile $exe }
    Step 'Extracting PortableGit'
    if (Test-Path $gitDir) { Remove-Item -Recurse -Force $gitDir }
    & $exe "-o$gitDir" -y | Out-Null
    if (-not (Test-Path (Join-Path $gitDir 'bin\bash.exe'))) { throw 'PortableGit extraction failed' }
} else { Step 'PortableGit already present' }

# --- Claude Code --------------------------------------------------------------
$appDir = Join-Path $Target 'app'
if ($Force -or -not (Test-Path (Join-Path $appDir 'claude.cmd'))) {
    Step 'Installing Claude Code from npm'
    $env:PATH = "$nodeDir;$env:PATH"
    & (Join-Path $nodeDir 'npm.cmd') install -g --prefix $appDir --cache (Join-Path $Target '.npm-cache') --allow-scripts=@anthropic-ai/claude-code '@anthropic-ai/claude-code@latest'
    if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
} else { Step 'Claude Code already present' }

# --- Computer use: standalone Python + Windows-MCP -----------------------------
# Installed straight into a relocatable python-build-standalone copy (no venv), so it
# keeps working whatever drive letter the stick gets. uv is only used here, at build time.
$pyDir = Join-Path $Target 'py'
if ($Force -or -not (Test-Path (Join-Path $pyDir 'Lib\site-packages\windows_mcp'))) {
    $uv = Join-Path $dl 'uv\uv.exe'
    if (-not (Test-Path $uv)) {
        Step 'Downloading uv'
        $rel = Invoke-RestMethod 'https://api.github.com/repos/astral-sh/uv/releases/latest' -Headers @{ 'User-Agent' = 'clud-build' }
        $asset = $rel.assets | Where-Object { $_.name -eq 'uv-x86_64-pc-windows-msvc.zip' }
        Invoke-WebRequest $asset.browser_download_url -OutFile (Join-Path $dl 'uv.zip')
        Expand-Archive (Join-Path $dl 'uv.zip') (Join-Path $dl 'uv') -Force
    }
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $dl 'pythons'
    $env:UV_CACHE_DIR = Join-Path $dl 'uv-cache'
    Step 'Installing standalone Python 3.13'
    & $uv python install 3.13 --quiet
    if ($LASTEXITCODE -ne 0) { throw 'uv python install failed' }
    $py = Get-ChildItem $env:UV_PYTHON_INSTALL_DIR -Directory -Filter 'cpython-3.13.*-windows-x86_64-none' | Sort-Object Name -Descending | Select-Object -First 1
    if (Test-Path $pyDir) { Remove-Item -Recurse -Force $pyDir }
    Copy-Item $py.FullName $pyDir -Recurse
    Step 'Installing Windows-MCP'
    & $uv pip install --quiet --python (Join-Path $pyDir 'python.exe') --system --break-system-packages windows-mcp
    if ($LASTEXITCODE -ne 0) { throw 'windows-mcp install failed' }
} else { Step 'Python + Windows-MCP already present' }

# --- Launcher, autorun, docs ----------------------------------------------------
Step 'Copying launcher and drive files'
New-Item -ItemType Directory -Force (Join-Path $Target 'config'), (Join-Path $Target 'work') | Out-Null
Copy-Item (Join-Path $src '*') $Target -Recurse -Force

# CLUD.exe: tiny stub carrying the icon (a .cmd can't have one), built with the
# C# compiler that ships with .NET Framework on every Windows 10/11.
Step 'Building CLUD.exe'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /target:exe /optimize+ "/win32icon:$(Join-Path $src 'clud.ico')" "/out:$(Join-Path $Target 'CLUD.exe')" (Join-Path $PSScriptRoot 'launcher\CLUD.cs')
if ($LASTEXITCODE -ne 0) { throw 'CLUD.exe build failed' }

# Register Windows-MCP for every session. Only written when missing so a rebuild never
# clobbers a live config (Claude Code keeps onboarding and account state in this file).
$cfg = Join-Path $Target 'config\.claude.json'
if (-not (Test-Path $cfg)) {
    Set-Content $cfg -Encoding Ascii -Value '{"mcpServers":{"windows-mcp":{"type":"stdio","command":"cmd","args":["/c","windows-mcp.cmd"],"env":{}}}}'
}

Step "Done. Bundle is in $Target"
