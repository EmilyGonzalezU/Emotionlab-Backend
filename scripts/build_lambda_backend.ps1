$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BuildDir = Join-Path $Root "tmp-backend-build"
$OutputZip = Join-Path $Root "infrastructure\terraform\packages\backend.zip"

Write-Host "=== EmotionLab - Build Backend Lambda ===" -ForegroundColor Cyan

# Limpiar build anterior
if (Test-Path $BuildDir) {
    Write-Host "Limpiando build anterior..."
    Remove-Item $BuildDir -Recurse -Force
}

New-Item -ItemType Directory -Force $BuildDir | Out-Null

# Instalar dependencias Linux/Python 3.13
Write-Host "Instalando dependencias para Lambda..." -ForegroundColor Yellow

python -m pip install `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.13 `
    --only-binary=:all: `
    --target $BuildDir `
    -r (Join-Path $Root "requirements-lambda.txt")

# Copiar código de la aplicación
Write-Host "Copiando código..." -ForegroundColor Yellow

$Folders = @(
    "routes",
    "services",
    "core",
    "middleware",
    "utils"
)

foreach ($Folder in $Folders) {
    Copy-Item `
        (Join-Path $Root $Folder) `
        $BuildDir `
        -Recurse `
        -Force
}

Copy-Item `
    (Join-Path $Root "app.py") `
    $BuildDir `
    -Force

Copy-Item `
    (Join-Path $Root "lambda_handler.py") `
    $BuildDir `
    -Force

Copy-Item `
    (Join-Path $Root "requirements-lambda.txt") `
    $BuildDir `
    -Force

# Eliminar bytecode y caches
Write-Host "Limpiando archivos temporales..." -ForegroundColor Yellow

Get-ChildItem $BuildDir -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force

Get-ChildItem $BuildDir -Recurse -File -Filter "*.pyc" |
    Remove-Item -Force

# Crear carpeta de salida
$OutputDir = Split-Path -Parent $OutputZip

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Force $OutputDir | Out-Null
}

# Crear ZIP temporal
$TempZip = Join-Path $Root "tmp-backend-test.zip"

if (Test-Path $TempZip) {
    Remove-Item $TempZip -Force
}

Write-Host "Comprimiendo Lambda..." -ForegroundColor Yellow

Compress-Archive `
    -Path "$BuildDir\*" `
    -DestinationPath $TempZip `
    -CompressionLevel Optimal `
    -Force

# Reemplazar el paquete Terraform
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