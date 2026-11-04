"""Read-only dependency report; never downloads models or changes phone state."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from runtime import WORK, adb, adb_path, devices, serial

result = {'python': sys.executable, 'work': str(WORK), 'ffmpeg': shutil.which('ffmpeg'), 'pwsh': shutil.which('pwsh')}
result['scrcpy'] = os.environ.get('UCAMPUS_SCRCPY') or shutil.which('scrcpy')
result['modules'] = {name: importlib.util.find_spec(name) is not None for name in ['PIL', 'numpy', 'sounddevice', 'faster_whisper']}
try:
    result.update(adb=adb_path(), devices=devices(), selected_device=serial(), screen=adb('shell', 'wm', 'size').decode().strip())
except Exception as error:
    result['adb_error'] = str(error)
if result['modules']['sounddevice']:
    import sounddevice as sd
    result['audio_devices'] = list(sd.query_devices())
    result['default_audio_devices'] = list(sd.default.device)
    result['selected_audio_output'] = os.environ.get('UCAMPUS_AUDIO_OUTPUT')
if result['modules']['faster_whisper']:
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel('base', device='cpu', compute_type='int8', local_files_only=True)
        result['cached_base_model'] = True
    except Exception as error:
        result['model_error'] = str(error)
if result['pwsh']:
    probe = "$v=New-Object -ComObject SAPI.SpVoice; @($v.GetVoices('Language=409') | ForEach-Object { $_.GetDescription() }) | ConvertTo-Json -Compress"
    completed = subprocess.run([result['pwsh'], '-NoProfile', '-Command', probe], capture_output=True, text=True)
    result['english_sapi_voices'] = completed.stdout.strip() if completed.returncode == 0 else completed.stderr.strip()
print(json.dumps(result, ensure_ascii=False, indent=2))
