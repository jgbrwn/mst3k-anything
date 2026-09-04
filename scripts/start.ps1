# Run with: .\scripts\start.ps1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    & $pyLauncher.Source -3 (Join-Path $Root "scripts\start.py") @args
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        throw "Python 3.10–3.14 is required. Run the installer first."
    }
    & $python.Source (Join-Path $Root "scripts\start.py") @args
}
exit $LASTEXITCODE
