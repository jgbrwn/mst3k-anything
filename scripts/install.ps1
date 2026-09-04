# Run with: Set-ExecutionPolicy -Scope Process Bypass; .\scripts\install.ps1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    & $pyLauncher.Source -3 (Join-Path $Root "scripts\install.py") @args
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        throw "Python 3.10–3.14 is required. Install Python 3.12, then rerun this script."
    }
    & $python.Source (Join-Path $Root "scripts\install.py") @args
}
exit $LASTEXITCODE
