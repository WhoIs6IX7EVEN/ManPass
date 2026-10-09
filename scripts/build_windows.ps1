$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Create .venv first: py -3.12 -m venv .venv' }
& $python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependencies install failed' }
& $python -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; release aborted' }
$entry = (Resolve-Path 'app\main.py').Path
$icon = (Resolve-Path 'assets\manpass.ico').Path
& (Join-Path (Get-Location) '.venv\Scripts\flet.exe') pack $entry --name ManPass --onedir --icon $icon --yes --distpath dist
if ($LASTEXITCODE -ne 0) { throw 'Flet pack failed' }
if (-not (Test-Path 'dist\ManPass\ManPass.exe')) { throw 'ManPass.exe not found in dist\ManPass' }
if (Test-Path 'ManPass-Portable.zip') { Remove-Item 'ManPass-Portable.zip' }
Compress-Archive -Path 'dist\ManPass\*' -DestinationPath 'ManPass-Portable.zip'
Write-Host 'DONE: dist\ManPass\ManPass.exe and ManPass-Portable.zip'
