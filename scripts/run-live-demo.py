"""Run live acceptance using existing OpenCode configuration and optional authentication."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

root = Path(__file__).resolve().parents[1]
env = os.environ.copy()
# Preserve existing authentication only when the user configured it.
password = env.get('OPENCODE_SERVER_PASSWORD', '')
if not password:
    env.pop('OPENCODE_SERVER_PASSWORD', None)
env.setdefault('OPENCODE_SERVER_USERNAME', 'opencode')
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
base = f'http://127.0.0.1:{port}'
env['OPENCODE_BASE_URL'] = base
headers = {}
if password:
    token = base64.b64encode((env['OPENCODE_SERVER_USERNAME'] + ':' + password).encode()).decode()
    headers['Authorization'] = 'Basic ' + token

def get(path, timeout=30):
    request = Request(base + path + '?' + urlencode({'directory': str(root)}),
                      headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)

executable = shutil.which('opencode')
if not executable:
    raise SystemExit('OpenCode is not on PATH.')
command = [executable, 'serve', '--hostname', '127.0.0.1', '--port', str(port)]
if executable.lower().endswith(('.cmd', '.bat')):
    command = subprocess.list2cmdline(command)
log_path = root / 'opencode-demo.log'
with log_path.open('w', encoding='utf-8') as log:
    server = subprocess.Popen(command, shell=isinstance(command, str), cwd=root,
                              env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 90
        while True:
            if server.poll() is not None:
                raise RuntimeError(f'OpenCode exited. Inspect {log_path}')
            try:
                get('/global/health')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'OpenCode did not start. Inspect {log_path}')
                time.sleep(1)

        print('OpenCode is listening. Waiting for project configuration (up to 3 minutes)...', flush=True)
        config_deadline = time.monotonic() + 180
        while True:
            if server.poll() is not None:
                raise RuntimeError(f'OpenCode exited during project initialization. Inspect {log_path}')
            remaining = config_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f'Project configuration is still unavailable after 3 minutes. Inspect {log_path}')
            try:
                config = get('/config', timeout=min(20, remaining))
                break
            except HTTPError as exc:
                raise RuntimeError(f'OpenCode configuration returned HTTP {exc.code}. Inspect {log_path}') from None
            except (TimeoutError, URLError, ConnectionError):
                print('Still waiting for OpenCode project initialization...', flush=True)
                time.sleep(min(2, max(0, config_deadline - time.monotonic())))
        print('Project configuration loaded.', flush=True)
        model = config.get('model')
        if not model:
            paths = get('/path')
            state_dir = paths.get('state') or str(
                Path(env.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))) / 'opencode')
            history_file = Path(state_dir) / 'model.json'
            history = json.loads(history_file.read_text(encoding='utf-8')) if history_file.exists() else {}
            providers = get('/provider')
            connected = set(providers.get('connected', []))
            available = {p['id']: p for p in providers.get('all', [])}
            for recent in history.get('recent', []):
                provider_id, model_id = recent.get('providerID'), recent.get('modelID')
                if provider_id in connected and model_id in available.get(provider_id, {}).get('models', {}):
                    model = provider_id + '/' + model_id
                    break
        if not model:
            raise RuntimeError('No configured or previously selected OpenCode model found. Open OpenCode, select your model with /models, exit, then rerun this script.')
        env['OPENCODE_MODEL'] = model
        env['CLEANROOM_LIVE_OPENCODE'] = '1'
        print(f'Using your existing OpenCode model: {model}', flush=True)
        print('Running requirements -> planning -> simulated collection/results -> QA review.', flush=True)
        print('Manufacturing approval is synthetic test input; final QA approval is not issued.', flush=True)
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                                 '-p', 'test_live_agents.py', '-v'], env=env, cwd=root)
        if result.returncode:
            raise RuntimeError(f'Live E2E failed. See the error above and {log_path}. No offline fallback was used.')
        print('\nPASS: all three live agents completed; six samples matched; no findings; review_ready.', flush=True)
        print('The acceptance test uses a temporary database. Checkout and server log remain at:', root)
    finally:
        if server.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(server.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                server.terminate()
            server.wait(timeout=15)
