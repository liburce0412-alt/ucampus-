"""Execute one reviewed stage with a persistent bridge, explicit readback and local checkpoints."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from audio_injection_trial import inject_once, pcm16, word_turn
from bridge_client import BridgeError, OutcomeUnknown
from project_paths import SKILL
from screen_frames import display_adb
from training_session import Session
from ucampus_readonly import encode


class Paused(BridgeError):
    pass


def selector(value,*,bounds=False):
    if bounds and isinstance(value,dict) and set(value)=={'bounds'}:
        box=value['bounds']
        if (not isinstance(box,list) or len(box)!=4 or any(type(n) is not int for n in box)
                or not (0<=box[0]<box[2]<=1440 and 0<=box[1]<box[3]<=3200)):
            raise ValueError('Use freshly inspected 1440x3200 control bounds')
        return
    if (not isinstance(value,dict) or not value or set(value)-{'text','id','description'}
            or any(not isinstance(v,str) or not v for v in value.values())):
        raise ValueError('Use exact nonempty text/id/description selectors')


def validate(plan,base):
    if not isinstance(plan,dict) or set(plan)!={'version','steps'} or type(plan['version']) is not int or plan['version']!=1:
        raise ValueError('Expected a version 1 plan with steps')
    if not isinstance(plan['steps'],list) or not 1<=len(plan['steps'])<=64:
        raise ValueError('Use 1..64 reviewed steps')
    required={'click':{'target','expect'},'select':{'target'},'wait':{'expect'},
              'word':{'word','wav','xy','expect'},'capture':{'target','expect','seconds'}}
    for step in plan['steps']:
        if not isinstance(step,dict) or step.get('action') not in required:
            raise ValueError('Unknown step action')
        fields=required[step['action']]|{'action','before'}
        if step['action']=='capture' and 'clock' in step:fields=(fields-{'expect'})|{'clock'}
        if not fields<=set(step) or set(step)-fields-{'timeout'}:
            raise ValueError('Missing or unknown step fields')
        if not isinstance(step['before'],list) or not 1<=len(step['before'])<=8:
            raise ValueError('Each step requires current page/question anchors')
        for item in step['before']:selector(item)
        for key in ('target','expect'):
            if key in step:selector(step[key],bounds=key=='target' and step['action']=='capture')
        timeout=step.get('timeout',30)
        if type(timeout) is not int or not 1<=timeout<=900:raise ValueError('Timeout must be 1..900 seconds')
        if step['action']=='capture':
            if 'clock' in step:
                current,total=clock_value(step['clock'])
                if current==total or timeout<total-current:raise ValueError('Wait timeout must cover an unfinished clip')
            if type(step['seconds']) is not int or not 1<=step['seconds']<=900:
                raise ValueError('Capture must be 1..900 seconds')
            if timeout>step['seconds']:raise ValueError('Capture duration must cover the whole wait timeout')
        if step['action']=='word':
            if not isinstance(step['word'],str) or not step['word'].strip() or not isinstance(step['wav'],str):
                raise ValueError('Word and reviewed WAV are required')
            xy=step['xy']
            if not isinstance(xy,list) or len(xy)!=2 or any(type(n) is not int for n in xy) or not (100<xy[0]<1340 and 600<xy[1]<2900):
                raise ValueError('Word needs inspected 1440x3200 microphone coordinates')
            pcm16((base/step['wav']).resolve(strict=True))
    return plan


def clock_value(text):
    match=re.fullmatch(r'(\d{1,2}):([0-5]\d)\s*/\s*(\d{1,2}):([0-5]\d)',str(text))
    if not match:raise ValueError('Use the exact observed mm:ss/mm:ss playback clock')
    m,s,tm,ts=map(int,match.groups());current,total=m*60+s,tm*60+ts
    if not 0<=current<=total<=900 or total==0:raise ValueError('Expected a 1..900 second clip')
    return current,total


class Runner:
    def __init__(self,session,work,base,*,runtime=None,uid=None,skill=SKILL,capture_transcriber=None):
        self.session,self.client=session,session.client
        self.work,self.base=Path(work),Path(base)
        self.runtime,self.uid,self.skill=runtime,uid,skill
        self.stop=self.work/'pause.stop'
        self.reads=self.clicks=0
        self.transition_reads=self.rejected_clicks=0
        self.capture_transcriber=capture_transcriber

    def observe(self,wait_ms=0,*,during_wait=False):
        if self.stop.exists():raise Paused('Pause requested; inspect the checkpoint before moving the task')
        self.reads+=1
        try:self.client.observe(wait_ms=wait_ms)
        except BridgeError as error:
            if not during_wait or type(error) is not BridgeError or str(error)!='no_active_window':raise
            # Navigation can briefly remove the root; only bounded readback loops may wait.
            self.client.last=None;self.transition_reads+=1
            time.sleep(.15)
            return False
        if self.client.last['truncated']:raise BridgeError('Incomplete tree; no further action sent')
        return True

    def present(self,item):
        ref=self.client.select(item,missing_ok=True)
        return ref is not None and self.client.nodes[ref].get('enabled',False)

    def anchors(self,items):
        if not all(self.present(item) for item in items):raise BridgeError('Page/question anchor changed')

    def click_ref(self,item):
        if 'bounds' in item:
            matches=[ref for ref,row in self.client.nodes.items() if row.get('bounds')==item['bounds']]
            if len(matches)!=1:raise BridgeError('Inspected playback control is missing or ambiguous')
            ref=matches[0]
        else:ref=self.client.select(item)
        while ref:
            row=self.client.nodes.get(ref,{})
            if row.get('password') or row.get('enabled') is False:raise BridgeError('Disabled or private control')
            if row.get('clickable'):return ref
            ref=ref.rpartition('.')[0]
        raise BridgeError('No semantic clickable control; inspect the layout instead of guessing coordinates')

    def click(self,step):
        for attempt in range(3):
            self.observe();self.anchors(step['before'])
            if step['action']=='select' and self.selected(step['target']):return False
            if 'expect' in step and self.present(step['expect']):
                raise BridgeError('Postcondition already present; inspect progress before replaying')
            ref=self.click_ref(step['target'])
            self.clicks+=1
            try:result=self.client.click(self.client.last['snapshot_id'],ref)
            except BridgeError as error:
                # These exact server errors occur before performAction; unknown outcomes never retry.
                if type(error) is not BridgeError or str(error) not in ('ui_changed','node_changed'):raise
                self.rejected_clicks+=1
                if attempt==2:raise
                time.sleep(.1)
                continue
            if not result.get('dispatched'):raise OutcomeUnknown('Click not confirmed; do not replay')
            return True

    def wait(self,item,timeout,anchors=()):
        deadline=time.monotonic()+timeout
        while True:
            if self.observe(wait_ms=150,during_wait=True):
                self.anchors(anchors)
                if self.present(item):return
            if time.monotonic()>=deadline:raise OutcomeUnknown('Expected state not observed; do not replay')

    def selected(self,item):
        leaf=self.client.select(item)
        parent=self.click_ref(item)
        return any(self.client.nodes[r].get('checked') or self.client.nodes[r].get('selected') for r in (leaf,parent))

    def select(self,step):
        if not self.click(step):return {'already_selected':True}
        deadline=time.monotonic()+step.get('timeout',30)
        while True:
            if self.observe(wait_ms=150,during_wait=True):
                self.anchors(step['before'])
                if self.selected(step['target']):return {'selected':True}
            if time.monotonic()>=deadline:raise OutcomeUnknown('Selected state not exposed or not accepted; inspect, do not tap again')

    def capture(self,step,directory):
        playback=None
        if 'clock' in step:
            self.observe();self.anchors(step['before'])
            self.client.select({'text':step['clock']})
            playback=clock_value(step['clock'])
        directory.mkdir()
        output=directory/'audio'
        stop=output/'capture.stop'
        asr_stop=output/'transcription.stop';asr=None;transcription={}
        def transcribe():
            try:transcription.update(self.capture_transcriber(output/'capture.wav',asr_stop,output/'transcript.jsonl'))
            except Exception as error:transcription['error']=str(error)
        # Keep verbose events and PCM out of model-facing output.
        with (directory/'capture-process.log').open('wb') as log:
            process=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).with_name('target_audio_capture.py')),
                                      '--uid',str(self.uid),'--seconds',str(step['seconds']),
                                      '--output',str(output),'--skill',str(self.skill)],
                                     stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                deadline=time.monotonic()+15
                while not (output/'ready.json').is_file():
                    if process.poll() is not None or time.monotonic()>=deadline:raise BridgeError('Capture was not ready; playback not started')
                    if self.stop.exists():raise Paused('Paused before playback')
                    time.sleep(.1)
                ready=json.loads((output/'ready.json').read_text(encoding='utf-8'))
                if ready.get('uid')!=self.uid or ready.get('mode')!='capture':raise BridgeError('Unexpected capture identity')
                if self.capture_transcriber:
                    asr=threading.Thread(target=transcribe,daemon=True);asr.start()
                self.click(step)
                started=time.monotonic();advanced=False
                deadline=time.monotonic()+step.get('timeout',30)
                while True:
                    if self.observe(wait_ms=150,during_wait=True):
                        if playback:
                            self.anchors(step['before'])
                            timers=[row.get('text','') for row in self.client.nodes.values()
                                    if re.fullmatch(r'\d{1,2}:[0-5]\d\s*/\s*\d{1,2}:[0-5]\d',str(row.get('text','')))]
                            if len(timers)!=1:raise BridgeError('Playback clock missing or ambiguous; do not replay')
                            initial,total=playback;current,observed_total=clock_value(timers[0])
                            if observed_total!=total:raise BridgeError('Playback clip changed')
                            advanced=advanced or current>initial
                            # shortcut: this observed player resets to zero; other end states need live calibration.
                            if advanced and current==0:
                                if time.monotonic()-started<total-initial-1:
                                    raise OutcomeUnknown('Playback reset before the remaining media duration; inspect saved audio')
                                break
                        elif self.present(step['expect']):break
                    if process.poll() is not None:raise OutcomeUnknown('Capture ended before the playback postcondition')
                    if time.monotonic()>=deadline:raise OutcomeUnknown('Playback outcome unknown; do not replay')
            finally:
                # Let the device flush tail PCM and unregister the policy normally.
                try:
                    if output.is_dir():stop.touch(exist_ok=True)
                    try:process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.terminate();process.wait(timeout=5)
                        raise OutcomeUnknown('Capture release unknown; inspect device policy/watchdog')
                    result_path=output/'result.json'
                    if process.returncode or not result_path.is_file():raise BridgeError('Capture process failed; inspect its local log')
                    result=json.loads(result_path.read_text(encoding='utf-8'))
                    if result.get('policy_released') is not True:raise OutcomeUnknown('Capture policy release not confirmed')
                finally:
                    if asr:
                        asr_stop.touch();asr.join(timeout=45)
                        if sys.exc_info()[0] is None:
                            if asr.is_alive():raise BridgeError('Audio saved; transcription has not finished; do not replay')
                            if 'error' in transcription:raise BridgeError('Audio saved; transcription failed: '+transcription['error'])
        return {'audio_seconds':result['seconds'],'policy_released':True,'content_verified':False,**transcription}

    def run(self,plan):
        validate(plan,self.base)
        if any(s['action'] in ('word','capture') for s in plan['steps']):
            if self.runtime is None or type(self.uid) is not int or self.uid<10000:
                raise ValueError('Digital audio requires the inspected application UID and device runtime')
        self.work.mkdir(parents=True,exist_ok=False)
        report={'status':'running','completed':0,'total':len(plan['steps']),'steps':[]}
        began=time.monotonic()
        def save():
            report.update(elapsed_seconds=round(time.monotonic()-began,3),bridge_reads=self.reads,clicks=self.clicks,
                          transition_reads=self.transition_reads,rejected_clicks=self.rejected_clicks)
            temp=self.work/'checkpoint.tmp'
            temp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            temp.replace(self.work/'checkpoint.json')
        save()
        try:
            for index,step in enumerate(plan['steps']):
                self.observe();self.anchors(step['before'])
                report['in_flight']=index+1;save()
                started=time.monotonic();action=step['action'];result={}
                if action=='select':result=self.select(step)
                elif action=='capture':result=self.capture(step,self.work/f'step-{index+1:02}')
                else:
                    if action=='click':self.click(step)
                    elif action=='word':
                        if self.present(step['expect']):raise BridgeError('Word postcondition already present; no recording started')
                        wav=(self.base/step['wav']).resolve(strict=True)
                        result=inject_once(self.session,self.runtime,wav,self.uid,self.work/f'step-{index+1:02}',
                                           lambda:word_turn(self.session,wav,step['word'],*step['xy']))
                    self.wait(step['expect'],step.get('timeout',30))
                report['steps'].append({'index':index+1,'action':action,'elapsed_seconds':round(time.monotonic()-started,3),**result})
                report['completed']=index+1;report['in_flight']=None;save()
            report['status']='complete'
        except BaseException as error:
            report.update(status='paused' if isinstance(error,Paused) else 'needs_review',
                          error=type(error).__name__+': '+str(error),next='Read current app state; never replay uncertain actions')
            raise
        finally:save()
        return {k:report[k] for k in ('status','completed','total','elapsed_seconds','bridge_reads','clicks')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--port',type=int,default=8794)
    parser.add_argument('--display-id',type=int,default=0)
    parser.add_argument('--capture-display-id',type=int)
    parser.add_argument('--uid',type=int)
    parser.add_argument('--skill',type=Path,default=SKILL)
    parser.add_argument('--validate',action='store_true',help='Validate the plan/WAV files without contacting a device')
    args=parser.parse_args()
    base=args.plan.resolve(strict=True).parent
    plan=validate(json.loads(args.plan.read_text(encoding='utf-8-sig')),base)
    if args.validate:
        print(encode({'valid':True,'steps':len(plan['steps']),'device_contacted':False}));return
    if args.output is None:parser.error('--output is required; use a fresh work directory')
    if args.output.exists():parser.error('Output already exists; inspect its checkpoint instead of replaying')
    scripts=args.skill.resolve(strict=True)/'scripts';sys.path.insert(0,str(scripts))
    import runtime
    spec=importlib.util.spec_from_file_location('control',scripts/'ucampus-control.py')
    control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)
    session=Session(control,args.port,args.display_id)
    try:
        if args.display_id and any(s['action']=='word' for s in plan['steps']):
            if args.capture_display_id is None:parser.error('Secondary word recording requires the inspected SurfaceFlinger ID')
            control.adb=display_adb(control.adb,session.client,capture_id=args.capture_display_id)
        result=Runner(session,args.output,base,runtime=runtime,uid=args.uid,skill=args.skill).run(plan)
        print(encode({**result,'checkpoint':str(args.output/'checkpoint.json')}))
    except Paused as error:
        print(encode({'status':'paused','error':str(error),'checkpoint':str(args.output/'checkpoint.json')}))
        raise SystemExit(2)
    except Exception as error:
        print(encode({'status':'needs_review','error':str(error),'checkpoint':str(args.output/'checkpoint.json')}))
        raise SystemExit(1)
    finally:session.client.close()


if __name__=='__main__':main()
