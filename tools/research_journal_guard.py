"""Heuristic publication guard. Not a semantic secret scanner or encryption proof."""
from pathlib import Path, PurePosixPath
import json
import re
import subprocess
import sys
from research_vault import inspect_envelope


def allowed_new_path(path):
    path = path.replace('\\', '/')
    p = PurePosixPath(path)
    if '..' in p.parts or p.is_absolute():
        return False
    lower = path.lower()
    if any(part in {'research_private', 'private_logs', '.identity', 'decrypted', '.env'} for part in p.parts):
        return False
    if p.name.lower() in {'identity.keybundle', 'recovery.key', 'private.pem', 'id_rsa', 'id_ed25519'}:
        return False
    if lower.endswith(('.pem', '.key', '.keybundle')):
        return False
    # Historical reports remain in history; new per-run plaintext is blocked.
    if re.match(r'^docs/r[0-9]+(?:_|\.)', lower):
        return False
    if lower.startswith('research_vault/entries/') and not lower.endswith('.vault'):
        return False
    return True


def audit_paths(paths, root='.'):
    problems = []
    for name in paths:
        if not allowed_new_path(name):
            problems.append('PRIVATE_OR_PLAINTEXT_PATH')
            continue
        path = Path(root) / name
        if path.exists() and str(name).endswith('.vault'):
            try:
                if path.stat().st_size > 24 * 1024 * 1024:
                    raise ValueError('too large')
                inspect_envelope(path.read_bytes())
            except Exception:
                problems.append('INVALID_ENVELOPE')
    return {'passed': not problems, 'checked_files': len(paths), 'reason_codes': sorted(set(problems)),
            'semantic_secret_scan': False, 'historical_plaintext_removed': False}


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Pass base and head commit SHAs')
    for sha in sys.argv[1:]:
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise SystemExit('Full commit SHAs required')
    raw = subprocess.check_output(['git', 'diff', '--name-only', '--diff-filter=ACMR', '-z', *sys.argv[1:]])
    paths = [x.decode('utf-8') for x in raw.split(b'\0') if x]
    report = audit_paths(paths)
    print(json.dumps(report, sort_keys=True))
    if not report['passed']:
        raise SystemExit(1)

if __name__ == '__main__':
    main()
