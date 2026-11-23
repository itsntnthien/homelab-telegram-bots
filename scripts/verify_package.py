"""Check publishable files for known secret shapes, runtime data and missing imports.

This is a focused packaging check, not a complete security audit.
"""
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.venv', 'node_modules', '__pycache__', 'state', 'backups', 'artifacts'}
PATTERNS = {
    'Telegram token': re.compile(r'\b\d{8,12}:[A-Za-z0-9_-]{35}\b'),
    'GitHub token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b'),
    'private key contents': re.compile(r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----\s+[A-Za-z0-9+/]{20,}'),
}


def main():
    if (ROOT/'.git').exists():
        names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT).decode().split('\0')
        paths = sorted({ROOT/name for name in names if name})
    else:
        paths = sorted(p for p in ROOT.rglob('*') if p.is_file() and not EXCLUDED.intersection(p.relative_to(ROOT).parts))
    errors = []
    for path in paths:
        relative = path.relative_to(ROOT)
        if path.name in {'.env', 'connection.json', 'api-key.txt', 'private-key.txt'} or \
           (path.name.startswith('.env.') and path.name != '.env.example') or \
           path.suffix in {'.db', '.sqlite', '.pem', '.key', '.log'} or EXCLUDED.intersection(relative.parts):
            errors.append(f'{relative}: forbidden publication file')
        data = path.read_bytes()
        if len(data) > 2_000_000:
            errors.append(f'{relative}: unexpected large file')
        if path.suffix in {'.png', '.jpg'}:
            continue
        content = data.decode('utf-8-sig')
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                errors.append(f'{relative}: possible {label}')
        if path.suffix == '.py':
            tree = ast.parse(content)
            for node in ast.walk(tree):
                imports = [a.name.split('.')[0] for a in node.names] if isinstance(node, ast.Import) else \
                          [(node.module or '').split('.')[0]] if isinstance(node, ast.ImportFrom) else []
                for name in imports:
                    if name and name not in sys.stdlib_module_names and name not in {'PIL', 'numpy'} and not (path.parent/(name+'.py')).exists():
                        if not (ROOT/'apps/trading-bot'/(name+'.py')).exists():
                            errors.append(f'{relative}: undeclared import {name}')
    sys.path.insert(0, str(ROOT/'apps/trading-bot'))
    from pine_config import verify_source
    verify_source()
    if errors:
        raise SystemExit('\n'.join(errors))
    original = json.loads((ROOT/'docs/source-provenance.json').read_text())['files']
    changed = [name for name, info in original.items() if hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != info['normalized_source_sha256']]
    print(f'Package checks passed: {len(paths)} files; frozen Pine source verified.')
    print('Files adjusted from server snapshot: '+(', '.join(changed) or 'none'))


if __name__ == '__main__':
    main()
