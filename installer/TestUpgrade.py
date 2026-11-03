"""Synthetic upgrade collision checks and an actual NSIS version-to-version run."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import unittest
import uuid
import winreg

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'build-support'))
import BuildInstaller
HELPER=BuildInstaller.helper()
APP_ID='com.eyecopy.desktop'


def identity(path):
    folded=str(path).translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))
    return hashlib.sha256(folded.encode('utf-8')).hexdigest()


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/'test-output'/'installer-upgrades'/uuid.uuid4().hex;self.root.mkdir(parents=True)
        self.marker={'applicationId':APP_ID,'schema':1}
        (self.root/'.eyecopy-uninstall-test.json').write_text(json.dumps(self.marker),encoding='utf-8')
        self.app=self.root/'installed';self.app.mkdir()
        (self.app/'.eyecopy-install.json').write_text(json.dumps({**self.marker,'kind':'application'}),encoding='utf-8')
        (self.app/'application.bin').write_bytes(b'Original application component')
        self.old={str(p.relative_to(self.app)):BuildInstaller.digest(p) for p in self.app.iterdir()}
        self.manifest(self.app,self.old,'1.0.0')
        self.next=self.root/'incoming.json'
        self.next.write_text(json.dumps({'application':'EyeCopy','version':'1.1.0','sourceFiles':{**self.old,'application.bin':'a'*64}}),encoding='utf-8')
        self.key='Software\\EyeCopy\\InstallerTests\\'+identity(self.root)

    def tearDown(self):
        def delete(path):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,path,0,winreg.KEY_READ|winreg.KEY_WRITE) as key:
                    names=[]
                    while True:
                        try:names.append(winreg.EnumKey(key,len(names)))
                        except OSError:break
                for name in names:delete(path+'\\'+name)
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER,path)
            except FileNotFoundError:pass
        delete(self.key)

    @staticmethod
    def manifest(root,files,version):
        (root/'.eyecopy-build.json').write_text(json.dumps({'application':'EyeCopy','version':version,'sourceFiles':files}),encoding='utf-8')

    def registered_download(self,path):
        profile=self.root/'profile';profile.mkdir()
        (profile/'.eyecopy-profile.json').write_text(json.dumps({**self.marker,'kind':'profile'}),encoding='utf-8')
        (profile/'state.json').write_text(json.dumps({'downloadRecords':{str(path):{'path':str(path)}}}),encoding='utf-8')
        name=self.key+'\\Profiles\\'+identity(profile)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,name) as key:winreg.SetValueEx(key,'Path',0,winreg.REG_SZ,str(profile))
        return profile

    def validate(self):
        before={str(p.relative_to(self.app)):p.read_bytes() for p in self.app.rglob('*') if p.is_file()}
        run=subprocess.run([str(HELPER),'--app-root',str(self.app),'--incoming-manifest',str(self.next),'--validate-install','--test-root',str(self.root)],capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
        after={str(p.relative_to(self.app)):p.read_bytes() for p in self.app.rglob('*') if p.is_file()}
        self.assertEqual(before,after,'Validation must never write application files.')
        return run

    def test_matching_old_component_allows_upgrade_and_keeps_nonclashing_unknown(self):
        (self.app/'original.mov').write_bytes(b'User original')
        self.assertEqual(self.validate().returncode,0)

    def test_unknown_new_collision_is_refused(self):
        (self.app/'new.bin').write_bytes(b'User original with incoming name')
        data=json.loads(self.next.read_text());data['sourceFiles']['new.bin']='b'*64;self.next.write_text(json.dumps(data))
        self.assertNotEqual(self.validate().returncode,0)

    def test_modified_old_component_is_refused(self):
        (self.app/'application.bin').write_bytes(b'Unrecognized replacement')
        self.assertNotEqual(self.validate().returncode,0)

    def test_tracked_download_is_refused_even_when_old_hash_matches(self):
        profile=self.registered_download(self.app/'application.bin');state=(profile/'state.json').read_bytes()
        self.assertNotEqual(self.validate().returncode,0)
        self.assertEqual((profile/'state.json').read_bytes(),state)

    def test_directory_collision_is_refused(self):
        (self.app/'new.bin').mkdir();data=json.loads(self.next.read_text());data['sourceFiles']['new.bin']='b'*64;self.next.write_text(json.dumps(data))
        self.assertNotEqual(self.validate().returncode,0)

    def test_file_ancestor_collision_is_refused(self):
        (self.app/'newdir').write_bytes(b'User file')
        data=json.loads(self.next.read_text());data['sourceFiles']['newdir/child.bin']='b'*64;self.next.write_text(json.dumps(data))
        self.assertNotEqual(self.validate().returncode,0)

    def test_unsafe_incoming_path_is_refused(self):
        data=json.loads(self.next.read_text());data['sourceFiles']['../outside.bin']='b'*64;self.next.write_text(json.dumps(data))
        self.assertNotEqual(self.validate().returncode,0)

    def test_invalid_incoming_hash_is_refused(self):
        data=json.loads(self.next.read_text());data['sourceFiles']['application.bin']='not-a-checksum';self.next.write_text(json.dumps(data))
        self.assertNotEqual(self.validate().returncode,0)

    def test_actual_nsis_upgrade_preserves_profile_and_downloads(self):
        from win32api import GetFileVersionInfo
        original=self.app/'keep-original.mov';original.write_bytes(b'User original inside install directory')
        profile=self.registered_download(original);state=(profile/'state.json').read_bytes()
        payload=self.root/'next-payload';payload.mkdir()
        (payload/'.eyecopy-install.json').write_text(json.dumps({**self.marker,'kind':'application'}),encoding='utf-8')
        (payload/'application.bin').write_bytes(b'Updated application component')
        (payload/'new-component.bin').write_bytes(b'New application component')
        uninstall=BuildInstaller.uninstaller(version='1.1.0',destination=self.root/'new-builder')
        (payload/'UninstallEyeCopy.exe').write_bytes(uninstall.read_bytes())
        self.manifest(payload,{str(p.relative_to(payload)):BuildInstaller.digest(p) for p in payload.iterdir()},'1.1.0')
        setup=self.root/'Setup-1.1.0.exe';BuildInstaller.compile_script('Setup.nsi',setup,payload,version='1.1.0')
        self.assertEqual(GetFileVersionInfo(str(setup),'\\StringFileInfo\\040904b0\\ProductVersion'),'1.1.0')
        command='"'+str(setup)+'" /S /EYC_TESTROOT="'+str(self.root)+'" /D='+str(self.app)
        run=subprocess.run(command,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
        self.assertEqual(run.returncode,0)
        self.assertEqual((self.app/'application.bin').read_bytes(),b'Updated application component')
        self.assertEqual((self.app/'new-component.bin').read_bytes(),b'New application component')
        self.assertEqual(original.read_bytes(),b'User original inside install directory')
        self.assertEqual((profile/'state.json').read_bytes(),state)
        self.assertEqual(json.loads((self.app/'.eyecopy-build.json').read_text())['version'],'1.1.0')


if __name__=='__main__':unittest.main(verbosity=2)
