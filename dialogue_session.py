"""Run reviewed short dialogue responses with one bridge/audio process and accepted-turn checks."""
import argparse
import hashlib
import io
import importlib.util
import json
from pathlib import Path
from project_paths import SKILL
import re
import subprocess
import sys
import time

from bridge_client import BridgeError
from training_session import Session,xml_tree


def progress(nodes):
    texts={row.get('text') or row.get('description','') for row in nodes.values()}
    if 'AI口语对话' not in texts:raise BridgeError('Expected the AI dialogue page')
    matches=[re.search(r'话轮数[:：]\s*(\d+)\s*/\s*(\d+)',text) for text in texts]
    counts=[tuple(map(int,m.groups())) for m in matches if m]
    if len(counts)!=1 or not 0<=counts[0][0]<=counts[0][1]<=20 or counts[0][1]==0:raise BridgeError('Missing or ambiguous dialogue progress')
    # shortcut: this calibrated layout exposes AI text and user voice bubbles; recheck if user transcripts appear.
    english=[nodes[ref].get('text','') for ref in sorted(nodes,key=lambda value:[int(n) for n in value.split('.')])
             if re.match(r'^[A-Za-z].{15}',nodes[ref].get('text',''))]
    return counts[0],texts,english[-1] if english else ''


def reply_audio_bounds(nodes,reply):
    matches=[ref for ref,row in nodes.items() if row.get('text')==reply]
    if len(matches)!=1:raise BridgeError('Missing unique current AI reply')
    ref=matches[0];parent,index=ref.rsplit('.',1)
    left=nodes[ref]['bounds'][0]
    candidates=[]
    for key,row in nodes.items():
        if key.rpartition('.')[0]!=parent or int(key.rsplit('.',1)[-1])>=int(index):continue
        b=row['bounds']
        if row.get('clickable') and abs(b[0]-left)<=2 and 100<=b[3]-b[1]<=220 and 180<=b[2]-b[0]<=650:
            candidates.append((int(key.rsplit('.',1)[-1]),b))
    if not candidates:raise BridgeError('Current AI audio bubble is not visible')
    return max(candidates)[1]


def audio_duration_visible(png,bounds):
    from PIL import Image
    image=Image.open(io.BytesIO(png)).convert('RGB')
    left,top,right,bottom=bounds
    if image.size!=(1440,3200) or not (0<=left<right<=1441 and 0<=top<bottom<=3200):
        raise BridgeError('AI audio bubble is outside the calibrated screenshot')
    # shortcut: this layout paints its duration without accessibility text; recheck on UI changes.
    crop=image.crop((left+100,top+25,right-15,bottom-25))
    pixels=crop.get_flattened_data() if hasattr(crop,'get_flattened_data') else crop.getdata()
    return sum(r<120 and g<120 and b<150 for r,g,b in pixels)>=80


class Dialogue:
    def __init__(self,session):self.session=session

    def read(self,wait_ms=0):
        client=self.session.client
        client.observe(wait_ms=wait_ms)
        if client.last['truncated']:raise BridgeError('Incomplete dialogue state')
        return progress(client.nodes)

    def wait_audio_ready(self,current,target,reply):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            count,texts,latest=self.read(wait_ms=150)
            if count!=(current,target) or latest!=reply:raise BridgeError('Dialogue changed while waiting for AI audio')
            bounds=reply_audio_bounds(self.session.client.nodes,reply)
            png=self.session.control.adb('exec-out','screencap','-p')
            if '按住说话' in texts and audio_duration_visible(png,bounds):return
            time.sleep(.4)
        raise BridgeError('AI audio duration is not ready; no press was sent')

    def turn(self,wav,current,target):
        control=self.session.control
        self.session.check_audio(wav)
        count,texts,before_reply=self.read()
        if count!=(current,target) or '按住说话' not in texts:
            raise BridgeError('Dialogue changed before recording; no press was sent')
        if not before_reply:raise BridgeError('Wait for the current AI reply before recording')
        began=time.monotonic()
        self.wait_audio_ready(current,target,before_reply)
        try:
            control.adb('shell','input','motionevent','DOWN','720','2970')
            deadline=time.monotonic()+8
            while True:
                count,texts,_=self.read(wait_ms=150)
                if count!=(current,target):raise BridgeError('Turn changed while preparing the recording')
                if any('松开' in text for text in texts):break
                if time.monotonic()>=deadline:
                    evidence=control.snapshot('dialogue-hold-error')
                    raise BridgeError('Hold-to-record state did not appear; no audio played. Held-state snapshot: '+str(evidence))
            control.play_audio(wav)
            time.sleep(.2)
        finally:
            control.adb('shell','input','motionevent','UP','720','2970')
        deadline=time.monotonic()+35
        candidate='';stable_since=time.monotonic()
        while time.monotonic()<deadline:
            count,texts,reply=self.read(wait_ms=150)
            if count[1]!=target or count[0] not in (current,current+1):
                raise BridgeError('Unexpected accepted-turn count; do not replay')
            if reply!=candidate:
                candidate=reply;stable_since=time.monotonic()
            ready=('按住说话' in texts and reply and reply!=before_reply
                   and time.monotonic()-stable_since>=.4)
            if count[0]==current+1 and (ready or count[0]==target):
                return {'before':current,'accepted':count[0],'target':target,'elapsed_seconds':round(time.monotonic()-began,3)}
        raise BridgeError('Accepted turn and next recording readiness were not both observed; inspect progress, do not resend')


