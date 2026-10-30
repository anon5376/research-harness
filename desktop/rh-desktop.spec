import os
from pathlib import Path
import sys

root = Path(SPECPATH).parent
macos = sys.platform == "darwin"
target_arch = os.environ.get("PYINSTALLER_TARGET_ARCH")
icon = root / "desktop" / "icons" / ("rh.icns" if macos else "rh.ico")
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
                 bundle_identifier="io.github.research-harness")
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Research Harness",
              console=False, icon=str(icon) if icon.exists() else None)
