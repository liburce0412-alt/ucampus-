import io
from pathlib import Path
import queue
import tempfile
import unittest
import wave
from unittest.mock import Mock,patch
from types import SimpleNamespace
import numpy as np
from audio_injection_trial import pcm16,Injector,inject_once,word_ready,word_turn,tap_turn,main


class InjectionTests(unittest.TestCase):
    def test_tap_recording_checks_current_page_before_any_input(self):
        session=SimpleNamespace(control=Mock())
        with self.assertRaisesRegex(ValueError,'current-page guard'):tap_turn(session,Path('speech.wav'),720,2600,None)
        guard=Mock(side_effect=RuntimeError('Different exercise'))
        with self.assertRaisesRegex(RuntimeError,'Different exercise'):tap_turn(session,Path('speech.wav'),720,2600,guard)
        session.control.adb.assert_not_called()

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
            for frames in (0,160,16000*21):
                with wave.open(str(path),'wb') as out:
                    out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(bytes(frames*2))
                with self.assertRaises(ValueError):pcm16(path)

    def test_long_pcm_requires_explicit_bound_and_preserves_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'speech.wav'
            for seconds in (90,120):
                with wave.open(str(path),'wb') as out:
                    out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
                    out.writeframes(b'\xe8\x03'*(16000*seconds))
                with self.assertRaises(ValueError):pcm16(path)
                self.assertEqual(len(pcm16(path,max_seconds=seconds)),32000*seconds)
                with self.assertRaises(ValueError):pcm16(path,max_seconds=seconds-1)
            with wave.open(str(path),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
                out.writeframes(b'\xe8\x03'*(16000*120+1))
            with self.assertRaises(ValueError):pcm16(path,max_seconds=120)

    def test_bad_duration_limit_fails_before_file_or_process_access(self):
        with patch('audio_injection_trial.subprocess.Popen') as start:
            for limit in (0,-1,121,True,20.5,'90'):
                with self.subTest(limit=limit):
                    with self.assertRaises(ValueError):pcm16('missing.wav',max_seconds=limit)
                    with self.assertRaises(ValueError):Injector('adb','device',10001,io.StringIO(),max_seconds=limit)
            start.assert_not_called()

    def test_native_bound_and_host_timeouts_match_without_replaying_unknown_result(self):
        for limit in (20,90,120):
            with self.subTest(limit=limit):
                process=SimpleNamespace(stdin=io.BytesIO(),stdout=io.BytesIO(),poll=Mock(return_value=None),
                                        wait=Mock(),returncode=0)
                with patch('audio_injection_trial.subprocess.Popen',return_value=process) as start,patch('audio_injection_trial.threading.Thread'):
                    injector=Injector('adb','device',10001,io.StringIO(),max_seconds=limit)
                arguments=start.call_args.args[0]
                self.assertEqual(arguments[arguments.index('inject'):],['inject','10001']+([] if limit==20 else [str(limit)]))
                injector.wait=Mock(side_effect=TimeoutError('delivery unknown'))
                with self.assertRaises(TimeoutError):injector.play(Path('speech.wav'))
                injector.wait.assert_called_once_with('played',limit+3)
                with self.assertRaisesRegex(RuntimeError,'Never replay'):injector.play(Path('speech.wav'))
                self.assertEqual(process.stdin.getvalue(),b'P')
                injector.close()
                process.wait.assert_called_once_with(timeout=limit+28)

    def test_ready_matches_capability_and_pcm_before_recording_action(self):
        pcm=b'\x01\x00'*(16000*90)
        legacy={'phase':'ready','uid':10001,'mode':'inject'}
        ready={**legacy,'max_supported_seconds':120,'max_seconds':90,'pcm_bytes':len(pcm),
               'duration_ms':90000,'rate':16000,'channels':1}
        cases=[(20,legacy,True),(90,ready,True),(90,legacy,False)]
        cases += [(90,{**ready,key:value},False) for key,value in
                  [('uid',10002),('mode','capture'),('max_supported_seconds',20),('max_seconds',20),
                   ('pcm_bytes',len(pcm)-2),('duration_ms',89999),('rate',48000),('channels',2)]]
        cases.append((90,{key:value for key,value in ready.items() if key!='duration_ms'},False))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wav=root/'speech.wav';wav.touch()
            for number,(limit,row,allowed) in enumerate(cases):
                with self.subTest(limit=limit,row=row):
                    normal_adb,normal_play,normal_check=Mock(),Mock(),Mock()
                    session=SimpleNamespace(control=SimpleNamespace(adb=normal_adb,play_audio=normal_play),check_audio=normal_check)
                    runtime=SimpleNamespace(serial=lambda:'device',adb_path=lambda:'adb',adb=Mock())
                    injector=Mock();injector.wait.return_value=row;action=Mock(return_value={'completed':True})
                    prepared=pcm if limit==90 else pcm[:32000]
                    with patch('audio_injection_trial.pcm16',return_value=prepared),patch('audio_injection_trial.Injector',return_value=injector),patch('audio_injection_trial.subprocess.run',return_value=SimpleNamespace(returncode=1)):
                        if allowed:
                            result=inject_once(session,runtime,wav,10001,root/str(number),action,max_seconds=limit)
                            action.assert_called_once_with()
                            self.assertTrue(result['policy_released'])
                            self.assertEqual(result['digital_audio_seconds'],len(prepared)/32000)
                        else:
                            with self.assertRaises(RuntimeError):inject_once(session,runtime,wav,10001,root/str(number),action,max_seconds=limit)
                            action.assert_not_called()
                    injector.close.assert_called_once_with()
                    self.assertIs(session.control.adb,normal_adb);self.assertIs(session.control.play_audio,normal_play)
                    self.assertIs(session.check_audio,normal_check)
                    self.assertFalse(hasattr(session.control,'release_audio'))
                    self.assertEqual(runtime.adb.call_args.args,('shell','rm','-f','/data/local/tmp/ucampus-audio-input.pcm'))

    def test_stream_play_is_single_use_and_waits_for_device_delivery(self):
        injector=Injector.__new__(Injector)
        injector.sent=False;injector.events=queue.Queue();injector.max_seconds=20
        injector.process=SimpleNamespace(stdin=io.BytesIO())
        injector.events.put({'phase':'played','bytes':32000})
        injector.play(Path('speech.wav'))
        self.assertEqual(injector.process.stdin.getvalue(),b'P')
        with self.assertRaises(RuntimeError):injector.play(Path('speech.wav'))

    def test_cleanup_failure_preserves_the_original_pre_recording_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wav=root/'speech.wav';wav.touch()
            control=SimpleNamespace(adb=Mock(),play_audio=Mock())
            session=SimpleNamespace(control=control,check_audio=Mock())
            runtime=SimpleNamespace(serial=lambda:'device',adb_path=lambda:'adb',adb=Mock())
            injector=Mock();injector.wait.return_value={'uid':10001,'mode':'inject'}
            injector.close.side_effect=RuntimeError('Probe exited after cancellation')
            error=ValueError('Recording coordinates changed; no input sent')
            with patch('audio_injection_trial.pcm16',return_value=b'\x01\x00'*160),patch('audio_injection_trial.Injector',return_value=injector),patch('audio_injection_trial.subprocess.run',return_value=SimpleNamespace(returncode=1)):
                with self.assertRaises(ValueError) as raised:
                    inject_once(session,runtime,wav,10001,root/'run',Mock(side_effect=error))
            self.assertIs(raised.exception,error)
            self.assertIn('cancellation',raised.exception.__notes__[0])
            self.assertEqual(runtime.adb.call_args.args,('shell','rm','-f','/data/local/tmp/ucampus-audio-input.pcm'))

    def test_device_error_cannot_be_treated_as_success(self):
        injector=Injector.__new__(Injector);injector.events=queue.Queue()
        injector.events.put({'phase':'error','stage':'register'})
        with self.assertRaises(RuntimeError):injector.wait('ready',1)

    def test_release_event_requires_unregistered_policy_status(self):
        injector=Injector.__new__(Injector)
        injector.events=queue.Queue();injector.log=io.StringIO()
        injector.requires_release=True;injector.released=False;injector.closed=False;injector.max_seconds=20
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
