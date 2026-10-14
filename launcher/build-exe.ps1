# Rebuilds stick\CLUD.exe (the icon-carrying stub that runs CLUD.cmd) from CLUD.cs.
# Windows only: uses the C# compiler that ships with .NET Framework on Windows 10/11.
# The result is committed, so the installer works from macOS/Linux without this step.
$ErrorActionPreference = 'Stop'
$stick = Join-Path $PSScriptRoot '..\stick'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /target:exe /optimize+ "/win32icon:$(Join-Path $stick 'clud.ico')" "/out:$(Join-Path $stick 'CLUD.exe')" (Join-Path $PSScriptRoot 'CLUD.cs')
if ($LASTEXITCODE -ne 0) { throw 'CLUD.exe build failed' }
