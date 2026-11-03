"""Build EyeCopy's per-user setup and portable uninstaller without global tools."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import urllib.error
import zipfile
import re

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT/'build-work/installer'
OUTPUT = ROOT/'build-output/installer'
NSIS_VERSION = '3.13'
NSIS_URL = 'https://prdownloads.sourceforge.net/nsis/nsis-3.13.zip?download'
NSIS_SHA256 = 'ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8'
NSIS_SOURCE_URL = 'https://prdownloads.sourceforge.net/nsis/nsis-3.13-src.tar.bz2?download'
NSIS_SOURCE_SHA256 = 'a8ffe024602d46b6d766f9e1ce30c324ad2a24daeacd3efc2642d436a0c157ac'


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def app_version(value=None):
    value=value or json.loads((ROOT/'package.json').read_text(encoding='utf-8'))['version']
    match=re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?',value)
    if not match or any(int(part)>65535 for part in match.groups()):
        raise ValueError('A supported three-part application version is required.')
    return value,'.'.join((*match.groups(),'0'))


def matching_source():
    archive=ROOT/'LICENSES/sources/nsis-3.13-src.tar.bz2';archive.parent.mkdir(parents=True,exist_ok=True)
    if not archive.is_file() or digest(archive)!=NSIS_SOURCE_SHA256:
        temporary=archive.with_suffix('.download')
        subprocess.run(['curl.exe','--fail','--location','--max-time','180','--output',str(temporary),NSIS_SOURCE_URL],check=True)
        if digest(temporary)!=NSIS_SOURCE_SHA256:raise RuntimeError('NSIS source does not match the publisher checksum.')
        temporary.replace(archive)
    record={'file':'sources/'+archive.name,'component':'NSIS','version':NSIS_VERSION,'url':NSIS_SOURCE_URL,
            'sha256':NSIS_SOURCE_SHA256,'bytes':archive.stat().st_size,'verifiedAgainstPublisherSha256':True,
            'metadataUrl':'https://sourceforge.net/projects/nsis/best_release.json'}
    path=ROOT/'LICENSES/source-archive-inventory.json';inventory=json.loads(path.read_text(encoding='utf-8'))
    inventory['archives']=[entry for entry in inventory['archives'] if entry.get('file')!=record['file']]+[record]
    path.write_text(json.dumps(inventory,indent=2),encoding='utf-8')
    return record


def compiler():
    WORK.mkdir(parents=True,exist_ok=True)
    archive=WORK/f'nsis-{NSIS_VERSION}.zip'
    if not archive.is_file() or digest(archive)!=NSIS_SHA256:
        temporary=archive.with_suffix('.download')
        try:
            with urllib.request.urlopen(NSIS_URL,timeout=60) as response,temporary.open('wb') as out: shutil.copyfileobj(response,out)
        except urllib.error.URLError:
            subprocess.run(['curl.exe','--fail','--location','--max-time','180','--output',str(temporary),NSIS_URL],check=True)
        if digest(temporary)!=NSIS_SHA256: raise RuntimeError('NSIS does not match the publisher checksum.')
        temporary.replace(archive)
    dest=WORK/f'nsis-{NSIS_VERSION}'
    if not (dest/'makensis.exe').is_file():
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                path=Path(member.filename)
                if path.is_absolute() or '..' in path.parts: raise RuntimeError('Unsafe NSIS archive path.')
            source.extractall(WORK)
    notice=ROOT/'LICENSES/NSIS-COPYING.txt'
    shutil.copy2(dest/'COPYING',notice)
    source=matching_source()
    provenance={'component':'NSIS','version':NSIS_VERSION,'downloadUrl':NSIS_URL,
                'publisherMetadataUrl':'https://sourceforge.net/projects/nsis/files/NSIS%203/3.13/',
                'archiveSha256':NSIS_SHA256,'publisherChecksumVerified':True,
                'scope':'Build tool and bundled installer engine; no global installation.','sourceArchive':source}
    (ROOT/'LICENSES/NSIS-provenance.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    return dest/'makensis.exe'


def helper():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    csc=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    target=OUTPUT/'ProfileCleanup.exe'
    receipt=OUTPUT/'cleanup-build.json'
    source_hash=digest(ROOT/'installer/ProfileCleanup.cs')
    if target.is_file() and receipt.is_file():
        cached=json.loads(receipt.read_text(encoding='utf-8'))
        if cached.get('sourceSha256')==source_hash and cached.get('binarySha256')==digest(target):return target
    subprocess.run([str(csc),'/nologo','/target:exe','/platform:x64','/optimize+',
                    '/reference:System.Web.Extensions.dll','/reference:System.Core.dll',
                    '/out:'+str(target),str(ROOT/'installer/ProfileCleanup.cs')],check=True)
    receipt.write_text(json.dumps({'sourceSha256':source_hash,'binarySha256':digest(target)}),encoding='utf-8')
    return target


def compile_script(name,output,app=None,version=None):
    tool=compiler(); cleanup=helper()
    version,windows_version=app_version(version)
    args=[str(tool),'/V2','/DOUTPUT_EXE='+str(output),'/DCLEANUP_HELPER='+str(cleanup),
          '/DAPP_ICON='+str(ROOT/'assets/eyecopy.ico'),'/DAPP_LICENSE='+str(ROOT/'LICENSE'),
          '/DAPP_VERSION='+version,'/DWIN_VERSION='+windows_version]
    if app: args.append('/DAPP_DIRECTORY='+str(app))
    subprocess.run([*args,str(ROOT/'installer'/name)],cwd=ROOT/'installer',check=True)


def uninstaller(version=None,destination=None):
    destination=Path(destination) if destination else OUTPUT
    destination.mkdir(parents=True,exist_ok=True)
    generator=destination/'UninstallerGenerator.exe';compile_script('GenerateUninstaller.nsi',generator,version=version)
    # The generator writes only a standalone uninstaller in the build folder.
    subprocess.run('"'+str(generator)+'" /S /D='+str(destination),check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    result=destination/'UninstallEyeCopy.exe'
    if not result.is_file(): raise RuntimeError('Portable uninstaller generation failed.')
    print(json.dumps({'uninstaller':str(result),'sha256':digest(result)}),flush=True)
    return result


def setup():
    app=ROOT/'release/EyeCopy'
    if not (app/'UninstallEyeCopy.exe').is_file() or not (app/'.eyecopy-install.json').is_file():
        raise RuntimeError('Build the complete portable package before setup.')
    version,_=app_version()
    manifest=json.loads((app/'.eyecopy-build.json').read_text(encoding='utf-8'))
    if manifest.get('version')!=version:raise RuntimeError('Rebuild the portable package for the current application version before setup.')
    (ROOT/'artifacts').mkdir(exist_ok=True)
    result=ROOT/'artifacts'/('Setup-EyeCopy-'+version+'.exe');compile_script('Setup.nsi',result,app)
    print(json.dumps({'setup':str(result),'bytes':result.stat().st_size,'sha256':digest(result)}),flush=True)
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--uninstaller-only',action='store_true');parser.add_argument('--setup',action='store_true');args=parser.parse_args()
    if args.uninstaller_only:uninstaller()
    else:setup()


if __name__=='__main__':main()
