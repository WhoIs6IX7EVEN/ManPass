$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (-not (Test-Path 'dist\ManPass\ManPass.exe')) { & '.\scripts\build_windows.ps1' }
$paths = @(
  '${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe',
  '${env:ProgramFiles}\Inno Setup 6\ISCC.exe'
)
$iscc = $null
foreach ($p in $paths) { $expanded = $ExecutionContext.InvokeCommand.ExpandString($p); if (Test-Path $expanded) { $iscc = $expanded; break } }
if (-not $iscc) { $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue; if ($cmd) { $iscc = $cmd.Source } }
if (-not $iscc) { throw 'Install Inno Setup 6 or add ISCC.exe to PATH' }
& $iscc 'installer\ManPass.iss'
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
Write-Host 'DONE: dist\installer\ManPass-Setup.exe'
