"""Build the bundled Windows media tool from pinned corresponding source."""
from pathlib import Path
import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
WORK = ROOT / 'build-work' / 'media'
VENDOR = ROOT / 'vendor' / 'ffmpeg'
PROVENANCE = ROOT / 'LICENSES' / 'FFmpeg-provenance.json'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def fetch(record, target):
    if target.is_file() and digest(target) == record['sha256']:
        return
    temporary = target.with_name(target.name + '.download')
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(record['url'], timeout=60) as response, temporary.open('wb') as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
    except urllib.error.URLError:
        # Windows curl uses the OS certificate store; TLS validation stays on.
        subprocess.run(['curl.exe', '--fail', '--location', '--retry', '2',
                        '--connect-timeout', '30', '--max-time', '300',
                        '--output', str(temporary), record['url']], check=True)
    if digest(temporary) != record['sha256']:
        temporary.unlink(missing_ok=True)
        raise RuntimeError('Downloaded input checksum differs: ' + target.name)
    temporary.replace(target)


def extract_source(archive, destination):
    destination.mkdir()
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            parts = Path(member.name).parts[1:]
            if not parts:
                continue
            target = destination.joinpath(*parts).resolve()
            if '..' in parts or destination.resolve() not in target.parents:
                raise RuntimeError('Source archive contains an unsafe path.')
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream, target.open('wb') as output:
                    shutil.copyfileobj(stream, output)


def short_path(path):
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        raise ctypes.WinError()
    if ' ' in buffer.value:
        raise RuntimeError('Build from a path without spaces on volumes without Windows short-path aliases.')
    return buffer.value


def verified_vendor():
    binary = VENDOR / 'ffmpeg.exe'
    if not binary.is_file() or not PROVENANCE.is_file():
        return False
    record = json.loads(PROVENANCE.read_text(encoding='utf-8'))
    return record.get('completeCorrespondingSourceConfirmed') is True and record.get('binarySha256') == digest(binary)


