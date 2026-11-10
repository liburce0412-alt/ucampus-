"""One local process for reviewed plans, Pre decisions and cached transcription."""
import argparse
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import wave

from audio_injection_trial import inject_once,word_turn
from bridge_client import compact,BridgeError
from project_paths import SKILL
from screen_frames import display_adb,check_display_visible
from training_session import Session
from workflow_session import Runner,Paused,validate
from freeze_runtime import verify


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def spoken_choice(transcript,options):
    matches=[i for i,word in enumerate(options)
             if re.search(r'(?<![a-z])'+re.escape(word.lower())+r'(?![a-z])',transcript.lower())]
    return matches[0] if len(matches)==1 else None


def page_key(nodes):
    # Include the question and progress text, not just A/B/C; animation-only bounds may move.
    return sorted((str(n.get('text') or ''),str(n.get('description') or ''))
                  for n in nodes.values() if n.get('text') or n.get('description'))


def choices(nodes,bounds):
    left,top,right,bottom=bounds
    rows=[n for n in nodes.values() if n.get('text') and not n.get('password')
          and left<=n['bounds'][0]<right and top<n['bounds'][1]<bottom]
    rows.sort(key=lambda n:n['bounds'][1]);values=[n['text'] for n in rows]
    if len(values)!=3 or len(set(values))!=3:raise BridgeError('Expected three distinct inspected options')
    return values


