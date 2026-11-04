"""Capture only U-campus playback through an explicitly prepared temporary UID audio policy."""
import argparse
import base64
import json
from pathlib import Path
from project_paths import SKILL
import subprocess
import sys
import threading
import wave

from audio_injection_trial import REMOTE_CLASSPATH


def stop_on_file(process,path,finished,report):
    while not finished.wait(.1) and process.poll() is None:
        if not path.is_file():continue
        try:
            process.stdin.write(b'X');process.stdin.flush()
            report['stop_requested']=True
        except (OSError,ValueError) as error:
            report['stop_request_error']=str(error)
        return


def receive(lines,wav,uid,seconds,log,on_ready):
    import numpy as np
    ready=released=completed=stopped=False
    total=peak=0;squares=0.0
    for line in lines:
        if len(line)>20000:raise ValueError('Oversized audio event')
        try:row=json.loads(line)
        except (ValueError,UnicodeDecodeError):continue
        phase=row.get('phase')
        if phase=='pcm':
            if not ready or completed or released:raise ValueError('PCM outside the active capture')
            raw=base64.b64decode(row['data'],validate=True)
            if not 0<len(raw)<=3200 or len(raw)%2 or total+len(raw)>16000*2*seconds+3200:
                raise ValueError('Invalid or excessive PCM')
            wav.writeframes(raw)
            values=np.frombuffer(raw,dtype='<i2').astype(np.float64)
            total+=len(raw);peak=max(peak,int(np.max(np.abs(values))));squares+=float(np.dot(values,values))
            continue
        log.write(json.dumps(row)+'\n');log.flush()
        if phase=='ready':
            if ready or row.get('uid')!=uid or row.get('mode')!='capture' or row.get('rate')!=16000 or row.get('channels')!=1:
                raise ValueError('Unexpected capture identity or format')
            ready=True;on_ready(row)
        elif phase=='captured':
            if not ready or completed or row.get('bytes')!=total:raise ValueError('Capture byte count mismatch')
            if type(row.get('stopped')) is not bool:raise ValueError('Missing capture stop state')
            stopped=row['stopped']
            completed=True
        elif phase=='released':
            if row.get('policy_status')!=1:raise ValueError('Audio policy is still registered')
            released=True
        elif phase in ('error','release-error'):raise RuntimeError(json.dumps(row))
    if not (ready and completed and released and (total or stopped)):raise RuntimeError('Capture or policy release was not confirmed')
    return {'bytes':total,'seconds':total/32000,'peak':peak,'rms':(squares/(total/2))**.5 if total else 0.0,
            'stopped':stopped,'policy_released':True,'content_verified':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uid',type=int,required=True)
    parser.add_argument('--seconds',type=int,default=30)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--stop-file',type=Path,help='Create this file to stop normally; default: OUTPUT/capture.stop')
    parser.add_argument('--skill',type=Path,default=SKILL)
    args=parser.parse_args()
    if args.uid<10000 or not 1<=args.seconds<=900 or args.output.exists():parser.error('Use an application UID, 1..900 seconds and a fresh output directory')
    stop_file=(args.stop_file or args.output/'capture.stop').resolve()
    if stop_file.exists():parser.error('The stop file already exists; use a fresh path')
    sys.path.insert(0,str(args.skill.resolve(strict=True)/'scripts'))
    import runtime
    serial=runtime.serial()
    if ':' not in serial:raise ValueError('Use the authorized wireless connection')
    args.output.mkdir(parents=True)
    result={'uid':args.uid,'requested_seconds':args.seconds,'stop_file':str(stop_file)};process=None
    finished=threading.Event();stop_report={'stop_requested':False};watcher=None
    try:
        with (args.output/'capture.wav').open('xb') as output, wave.open(output,'wb') as wav, (args.output/'events.jsonl').open('x',encoding='utf-8') as log:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'')
            process=subprocess.Popen([runtime.adb_path(),'-s',serial,'shell','-T','CLASSPATH='+REMOTE_CLASSPATH,
                                      'app_process','/','local.ucampus.audio.InjectionProbe','capture',str(args.uid),str(args.seconds)],
                                     stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            watcher=threading.Thread(target=stop_on_file,args=(process,stop_file,finished,stop_report),daemon=True)
            watcher.start()
            def ready(row):
                output.flush()
                ready_file=args.output/'ready.tmp'
                ready_file.write_text(json.dumps({**row,'stop_file':str(stop_file)})+'\n',encoding='utf-8')
                ready_file.replace(args.output/'ready.json')
                print(json.dumps({'capture_ready':True,'wav':str(args.output/'capture.wav'),'uid':args.uid,'stop_file':str(stop_file)}),flush=True)
            def lines():
                while True:
                    line=process.stdout.readline(20001)
                    if not line:break
                    yield line
                    output.flush()
            result.update(receive(lines(),wav,args.uid,args.seconds,log,ready))
            process.wait(timeout=5)
            if process.returncode:raise RuntimeError('Device capture process failed: '+str(process.returncode))
    except BaseException as error:
        result['error']=type(error).__name__+': '+str(error)
        raise
    finally:
        finished.set()
        if watcher:watcher.join(timeout=1)
        result.update(stop_report)
        if process is not None and process.poll() is None:
            try:process.stdin.close()
            except OSError:pass
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                result['cleanup_warning']='Host stopped; inspect device policy/watchdog before continuing'
        (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
