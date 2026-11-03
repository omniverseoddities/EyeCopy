"""Set the version and optional public GitHub repository for a release build."""
from pathlib import Path
import argparse
import json
import re
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'build-support'))
from write_version_info import release_version, write_version_info


def repository_url(value):
    value = value.strip()
    if not value:
        return ''
    if '://' not in value:
        value = 'https://github.com/' + value
    parsed = urlparse(value)
    if parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com' or parsed.query or parsed.fragment:
        raise ValueError('Use the public HTTPS GitHub repository URL or OWNER/REPOSITORY.')
    parts = parsed.path.strip('/').split('/')
    if len(parts) != 2:
        raise ValueError('The repository must have one owner and one repository name.')
    owner, repo = parts
    if repo.endswith('.git'):
        repo = repo[:-4]
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', owner) or \
       not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repo) or repo in {'.', '..'}:
        raise ValueError('The GitHub owner or repository name is invalid.')
    return f'https://github.com/{owner}/{repo}'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', help='Stable version, such as 1.0.1.')
    parser.add_argument('--repository', help='Public GitHub URL or OWNER/REPOSITORY; use an empty string to disable.')
    args = parser.parse_args()
    metadata = json.loads((ROOT/'package.json').read_text(encoding='utf-8'))
    old = metadata['version']
    if args.version:
        value = args.version.removeprefix('v')
        if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value) or any(int(p) > 65535 for p in value.split('.')):
            parser.error('Use a stable three-part Windows-compatible version, such as 1.0.1.')
        metadata['version'] = value
    if args.repository is not None:
        try:
            value = repository_url(args.repository)
        except ValueError as error:
            parser.error(str(error))
        if value:
            metadata['repository'] = {'type': 'git', 'url': value}
        else:
            metadata.pop('repository', None)
    lock = json.loads((ROOT/'package-lock.json').read_text(encoding='utf-8'))
    lock['version'] = lock['packages']['']['version'] = metadata['version']
    for path, content in ((ROOT/'package.json', metadata), (ROOT/'package-lock.json', lock)):
        path.write_text(json.dumps(content, indent=2) + '\n', encoding='utf-8')
    write_version_info(ROOT)
    if old != metadata['version']:
        for path in [ROOT/'README.md', ROOT/'README.txt', ROOT/'QUICK START.txt', ROOT/'GUIDE.html',
                     ROOT/'Transport Options.html', *list((ROOT/'docs').glob('*.md'))]:
            text = path.read_text(encoding='utf-8')
            path.write_text(text.replace(old, metadata['version']), encoding='utf-8')
    print(json.dumps({'version': release_version(ROOT), 'repositoryConfigured': bool(metadata.get('repository')),
                      'nextStep': 'BuildPortable.py, then PrepareRelease.py; publish matching GitHub Release assets.'}))


if __name__ == '__main__':
    main()
