"""Opt-in latest-frame capture using the already installed scrcpy server and PyAV."""
import argparse
import io
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
import threading
import time


def check_capture_display(normal,display_id,capture_id):
    if type(capture_id) is not int or not 0<capture_id<2**64:
        raise ValueError('Invalid SurfaceFlinger display id')
    windows=normal('shell','dumpsys','window','displays').decode('utf-8','replace')
    surfaces=normal('shell','dumpsys','SurfaceFlinger','--display-id').decode('utf-8','replace')
    logical={int(n) for n in re.findall(r'Display:\s+mDisplayId=(\d+)\b',windows)}-{0}
    virtual=re.findall(r'^Display (\d+) \(Virtual display\): displayName="([^"]*)"',surfaces,re.M)
    # shortcut: accept only one virtual screen; multiple virtual displays need a stronger identity mapping.
    if logical!={display_id} or virtual!=[(str(capture_id),'scrcpy')]:
        raise ValueError('Secondary logical/SurfaceFlinger display mapping is missing or ambiguous')


def check_display_visible(normal,client):
    windows=client.rpc('windows').get('windows',[])
    focused=[w for w in windows if w.get('display_id')==client.display_id and w.get('focused')]
    if len(focused)!=1:raise RuntimeError('U-campus is not the focused secondary window')
    state=normal('shell','dumpsys','activity','activities').decode('utf-8','replace')
    line=re.search(r'mVisibleWindowsWithDisplayId:\[(.*)',state)
    screen=re.search(r'Display'+str(client.display_id)+r':\[([^\]]*)\]',line.group(1)) if line else None
    if not screen or 'cn.unipus.cloud' not in screen.group(1):
        raise RuntimeError('U-campus is not visible on the secondary display')
    dump=normal('shell','dumpsys','window','windows').decode('utf-8','replace')
    visible=False
    for block in re.split(r'(?=\n\s*Window #\d+ Window\{)',dump):
        if not re.search(r'(?:mDisplayId|displayId)='+str(client.display_id)+r'\b',block):continue
        if not all(re.search(r'\b'+key+r'=true\b',block) for key in ('mHasSurface','isOnScreen','isVisible')):continue
        header=next((row for row in block.splitlines() if re.search(r'Window #\d+ Window\{',row)),'')
        kind=re.search(r'\bty=([^\s,}]+)',block)
        if 'cn.unipus.cloud' in header:visible=True
        elif not kind or kind.group(1) not in {'STATUS_BAR','NAVIGATION_BAR','WALLPAPER'}:
            raise RuntimeError('Another visible window covers the secondary display')
    if not visible:raise RuntimeError('U-campus surface is not visible')


def display_adb(normal,client,frames=None,capture_id=None):
    if client.display_id<=0 or (frames is not None and frames.display_id!=client.display_id):
        raise ValueError('The control tree and video must identify the same secondary display')
    if capture_id is not None:check_capture_display(normal,client.display_id,capture_id)
    elif frames is None:raise ValueError('An explicit secondary capture is required')
    def command(*args):
        check_display_visible(normal,client)
        if args==('exec-out','screencap','-p'):
            if capture_id is not None:
                client.observe(full=True)
                if client.last['truncated']:raise RuntimeError('Incomplete secondary display; no screenshot read')
                png=normal('exec-out','screencap','-p','-d',str(capture_id))
            else:png=frames.png(after=time.monotonic())
            check_display_visible(normal,client)
            return png
        if len(args)==5 and args[:3]==('shell','input','tap'):
            client.observe(full=True)
            if client.last['truncated']:raise RuntimeError('Incomplete secondary display; no tap sent')
            return normal('shell','input','-d',str(client.display_id),*args[2:])
        raise ValueError('Secondary word probe permits only explicit-display screenshots and taps')
    return command


class FrameCache:
    def __init__(self):
        self.condition=threading.Condition()
        self.latest=None
        self.received=0.0
        self.count=0
        self.error=None

    def put(self,image):
        with self.condition:
            self.latest=image
            self.received=time.monotonic()
            self.count+=1
            self.condition.notify_all()

    def png(self,after=0.0,max_age=.5,timeout=4):
        deadline=time.monotonic()+timeout
        with self.condition:
            while True:
                now=time.monotonic()
                if self.error:raise RuntimeError(self.error)
                if self.latest is not None and self.received>=after and now-self.received<=max_age:
                    image=self.latest
                    break
                if now>=deadline:raise TimeoutError('No sufficiently fresh video frame; use a new ADB screenshot')
                self.condition.wait(min(.2,deadline-now))
        output=io.BytesIO()
        image.save(output,format='PNG')
        return output.getvalue()


