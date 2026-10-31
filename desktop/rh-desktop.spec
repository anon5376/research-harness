import os
from pathlib import Path
import re
import sys

root = Path(SPECPATH).parent
macos = sys.platform == "darwin"
target_arch = os.environ.get("PYINSTALLER_TARGET_ARCH")
icon = root / "desktop" / "icons" / ("rh.icns" if macos else "rh.ico")
# Finder and About show the bundle's version; without one PyInstaller writes 0.0.0
version = re.search(r'^__version__ = "(.+)"$', (root / "rh" / "__init__.py").read_text(encoding="utf-8"), re.M)[1]
a = Analysis(
    [str(root / "desktop" / "app.py")],
    pathex=[str(root)],
    datas=[(str(root / "rh"), "rh")],
    hiddenimports=[],
)
pyz = PYZ(a.pure)
if macos:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Research Harness",
              console=False, icon=str(icon) if icon.exists() else None,
              target_arch=target_arch)
    coll = COLLECT(exe, a.binaries, a.datas, name="Research Harness")
    app = BUNDLE(coll, name="Research Harness.app",
                 icon=str(icon) if icon.exists() else None,
                 bundle_identifier="io.github.research-harness",
                 info_plist={"CFBundleShortVersionString": version, "CFBundleVersion": version})
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Research Harness",
              console=False, icon=str(icon) if icon.exists() else None)
