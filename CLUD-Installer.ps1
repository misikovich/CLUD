# CLUD Installer for Windows. Fetches uv (and through it Python + Textual) into .tools\
# next to this file the first time, so nothing gets installed system-wide.
# Double-click CLUD-Installer.bat to open it in a proper terminal window, or run this
# script directly. Arguments (--list, --target ...) are passed through to the installer;
# -Pause keeps the window open if something goes wrong.
$ErrorActionPreference = 'Stop'
$pause = $args -contains '-Pause'
$rest = @($args | Where-Object { $_ -ne '-Pause' })
$Host.UI.RawUI.WindowTitle = 'CLUD Installer'

$here = $PSScriptRoot
$tools = Join-Path $here '.tools'
$uv = Join-Path $tools 'uv\uv.exe'

try {
    if (-not (Test-Path $uv)) {
        Write-Host 'Fetching uv, one time only, about 20 MB...'
        New-Item -ItemType Directory -Force $tools | Out-Null
        $ProgressPreference = 'SilentlyContinue'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $zip = Join-Path $tools 'uv.zip'
        Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
        Expand-Archive -Force $zip (Join-Path $tools 'uv')
        Remove-Item $zip
    }

    $env:UV_CACHE_DIR = Join-Path $tools 'uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $tools 'python'
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $env:CLUD_UV = $uv

    & $uv run --quiet --python 3.13 (Join-Path $here 'installer\clud_installer.py') @rest
    $rc = $LASTEXITCODE
} catch {
    Write-Host "CLUD Installer couldn't start: $_" -ForegroundColor Red
    $rc = 1
}

if ($rc -ne 0 -and $pause) { Read-Host 'Press Enter to close' | Out-Null }
exit $rc
