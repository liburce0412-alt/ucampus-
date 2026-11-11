"""Persistent bridge integration for the existing, calibrated U-campus recording tools."""
import argparse
import importlib.util
import io
import json
from pathlib import Path
from project_paths import SKILL
import sys
import time
import wave
import xml.etree.ElementTree as ET

from bridge_client import Client, BridgeError


def xml_tree(nodes):
    """Retain ancestry for the legacy sentence matcher, including absent container nodes."""
    root = ET.Element('hierarchy', {'rotation': '0', 'coverage': 'visible_only'})
    elements = {}
    for ref in sorted(nodes, key=lambda value: [int(n) for n in value.split('.')]):
        parts = ref.split('.')
        for depth in range(1, len(parts) + 1):
            key = '.'.join(parts[:depth])
            if key not in elements:
                parent = elements.get('.'.join(parts[:depth-1]), root)
                elements[key] = ET.SubElement(parent, 'node')
        row = nodes[ref]
        b = row['bounds']
        elements[ref].attrib.update({
            'text': '[redacted]' if row.get('password') else row.get('text') or row.get('description', ''),
            'content-desc': '' if row.get('password') else row.get('description', ''),
            'resource-id': row.get('id', ''),
            'bounds': f'[{b[0]},{b[1]}][{b[2]},{b[3]}]',
            'enabled': str(row.get('enabled', False)).lower(),
            'clickable': str(row.get('clickable', False)).lower(),
        })
    return ET.tostring(root, encoding='utf-8')


def recording_phase(nodes):
    texts = [''.join(str(row.get('text') or row.get('description', '')).split()) for row in nodes.values()]
    if any('已提交' in text and '等待评分' in text for text in texts):
        return 'submitted'
    if any('录音中' in text and '点击提交' in text for text in texts):
        return 'recording'
    if any('准备中' in text for text in texts):
        return 'preparing'
    if '重录' in texts:
        return 'retry_available'
    return 'idle'


