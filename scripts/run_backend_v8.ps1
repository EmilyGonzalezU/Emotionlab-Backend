param(
    [string]$PythonPath = ".venv-v8\python.exe",
    [string]$EntryPoint = "app.py"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path

function Resolve-ProjectPath([string]$PathValue) {
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return (Resolve-Path -LiteralPath $PathValue -ErrorAction Stop).Path
    }
    return (Resolve-Path -LiteralPath (Join-Path $repoRoot $PathValue) -ErrorAction Stop).Path
}

$python = Resolve-ProjectPath $PythonPath
$entry = Resolve-ProjectPath $EntryPoint
$manifest = Join-Path $repoRoot "ia\modelo_v8\manifest_sha256_v1.json"
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw "No se encontró el manifiesto de integridad del paquete V8: $manifest"
}

$runtimeOutput = & $python -c "import json,sys,joblib,numpy,pandas,scipy,sklearn,threadpoolctl; print(json.dumps({'python':f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}','numpy':numpy.__version__,'pandas':pandas.__version__,'scipy':scipy.__version__,'sklearn':sklearn.__version__,'joblib':joblib.__version__,'threadpoolctl':threadpoolctl.__version__}))"
if ($LASTEXITCODE -ne 0) {
    throw "No se pudieron validar las dependencias del entorno V8: $python"
}

try {
    $runtime = ($runtimeOutput | Out-String).Trim() | ConvertFrom-Json
} catch {
    throw "El entorno V8 devolvió información de versiones no válida."
}

$expected = @{
    python = "3.13.18"
    numpy = "2.4.6"
    pandas = "2.2.3"
    scipy = "1.16.3"
    sklearn = "1.6.1"
    joblib = "1.6.0"
    threadpoolctl = "3.7.0"
}
$mismatches = @(
    foreach ($name in $expected.Keys) {
        if ([string]$runtime.$name -ne $expected[$name]) {
            "$name esperado $($expected[$name]); detectado $($runtime.$name)"
        }
    }
)
if ($mismatches.Count -gt 0) {
    throw "El entorno no coincide con los pins de V8: $($mismatches -join '; ')"
}

Write-Host "Iniciando EmotionLab V8 con Python $($runtime.python), scikit-learn $($runtime.sklearn) y Joblib $($runtime.joblib)."
Write-Host "La inferencia V8 se ejecutará localmente. La autenticación y persistencia siguen la configuración del .env."

$hadV8Mode = Test-Path Env:EMOTION_MODEL_V8_MODE
$previousV8Mode = $env:EMOTION_MODEL_V8_MODE
$exitCode = 1
Push-Location $repoRoot
try {
    $env:EMOTION_MODEL_V8_MODE = "local"
    & $python $entry
    $exitCode = $LASTEXITCODE
} finally {
    if ($hadV8Mode) {
        $env:EMOTION_MODEL_V8_MODE = $previousV8Mode
    } else {
        Remove-Item Env:EMOTION_MODEL_V8_MODE -ErrorAction SilentlyContinue
    }
    Pop-Location
}

exit $exitCode
