"""Bounded U-campus process-log metadata; never persists raw logs or credential values."""
import argparse
import datetime
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

PLUGINS = ('UCSOEPlugin','UCAudioPlugin','UCPcmPlugin','UCClioPlugin','UCHttpTaskPlugin','UCFooPlugin')
FIELDS = {'engineType','requestId','audioUrl','audioSOEUrl','recordLocalPath','duration','params','result',
          'scores','recordId','userId','status','action','method','taskId','exerciseId','progress','code'}
ROUTE_WORDS = {'api','portal_api','publish','latest','soe-prod','clio','speech-proxy',
               'training','progress','task','tasks','exercise','exercises','audio','media','course','courses'}


def metadata(line):
    events=[]
    plugin=next((p for p in PLUGINS if p in line),None)
    if plugin:
        fields=sorted(set(re.findall(r'"([A-Za-z][A-Za-z0-9_]*)\\*"\s*:',line)) & FIELDS)
        action='start' if 'StartWithCallback' in line else 'stop' if 'StopWithCallback' in line else None
        events.append({'kind':'plugin_log','plugin':plugin,'fields':fields,**({'action':action} if action else {})})
    for raw in re.findall(r'https?://[^\s"<>\\]+',line):
        try:
            url=urlsplit(raw.rstrip('),;}'))
            host=(url.hostname or '').lower()
        except ValueError:
            continue
        if host!='unipus.cn' and not host.endswith('.unipus.cn'):
            continue
        parts=[]
        for part in url.path.split('/'):
            if not part:parts.append('');continue
            suffix=Path(part).suffix
            if suffix not in ('.mp3','.wav','.mp4','.json','.html','.js'):suffix=''
            # Short lowercase path tokens can also identify a user or authenticate a request.
            parts.append(part if part in ROUTE_WORDS else '[id]'+suffix)
        events.append({'kind':'url_in_log','origin':url.scheme+'://'+host,'path':'/'.join(parts),
                       'scope':'log candidate; not proof of an HTTP request or callable API'})
    return events


def run(adb,serial,output,seconds):
    def pid():
        value=subprocess.check_output([adb,'-s',serial,'shell','pidof','cn.unipus.cloud'],timeout=15).decode().strip()
        values=value.split()
        if len(values)!=1 or not values[0].isdigit():raise RuntimeError('Require exactly one live U-campus main PID')
        return values[0]
    target_pid=pid()
    inbox=queue.Queue(maxsize=128)
    process=subprocess.Popen([adb,'-s',serial,'logcat','--pid='+target_pid,'-v','brief','-T','1'],
                             stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    def collect():
        while True:
            raw=process.stdout.readline(65537)
            if not raw:break
            if len(raw)>65536:
                while raw and not raw.endswith(b'\n'):raw=process.stdout.readline(65537)
                continue
            event=metadata(raw.decode('utf-8','replace'))
            if event:
                try:inbox.put_nowait(event)
                except queue.Full:pass
    reader=threading.Thread(target=collect,daemon=True)
    reader.start()
    count=0
    try:
        with output.open('x',encoding='utf-8') as stream:
            stream.write(json.dumps({'kind':'start','pid':target_pid,'seconds_limit':seconds,'raw_logs_saved':False})+'\n')
            stream.flush()
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline and (process.poll() is None or not inbox.empty()):
                try:events=inbox.get(timeout=min(.5,max(.01,deadline-time.monotonic())))
                except queue.Empty:continue
                for event in events:
                    event['time']=datetime.datetime.now().astimezone().isoformat()
                    stream.write(json.dumps(event,ensure_ascii=False)+'\n');count+=1
                stream.flush()
            stream.write(json.dumps({'kind':'end','events':count,'same_pid_at_end':pid()==target_pid})+'\n')
    finally:
        if process.poll() is None:process.terminate()
        process.wait(timeout=5)
        reader.join(timeout=2)
        process.stdout.close()
    return {'events':count,'output':str(output),'raw_logs_saved':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb',required=True)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=600)
    args=parser.parse_args()
    if not 1<=args.seconds<=1800:parser.error('Use 1..1800 seconds')
    if args.output.exists():parser.error('Use a fresh output file')
    print(json.dumps(run(args.adb,args.serial,args.output,args.seconds),ensure_ascii=False))
