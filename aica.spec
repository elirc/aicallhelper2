# PyInstaller spec — windowed (no console) folder build.
# Build: .venv\Scripts\pyinstaller aica.spec

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[("frontend/dist", "frontend/dist")],
    hiddenimports=[
        "app_core",
        "pyaudiowpatch",
        "win32crypt",
        "win32event",
        "win32api",
        "win32gui",
        "win32con",
        "winerror",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "mypy", "ruff"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AICallAssistant",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # windowed — no console flash
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AICallAssistant",
)
