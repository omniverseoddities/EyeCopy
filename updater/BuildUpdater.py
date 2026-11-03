"""Compile the small Windows entry point for EyeCopy's shared updater."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def compiler():
    return Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'


def build():
    metadata = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    version = metadata['version']
    if not re.fullmatch(r'(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})', version) or any(int(n) > 65535 for n in version.split('.')):
        raise ValueError('A stable three-part Windows version is required.')
    output = ROOT / 'build-output/updater'
    output.mkdir(parents=True, exist_ok=True)
    assembly = output / 'AssemblyInfo.cs'
    assembly.write_text('''using System.Reflection;
[assembly: AssemblyTitle("EyeCopy Update")]
[assembly: AssemblyDescription("Open EyeCopy update settings")]
[assembly: AssemblyCompany("EyeCopy contributors")]
[assembly: AssemblyProduct("EyeCopy")]
[assembly: AssemblyVersion("%s.0")]
[assembly: AssemblyFileVersion("%s.0")]
[assembly: AssemblyInformationalVersion("%s")]
''' % (version, version, version), encoding='utf-8')
    target = output / 'UpdateEyeCopy.exe'
    subprocess.run([str(compiler()), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/debug-',
                    '/reference:System.Web.Extensions.dll', '/reference:System.Windows.Forms.dll', '/reference:System.Core.dll',
                    '/win32icon:' + str(ROOT / 'assets/eyecopy.ico'), '/out:' + str(target),
                    str(ROOT / 'updater/UpdateLauncher.cs'), str(assembly)], check=True, cwd=ROOT)
    report = {'updater': str(target), 'version': version, 'bytes': target.stat().st_size,
              'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    print(json.dumps(report), flush=True)
    return target


if __name__ == '__main__':
    build()
