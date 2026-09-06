#!/usr/bin/env python3
"""Build a redistributable source release using an explicit file allowlist."""
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.search(r'^version = "([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.M)[1]
NAME = f'paperflow-web-{VERSION}'
DIRS = ('paperflow', 'tests', 'deploy/server', '.github/workflows')
FILES = ('README.md', 'LICENSE', 'pyproject.toml', 'requirements.txt', '.env.example', '.gitignore',
         'input.example.txt', 'scripts/build_server_release.py')
SUFFIXES = {'.py', '.html', '.css', '.js', '.json', '.sh', '.service', '.example', '.yml'}


def build():
    out = ROOT / 'dist'
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / NAME
        members = [ROOT / path for path in FILES]
        for directory in DIRS:
            members.extend(p for p in (ROOT / directory).rglob('*')
                           if p.is_file() and not p.is_symlink() and p.suffix in SUFFIXES
                           and '__pycache__' not in p.parts and not p.name.startswith('._'))
        for source in members:
            relative = source.relative_to(ROOT)
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        manifest = {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(target.rglob('*')) if p.is_file()}
        (target / 'MANIFEST.json').write_text(json.dumps({'version': VERSION, 'sha256': manifest}, indent=2)+'\n')
        with tarfile.open(out / f'{NAME}.tar.gz', 'w:gz') as tar:
            tar.add(target, arcname=NAME)
        with zipfile.ZipFile(out / f'{NAME}.zip', 'w', zipfile.ZIP_DEFLATED) as bundle:
            for p in sorted(target.rglob('*')):
                bundle.write(p, p.relative_to(target.parent))
        paths = [out / f'{NAME}.tar.gz', out / f'{NAME}.zip']
        (out / 'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in paths))
        print('\n'.join(str(p) for p in paths))


if __name__ == '__main__':
    build()