class ScreenFrames:
    def __init__(self,adb,serial,scrcpy,display_id=0):
        if type(display_id) is not int or not 0<=display_id<=2147483647:
            raise ValueError('Invalid explicit display id')
        self.display_id=display_id
        self.adb,self.serial=adb,serial
        self.scrcpy=Path(scrcpy).resolve(strict=True)
        self.server=self.scrcpy.with_name('scrcpy-server')
        if not self.server.is_file():raise FileNotFoundError('Matching scrcpy-server is missing')
        version=subprocess.check_output([str(self.scrcpy),'--version'],timeout=10).decode('utf-8','replace')
        match=re.search(r'scrcpy (\d+(?:\.\d+)+)',version)
        if not match:raise RuntimeError('Cannot identify the installed scrcpy version')
        self.version=match.group(1)
        self.scid=f'{secrets.randbits(31):08x}'
        self.remote=f'/data/local/tmp/ucampus-frames-{self.scid}.jar'
        self.port=None
        self.process=None
        self.socket=None
        self.reader=None
        self.thread=None
        self.cache=FrameCache()
        self.closed=False
        self.pushed=False

    def command(self,*args):
        return subprocess.check_output([self.adb,'-s',self.serial,*args],timeout=20)

    def start(self):
        try:
            self.command('get-state')
            self.command('push',str(self.server),self.remote)
            self.pushed=True
            self.port=int(self.command('forward','--no-rebind','tcp:0','localabstract:scrcpy_'+self.scid).strip())
            args=[self.adb,'-s',self.serial,'shell','CLASSPATH='+self.remote,'app_process','/',
                  'com.genymobile.scrcpy.Server',self.version,'scid='+self.scid,'log_level=error',
                  'tunnel_forward=true','audio=false','control=false','cleanup=false','raw_stream=true',
                  'max_size=0','max_fps=5','video_bit_rate=2000000','video_codec=h264',
                  'display_id='+str(self.display_id)]
            self.process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                connection=None
                try:
                    connection=socket.create_connection(('127.0.0.1',self.port),timeout=.5)
                    if connection.recv(1,socket.MSG_PEEK)!=b'\x00':raise ConnectionError('H.264 stream not ready')
                    connection.settimeout(5)
                    self.socket=connection
                    break
                except (OSError,ConnectionError):
                    if connection:connection.close()
                    if self.process.poll() is not None:raise RuntimeError('scrcpy frame server exited before video started')
                    time.sleep(.1)
            if self.socket is None:raise TimeoutError('scrcpy video connection timed out')
            self.reader=self.socket.makefile('rb',buffering=0)
            self.thread=threading.Thread(target=self.decode,daemon=True)
            self.thread.start()
            self.cache.png(timeout=8)
            return self
        except Exception:
            self.close()
            raise

    def decode(self):
        import av
        try:
            with av.open(self.reader,format='h264',options={'probesize':'32','analyzeduration':'0','flags':'low_delay'}) as stream:
                for frame in stream.decode(video=0):
                    if self.closed:break
                    self.cache.put(frame.to_image())
        except Exception as error:
            if not self.closed:
                with self.cache.condition:
                    self.cache.error='Video decode stopped: '+type(error).__name__
                    self.cache.condition.notify_all()

    def png(self,after=0.0):
        # shortcut: freshness is host receive time; retain UI/pixel guards and do not use this as device event timing.
        return self.cache.png(after=after)

    def close(self):
        self.closed=True
        if self.socket:
            try:self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        if self.thread:self.thread.join(timeout=6)
        if self.reader:self.reader.close()
        if self.socket:self.socket.close()
        if self.process:
            if self.process.poll() is None:self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        if self.port:
            self.command('forward','--remove','tcp:'+str(self.port))
            self.port=None
        if self.pushed:
            self.command('shell','rm','-f',self.remote)
            self.pushed=False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb',required=True)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--scrcpy',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--display-id',type=int,default=0)
    args=parser.parse_args()
    if args.output.exists():parser.error('Use a fresh output directory')
    args.output.mkdir(parents=True)
    frames=ScreenFrames(args.adb,args.serial,args.scrcpy,args.display_id)
    try:
        started=time.monotonic()
        frames.start()
        startup=time.monotonic()-started
        samples=[]
        for i in range(10):
            began=time.monotonic()
            png=frames.png(after=began)
            samples.append({'milliseconds':(time.monotonic()-began)*1000,'png_bytes':len(png),
                            'received_age_ms':(time.monotonic()-frames.cache.received)*1000})
            if i==9:(args.output/'last-frame.png').write_bytes(png)
        result={'startup_seconds':startup,'samples':samples,'decoded_frames':frames.cache.count,
                'freshness':'host receive time, not device capture timestamp','audio':False,'control':False,
                'display_id':args.display_id}
        (args.output/'benchmark.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result))
    finally:
        frames.close()


if __name__=='__main__':main()
