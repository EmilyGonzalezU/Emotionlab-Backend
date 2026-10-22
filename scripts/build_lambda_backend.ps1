$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    python scripts/build_lambda_packages.py
    if ($LASTEXITCODE -ne 0) { throw "No se pudieron construir los ZIP de Lambda." }
    python scripts/verify_lambda_packages.py
    if ($LASTEXITCODE -ne 0) { throw "La verificaci?n de los ZIP de Lambda fall?." }
}
finally {
    Pop-Location
}
