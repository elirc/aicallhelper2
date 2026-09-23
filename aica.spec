# PyInstaller spec - windowed (no console) folder build.
# Build: .venv\Scripts\pyinstaller aica.spec --noconfirm   (build.ps1 does this)
#
# The exe's Windows version resource and the bundled build_info.json are both
# generated here from pyproject.toml (tools/release_meta.py), so Explorer's
# Properties > Details, the installer and the source can never disagree, and a
# shipped folder always says which source revision it was built from.

import os
import sys

sys.path.insert(0, SPECPATH)  # noqa: F821 - injected by PyInstaller

from PyInstaller.utils.win32.versioninfo import (  # noqa: E402
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

from tools import release_meta  # noqa: E402

VERSION = release_meta.read_version()
VERSION_TUPLE = release_meta.version_tuple(VERSION)

_build_info_dir = os.path.join(workpath, "build_info")  # noqa: F821 - injected by PyInstaller
_build_info_path = os.path.join(_build_info_dir, "build_info.json")
os.makedirs(_build_info_dir, exist_ok=True)
release_meta.main(["release_meta", "build-info", _build_info_path])

version_resource = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=VERSION_TUPLE,
        prodvers=VERSION_TUPLE,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,  # VOS_NT_WINDOWS32
        fileType=0x1,  # VFT_APP
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "AI Call Assistant"),
                        StringStruct("FileDescription", "AI Call Assistant"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", "AICallAssistant"),
                        StringStruct("OriginalFilename", "AICallAssistant.exe"),
                        StringStruct("ProductName", "AI Call Assistant"),
                        StringStruct("ProductVersion", VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("frontend/dist", "frontend/dist"),
        (_build_info_path, "."),
    ],
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
    excludes=["tkinter", "pytest", "mypy", "ruff", "tools"],
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
    console=False,  # windowed - no console flash
    version=version_resource,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AICallAssistant",
)
