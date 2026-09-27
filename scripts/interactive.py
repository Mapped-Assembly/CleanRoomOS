"""Bootstrap the local controller for the OpenCode tools; no server or model config."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = {'status', 'events', 'notifications', 'context', 'propose', 'collect', 'results', 'review',
           'allow', 'disallow', 'qa-approve', 'qa-reject', 'qa-resolve'}


def arguments(request):
    if not isinstance(request, dict) or set(request) - {'action', 'resolved', 'normal', 'reason', 'revision', 'package_revision'}:
        raise ValueError('Invalid controller request')
    action = request.get('action')
    if action not in ACTIONS:
        raise ValueError('Unsupported action')
    args = [action, '--db', str(ROOT / 'cleanroom-interactive.sqlite'), '--actor', 'interactive-user']
    if action == 'propose':
        args.append('--fixture-plan')  # Matches the bundled synthetic LIMS sample IDs.
    if action.startswith('qa-'):
        args += ['--role', 'qa']
    for key, flag in [('revision', '--revision'), ('package_revision', '--package-revision')]:
        if key in request:
            value = request[key]
            if type(value) is not int or value < 1:
                raise ValueError(f'{key} must be a positive integer')
            args += [flag, str(value)]
    for key in ('resolved', 'normal'):
        if key in request and type(request[key]) is not bool:
            raise ValueError(f'{key} must be boolean')
        if request.get(key):
            args.append('--' + key)
    if 'reason' in request:
        if not isinstance(request['reason'], str) or not request['reason'].strip():
            raise ValueError('Reason must be nonempty text')
        args += ['--reason', request['reason']]
    return args


def main():
    args = arguments(json.loads(sys.argv[1]))
    if sys.version_info < (3, 11):
        raise RuntimeError('Install Python 3.11+ and restart OpenCode.')
    python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
        if not python.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True, stdout=sys.stderr)
        check = subprocess.run([str(python), '-c', 'import cleanroom_os, pydantic; assert int(pydantic.__version__.split(".")[0]) == 2'],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode:
            subprocess.run([str(python), '-m', 'pip', 'install', '-e', str(ROOT)], check=True, stdout=sys.stderr)
        return subprocess.call([str(python), str(Path(__file__).resolve()), sys.argv[1]], cwd=ROOT)
    os.chdir(ROOT)
    from cleanroom_os.workflow import main as workflow
    sys.argv = ['cleanroom', *args]
    workflow()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
