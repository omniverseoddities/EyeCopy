from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs, collect_data_files
from pathlib import Path
import sys
sys.setrecursionlimit(5000)
root = Path(SPECPATH).parent
hidden = collect_submodules('pymobiledevice3') + collect_submodules('pillow_heif') + [
    'backend.streaming', 'backend.video_duration', 'backend.pc_recycle', 'backend.recycle_undo', 'backend.phone_folders', 'PIL.Image', 'PIL.ImageOps',
    'pythoncom', 'pywintypes', 'win32com.server.policy', 'win32com.server.exception',
    'win32com.shell.shell', 'win32com.shell.shellcon',
]
a = Analysis([str(root / 'backend' / 'engine.py')], pathex=[str(root)],
    binaries=collect_dynamic_libs('pillow_heif'),
    datas=collect_data_files('pymobiledevice3'), hiddenimports=hidden,
    excludes=['matplotlib', 'pytest', 'tkinter'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='EyeCopyEngine',
    console=True, icon=str(root / 'assets' / 'eyecopy.ico'),
    version=str(root / 'build-support' / 'version-info.txt'))
coll = COLLECT(exe, a.binaries, a.datas, name='EyeCopyEngine')
