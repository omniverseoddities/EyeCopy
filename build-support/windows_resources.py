"""Apply EyeCopy's icon and product metadata to its Windows launcher."""
from pathlib import Path
import struct


def _block(name, value=b"", children=(), text=False, value_length=None):
    data = bytearray(b"\0" * 6 + name.encode("utf-16le") + b"\0\0")
    data.extend(b"\0" * (-len(data) % 4))
    data.extend(value)
    for child in children:
        data.extend(b"\0" * (-len(data) % 4))
        data.extend(child)
    struct.pack_into("<HHH", data, 0, len(data),
                     value_length if value_length is not None else len(value), int(text))
    return bytes(data)


def brand_launcher(executable, icon, version):
    import win32api

    parts = tuple(int(part) for part in version.split("."))
    if len(parts) != 3 or any(not 0 <= value <= 65535 for value in parts):
        raise ValueError("A three-part Windows product version is required.")
    major, minor, patch = parts
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000, major << 16 | minor, patch << 16,
                        major << 16 | minor, patch << 16, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    values = {"CompanyName": "EyeCopy contributors", "FileDescription": "EyeCopy",
              "FileVersion": version, "InternalName": "EyeCopy", "OriginalFilename": "EyeCopy.exe",
              "ProductName": "EyeCopy", "ProductVersion": version}
    strings = [_block(name, (value + "\0").encode("utf-16le"), text=True,
                      value_length=len(value) + 1) for name, value in values.items()]
    version_data = _block("VS_VERSION_INFO", fixed, [
        _block("StringFileInfo", children=[_block("040904B0", children=strings, text=True)], text=True),
        _block("VarFileInfo", children=[_block("Translation", struct.pack("<HH", 0x409, 1200))], text=True)])

    ico = Path(icon).read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", ico)
    if reserved or kind != 1 or not 1 <= count <= 32:
        raise ValueError("Invalid application icon.")
    group = bytearray(struct.pack("<HHH", 0, 1, count))
    images = []
    for index in range(count):
        entry = ico[6 + index * 16:22 + index * 16]
        width, height, colors, zero, planes, bits, length, offset = struct.unpack("<BBBBHHII", entry)
        image = ico[offset:offset + length]
        if len(image) != length:
            raise ValueError("Incomplete application icon.")
        images.append(image)
        group.extend(struct.pack("<BBBBHHIH", width, height, colors, zero, planes, bits, length, index + 1))
    library = win32api.LoadLibraryEx(str(executable), 0, 2)
    try:
        groups = [(name, language) for name in win32api.EnumResourceNames(library, 14)
                  for language in win32api.EnumResourceLanguages(library, 14, name)]
        versions = [(name, language) for name in win32api.EnumResourceNames(library, 16)
                    for language in win32api.EnumResourceLanguages(library, 16, name)]
    finally:
        win32api.FreeLibrary(library)
    update = win32api.BeginUpdateResource(str(executable), False)
    try:
        for name, language in groups or [(1, 1033)]:
            win32api.UpdateResource(update, 14, name, bytes(group), language)
        for index, image in enumerate(images):
            win32api.UpdateResource(update, 3, index + 1, image, 1033)
        for name, language in versions or [(1, 1033)]:
            win32api.UpdateResource(update, 16, name, version_data, language)
    except Exception:
        win32api.EndUpdateResource(update, True)
        raise
    else:
        win32api.EndUpdateResource(update, False)
    actual = win32api.GetFileVersionInfo(str(executable), "\\StringFileInfo\\040904B0\\ProductName")
    if actual != "EyeCopy":
        raise RuntimeError("Windows launcher branding verification failed.")
