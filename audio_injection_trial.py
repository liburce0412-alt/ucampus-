"""One reviewed dialogue response or Pre word through a bounded U-campus-only digital route."""
import argparse
import importlib.util
import json
from pathlib import Path
from project_paths import SKILL
import queue
import subprocess
import sys
import threading
import time
import wave

from dialogue_session import Dialogue
from screen_frames import ScreenFrames,display_adb
from training_session import Session

REMOTE_PCM='/data/local/tmp/ucampus-audio-input.pcm'
REMOTE_CLASSPATH='/data/local/tmp/ucampus-audio-probe.jar:/data/local/tmp/ucampus-audio-runtime.jar'


def pcm16(wav):
    import numpy as np
    with wave.open(str(wav),'rb') as stream:
        rate,channels=stream.getframerate(),stream.getnchannels()
        frames=stream.getnframes()
        if stream.getsampwidth()!=2 or channels not in (1,2) or not 8000<=rate<=96000 or not 0<frames/rate<=20:
            raise ValueError('Use a nonempty PCM16 mono/stereo response of at most 20 seconds')
        raw=stream.readframes(frames)
    if len(raw)!=frames*channels*2:raise ValueError('Incomplete source WAV')
    samples=np.frombuffer(raw,dtype='<i2').astype(np.float64).reshape(-1,channels).mean(axis=1)
    if np.sqrt(np.mean(samples*samples))<5:raise ValueError('Silent source WAV')
    if rate!=16000:samples=np.interp(np.arange(round(frames*16000/rate))*rate/16000,np.arange(frames),samples)
    return np.rint(samples).clip(-32768,32767).astype('<i2').tobytes()


