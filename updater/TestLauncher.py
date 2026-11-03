"""Test updater entry-point guards with synthetic files and an injected launcher."""
from pathlib import Path
import json
import subprocess
import uuid
import _winapi
from BuildUpdater import ROOT, compiler


def main():
    output = ROOT / 'test-output/updater-launcher' / uuid.uuid4().hex
    output.mkdir(parents=True)
    target = output / 'LauncherTests.exe'
    subprocess.run([str(compiler()), '/nologo', '/target:exe', '/platform:x64', '/optimize+', '/debug-',
                    '/reference:System.Web.Extensions.dll', '/reference:System.Windows.Forms.dll', '/reference:System.Core.dll',
                    '/main:LauncherTests', '/out:' + str(target), str(ROOT/'updater/UpdateLauncher.cs'),
                    str(ROOT/'updater/TestLauncher.cs')], check=True, cwd=ROOT)
    data = output / 'fixtures'
    data.mkdir()
    real = data / 'real'
    child = real / 'child'
    child.mkdir(parents=True)
    marker = {'applicationId': 'com.eyecopy.desktop', 'schema': 1, 'kind': 'application'}
    for folder in (real, child):
        (folder/'.eyecopy-install.json').write_text(json.dumps(marker), encoding='utf-8')
        (folder/'EyeCopy.exe').write_bytes(b'MZ\0\0')
    _winapi.CreateJunction(str(real), str(data/'junction-root'))
    _winapi.CreateJunction(str(real), str(data/'junction-parent'))
    profile = data/'profile-real'
    profile.mkdir()
    (profile/'.eyecopy-profile.json').write_text(json.dumps({**marker, 'kind': 'profile'}), encoding='utf-8')
    _winapi.CreateJunction(str(profile), str(data/'profile-link'))
    run = subprocess.run([str(target), str(data)], cwd=ROOT, capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    if run.returncode:
        raise RuntimeError(run.stdout + run.stderr)
    report = json.loads(run.stdout)
    assert report['passed'] and report['checks'] == 24 and report['realApplicationLaunches'] == 0
    (ROOT/'test-output/updater-launcher-tests.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
