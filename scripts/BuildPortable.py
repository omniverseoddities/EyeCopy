"""Build the Windows x64 distribution; no development runtime is needed to use it."""
from pathlib import Path
import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent

def ignore_runtime_test_data(directory, names):
    # PyInstaller's IPython hook collects upstream unit-test sample media and
    # loose test sources. They are not needed by the import-time dependency.
    # Preserve the frozen module archive, DLLs, package data, and type stubs;
    # a dependency's legitimate `test` API/stub directory is not a test suite.
    return [name for name in names if name == 'tests' and (Path(directory) / name).is_dir()]

def icons():
    from PIL import Image, ImageDraw
    assets = ROOT / 'assets'
    assets.mkdir(exist_ok=True)
    image = Image.new('RGBA', (256, 256), '#160e0b')
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 248, 248), radius=56, fill='#180e0b', outline='#ff7034', width=8)
    points = []
    for i in range(101):
        x = 34 + i * 1.88
        points.append((x, 128 - 57 * math.sin(math.pi * i / 100)))
    for i in range(100, -1, -1):
        x = 34 + i * 1.88
        points.append((x, 128 + 57 * math.sin(math.pi * i / 100)))
    draw.polygon(points, fill='#ff7a30')
    draw.ellipse((76, 76, 180, 180), fill='#ff3d25')
    draw.ellipse((94, 94, 162, 162), fill='#1c100c')
    draw.ellipse((105, 101, 123, 119), fill='#fff7ef')
    image.save(assets / 'eyecopy.png')
    image.save(assets / 'eyecopy.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])

def compile_helpers():
    framework = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319'
    compiler = framework / 'csc.exe'
    dest = ROOT / 'assets' / 'native'
    dest.mkdir(parents=True, exist_ok=True)
    common = ['/nologo', '/target:exe', '/platform:x64', '/optimize+', '/reference:System.Web.Extensions.dll']
    specifications = [
        ('desktop/native-drag/VirtualFileDrag.cs', 'EyeCopyDrag.exe', ['/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll']),
        ('scripts/MarkerDropProbe.cs', 'MarkerDropProbe.exe', ['/reference:Microsoft.CSharp.dll', '/reference:System.Windows.Forms.dll']),
        ('scripts/DropFolderProbe.cs', 'DropFolderProbe.exe', ['/reference:Microsoft.CSharp.dll'] + ['/reference:' + str(framework/'WPF'/n) for n in ('UIAutomationClient.dll','UIAutomationTypes.dll','WindowsBase.dll')]),
    ]
    for source, name, refs in specifications:
        subprocess.run([str(compiler), *common, *refs, '/out:' + str(dest/name), str(ROOT/source)], check=True)

def vendor_ffmpeg():
    subprocess.run([sys.executable, str(ROOT/'build-support/media/BuildMedia.py')], cwd=ROOT, check=True)
    binary = ROOT / 'vendor/ffmpeg/ffmpeg.exe'
    provenance = json.loads((ROOT/'LICENSES/FFmpeg-provenance.json').read_text(encoding='utf-8'))
    if hashlib.sha256(binary.read_bytes()).hexdigest() != provenance['binarySha256']:
        raise RuntimeError('The media binary does not match its source provenance.')
    if provenance.get('completeCorrespondingSourceConfirmed') is not True:
        raise RuntimeError('Matching media source and build material are required for packaging.')

def package():
    release = ROOT / 'release'
    release.mkdir(exist_ok=True)
    target = release / 'EyeCopy'
    if target.exists():
        if target.is_symlink() or target.resolve().parent != release.resolve() or not (target / '.eyecopy-build.json').is_file():
            raise RuntimeError('Existing distribution is not an owned EyeCopy build; refusing to replace it.')
        shutil.rmtree(target)
    electron = ROOT / 'node_modules' / 'electron' / 'dist'
    shutil.copytree(electron, target)
    (target/'electron.exe').rename(target/'EyeCopy.exe')
    shutil.copy2(ROOT/'build-output/installer/UninstallEyeCopy.exe', target/'UninstallEyeCopy.exe')
    shutil.copy2(ROOT/'build-output/updater/UpdateEyeCopy.exe', target/'UpdateEyeCopy.exe')
    (target/'.eyecopy-install.json').write_text(json.dumps({
        'applicationId': 'com.eyecopy.desktop', 'schema': 1, 'kind': 'application'
    }, indent=2), encoding='utf-8')
    sys.path.insert(0, str(ROOT/'build-support'))
    from windows_resources import brand_launcher
    version = json.loads((ROOT/'package.json').read_text(encoding='utf-8'))['version']
    brand_launcher(target/'EyeCopy.exe', ROOT/'assets/eyecopy.ico', version)
    app = target / 'resources' / 'app'
    app.mkdir(parents=True, exist_ok=True)
    for folder in ('desktop','ui','assets'):
        shutil.copytree(ROOT/folder, app/folder, ignore=shutil.ignore_patterns('test-*','README.md','.gitignore','screenshot.png','__pycache__'))
    (app/'diagnostics').mkdir()
    for name in ('Get-IPhoneUsbDiagnostics.ps1', 'UsbHubProbe.cs'):
        shutil.copy2(ROOT/'diagnostics'/name, app/'diagnostics'/name)
    (app/'scripts').mkdir()
    for name in ('MarkerDrop.js','DropFolderProbe.js'):
        shutil.copy2(ROOT/'scripts'/name, app/'scripts'/name)
    for name in ('package.json','GUIDE.html','Transport Options.html'):
        shutil.copy2(ROOT/name, app/name)
    (app/'node_modules').mkdir()
    shutil.copytree(ROOT/'node_modules'/'koffi', app/'node_modules'/'koffi')
    shutil.copytree(ROOT/'node_modules'/'@koromix'/'koffi-win32-x64', app/'node_modules'/'@koromix'/'koffi-win32-x64')
    shutil.copytree(ROOT/'vendor'/'ffmpeg', app/'vendor'/'ffmpeg')
    shutil.copytree(ROOT/'build-output'/'EyeCopyEngine', app/'runtime'/'engine', ignore=ignore_runtime_test_data)
    if (ROOT/'LICENSES').is_dir():
        shutil.copytree(ROOT/'LICENSES', target/'LICENSES',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    if (ROOT/'SOURCE-NOTICE.txt').is_file():
        shutil.copy2(ROOT/'SOURCE-NOTICE.txt', target/'SOURCE-NOTICE.txt')
    shutil.copy2(ROOT/'LICENSE', target/'LICENSE')
    shutil.copy2(ROOT/'README.txt', target/'README.txt')
    shutil.copy2(ROOT/'GUIDE.html', target/'GUIDE.html')
    shutil.copy2(ROOT/'Transport Options.html', target/'Transport Options.html')
    shutil.copytree(ROOT/'docs', target/'docs')
    sources = target / 'Sources' / 'EyeCopy'
    sources.mkdir(parents=True)
    for folder in ('backend', 'desktop', 'ui', 'build-support', 'iphone-companion', 'installer', 'updater'):
        shutil.copytree(ROOT/folder, sources/folder, ignore=shutil.ignore_patterns('test_*','test-*','Test*.py','Test*.cs','__pycache__'))
    (sources/'scripts').mkdir()
    for name in ('BuildPortable.py','PrepareRelease.py','AuditRelease.py','ConfigureRelease.py','MarkerDrop.js','MarkerDropProbe.cs','DropFolderProbe.js','DropFolderProbe.cs'):
        shutil.copy2(ROOT/'scripts'/name, sources/'scripts'/name)
    for name in ('requirements.txt','requirements-lock.txt','requirements-build.txt','package.json','package-lock.json','README.txt','QUICK START.txt','GUIDE.html','Transport Options.html','LICENSE','SOURCE-NOTICE.txt'):
        shutil.copy2(ROOT/name,sources/name)
    shutil.copytree(ROOT/'docs', sources/'docs')
    shutil.copy2(ROOT/'QUICK START.txt', target/'QUICK START.txt')
    files = {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file()}
    (target/'.eyecopy-build.json').write_text(json.dumps({'application':'EyeCopy','version':version,'architecture':'Windows x64','bundledEngine':True,'sourceFiles':files},indent=2),encoding='utf-8')
    print(json.dumps({'distribution':str(target),'files':len(files),'bytes':sum(p.stat().st_size for p in target.rglob('*') if p.is_file())}),flush=True)
    return target

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--skip-engine', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' or sys.maxsize < 2**32:
        raise RuntimeError('Build requires Windows x64.')
    sys.path.insert(0, str(ROOT/'build-support'))
    from write_version_info import write_version_info
    write_version_info(ROOT)
    icons(); compile_helpers(); vendor_ffmpeg()
    if args.prepare_only:
        return
    subprocess.run([sys.executable, str(ROOT/'build-support/BuildInstaller.py'), '--uninstaller-only'], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT/'updater/BuildUpdater.py')], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT/'LICENSES/CollectNotices.py')], cwd=ROOT, check=True)
    if not args.skip_engine:
        subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--distpath',str(ROOT/'build-output'),'--workpath',str(ROOT/'build-work'),str(ROOT/'build-support'/'EyeCopyEngine.spec')],cwd=ROOT,check=True)
    package()

if __name__ == '__main__':
    main()
