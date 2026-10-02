#!/usr/bin/env python3
"""Install image-gen: own .venv, Python dependencies and an empty private config file.

  python3 setup.py                 # venv + requirements + ~/.config/media-skills/image-gen.env (if missing)
  python3 setup.py --with-upscale  # plus torch CPU + spandrel for upscale.py (~1 GB)
  python3 setup.py --check         # only report what is installed; changes nothing

Never reads, prints or sends API keys. No paid request is made.
"""
import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = 'image-gen'
CONFIG = Path((os.getenv('XDG_CONFIG_HOME') or Path.home() / '.config')).expanduser() / 'media-skills' / f'{NAME}.env'


def config_state():
    if not CONFIG.is_file():
        return 'missing'
    filled = [line.split('=', 1)[0] for line in CONFIG.read_text().splitlines()
              if '=' in line and not line.lstrip().startswith('#') and line.split('=', 1)[1].strip()]
    return 'LAOZHANG_API_KEY set' if 'LAOZHANG_API_KEY' in filled else 'present, LAOZHANG_API_KEY empty'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--check', action='store_true', help='report only, install nothing')
    p.add_argument('--with-upscale', action='store_true', help='also install torch CPU + spandrel for upscale.py')
    a = p.parse_args()
    if sys.version_info < (3, 10):
        raise SystemExit('Python 3.10+ required')
    if os.name != 'posix':
        raise SystemExit('Use Linux, macOS or WSL2.')
    py = ROOT / '.venv/bin/python'
    if a.check:
        print(f'python venv: {"ok" if py.is_file() else "missing (run python3 setup.py)"}')
        for tool in ('ffmpeg', 'ffprobe'):
            print(f'{tool}: {shutil.which(tool) or "missing (needed only for video_loop.py)"}')
        print(f'config {CONFIG}: {config_state()}')
        return
    if not py.is_file():
        venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    pip = [str(py), '-m', 'pip', 'install', '--disable-pip-version-check', '-q']
    subprocess.run(pip + ['-r', str(ROOT / 'requirements.txt')], check=True)
    if a.with_upscale:
        subprocess.run(pip + ['--index-url', 'https://download.pytorch.org/whl/cpu', 'torch'], check=True)
        subprocess.run(pip + ['-r', str(ROOT / 'requirements-upscale.txt')], check=True)
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(ROOT / '.env.example', CONFIG)
        CONFIG.chmod(0o600)
        print(f'Created {CONFIG} (chmod 600). Put your LAOZHANG_API_KEY there.')
    print(f'Ready: {py}')
    print(f'Config: {CONFIG} — {config_state()}')
    print(f'Test without paid calls: {py} -m unittest discover -s {ROOT / "tests"}')


if __name__ == '__main__':
    main()
