from PyInstaller.utils.hooks import collect_submodules


hiddenimports = []

# Flask
hiddenimports += collect_submodules("flask")
hiddenimports += collect_submodules("flask_cors")

# Autenticación / criptografía legacy
hiddenimports += collect_submodules("passlib")
hiddenimports += collect_submodules("bcrypt")
hiddenimports += collect_submodules("jwt")
hiddenimports += collect_submodules("cryptography")

# Configuración
hiddenimports += collect_submodules("dotenv")

# Generación de reportes
hiddenimports += collect_submodules("reportlab")
hiddenimports += collect_submodules("openpyxl")


a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
        "sklearn",
        "pandas",
        "joblib",
        "firebase_admin",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EmotionLabBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="EmotionLabBackend",
)