def versions(skill):
    root=Path(__file__).resolve().parent
    files=[root/name for name in ('training_worker.py','workflow_session.py','bridge_client.py',
           'training_session.py','audio_injection_trial.py','target_audio_capture.py','screen_frames.py')]
    files.extend(skill/'scripts'/name for name in ('runtime.py','ucampus-control.py','make-speech.ps1','audio-parts.py'))
    return {str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


class Worker:
    def __init__(self,session,runtime,args,receive,emit=print):
        self.session,self.client,self.runtime=session,session.client,runtime
        self.args,self.root,self.receive,self.emit=args,args.output,receive,emit
        self.model=None;self.sequence=0;self.request=0
        self.parts=module('worker_audio_parts',args.skill/'scripts/audio-parts.py')
        if not hasattr(self.parts,'transcribe_parts'):raise ValueError('Use the bundled runtime with resident transcription support')
        self.initial_versions=versions(args.skill)
        self.root.mkdir(parents=True,exist_ok=False)
        self.started=time.monotonic()
        self.manifest={'started_at':datetime.now(timezone.utc).isoformat(),'versions':self.initial_versions,
                       'serial':runtime.serial(),'display_id':self.client.display_id,'uid':args.uid,
                       'display_deadline':args.display_deadline,'initialization_seconds':None}
        self.save_manifest()

    def save_manifest(self):
        (self.root/'manifest.json').write_text(json.dumps(self.manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    def check(self,seconds=0):
        if (self.root/'pause.stop').exists():raise Paused('Worker pause requested')
        if self.args.display_deadline:
            end=datetime.fromisoformat(self.args.display_deadline)
            if (end-datetime.now(timezone.utc)).total_seconds()<seconds+30:
                raise BridgeError('Display lease cannot cover this stage; hand off at the current boundary')

    def warm(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            began=time.monotonic()
            self.model=WhisperModel('base',device='cpu',compute_type='int8',local_files_only=True)
            self.manifest['asr_load_seconds']=round(time.monotonic()-began,3);self.save_manifest()

    def text(self,data):
        self.warm()
        segments,_=self.model.transcribe(data,language='en',beam_size=3,vad_filter=True,condition_on_previous_text=False)
        return ' '.join(s.text.strip() for s in segments).strip()

    def decide(self,runner,kind,options,extra):
        key=page_key(self.client.nodes);runner.observe()
        if page_key(self.client.nodes)!=key:raise BridgeError('Question changed before the decision request')
        self.request+=1;ident=secrets.token_hex(6)
        self.emit({'type':'decision','request_id':ident,'kind':kind,'choices':options,**extra})
        began=time.monotonic();answer=self.receive(600)
        if set(answer)!={'request_id','choice'} or answer['request_id']!=ident or answer['choice'] not in ('A','B','C'):
            raise BridgeError('Stale or malformed decision; no answer was clicked')
        runner.observe()
        if page_key(self.client.nodes)!=key:raise BridgeError('Question changed while waiting for a decision')
        self.event('decision',kind=kind,seconds=round(time.monotonic()-began,3))
        return ord(answer['choice'])-ord('A')

    def event(self,kind,**values):
        with (self.root/'events.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'time':datetime.now(timezone.utc).isoformat(),'kind':kind,**values},ensure_ascii=False)+'\n')

    def transcribe_capture(self,source,stop,output):
        return self.parts.transcribe_parts(self.model,source,output,watch=True,stop=stop)

    def pre(self,command,directory):
        allowed={'command','capture','enter','start','start_at'}
        if set(command)-allowed or not isinstance(command.get('capture'),str):raise ValueError('Pre requires a capture path')
        if type(command.get('enter',False)) is not bool:raise ValueError('enter must be boolean')
        first=command.get('start_at',1)
        if type(first) is not int or not 1<=first<=7:raise ValueError('start_at must be 1..7')
        if command.get('enter') and ('start' in command or first!=1):raise ValueError('Do not re-enter a resumed recording')
        if not command.get('enter') and 'start' not in command:raise ValueError('Resume requires the inspected audio offset')
        source=Path(command['capture']).resolve(strict=True)
        ready=json.loads(source.with_name('ready.json').read_text(encoding='utf-8'))
        if ready.get('uid')!=self.args.uid or ready.get('mode')!='capture':raise ValueError('Wrong capture identity')
        def position():
            with wave.open(str(source),'rb') as wav:
                if (wav.getframerate(),wav.getnchannels(),wav.getsampwidth())!=(16000,1,2):raise ValueError('Expected mono PCM16 16 kHz capture')
                return wav.getnframes()/16000
        start=command.get('start',max(0,position()-.2))
        if type(start) not in (int,float) or not 0<=start<=position():raise ValueError('Invalid resume offset')
        before=position();time.sleep(.5)
        if position()<=before:raise BridgeError('Capture is not growing; no playback started')
        self.check(180);self.warm();directory.mkdir()
        runner=Runner(self.session,directory,directory,runtime=self.runtime,uid=self.args.uid,skill=self.args.skill)
        runner.stop=self.root/'pause.stop'
        report={'status':'running','completed_words':0,'in_flight':None}
        def save():
            report.update(bridge_reads=runner.reads,clicks=runner.clicks,
                          transition_reads=runner.transition_reads,rejected_clicks=runner.rejected_clicks)
            temp=directory/'checkpoint.tmp';temp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            temp.replace(directory/'checkpoint.json')
        def observe():self.check();runner.observe();return self.client.nodes
        def click(target,before):
            runner.click({'action':'click','before':[{'text':t} for t in before],'target':{'text':target}})
        def wait_text(names,timeout=30):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                self.check()
                if runner.observe(wait_ms=150,during_wait=True):
                    found={n.get('text') for n in self.client.nodes.values() if n.get('enabled')} & set(names)
                    if found:return found
            raise BridgeError('Expected next state did not appear; do not replay')
        try:
            save()
            if command.get('enter'):
                # Warm the local model before measuring the first audio offset.
                start=max(0,position()-.2);observe();click('进入 Pre-listening',['进入 Pre-listening'])
            for number in range(first,8):
                self.check(90);report.update(in_flight=number,audio_start=start);save()
                deadline=time.monotonic()+35
                while time.monotonic()<deadline:
                    self.check()
                    if not runner.observe(wait_ms=150,during_wait=True):continue
                    nodes=self.client.nodes;data=self.parts.read_part(source,start,30)
                    import numpy as np
                    active=np.flatnonzero(np.abs(data)>.001)
                    if (active.size and len(data)-active[-1]>=8000
                            and any(n.get('text')=='点击播放' for n in nodes.values())):break
                    if source.with_name('result.json').exists():raise BridgeError('Capture ended before the current clip was complete')
                    time.sleep(.2)
                else:raise BridgeError('No completed audible short clip; inspect saved audio')
                options=choices(nodes,self.args.option_bounds)
                transcript=self.text(data);selected=spoken_choice(transcript,options)
                if selected is None:selected=self.decide(runner,'heard',options,{'transcript':transcript})
                word=options[selected];click(word,options);wait_text({'下一题'});click('下一题',[word,'下一题'])
                wait_text({'选出加粗单词或短语的释义'});meanings=choices(observe(),self.args.option_bounds)
                selected=self.decide(runner,'meaning',meanings,{'word':word})
                click(meanings[selected],meanings);wait_text({'下一题'});click('下一题',[meanings[selected],'下一题'])
                wait_text({'点击跟读'});nodes=observe()
                heads=[n['text'] for n in nodes.values() if re.fullmatch(r'[A-Za-z]+(?:[ -][A-Za-z]+)*',str(n.get('text') or ''))
                       and n['bounds'][2]-n['bounds'][0]>=900 and 700<n['bounds'][1]<1100]
                if len(heads)!=1 or heads[0].casefold()!=word.casefold():raise BridgeError('Recording headword changed')
                speech=self.root/'speech';speech.mkdir(exist_ok=True)
                key=hashlib.sha256(word.encode()).hexdigest()[:16];audio=speech/(key+'.wav')
                if not audio.exists():
                    textfile=speech/(key+'.txt');textfile.write_text(word+'\n',encoding='utf-8')
                    began=time.monotonic()
                    speech_result=subprocess.run(['pwsh.exe','-NoProfile','-File',str(self.args.skill/'scripts/make-speech.ps1'),
                                                 '-TextPath',str(textfile),'-WavPath',str(audio)],check=True,
                                                 capture_output=True,text=True,encoding='utf-8',timeout=30)
                    self.event('speech_ready',backend=json.loads(speech_result.stdout)['backend'],
                               seconds=round(time.monotonic()-began,3))
                result=inject_once(self.session,self.runtime,audio,self.args.uid,directory/f'word-{number}',
                                   lambda:word_turn(self.session,audio,word,*self.args.xy))
                if not result.get('policy_released'):raise BridgeError('Input policy release unknown')
                found=wait_text({'下一题','进入While-listening'})
                report.update(completed_words=report['completed_words']+1,in_flight=None);save()
                self.event('word_complete',number=number,seconds=result.get('elapsed_seconds'),policy_released=True)
                if '进入While-listening' in found:
                    report['status']='complete';save();return report
                start=max(0,position()-.2);click('下一题',[word,'下一题'])
            raise BridgeError('Unexpectedly many Pre words')
        except BaseException as error:
            report.update(status='paused' if isinstance(error,Paused) else 'needs_review',error=str(error));save();raise

    def execute(self,command):
        self.check()
        if versions(self.args.skill)!=self.initial_versions:raise BridgeError('Runtime files changed; restart from a fresh observed boundary')
        if not isinstance(command,dict):raise ValueError('Expected a command object')
        command=dict(command);purpose=command.pop('purpose','training')
        if purpose not in ('training','recovery','debug'):raise ValueError('Unknown timing purpose')
        kind=command.get('command')
        if kind=='observe' and set(command)=={'command'}:return self.client.observe()
        self.sequence+=1;directory=self.root/f'stage-{self.sequence:03}'
        began=time.monotonic();started=datetime.now(timezone.utc).isoformat();requests=self.request
        try:
            if kind=='pre':result=self.pre(command,directory)
            elif kind=='plan' and set(command)=={'command','plan'}:
                plan=validate(command['plan'],self.root)
                self.check(sum(s.get('timeout',30) for s in plan['steps']))
                captures=any(s['action']=='capture' for s in plan['steps'])
                if captures:self.warm()
                runner=Runner(self.session,directory,self.root,runtime=self.runtime,uid=self.args.uid,skill=self.args.skill,
                              capture_transcriber=self.transcribe_capture if captures else None)
                runner.stop=self.root/'pause.stop';result=runner.run(plan)
                if captures:
                    result['transcripts']=[json.loads(line) for path in sorted(directory.glob('step-*/audio/transcript.jsonl'))
                                           for line in path.read_text(encoding='utf-8').splitlines()]
            elif kind=='transcribe' and set(command)=={'command','wav','start','seconds'}:
                if type(command['start']) not in (int,float) or command['start']<0 or type(command['seconds']) not in (int,float) or not 1<=command['seconds']<=90:
                    raise ValueError('Transcribe one bounded 1..90 second interval')
                result={'transcript':self.text(self.parts.read_part(Path(command['wav']).resolve(strict=True),command['start'],command['seconds']))}
            else:raise ValueError('Use observe, pre, plan or transcribe')
            self.event('stage_complete',stage=self.sequence,stage_kind=kind,purpose=purpose,started_at=started,
                       seconds=round(time.monotonic()-began,3),decision_requests=self.request-requests)
            return {'stage':self.sequence,'result':result,'evidence':str(directory if directory.exists() else self.root)}
        except BaseException as error:
            self.event('stage_failed',stage=self.sequence,stage_kind=kind,purpose=purpose,started_at=started,
                       seconds=round(time.monotonic()-began,3),decision_requests=self.request-requests,error=str(error));raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--uid',type=int,required=True)
    parser.add_argument('--port',type=int,default=8794)
    parser.add_argument('--display-id',type=int,required=True)
    parser.add_argument('--capture-display-id',type=int,required=True)
    parser.add_argument('--xy',type=int,nargs=2,required=True)
    parser.add_argument('--option-bounds',type=int,nargs=4,required=True)
    parser.add_argument('--skill',type=Path,default=SKILL)
    lease=parser.add_mutually_exclusive_group(required=True)
    lease.add_argument('--display-unlimited',action='store_true',help='Owned scrcpy was started without --time-limit; close it after the job')
    lease.add_argument('--display-deadline',help='Actual scrcpy expiry, ISO timestamp with timezone')
    args=parser.parse_args();args.output=args.output.resolve();args.skill=args.skill.resolve(strict=True)
    if args.output.exists() or args.uid<10000 or args.display_id<=0:parser.error('Use a fresh output, inspected UID and secondary display')
    if not (100<args.xy[0]<1340 and 600<args.xy[1]<2900):parser.error('Inspected 1440x3200 recording coordinates required')
    if os.environ.get('UCAMPUS_RECORDING_SIZE')!='1440x3200':parser.error('Set UCAMPUS_RECORDING_SIZE=1440x3200 after inspecting this layout')
    l,t,r,b=args.option_bounds
    if not (0<=l<r<=1440 and 0<=t<b<=3200):parser.error('Invalid inspected option bounds')
    if args.display_deadline and datetime.fromisoformat(args.display_deadline).tzinfo is None:parser.error('Display deadline needs a timezone')
    began=time.monotonic()
    verify(Path(__file__).parent)
    sys.path.insert(0,str(args.skill/'scripts'));import runtime
    serial=runtime.serial()
    package=runtime.adb('shell','pm','list','packages','-U','cn.unipus.cloud').decode()
    if re.findall(r'^package:cn\.unipus\.cloud\s+uid:(\d+)\s*$',package,re.M)!=[str(args.uid)]:
        raise BridgeError('Application UID does not match the selected device')
    forwards=runtime.adb('forward','--list').decode().splitlines()
    if not any(row.split()[:2]==[serial,f'tcp:{args.port}'] for row in forwards):raise BridgeError('Bridge forward does not belong to the selected device')
    control=module('worker_control',args.skill/'scripts/ucampus-control.py');session=Session(control,args.port,args.display_id)
    messages=queue.Queue()
    def reader():
        for line in sys.stdin:messages.put(line)
        messages.put(None)
        if args.output.is_dir():(args.output/'pause.stop').touch(exist_ok=True)
    threading.Thread(target=reader,daemon=True).start()
    def receive(timeout):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            worker.check()
            try:line=messages.get(timeout=.2)
            except queue.Empty:continue
            if line is None:raise EOFError('Input closed; no pending action was replayed')
            if len(line)>65536:raise ValueError('Command too large')
            return json.loads(line)
        raise Paused('Decision/command wait timed out; inspect checkpoint')
    def emit(value):print(json.dumps(value,ensure_ascii=False),flush=True)
    try:
        control.adb=display_adb(control.adb,session.client,capture_id=args.capture_display_id)
        check_display_visible(runtime.adb,session.client);session.client.observe()
        worker=Worker(session,runtime,args,receive,emit)
        worker.manifest['initialization_seconds']=round(time.monotonic()-began,3);worker.save_manifest()
        emit({'type':'ready','output':str(args.output),'display_id':args.display_id,'uid':args.uid})
        while True:
            command=receive(1800)
            if command=={'command':'close'}:emit({'type':'closed'});break
            emit({'type':'complete',**worker.execute(command)})
    except (EOFError,Paused) as error:
        emit({'type':'paused','error':str(error)});raise SystemExit(2)
    except Exception as error:
        emit({'type':'needs_review','error':str(error),'next':'Read current state; never replay uncertain actions'})
        raise SystemExit(1)
    finally:session.client.close()


if __name__=='__main__':main()
