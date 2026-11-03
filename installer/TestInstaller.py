"""Exercise setup/uninstall only in marked synthetic directories and registry keys."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
import winreg

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'build-support'))
import BuildInstaller


def identity(path):
    folded=str(path).translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))
    return hashlib.sha256(folded.encode('utf-8')).hexdigest()


def main():
    fixture=ROOT/'test-output'/'installer-roundtrip'/uuid.uuid4().hex
    fixture.mkdir(parents=True)
    marker={'applicationId':'com.eyecopy.desktop','schema':1}
    (fixture/'.eyecopy-uninstall-test.json').write_text(json.dumps(marker),encoding='utf-8')
    payload=fixture/'payload';payload.mkdir()
    (payload/'.eyecopy-install.json').write_text(json.dumps({**marker,'kind':'application'}),encoding='utf-8')
    (payload/'application.bin').write_bytes(b'Synthetic application payload')
    binary=BuildInstaller.uninstaller()
    (payload/'UninstallEyeCopy.exe').write_bytes(binary.read_bytes())
    inventory={str(p.relative_to(payload)):BuildInstaller.digest(p) for p in payload.iterdir() if p.is_file()}
    (payload/'.eyecopy-build.json').write_text(json.dumps({'application':'EyeCopy','version':'1.0.0','sourceFiles':inventory}),encoding='utf-8')
    setup=fixture/'SetupFixture.exe';BuildInstaller.compile_script('Setup.nsi',setup,payload)
    install=fixture/'installed';profile=fixture/'profile';profile.mkdir()
    (profile/'.eyecopy-profile.json').write_text(json.dumps({**marker,'kind':'profile'}),encoding='utf-8')
    cache=profile/'cache';cache.mkdir();(cache/'synthetic.jpg').write_bytes(b'Synthetic thumbnail')
    originals=fixture/'originals';originals.mkdir();photo=originals/'photo.jpg';photo.write_bytes(b'Synthetic original photo')
    (profile/'state.json').write_text(json.dumps({'downloadRecords':{str(photo):{'path':str(photo)}}}),encoding='utf-8')
    root_key='Software\\EyeCopy\\InstallerTests\\'+identity(fixture)
    profile_key=root_key+'\\Profiles\\'+identity(profile)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,profile_key) as key:winreg.SetValueEx(key,'Path',0,winreg.REG_SZ,str(profile))
    checks=[]
    try:
        command='"'+str(setup)+'" /S /EYC_TESTROOT="'+str(fixture)+'" /D='+str(install)
        result=subprocess.run(command,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
        assert result.returncode==0,result.returncode
        assert (install/'application.bin').read_bytes()==(payload/'application.bin').read_bytes()
        assert (install/'UninstallEyeCopy.exe').is_file()
        checks.append('Silent per-user setup installs the complete synthetic payload in an isolated directory.')
        unknown=install/'keep-original.mov';unknown.write_bytes(b'Synthetic user download in application folder')
        command='"'+str(install/'UninstallEyeCopy.exe')+'" /S /EYC_TESTROOT="'+str(fixture)+'"'
        result=subprocess.run(command,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
        assert result.returncode==0,result.returncode
        deadline=time.monotonic()+15
        while (install/'UninstallEyeCopy.exe').exists() and time.monotonic()<deadline:time.sleep(.05)
        assert not (install/'application.bin').exists()
        assert not (install/'UninstallEyeCopy.exe').exists()
        assert not profile.exists()
        assert unknown.read_bytes()==b'Synthetic user download in application folder'
        assert photo.read_bytes()==b'Synthetic original photo'
        checks.append('The actual portable uninstaller removes owned application files, its own executable and registered profile data.')
        checks.append('Original downloads outside and inside the application directory are kept byte-for-byte.')
        foreign=fixture/'unowned';foreign.mkdir();(foreign/'photo.jpg').write_bytes(b'Unowned synthetic original')
        command='"'+str(setup)+'" /S /EYC_TESTROOT="'+str(fixture)+'" /D='+str(foreign)
        result=subprocess.run(command,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
        assert result.returncode!=0
        assert list(p.name for p in foreign.iterdir())==['photo.jpg']
        checks.append('Setup refuses to overwrite an existing unowned directory.')
        report={'passed':True,'checks':checks,'syntheticFilesOnly':True,'realProfilesModified':False,'globalInstallationRegistered':False}
        (ROOT/'test-output/installer-roundtrip.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))
    finally:
        def delete_key(path):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,path,0,winreg.KEY_READ|winreg.KEY_WRITE) as key:
                    names=[]
                    while True:
                        try:names.append(winreg.EnumKey(key,len(names)))
                        except OSError:break
                for name in names:delete_key(path+'\\'+name)
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER,path)
            except FileNotFoundError:pass
        delete_key(root_key)


if __name__=='__main__':main()
