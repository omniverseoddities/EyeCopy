"""Apply the documented Windows response-file and local-path privacy changes."""
from pathlib import Path
import os

work = Path(os.environ['MEDIA_WORK'])
prefix = Path(os.environ['MEDIA_PREFIX'])
header = work / 'ffmpeg' / 'config.h'
content = header.read_text(encoding='utf-8')
for actual, generic in ((prefix, '/usr/local'), (work, '/usr/src/eyecopy-media')):
    content = content.replace(str(actual).replace('\\', '/'), generic)
header.write_text(content, encoding='utf-8', newline='\n')

# Windows process command lines cannot contain every avformat object filename.
# GNU ar accepts exactly the same input list through its response-file syntax.
makefile = work / 'ffmpeg' / 'ffbuild' / 'library.mak'
content = makefile.read_text(encoding='utf-8')
old = '$(AR) $(ARFLAGS) $(AR_O) $^'
new = '$(file >$@.rsp,$^)$(AR) $(ARFLAGS) $(AR_O) @$@.rsp'
if old in content:
    content = content.replace(old, new)
    makefile.write_text(content, encoding='utf-8', newline='\n')
elif new not in content:
    raise RuntimeError('The FFmpeg archiver recipe does not match the pinned source.')
