"""Resolve per-run dependencies without embedding a machine or device identity."""
import io
import os
from pathlib import Path
import shutil
import subprocess
from functools import lru_cache

WORK = Path(os.environ.get('UCAMPUS_WORK', str(Path.cwd() / 'work' / 'ucampus-run'))).resolve()

def adb_path():
    candidates = [os.environ.get('UCAMPUS_ADB'), shutil.which('adb')]
    for key in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        if os.environ.get(key):
            candidates.append(str(Path(os.environ[key]) / 'platform-tools' / 'adb.exe'))
    if os.environ.get('LOCALAPPDATA'):
        candidates.append(str(Path(os.environ['LOCALAPPDATA']) / 'Android/Sdk/platform-tools/adb.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError('ADB not found. Set UCAMPUS_ADB to an existing adb executable.')

def devices():
    raw = subprocess.check_output([adb_path(), 'devices'], timeout=15).decode()
    return [line.split()[:2] for line in raw.splitlines()[1:] if line.strip() and not line.startswith('*')]

# Pin the validated device for this process; a disconnect must not switch targets.
@lru_cache(maxsize=1)
def serial():
    connected = devices()
    selected = os.environ.get('UCAMPUS_SERIAL')
    if selected:
        if [selected, 'device'] not in connected:
            raise RuntimeError('Selected device is absent or not authorized.')
        return selected
    ready = [row[0] for row in connected if row[1] == 'device']
    if len(ready) != 1:
        raise RuntimeError('Set UCAMPUS_SERIAL: expected exactly one authorized device.')
    return ready[0]

def adb(*args):
    return subprocess.check_output([adb_path(), '-s', serial(), *args], timeout=25)

def audio_device(direction):
    value = os.environ.get('UCAMPUS_AUDIO_' + direction)
    if value is None:
        raise RuntimeError('Choose an audio endpoint with doctor.py, then set UCAMPUS_AUDIO_' + direction)
    return int(value)

def assert_recording_profile(png):
    from PIL import Image
    size = Image.open(io.BytesIO(png)).size
    declared = os.environ.get('UCAMPUS_RECORDING_SIZE', '')
    if declared != f'{size[0]}x{size[1]}':
        raise RuntimeError('Calibrate the purple/green recording detector on this layout, then set UCAMPUS_RECORDING_SIZE to the screenshot dimensions. No tap was sent.')
