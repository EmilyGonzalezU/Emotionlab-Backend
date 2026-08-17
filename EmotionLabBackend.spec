from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []

# Firebase
hiddenimports += collect_submodules("firebase_admin")
hiddenimports += collect_submodules("google.cloud")

# Machine Learning
hiddenimports += collect_submodules("sklearn")
hiddenimports += collect_submodules("pandas")

# Passlib / bcrypt
hiddenimports += collect_submodules("passlib")
hiddenimports += collect_submodules("bcrypt")

# Flask y extras
hiddenimports += collect_submodules("flask")
hiddenimports += collect_submodules("flask_cors")
hiddenimports += collect_submodules("openpyxl")
hiddenimports += collect_submodules("reportlab")
hiddenimports += collect_submodules("jwt")
hiddenimports += collect_submodules("cryptography")
hiddenimports += collect_submodules("dotenv")


a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],

    # IMPORTANTE: NUNCA incluir .env ni firebase_key.json dentro del
    # ejecutable, porque son archivos de configuración con SECRETOS y
    # deben poder cambiarse sin recompilar. Se copian externamente
    # en resources/config/ por el instalador de Electron.
    datas=[
        ("ia/modelo_pipeline.pkl", "ia"),
    ],

    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
    ],
    noarchive=False,
)


pyz = PYZ(a.pure)


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
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
    upx_exclude=[],
    name="EmotionLabBackend",
)