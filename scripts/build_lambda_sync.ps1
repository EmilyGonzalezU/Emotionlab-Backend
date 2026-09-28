$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$SourceDir = Join-Path $Root "lambda-sync"
$BuildDir = Join-Path $Root "tmp-sync-build"
$OutputZip = Join-Path $Root "infrastructure\terraform\packages\sync.zip"

Write-Host "=== EmotionLab - Build Sync Lambda ===" -ForegroundColor Cyan

if (-not (Test-Path $SourceDir)) {
    throw "No existe la carpeta lambda-sync: $SourceDir"
}

if (Test-Path $BuildDir) {
    Write-Host "Limpiando build anterior..."
    Remove-Item $BuildDir -Recurse -Force
}

New-Item -ItemType Directory -Force $BuildDir | Out-Null

Write-Host "Copiando Lambda Sync..." -ForegroundColor Yellow

Copy-Item `
    (Join-Path $SourceDir "*") `
    $BuildDir `
    -Recurse `
    -Force

Write-Host "Eliminando archivos temporales..." -ForegroundColor Yellow

Get-ChildItem $BuildDir -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force

Get-ChildItem $BuildDir -Recurse -File -Filter "*.pyc" |
    Remove-Item -Force

Get-ChildItem $BuildDir -Recurse -Directory -Filter "*.egg-info" |
    Remove-Item -Recurse -Force

$OutputDir = Split-Path -Parent $OutputZip

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Force $OutputDir | Out-Null
}

$TempZip = Join-Path $Root "tmp-sync-test.zip"

if (Test-Path $TempZip) {
    Remove-Item $TempZip -Force
}

Write-Host "Comprimiendo Lambda..." -ForegroundColor Yellow

Compress-Archive `
    -Path "$BuildDir\*" `
    -DestinationPath $TempZip `
    -CompressionLevel Optimal `
    -Force

Copy-Item $TempZip $OutputZip -Force

$Size = (Get-Item $OutputZip).Length
$SizeMB = [math]::Round($Size / 1MB, 2)

Write-Host ""
Write-Host "=== BUILD COMPLETADO ===" -ForegroundColor Green
Write-Host "ZIP: $OutputZip"
Write-Host "Tamaño: $SizeMB MB"

Write-Host ""
Write-Host "SHA256:" -ForegroundColor Cyan
Get-FileHash $OutputZip -Algorithm SHA256
