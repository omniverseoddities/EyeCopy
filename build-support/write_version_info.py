"""Generate Windows engine metadata from package.json's release version."""
from pathlib import Path
import json
import re


def release_version(root):
    value = json.loads((Path(root)/'package.json').read_text(encoding='utf-8'))['version']
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('Use a stable three-part release version, such as 1.0.1.')
    if any(int(part) > 65535 for part in value.split('.')):
        raise ValueError('Windows version components must be at most 65535.')
    return value


def write_version_info(root):
    root = Path(root)
    version = release_version(root)
    numbers = ', '.join(version.split('.') + ['0'])
    text = f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers=({numbers}), prodvers=({numbers}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'EyeCopy contributors'),
    StringStruct('FileDescription', 'EyeCopy transfer engine'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', 'EyeCopyEngine'),
    StringStruct('OriginalFilename', 'EyeCopyEngine.exe'),
    StringStruct('ProductName', 'EyeCopy'),
    StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
'''
    target = root/'build-support/version-info.txt'
    if not target.exists() or target.read_text(encoding='utf-8') != text:
        target.write_text(text, encoding='utf-8')
    return version
