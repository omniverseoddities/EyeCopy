"""Create validated local GitHub release assets; this script publishes nothing."""
from pathlib import Path
import json
import os
import subprocess
import sys
import uuid
import zipfile

from AuditRelease import ROOT, audit_distribution, audit_source, digest, source_files


def archive(target, files):
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as output:
        for path, relative in files:
            assert '__pycache__' not in Path(relative).parts and path.suffix.lower() not in {'.pyc', '.pyo'}, \
                'Generated Python caches must not be distributed.'
            output.write(path, 'EyeCopy/' + relative.replace('\\', '/'))
    with zipfile.ZipFile(temporary) as output:
        assert output.testzip() is None
    os.replace(temporary, target)
    return {'file': target.name, 'bytes': target.stat().st_size, 'sha256': digest(target), 'crcPassed': True}


def main():
    subprocess.run([sys.executable, str(ROOT/'build-support/BuildInstaller.py'), '--setup'], cwd=ROOT, check=True)
    source_check = audit_source()
    distribution = ROOT/'release/EyeCopy'
    distribution_check = audit_distribution(distribution)
    output = ROOT/'artifacts'
    output.mkdir(exist_ok=True)
    assert not output.is_symlink()
    previous_path = output/'verification.json'
    previous = json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else {}
    names = ('EyeCopy-Windows-x64.zip', 'EyeCopy-source.zip', 'EyeCopy-dependency-sources.zip')
    for name in names:
        existing = output/name
        if existing.exists():
            receipt = next((entry for entry in previous.get('archives', []) if entry['file'] == name), None)
            assert receipt and digest(existing) == receipt['sha256'], 'Refusing to replace an unverified existing archive.'
    archives = [archive(output/names[0], [(p, str(p.relative_to(distribution)))
                for p in sorted(distribution.rglob('*')) if p.is_file()])]
    archives.append(archive(output/names[1], [(p, str(p.relative_to(ROOT))) for p in source_files()]))
    dependencies = [(p, str(p.relative_to(ROOT))) for p in sorted((ROOT/'LICENSES').rglob('*'))
                    if p.is_file() and '__pycache__' not in p.parts and p.suffix.lower() not in {'.pyc', '.pyo'}]
    dependencies.extend((p, str(p.relative_to(ROOT))) for p in sorted((ROOT/'build-support/media').rglob('*'))
                        if p.is_file() and '__pycache__' not in p.parts and p.suffix.lower() not in {'.pyc', '.pyo'})
    archives.append(archive(output/names[2], dependencies))
    version = json.loads((ROOT/'package.json').read_text(encoding='utf-8'))['version']
    setup = output/f'Setup-EyeCopy-{version}.exe'
    archives.append({'file': setup.name, 'bytes': setup.stat().st_size, 'sha256': digest(setup)})
    (output/'SHA256SUMS.txt').write_text(''.join(entry['sha256'] + '  ' + entry['file'] + '\n'
                                              for entry in archives), encoding='ascii')
    report = {'application': 'EyeCopy', 'release': 'v'+version, 'version': version,
              'source': source_check, 'distribution': distribution_check, 'archives': archives,
              'published': False, 'requiresAppleUsbSupport': True}
    (output/'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
