"""Scan Git's public file set or a source ZIP; print locations, never values."""
import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'token': r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|\bsk-[A-Za-z0-9_-]{20,})',
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'production-address': r'(?:154[.]12[.]40[.]159|192[.]3[.]247[.]113|kisara[.](?:art|info))',
    'private-path': r'(?:[A-Z]:[\\/]VSC|[A-Z]:[\\/]Users[\\/]|/root/)',
    'removed-integration': r'(?:/api/rogue/|/api/public/rogue/|ROGUE_LANCER_|/patrol)',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--zip', type=Path)
    args = parser.parse_args()
    if args.zip:
        with zipfile.ZipFile(args.zip) as archive:
            files = [(name, archive.read(name)) for name in archive.namelist() if not name.endswith('/')]
    else:
        names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT).decode().split('\0')
        files = [(name, (ROOT / name).read_bytes()) for name in dict.fromkeys(names) if name and (ROOT / name).is_file()]
    failures = []
    for name, content in files:
        path = Path(name)
        if (path.suffix in {'.sqlite3', '.sqlite', '.db', '.env', '.pem', '.key'}
                or path.name in {'credentials.txt', 'known_hosts'}
                or any(part in {'node_modules', '.venv', 'validation', 'data', '__pycache__'} for part in path.parts)):
            failures.append((name, 0, 'private-file'))
        # These files intentionally describe/detect removed or unsafe inputs.
        if name in {'scripts/check_public_source.py', 'tests/test_standalone.py'}:
            continue
        for number, line in enumerate(content.decode('utf-8', errors='replace').splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if re.search(pattern, line):
                    failures.append((name, number, kind))
    for name, number, kind in failures:
        print(f'{name}:{number}: {kind}')
    print(f'Checked {len(files)} files; findings={len(failures)}. Pattern scan is not a complete secret audit.')
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
