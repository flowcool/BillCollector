"""Test a local BD image without network or live mounts. Keep the proof container."""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

BOOTSTRAP = r'''
import hashlib, json, os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, '/apps')
from billcollector import bourse_direct, config, downloads, notice, runner
import pypdf
assert os.getuid() == 1003 and os.getgid() == 1000
assert pypdf.__version__ == '6.21.2'
root = Path(tempfile.mkdtemp(prefix='bd-image-proof-'))
shutil.copytree('/checks/tests', root/'tests')
shutil.copytree('/apps/recipes_playwright', root/'apps/recipes_playwright')
hashes = {name: hashlib.sha256((Path('/apps')/name).read_bytes()).hexdigest()
          for name in ('billcollector/bourse_direct.py', 'billcollector/notice.py',
                       'billcollector/downloads.py', 'billcollector/runner.py',
                       'recipes_playwright/recipe-pw__boursedirect.yaml')}
print(json.dumps({'packaged_source_sha256':hashes, 'uid':os.getuid(),
                  'gid':os.getgid(), 'pypdf':pypdf.__version__}), flush=True)
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/apps/browser'
os.environ['BC_BROWSER_DIR'] = '/apps/browser'
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
sys.path.insert(0, '/test-deps')
import pytest
code = pytest.main([str(root/'tests/test_bourse_direct.py'),
                    str(root/'tests/test_notice_identity.py'), '-q',
                    '--basetemp', str(root/'pytest-proof')])
if code:
    sys.exit(code)
# Reuse the selected engine's non-root, sandbox, headed, UI and legacy checks.
import subprocess
sys.exit(subprocess.run([sys.executable, '/checks/tests/check_image_runtime.py']).returncode)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--proof-dir', type=Path, required=True)
    parser.add_argument('--container-name', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.proof_dir.absolute()
    root.mkdir(parents=True, exist_ok=False)
    # The image has runtime dependencies. Add only the test runner's pure Python
    # packages, so host compiled dependencies cannot shadow the image runtime.
    deps = root/'test-deps'
    deps.mkdir(mode=0o755)
    deps.chmod(0o755)
    for name in ('pytest', '_pytest', 'pluggy', 'iniconfig', 'packaging', 'py'):
        origin = Path(importlib.util.find_spec(name).origin)
        if origin.name == '__init__.py':
            shutil.copytree(origin.parent, deps/name)
        else:
            shutil.copy2(origin, deps/origin.name)
    for path in deps.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)
    engine = args.engine.absolute()
    revision = subprocess.run(['git', '-C', str(engine), 'rev-parse', 'HEAD'],
                              check=True, capture_output=True, text=True).stdout.strip()
    image = json.loads(subprocess.run(['docker', 'image', 'inspect', args.image], check=True,
                                     capture_output=True, text=True).stdout)[0]
    if image['Config']['Labels']['org.opencontainers.image.revision'] != revision:
        raise ValueError('Image revision differs from the selected engine')
    command = ['docker', 'create', '--name', args.container_name, '-i',
               '--network', 'none', '--shm-size', '256m',
               '--security-opt', f'seccomp={engine}/docker/chromium-seccomp.json',
               '--mount', f'type=bind,src={engine}/tests,dst=/checks/tests,readonly',
               '--mount', f'type=bind,src={deps},dst=/test-deps,readonly',
               '--entrypoint', 'python3', args.image, '-']
    container = subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()
    (root/'bootstrap.py').write_text(BOOTSTRAP)
    with (root/'container.log').open('w') as log:
        result = subprocess.run(['docker', 'start', '-ai', container],
                                check=False, input=BOOTSTRAP, text=True, stdout=log, stderr=subprocess.STDOUT)
    state = json.loads(subprocess.run(['docker', 'inspect', container], check=True,
                                     capture_output=True, text=True).stdout)[0]
    hashes = {name: hashlib.sha256((engine/'apps'/name).read_bytes()).hexdigest()
              for name in ('billcollector/bourse_direct.py', 'billcollector/notice.py',
                           'billcollector/downloads.py', 'billcollector/runner.py',
                           'recipes_playwright/recipe-pw__boursedirect.yaml')}
    log_text = (root/'container.log').read_text()
    packaged = next((json.loads(line)['packaged_source_sha256'] for line in log_text.splitlines()
                     if line.startswith('{"packaged_source_sha256"')), None)
    proof = {'image_id': state['Image'], 'engine_revision': revision, 'container_id': container,
             'exit_code': state['State']['ExitCode'], 'start_exit_code': result.returncode,
             'packaged_source_matches_checkout': packaged == hashes,
             'network': state['HostConfig']['NetworkMode'], 'container_retained': True}
    (root/'result.json').write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof))
    if result.returncode or proof['exit_code'] or packaged != hashes:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
