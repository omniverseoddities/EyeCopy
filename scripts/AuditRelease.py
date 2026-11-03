"""Check the source export and Windows package before creating release assets."""
from pathlib import Path
import argparse
import hashlib
import json
import marshal
import os
import re
import types

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = ('.gitignore', '.gitattributes', '.npmrc', 'AGENTS.md', 'README.md', 'README.txt',
              'CONTRIBUTING.md', 'LICENSE', 'SOURCE-NOTICE.txt', 'GUIDE.html', 'Transport Options.html',
              'QUICK START.txt', 'package.json', 'package-lock.json', 'requirements.txt',
              'requirements-lock.txt', 'requirements-build.txt')
SOURCE_DIRS = ('backend', 'desktop', 'ui', 'assets', 'scripts', 'tests', 'diagnostics', 'build-support', 'installer', 'updater',
               'iphone-companion', 'docs', '.github', 'LICENSES')
TEXT_SUFFIXES = {'.py', '.js', '.json', '.cs', '.swift', '.html', '.css', '.txt', '.md', '.yml',
                 '.yaml', '.spec', '.sh', '.plist', '.ps1', '.toml', '.patch', '.cfg', '.nsi', '.nsh', '.gitattributes', '.gitignore', '.npmrc'}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def source_files():
    files = [ROOT / name for name in ROOT_FILES]
    for name in SOURCE_DIRS:
        for path in (ROOT / name).rglob('*'):
            relative = path.relative_to(ROOT)
            if '__pycache__' in relative.parts or path.suffix in {'.pyc', '.pyo'}:
                continue
            if relative.parts[:2] == ('LICENSES', 'sources'):
                continue
            if relative.parts[:2] == ('assets', 'native') and path.suffix.lower() == '.exe':
                continue
            if path.is_file():
                files.append(path)
    for path in files:
        assert path.is_file() and not path.is_symlink(), f'Missing source file: {path.name}'
        assert path.stat().st_size < 50 * 1024 * 1024, f'Use release assets for large files: {path.name}'
    return sorted(set(files))


def private_tokens(additional=()):
    # Machine-specific values are discovered at validation time, never embedded.
    values = [os.environ.get('USERPROFILE', ''), str(ROOT.resolve()), *additional]
    username = os.environ.get('USERNAME', '')
    if len(username) > 4 and username.lower() not in {'runneradmin', 'runner', 'administrator'}:
        values.append(username)
    return tuple(value.casefold() for value in values if value)


def check_text(value, label, tokens, authored=False):
    folded = value.casefold()
    assert not any(token in folded for token in tokens), f'Private or obsolete content: {label}'
    if authored:
        assert not re.search(r'[A-Za-z]:[\\/]Users[\\/](?!Public\b|<)', value), f'Home path: {label}'
        assert not re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', value), f'Private key: {label}'


def audit_source(additional=()):
    files = source_files()
    tokens = private_tokens(additional)
    checked = 0
    for path in files:
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {'LICENSE', 'PKGBUILD', '.gitignore', '.gitattributes', '.npmrc'}:
            check_text(path.read_text(encoding='utf-8', errors='replace'), str(path.relative_to(ROOT)), tokens,
                       authored='LICENSES' not in path.relative_to(ROOT).parts)
            checked += 1
    metadata = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    lock = json.loads((ROOT / 'package-lock.json').read_text(encoding='utf-8'))
    assert metadata['version'] == lock['version'] == lock['packages']['']['version']
    assert re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', metadata['version'])
    assert metadata['license'] == 'GPL-3.0-or-later'
    return {'passed': True, 'files': len(files), 'textFilesChecked': checked, 'version': metadata['version']}


def _code_check(code, tokens):
    count = 0
    if isinstance(code, types.CodeType):
        assert not os.path.isabs(code.co_filename), 'Frozen code contains an absolute build filename.'
        check_text(code.co_filename, 'frozen code filename', tokens)
        count = 1
        for value in code.co_consts:
            if isinstance(value, str):
                check_text(value, 'frozen code constant', tokens)
            elif isinstance(value, types.CodeType):
                count += _code_check(value, tokens)
    return count