def run(session,scripts,responses):
    if not isinstance(responses,list) or not responses or any(not isinstance(s,str) or not s.strip() for s in responses):
        raise ValueError('Provide the reviewed response list')
    dialogue=Dialogue(session)
    (current,target),texts,reply=dialogue.read()
    if len(responses)<target:raise ValueError('Not enough reviewed responses')
    if current==target:return {'complete':True,'accepted':current,'target':target}
    if not reply:raise BridgeError('Wait for the first AI message before recording')
    control=session.control
    png=control.adb('exec-out','screencap','-p')
    control.assert_recording_profile(png)
    from PIL import Image
    if Image.open(io.BytesIO(png)).size!=(1440,3200):raise BridgeError('Hold coordinates require the calibrated 1440x3200 dialogue layout')
    control.snapshot('dialogue-start',png)
    wavs={}
    for index in range(current,target):
        text=responses[index]
        key=hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]
        wav=control.WORK/f'speech-{key}.wav'
        if not wav.exists():
            txt=wav.with_suffix('.txt');txt.write_text(text,encoding='utf-8')
            subprocess.run(['pwsh.exe','-NoProfile','-File',str(scripts/'make-speech.ps1'),
                            '-TextPath',str(txt),'-WavPath',str(wav)],check=True,stdout=subprocess.DEVNULL)
        wavs[index]=wav
    with (control.WORK/'bridge-dialogue.jsonl').open('a',encoding='utf-8') as log:
        for index in range(current,target):
            try:
                result=dialogue.turn(wavs[index],index,target)
            except Exception as error:
                result={'before':index,'target':target,'error':str(error),'next':'Inspect current progress; do not resend automatically'}
                try:
                    result['screenshot']=control.snapshot('dialogue-error')
                    session.client.observe()
                    path=Path(result['screenshot']).with_suffix('.xml')
                    path.write_bytes(xml_tree(session.client.nodes))
                    result['ui']=str(path)
                except Exception as evidence_error:
                    result['evidence_error']=str(evidence_error)
                log.write(json.dumps(result)+'\n');log.flush()
                raise
            log.write(json.dumps(result)+'\n');log.flush()
            print(json.dumps(result),flush=True)
    return {'complete':True,'accepted':target,'target':target,'screenshot':control.snapshot('dialogue-complete')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8794)
    parser.add_argument('--skill',type=Path,default=SKILL)
    parser.add_argument('--responses',type=Path,required=True)
    args=parser.parse_args()
    scripts=args.skill.resolve(strict=True)/'scripts'
    sys.path.insert(0,str(scripts))
    spec=importlib.util.spec_from_file_location('control',scripts/'ucampus-control.py')
    control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)
    session=Session(control,args.port)
    try:print(json.dumps(run(session,scripts,json.loads(args.responses.read_text(encoding='utf-8'))),ensure_ascii=False))
    finally:session.client.close()


if __name__=='__main__':main()
