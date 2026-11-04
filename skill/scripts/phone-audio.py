"""Temporarily mute phone media playback; preserve microphone and original volume."""
import json,re,sys
from runtime import adb, WORK

state=WORK/'phone-media-volume.json'
def quiet():
    current=adb('shell','cmd','media_session','volume','--stream','3','--get').decode()
    volume=int(re.search(r'volume is (\d+)',current).group(1))
    if not state.exists(): state.write_text(json.dumps({'original':volume}),encoding='utf-8')
    if volume>0: adb('shell','input','keyevent','164')
    actual=adb('shell','cmd','media_session','volume','--stream','3','--get').decode()
    if int(re.search(r'volume is (\d+)',actual).group(1))!=0:
        raise RuntimeError('Phone media mute not confirmed')

def restore():
    if state.exists():
        original=json.loads(state.read_text(encoding='utf-8'))['original']
        current=adb('shell','cmd','media_session','volume','--stream','3','--get').decode()
        if original>0 and int(re.search(r'volume is (\d+)',current).group(1))==0:
            adb('shell','input','keyevent','164')
        adb('shell','cmd','media_session','volume','--stream','3','--set',str(original))
        state.unlink()

if __name__=='__main__':
    {'quiet':quiet,'restore':restore}[sys.argv[1]]()