def audit_distribution(distribution, additional=()):
    from PyInstaller.archive.readers import CArchiveReader
    import win32api

    distribution = Path(distribution).resolve()
    assert ROOT in distribution.parents and not distribution.is_symlink()
    manifest_path = distribution / '.eyecopy-build.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    version = json.loads((ROOT/'package.json').read_text(encoding='utf-8'))['version']
    assert manifest['application'] == 'EyeCopy' and manifest['version'] == version
    ownership = json.loads((distribution/'.eyecopy-install.json').read_text(encoding='utf-8'))
    assert ownership == {'applicationId': 'com.eyecopy.desktop', 'schema': 1, 'kind': 'application'}
    assert (distribution/'UninstallEyeCopy.exe').is_file()
    assert (distribution/'UpdateEyeCopy.exe').is_file()
    files = [path for path in distribution.rglob('*') if path.is_file()]
    assert {str(p.relative_to(distribution)) for p in files} == set(manifest['sourceFiles']) | {'.eyecopy-build.json'}
    tokens = private_tokens(additional)
    text_count, binary_count = 0, 0
    for path in files:
        relative = path.relative_to(distribution)
        assert not path.is_symlink()
        assert '__pycache__' not in relative.parts and path.suffix.lower() not in {'.pyc', '.pyo'}, \
            f'Generated Python cache: {relative}'
        assert not set(relative.parts) & {'test-output', '.venv', 'downloads', 'build-work', '.git'}, str(relative)
        assert relative.parts[0] != 'data' and relative.parts[:3] != ('resources', 'app', 'data'), str(relative)
        assert path.name not in {'state.json', 'desktop.log', 'desktop-settings.json'}
        if path != manifest_path:
            assert digest(path) == manifest['sourceFiles'][str(relative)], f'Package hash mismatch: {relative}'
        if path.suffix.lower() in TEXT_SUFFIXES:
            check_text(path.read_text(encoding='utf-8', errors='replace'), str(relative), tokens)
            text_count += 1
        if path.suffix.lower() in {'.exe', '.dll', '.pyd', '.node'}:
            raw = path.read_bytes()
            for token in tokens:
                assert token.encode('utf-8') not in raw.lower(), f'Private binary string: {relative}'
                assert token.encode('utf-16le') not in raw.lower(), f'Private binary string: {relative}'
            binary_count += 1
    for name in ('ProductName', 'FileDescription', 'ProductVersion'):
        value = win32api.GetFileVersionInfo(str(distribution/'EyeCopy.exe'), '\\StringFileInfo\\040904B0\\' + name)
        assert value == (version if name == 'ProductVersion' else 'EyeCopy')
    engine = distribution / 'resources/app/runtime/engine/EyeCopyEngine.exe'
    archive = CArchiveReader(str(engine))
    code_objects = 0
    for name, entry in archive.toc.items():
        if entry[-1] == 'z':
            embedded = archive.open_embedded_archive(name)
            for module in embedded.toc:
                code_objects += _code_check(embedded.extract(module), tokens)
        elif entry[-1] in {'s', 'm', 'M'}:
            code_objects += _code_check(marshal.loads(archive.extract(name)), tokens)
    provenance = json.loads((distribution/'LICENSES/FFmpeg-provenance.json').read_text(encoding='utf-8'))
    binary = distribution/'resources/app/vendor/ffmpeg/ffmpeg.exe'
    assert digest(binary) == provenance['binarySha256']
    assert provenance['completeCorrespondingSourceConfirmed'] is True
    return {'passed': True, 'files': len(files), 'textFilesChecked': text_count,
            'nativeBinariesChecked': binary_count, 'frozenCodeObjectsChecked': code_objects,
            'launcherMetadataVerified': True, 'manifestHashesVerified': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--distribution', type=Path, default=ROOT/'release/EyeCopy')
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--denylist', type=Path)
    args = parser.parse_args()
    additional = json.loads(args.denylist.read_text(encoding='utf-8')) if args.denylist else ()
    report = {'source': audit_source(additional)}
    if not args.source_only:
        report['distribution'] = audit_distribution(args.distribution, additional)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