class Injector:
    def __init__(self,adb,serial,uid,log):
        if type(uid) is not int or uid<10000:raise ValueError('Explicit application UID required')
        self.events=queue.Queue();self.log=log;self.sent=False;self.released=False;self.requires_release=False;self.closed=False
        self.process=subprocess.Popen([adb,'-s',serial,'shell','-T','CLASSPATH='+REMOTE_CLASSPATH,
                                       'app_process','/','local.ucampus.audio.InjectionProbe','inject',str(uid)],
                                      stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.thread=threading.Thread(target=self.read,daemon=True);self.thread.start()

    def read(self):
        for raw in self.process.stdout:
            try:row=json.loads(raw)
            except (ValueError,UnicodeDecodeError):continue
            self.log.write(json.dumps(row)+'\n');self.log.flush()
            if row.get('phase')=='ready' or row.get('phase')=='release-error' or row.get('stage') in ('create-track','wait-play','play','wait-stop'):
                self.requires_release=True
            if row.get('phase')=='released':self.released=row.get('policy_status')==1
            self.events.put(row)
        self.events.put({'phase':'eof'})

    def wait(self,phase,seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:row=self.events.get(timeout=min(.2,max(.001,deadline-time.monotonic())))
            except queue.Empty:continue
            if row.get('phase')==phase:return row
            if row.get('phase') in ('error','release-error','eof'):raise RuntimeError(json.dumps(row))
        raise TimeoutError('Audio injection '+phase+' was not observed')

    def play(self,wav):
        if self.sent:raise RuntimeError('Never replay this injection automatically')
        self.sent=True
        self.process.stdin.write(b'P');self.process.stdin.flush()
        self.wait('played',23)

    def close(self):
        if self.closed:return
        if self.process.poll() is None:
            try:self.process.stdin.write(b'X');self.process.stdin.flush();self.process.stdin.close()
            except (BrokenPipeError,OSError):pass
        try:self.process.wait(timeout=48)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            raise RuntimeError('Device watchdog outcome unknown; inspect target policy before continuing')
        finally:self.thread.join(timeout=2)
        self.closed=True
        if self.requires_release and not self.released:raise RuntimeError('Policy release was not confirmed; inspect routing before continuing')
        if self.process.returncode:raise RuntimeError('Injection process exited with '+str(self.process.returncode))


def word_ready(session,word):
    session.client.observe(full=True)
    texts={row.get('text') or row.get('description','') for row in session.client.nodes.values()}
    if session.client.last['truncated'] or not {'Pre-listening','跟读单词或短语',word,'点击跟读'}<=texts:
        raise ValueError('Expected the visible unrecorded Pre word; no recording was started')


def word_turn(session,wav,word,x,y):
    control=session.control
    word_ready(session,word)
    png=control.adb('exec-out','screencap','-p');control.assert_recording_profile(png)
    from PIL import Image
    import io
    if Image.open(io.BytesIO(png)).size!=(1440,3200) or not (100<x<1340 and 600<y<2900):
        raise ValueError('Word recording needs the inspected 1440x3200 layout and explicit coordinates')
    result={'word':word,'before':control.snapshot('injection-word-before',png)}
    started=time.monotonic();recording=False
    try:
        control.adb('shell','input','tap',str(x),str(y))
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            png=control.adb('exec-out','screencap','-p')
            if control.recording_at(png,x,y):
                recording=True
                result['recording_ready']=control.snapshot('injection-word-recording',png)
                break
            time.sleep(.15)
        else:raise RuntimeError('Word microphone did not enter recording; no audio played')
        control.play_audio(wav)
        time.sleep(.2)
    finally:
        # Stop only the still-visible calibrated microphone; never guess an end/next button.
        if recording:
            png=control.adb('exec-out','screencap','-p')
            if control.recording_at(png,x,y):control.adb('shell','input','tap',str(x),str(y))
    result.update(elapsed_seconds=round(time.monotonic()-started,3),
                  screenshot=control.snapshot('injection-word-after'),platform_acceptance='pending_app_readback')
    return result


def inject_once(session,runtime,wav,uid,output,action):
    """Own one temporary PCM and route, and always restore the caller's controls."""
    wav=Path(wav).resolve(strict=True);pcm=pcm16(wav)
    if type(uid) is not int or uid<10000:raise ValueError('Explicit application UID required')
    if ':' not in runtime.serial():raise ValueError('Use the authorized wireless connection')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    local=output/'input.pcm'
    with local.open('xb') as stream:stream.write(pcm)
    status=subprocess.run([runtime.adb_path(),'-s',runtime.serial(),'shell','test','-e',REMOTE_PCM],capture_output=True,timeout=15).returncode
    if status==0:raise RuntimeError('Temporary PCM already exists; inspect its ownership first')
    if status!=1:raise RuntimeError('Could not check the temporary PCM path')
    control=session.control
    normal_adb,normal_play,normal_check=control.adb,control.play_audio,session.check_audio
    try:
        # The exact path was absent; also remove a partial upload if push fails.
        runtime.adb('push',str(local),REMOTE_PCM)
        with (output/'injection.jsonl').open('x',encoding='utf-8') as log:
            injector=Injector(runtime.adb_path(),runtime.serial(),uid,log)
            try:
                if injector.wait('ready',12).get('uid')!=uid:raise RuntimeError('Unexpected route UID')
                control.play_audio=injector.play
                def check_prepared(path):
                    if Path(path).resolve()!=wav:raise ValueError('Unexpected speech file')
                session.check_audio=check_prepared
                def input_and_release(*command):
                    value=normal_adb(*command)
                    if command[:4]==('shell','input','motionevent','UP'):injector.close()
                    return value
                control.adb=input_and_release
                result=action()
            finally:injector.close()
        return {**result,'source':str(wav),'digital_pcm_bytes':len(pcm),'policy_released':True,
                'speaker_playback':False,'audio_content_verified':False}
    finally:
        control.adb,control.play_audio,session.check_audio=normal_adb,normal_play,normal_check
        runtime.adb('shell','rm','-f',REMOTE_PCM)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wav',type=Path)
    parser.add_argument('--uid',type=int,required=True)
    parser.add_argument('--port',type=int,default=8794)
    parser.add_argument('--skill',type=Path,default=SKILL)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--word',help='Inspected Pre headword; otherwise use the AI dialogue page')
    parser.add_argument('--xy',type=int,nargs=2,metavar=('X','Y'))
    parser.add_argument('--display-id',type=int,default=0)
    parser.add_argument('--scrcpy',type=Path)
    parser.add_argument('--capture-display-id',type=int)
    args=parser.parse_args()
    if bool(args.word)!=bool(args.xy):parser.error('--word and --xy must be provided together')
    if (not 0<=args.display_id<=2147483647 or (args.capture_display_id is not None and not args.display_id)
            or (args.display_id and not (args.word and (args.scrcpy or args.capture_display_id is not None)))):
        parser.error('Secondary injection requires an inspected Pre word and explicit capture; dialogue is calibrated only on display 0')
    wav=args.wav.resolve(strict=True);pcm16(wav)
    if args.output.exists():raise ValueError('Use a fresh evidence directory')
    scripts=args.skill.resolve(strict=True)/'scripts';sys.path.insert(0,str(scripts))
    import runtime
    if ':' not in runtime.serial():raise ValueError('This test must use the authorized hotspot wireless serial')
    spec=importlib.util.spec_from_file_location('control',scripts/'ucampus-control.py')
    control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)
    session=Session(control,args.port,args.display_id);dialogue=Dialogue(session)
    raw_adb=control.adb
    frames=None;result={'display_id':args.display_id}
    args.output.mkdir(parents=True)
    try:
        if args.display_id:
            session.client.observe(full=True)
            if args.capture_display_id is None:
                frames=ScreenFrames(runtime.adb_path(),runtime.serial(),args.scrcpy,args.display_id)
                frames.start()
            control.adb=display_adb(raw_adb,session.client,frames,args.capture_display_id)
            result['capture_display_id']=args.capture_display_id
        if args.word:
            word_ready(session,args.word)
        else:
            (current,target),texts,reply=dialogue.read()
            if current>=target:raise ValueError('There is no pending dialogue response')
            dialogue.wait_audio_ready(current,target,reply)
        control.assert_recording_profile(control.adb('exec-out','screencap','-p'))
        action=(lambda:word_turn(session,wav,args.word,*args.xy)) if args.word else (lambda:dialogue.turn(wav,current,target))
        result.update(inject_once(session,runtime,wav,args.uid,args.output,action))
    except Exception as error:
        result.update(error=str(error),next='Inspect current recording/progress; do not resend automatically')
        raise
    finally:
        try:
            if frames:frames.close()
        except Exception as error:
            result['display_cleanup_error']=str(error)
            raise
        finally:
            session.client.close()
            (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
