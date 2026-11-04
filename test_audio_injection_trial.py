import io
from pathlib import Path
import queue
import tempfile
import unittest
import wave
from unittest.mock import Mock,patch
from types import SimpleNamespace
import numpy as np
from audio_injection_trial import pcm16,Injector,word_ready,word_turn,main


class InjectionTests(unittest.TestCase):
    def test_secondary_cli_rejects_missing_capture_or_uncalibrated_dialogue(self):
        for extra in (['--display-id','-1'],['--display-id','7','--scrcpy','scrcpy.exe'],
                      ['--display-id','7','--word','test','--xy','720','2320']):
            with patch('sys.argv',['audio_injection_trial.py','missing.wav','--uid','10368','--output','fresh',*extra]),patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit) as error:main()
                self.assertEqual(error.exception.code,2)

    def test_stereo_resampling_preserves_duration_and_expected_mono_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'speech.wav'
            samples=np.tile(np.array([2000,-1000],dtype='<i2'),(800,1))
            with wave.open(str(path),'wb') as out:
                out.setnchannels(2);out.setsampwidth(2);out.setframerate(8000);out.writeframes(samples.tobytes())
            output=np.frombuffer(pcm16(path),dtype='<i2')
            self.assertEqual(len(output),1600)
            self.assertTrue(np.all(output==500))

    def test_silent_or_overlong_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'speech.wav'
            for frames in (160,16000*21):
                with wave.open(str(path),'wb') as out:
                    out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(bytes(frames*2))
                with self.assertRaises(ValueError):pcm16(path)

    def test_stream_play_is_single_use_and_waits_for_device_delivery(self):
        injector=Injector.__new__(Injector)
        injector.sent=False;injector.events=queue.Queue()
        injector.process=SimpleNamespace(stdin=io.BytesIO())
        injector.events.put({'phase':'played','bytes':32000})
        injector.play(Path('speech.wav'))
        self.assertEqual(injector.process.stdin.getvalue(),b'P')
        with self.assertRaises(RuntimeError):injector.play(Path('speech.wav'))

    def test_device_error_cannot_be_treated_as_success(self):
        injector=Injector.__new__(Injector);injector.events=queue.Queue()
        injector.events.put({'phase':'error','stage':'register'})
        with self.assertRaises(RuntimeError):injector.wait('ready',1)

    def test_release_event_requires_unregistered_policy_status(self):
        injector=Injector.__new__(Injector)
        injector.events=queue.Queue();injector.log=io.StringIO()
        injector.requires_release=True;injector.released=False;injector.closed=False
        injector.process=SimpleNamespace(stdout=io.BytesIO(b'{"phase":"released","policy_status":2}\n'),
                                         poll=lambda:0,wait=lambda **kwargs:None,returncode=0)
        injector.thread=Mock()
        injector.read()
        with self.assertRaisesRegex(RuntimeError,'release was not confirmed'):injector.close()

    def test_word_guard_rejects_changed_or_already_recorded_page(self):
        client=SimpleNamespace(observe=Mock(),last={'truncated':False},nodes={})
        session=SimpleNamespace(client=client)
        for words in (['Pre-listening','decline','点击跟读'],['Pre-listening','跟读单词或短语','other','点击跟读']):
            client.nodes={str(i):{'text':s} for i,s in enumerate(words)}
            with self.assertRaises(ValueError):word_ready(session,'decline')

    def test_word_stops_confirmed_recording_even_when_audio_fails(self):
        from PIL import Image
        stream=io.BytesIO();Image.new('RGB',(1440,3200)).save(stream,format='PNG')
        control=SimpleNamespace(adb=Mock(return_value=stream.getvalue()),assert_recording_profile=Mock(),
                                snapshot=Mock(return_value='test.png'),recording_at=Mock(return_value=True),
                                play_audio=Mock(side_effect=RuntimeError('device play failed')))
        with patch('audio_injection_trial.word_ready'):
            with self.assertRaisesRegex(RuntimeError,'device play failed'):
                word_turn(SimpleNamespace(control=control),Path('speech.wav'),'decline',720,2320)
        taps=[c.args for c in control.adb.call_args_list if c.args[:3]==('shell','input','tap')]
        self.assertEqual(len(taps),2)


if __name__=='__main__':unittest.main()