def record_build(inputs):
    binary = VENDOR / 'ffmpeg.exe'
    version = subprocess.run([str(binary), '-version'], capture_output=True, text=True, check=True, timeout=15)
    license_output = subprocess.run([str(binary), '-L'], capture_output=True, text=True, check=True, timeout=15)
    output = version.stdout + version.stderr
    if str(Path.home()).replace('\\', '/').lower() in output.replace('\\', '/').lower():
        raise RuntimeError('The media executable exposes a local build path.')
    (VENDOR / 'LICENSE.txt').write_text(license_output.stdout + license_output.stderr, encoding='utf-8')
    (ROOT / 'LICENSES' / 'FFmpeg-binary-license-and-configuration.txt').write_text(output + '\n' + license_output.stdout + license_output.stderr, encoding='utf-8')
    (VENDOR / 'SOURCE.txt').write_text('FFmpeg 7.1 with x264 and NVIDIA codec API headers.\nLicense: GPL version 3 or later.\nMatching sources, hashes and build recipes: LICENSES and Sources/EyeCopy/build-support/media.\n', encoding='utf-8')
    sources = [{**entry, 'archive': entry['archive']} for entry in inputs['sources']]
    recipe = [{'file': str(p.relative_to(ROOT)).replace('\\', '/'), 'sha256': digest(p)}
              for p in HERE.iterdir() if p.is_file() and p.name != 'README.md']
    record = {'component': 'FFmpeg', 'version': '7.1', 'binaryRelativePath': 'vendor/ffmpeg/ffmpeg.exe',
              'binarySha256': digest(binary), 'binaryBytes': binary.stat().st_size,
              'license': 'GPL-3.0-or-later', 'provider': 'Built from the supplied source and recipes',
              'versionAndBuildConfiguration': output, 'sourceArchives': sources,
              'buildRecipe': recipe, 'toolchain': inputs['toolchain'], 'assembler': inputs['assembler'],
              'nativeExternalLibraries': ['x264'], 'apiHeaders': ['nv-codec-headers'],
              'completeCorrespondingSourceConfirmed': True,
              'generatedChanges': 'Windows ar response files; generic paths in generated configuration metadata.',
              'byteForByteReproducibilityClaimed': False}
    PROVENANCE.write_text(json.dumps(record, indent=2), encoding='utf-8')
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='Rebuild even when the vendored executable is verified.')
    parser.add_argument('--resume', action='store_true', help='Resume an owned configured workspace without rerunning configure.')
    parser.add_argument('--record-only', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--jobs', type=int, default=min(12, os.cpu_count() or 4))
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This media builder requires Windows x64.')
    inputs = json.loads((HERE / 'inputs.json').read_text(encoding='utf-8'))
    if verified_vendor() and not args.force and not args.record_only:
        print(json.dumps({'media': 'verified', 'sha256': digest(VENDOR / 'ffmpeg.exe')}))
        return
    if args.record_only:
        print(json.dumps(record_build(inputs)))
        return
    marker = WORK / '.eyecopy-media-build.json'
    if WORK.exists() and not marker.is_file():
        raise RuntimeError('Refusing to reuse a media build directory without its ownership marker.')
    WORK.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'application': 'EyeCopy', 'inputsSha256': digest(HERE / 'inputs.json')}), encoding='utf-8')
    for source in inputs['sources']:
        archive = ROOT / 'LICENSES' / source['archive']
        fetch(source, archive)
        destination = WORK / source['folder']
        if args.force and not args.resume and destination.exists():
            if destination.is_symlink() or destination.resolve().parent != WORK.resolve():
                raise RuntimeError('The media source directory is outside the owned workspace.')
            shutil.rmtree(destination)
        if not destination.exists():
            extract_source(archive, destination)
    toolchain, assembler = inputs['toolchain'], inputs['assembler']
    tool_file, nasm_file = WORK / toolchain['file'], WORK / assembler['file']
    fetch(toolchain, tool_file)
    fetch(assembler, nasm_file)
    if not (WORK / 'w64devkit' / 'bin' / 'gcc.exe').is_file():
        sevenzip = shutil.which('7z') or str(Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / '7-Zip' / '7z.exe')
        if not Path(sevenzip).is_file():
            raise RuntimeError('Install 7-Zip to extract the pinned portable build toolchain.')
        subprocess.run([sevenzip, 'x', '-y', '-o' + str(WORK), str(tool_file)], check=True)
    nasm = WORK / ('nasm-' + assembler['version'])
    if not (nasm / 'nasm.exe').is_file():
        with zipfile.ZipFile(nasm_file) as archive:
            for name in archive.namelist():
                target = (WORK / name).resolve()
                if WORK.resolve() not in target.parents:
                    raise RuntimeError('Assembler archive contains an unsafe path.')
            archive.extractall(WORK)
    bash = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git' / 'bin' / 'bash.exe'
    if not bash.is_file():
        raise RuntimeError('Install Git for Windows, including its Bash shell, to build media support.')
    prefix = WORK / 'prefix'
    prefix.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment.update(MEDIA_WORK=short_path(WORK), MEDIA_PREFIX=short_path(prefix),
                       MEDIA_PYTHON=sys.executable, MEDIA_JOBS=str(max(1, min(args.jobs, 32))),
                       MEDIA_RESUME='1' if args.resume else '0')
    environment['PATH'] = ';'.join((str(WORK / 'w64devkit' / 'bin'), str(nasm),
                                    str(bash.parent.parent / 'usr' / 'bin'), environment['PATH']))
    subprocess.run([str(bash), '--noprofile', '--norc', str(HERE / 'build.sh')], env=environment, check=True)
    VENDOR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(prefix / 'bin' / 'ffmpeg.exe', VENDOR / 'ffmpeg.exe')
    print(json.dumps(record_build(inputs)))


if __name__ == '__main__':
    main()