class Session:
    def __init__(self, control, port=8794, display_id=0):
        self.control = control
        self.client = Client(port, display_id=display_id)
        self.expected_text = None

    def read_xml(self):
        self.observe()
        if self.client.last['truncated']:
            raise BridgeError('Incomplete control tree; inspect with the original tool')
        return xml_tree(self.client.nodes)

    def observe(self,wait_ms=0):
        if self.client.display_id:self.client.observe_visible(wait_ms=wait_ms)
        else:self.client.observe(wait_ms=wait_ms)

    def state(self, wait_ms=0):
        self.observe(wait_ms)
        if self.client.last['truncated']:
            raise BridgeError('Incomplete recording state')
        texts = {row.get('text') or row.get('description', '') for row in self.client.nodes.values()}
        # The introduction scrolls offscreen; the expanded sentence remains visible.
        anchor = self.expected_text or '跟读以下句子。'
        if not {'范例学习', anchor} <= texts:
            raise BridgeError('Left the calibrated sentence repetition page')
        return recording_phase(self.client.nodes)

    def wait_phase(self, accepted, timeout, events):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            phase = self.state(wait_ms=150)
            if not events or events[-1]['phase'] != phase:
                events.append({'phase': phase, 'monotonic': time.monotonic()})
            if phase in accepted:
                return phase
        raise BridgeError('Recording state timed out; inspect current state before any retry')

    def check_audio(self, wav):
        import sounddevice as sd
        with wave.open(str(wav), 'rb') as stream:
            if stream.getsampwidth() != 2 or stream.getnframes() == 0:
                raise ValueError('Expected nonempty PCM16 speech')
            sd.check_output_settings(device=self.control.audio_device('OUTPUT'),
                                     channels=stream.getnchannels(), samplerate=stream.getframerate(), dtype='int16')

    def open_sentence(self,text):
        from workflow_session import Runner
        Runner(self,self.control.WORK,self.control.WORK).click({
            'action':'click','before':[{'text':'范例学习'},{'text':text}],'target':{'text':text}})

    def speak_turn(self, wav, x, y, expected_text):
        """Wait on live text; retain pixel calibration for the self-drawn microphone."""
        from PIL import Image
        wav = Path(wav).resolve(strict=True)
        self.check_audio(wav)
        self.expected_text = expected_text
        phase = self.state()
        nodes = self.client.nodes
        controls = [(ref, row) for ref, row in nodes.items() if row.get('text') in ('录音', '重录')]
        if len(controls) != 1 or phase not in ('idle', 'retry_available'):
            raise BridgeError('Expected exactly one idle recording control')
        ref, row = controls[0]
        parent = ref.rpartition('.')[0] + '.'
        if not any(key.startswith(parent) and item.get('text') == expected_text for key, item in nodes.items()):
            raise BridgeError('The expanded sentence changed; no recording was started')
        left, top, right, bottom = row['bounds']
        if ((x, y) != ((left + right)//2, (top + bottom)//2 - 187)
                or not (600 < y < 2900 and 0 <= left < right <= 1440 and top < bottom <= 3000)):
            raise BridgeError('Recording coordinates do not match the calibrated label')
        png = self.control.adb('exec-out', 'screencap', '-p')
        self.control.assert_recording_profile(png)
        events = []
        result = {'backend': 'bridge-text-with-pixel-gate', 'before': self.control.snapshot('before-speech', png)}
        started = time.monotonic()
        try:
            self.control.adb('shell', 'input', 'tap', str(x), str(y))
            self.wait_phase({'recording'}, 12, events)
            png = self.control.adb('exec-out', 'screencap', '-p')
            width = Image.open(io.BytesIO(png)).width
            xy = next(((a, b) for a, b in ((x, y), (width//2, y)) if self.control.recording_at(png, a, b)), None)
            if xy is None:
                raise BridgeError('Text indicates recording but the microphone is not calibrated; audio not played')
            result['recording_ready'] = self.control.snapshot('recording-ready', png)
            result['recording_xy'] = xy
            self.control.play_audio(wav)
            phase = self.state()
            result['still_recording_after_playback'] = phase == 'recording'
            if phase == 'recording':
                png = self.control.adb('exec-out', 'screencap', '-p')
                if not self.control.recording_at(png, *xy):
                    raise BridgeError('Recording control changed; stop outcome needs inspection')
                self.control.adb('shell', 'input', 'tap', *map(str, xy))
            elif phase not in ('submitted', 'retry_available'):
                raise BridgeError('Unexpected state after speech; inspect before retrying')
            release=getattr(self.control,'release_audio',None)
            if release is not None:
                self.wait_phase({'submitted','retry_available'},12,events)
                release()
            # Switching sentences during submission can discard the recording; wait for saved interaction, not a score.
            result['final_state'] = self.wait_phase({'retry_available'}, 30, events)
            result['platform_acceptance'] = 'pending_stage_check'
            result['screenshot'] = self.control.snapshot('speak-turn')
            result['status'] = 'recording-complete'
            return result
        except Exception as error:
            result.update(status='error', error=str(error), next='Inspect current recording; do not replay automatically')
            raise
        finally:
            result['elapsed_seconds'] = round(time.monotonic() - started, 3)
            result['states'] = [{'phase': e['phase'], 'seconds': round(e['monotonic']-started, 3)} for e in events]
            with (self.control.WORK/'bridge-recording.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8794)
    parser.add_argument('--skill', type=Path, default=SKILL)
    parser.add_argument('--display-id',type=int,default=0)
    parser.add_argument('--capture-display-id',type=int)
    parser.add_argument('--uid',type=int,help='Use the inspected U-campus UID for digital input')
    sub = parser.add_subparsers(dest='command', required=True)
    follow = sub.add_parser('followread')
    follow.add_argument('--start-at', type=int, default=1)
    follow.add_argument('--sentences',type=Path,help='Reviewed complete English sentence JSON array')
    record = sub.add_parser('speak-turn')
    record.add_argument('wav', type=Path)
    record.add_argument('x', type=int)
    record.add_argument('y', type=int)
    record.add_argument('--text', required=True)
    args = parser.parse_args()
    if args.display_id<0 or (args.uid is not None and args.uid<10000):parser.error('Invalid inspected display or application UID')
    if args.display_id and (args.capture_display_id is None or args.uid is None):
        parser.error('Secondary speaking requires an explicit capture display and UID audio route')
    from freeze_runtime import verify
    verify(Path(__file__).resolve().parent)
    scripts = args.skill.resolve(strict=True)/'scripts'
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location('followread', scripts/'followread.py')
    followread = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(followread)
    session = Session(followread.control, args.port,args.display_id)
    try:
        if args.display_id:
            from screen_frames import display_adb
            followread.control.adb=display_adb(followread.control.adb,session.client,capture_id=args.capture_display_id)
        if args.uid is not None:
            import runtime
            from audio_injection_trial import inject_once
            original=session.speak_turn
            def digital_turn(wav,x,y,text):
                def record():
                    record_x,record_y=x,y
                    if args.command=='followread':
                        _,_,label=followread.wait_expanded(session,text)
                        left,top,right,bottom=followread.bounds(label)
                        record_x,record_y=(left+right)//2,(top+bottom)//2-187
                    return original(wav,record_x,record_y,text)
                return inject_once(session,runtime,wav,args.uid,followread.control.WORK/f'injection-{time.time_ns()}',
                                   record)
            session.speak_turn=digital_turn
        if args.command == 'followread':
            sentences=json.loads(args.sentences.read_text(encoding='utf-8')) if args.sentences else None
            followread.run(bridge=session,start_at=args.start_at,sentences=sentences)
        else:
            print(json.dumps(session.speak_turn(args.wav,args.x,args.y,args.text),ensure_ascii=False))
    finally:
        session.client.close()


if __name__ == '__main__':
    main()
