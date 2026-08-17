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


a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],

    datas=[
        ("ia/modelo_pipeline.pkl", "ia"),
        ("config/firebase_key.json", "config"),
        ("config/.env", "config"),
    ],

    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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