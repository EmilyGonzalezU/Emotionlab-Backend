param(
    [string]$PythonPath = ".venv-v2\Scripts\python.exe",
    [string]$EntryPoint = "app.py"
)

$ErrorActionPreference = "Stop"
$python = Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop

$runtime = & $python -c "import json,sys,sklearn; print(json.dumps({'python':f'{sys.version_info.major}.{sys.version_info.minor}','sklearn':sklearn.__version__}))"
if ($LASTEXITCODE -ne 0) { throw "No se pudo consultar el entorno local de V2." }
$runtimeInfo = $runtime | ConvertFrom-Json
if ($runtimeInfo.python -ne "3.12" -or $runtimeInfo.sklearn -ne "1.6.1") {
    throw "El backend V2 requiere Python 3.12 y scikit-learn 1.6.1; entorno detectado: Python $($runtimeInfo.python), scikit-learn $($runtimeInfo.sklearn)."
}

Write-Host "Iniciando EmotionLab con Python $($runtimeInfo.python) y scikit-learn $($runtimeInfo.sklearn)."
$env:EMOTION_MODEL_V2_MODE = "local"
& $python $EntryPoint
exit $LASTEXITCODE
