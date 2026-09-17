# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec (WP4, 2026-09-17).

macOS  -> dist/eFactura Sync.app   (onedir bundle; zipped by the release workflow)
Windows-> dist/eFactura-Sync.exe   (onefile)
Both run the `ui` command by default (see efactura_sync/__main__.py) and are
unsigned in v1 — the README documents the Gatekeeper / SmartScreen bypass.

Build: pyinstaller efactura_sync.spec
"""

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
__version__ = re.search(r'__version__ = "([^"]+)"',
                        (ROOT / "efactura_sync" / "__init__.py").read_text()).group(1)

APP_NAME = "eFactura Sync"
EXE_NAME = "eFactura-Sync"

a = Analysis(
    [str(ROOT / "efactura_sync" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # Jinja templates are data, not code: without this every page would 500.
    datas=[(str(ROOT / "efactura_sync" / "web" / "templates"), "efactura_sync/web/templates")],
    hiddenimports=collect_submodules("efactura_sync") + ["lxml._elementpath"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        console=False,
        target_arch=None,              # native arch of the build runner
    )
    coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        bundle_identifier="ro.efactura-sync.app",
        info_plist={
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            # Background helper that opens a browser; keep it out of the Dock's
            # foreground so the browser window is what the user sees.
            "LSUIElement": False,
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=EXE_NAME,
        console=False,
        upx=False,
    )
