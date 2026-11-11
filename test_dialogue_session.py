from pathlib import Path
import hashlib
import io
import itertools
import json
import tempfile
import types
import unittest
import wave
from unittest.mock import Mock,patch
from dialogue_session import Dialogue,progress,run,reply_audio_bounds,audio_duration_visible
from bridge_client import BridgeError


class DialogueTests(unittest.TestCase):
    def make(self,states):
        control=types.SimpleNamespace(adb=Mock(),play_audio=Mock())
        session=types.SimpleNamespace(control=control,check_audio=Mock())
        dialogue=Dialogue(session)
        dialogue.read=Mock(side_effect=states)
        dialogue.wait_audio_ready=Mock(side_effect=lambda current,target,reply:reply)
        return dialogue,control

    def test_requires_exact_progress_and_target_page(self):
        with self.assertRaises(BridgeError):progress({'0':{'text':'话轮数:1/10'}})
        with self.assertRaises(BridgeError):progress({'0':{'text':'AI口语对话'},'1':{'text':'话轮数:0/0'}})
        self.assertEqual(progress({'0':{'text':'AI口语对话'},'1':{'text':'话轮数：1/10'}})[0],(1,10))

    def test_accepted_turn_advances_once_and_releases_hold(self):
        old='What would you share online?';new='What have you learned from others?'
        d,c=self.make(itertools.chain([((0,10),{'按住说话'},old),((0,10),{'松开发送'},old)],
                     itertools.repeat(((1,10),{'按住说话'},new))))
        with patch('dialogue_session.time.sleep'),patch('dialogue_session.time.monotonic',side_effect=itertools.count(0,.1)):
            result=d.turn(Path('speech.wav'),0,10)
        self.assertEqual(result['accepted'],1)
        self.assertEqual([call.args[3] for call in c.adb.call_args_list],['DOWN','UP'])
        c.play_audio.assert_called_once()

    def test_read_failure_always_releases_and_never_replays(self):
        d,c=self.make([((0,10),{'按住说话'},'What would you share online?'),BridgeError('disconnected')])
        with self.assertRaises(BridgeError):d.turn(Path('speech.wav'),0,10)
        c.play_audio.assert_not_called()
        self.assertEqual([call.args[3] for call in c.adb.call_args_list],['DOWN','UP'])

    def test_unexpected_count_stops_after_one_recording(self):
        reply='What would you share online?'
        d,c=self.make([((0,10),{'按住说话'},reply),((0,10),{'松开'},reply),((2,10),{'按住说话'},reply)])
        with patch('dialogue_session.time.sleep'),self.assertRaises(BridgeError):d.turn(Path('speech.wav'),0,10)
        self.assertEqual(c.play_audio.call_count,1)

    def test_count_and_idle_label_without_new_reply_do_not_advance(self):
        reply='What would you share online?'
        d,c=self.make(itertools.chain([((0,10),{'按住说话'},reply),((0,10),{'松开'},reply)],
                     itertools.repeat(((1,10),{'按住说话'},reply))))
        with patch('dialogue_session.time.sleep'),patch('dialogue_session.time.monotonic',side_effect=itertools.count(0,1)):
            with self.assertRaisesRegex(BridgeError,'next recording readiness'):d.turn(Path('speech.wav'),0,10)
        self.assertEqual(c.play_audio.call_count,1)
        self.assertEqual([call.args[3] for call in c.adb.call_args_list],['DOWN','UP'])

    def test_final_turn_does_not_need_another_ai_question(self):
        reply='What would you share online?'
        d,c=self.make([((9,10),{'按住说话'},reply),((9,10),{'松开'},reply),((10,10),set(),reply)])
        with patch('dialogue_session.time.sleep'):
            self.assertEqual(d.turn(Path('speech.wav'),9,10)['accepted'],10)

    def test_secondary_dialogue_requires_and_uses_inspected_hold_position(self):
        reply='What would you share online?'
        d,c=self.make([((9,10),{'按住说话'},reply),((9,10),{'松开'},reply),((10,10),set(),reply)])
        d.session.client=types.SimpleNamespace(display_id=7)
        with self.assertRaisesRegex(ValueError,'hold position'):Dialogue(d.session)
        calibrated=Dialogue(d.session,[719,2990]);calibrated.read=d.read;calibrated.wait_audio_ready=d.wait_audio_ready
        with patch('dialogue_session.time.sleep'):calibrated.turn(Path('speech.wav'),9,10)
        self.assertEqual([call.args[-2:] for call in c.adb.call_args_list],[('719','2990'),('719','2990')])

    def test_digital_batch_reuses_session_and_routes_each_remaining_reply(self):
        from PIL import Image
        image=io.BytesIO();Image.new('RGB',(1440,3200)).save(image,format='PNG')
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);text='I enjoy sharing ideas with my friends.'
            wav=work/f'speech-{hashlib.sha256(text.encode()).hexdigest()[:20]}.wav'
            with wave.open(str(wav),'wb') as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\x00\x04'*1600)
            control=types.SimpleNamespace(WORK=work,adb=Mock(return_value=image.getvalue()),
                                          assert_recording_profile=Mock(),snapshot=Mock(return_value='result.png'))
            session=types.SimpleNamespace(control=control);runtime=Mock();dialogue=Mock()
            dialogue.read.side_effect=[((8,10),{'按住说话'},'Current reply.'),((8,10),{'按住说话'},'Current reply.'),
                                       ((9,10),{'按住说话'},'Next reply.')]
            dialogue.turn.side_effect=lambda audio,index,target:{'accepted':index+1,'target':target}
            with patch('dialogue_session.Dialogue',return_value=dialogue),patch('audio_injection_trial.inject_once',side_effect=lambda *args:args[-1]()) as injection:
                result=run(session,work,[text]*10,runtime=runtime,uid=10001,xy=[719,2990])
            self.assertTrue(result['complete']);self.assertEqual(injection.call_count,2)
            self.assertTrue(all(call.args[0] is session and call.args[1] is runtime for call in injection.call_args_list))
            self.assertEqual([call.args[1] for call in dialogue.turn.call_args_list],[8,9])

    def test_latest_reply_uses_tree_order_after_delta_updates(self):
        nodes={'0.10':{'text':'What have you learned from others?'},'0.2':{'text':'What would you share online?'},
               '0.0':{'text':'AI口语对话'},'0.1':{'text':'话轮数：1/10'}}
        self.assertEqual(progress(nodes)[2],'What have you learned from others?')

    def test_audio_duration_gate_distinguishes_empty_bubble_from_painted_duration(self):
        from PIL import Image,ImageDraw
        nodes={'0.2':{'text':'','clickable':True,'bounds':[243,1000,555,1169]},
               '0.3':{'text':'What would you share online?','bounds':[243,1200,1226,1400]}}
        bounds=reply_audio_bounds(nodes,nodes['0.3']['text'])
        image=Image.new('RGB',(1440,3200),'white');draw=ImageDraw.Draw(image)
        draw.rectangle((270,1050,310,1090),fill='black')
        raw=io.BytesIO();image.save(raw,format='PNG')
        self.assertFalse(audio_duration_visible(raw.getvalue(),bounds))
        draw.rectangle((370,1050,380,1080),fill='black')
        raw=io.BytesIO();image.save(raw,format='PNG')
        self.assertTrue(audio_duration_visible(raw.getvalue(),bounds))

    def test_missing_audio_duration_never_presses_record(self):
        d,c=self.make([((0,10),{'按住说话'},'What would you share online?')])
        d.wait_audio_ready.side_effect=BridgeError('AI audio is pending')
        with self.assertRaises(BridgeError):d.turn(Path('speech.wav'),0,10)
        c.adb.assert_not_called();c.play_audio.assert_not_called()

    def test_same_turn_reply_can_finish_streaming_before_audio_is_ready(self):
        reply='The finished AI message for this same turn.'
        control=types.SimpleNamespace(adb=Mock(return_value=b'frame'))
        session=types.SimpleNamespace(control=control,client=types.SimpleNamespace(nodes={}))
        dialogue=Dialogue(session)
        dialogue.read=Mock(side_effect=itertools.chain([((1,10),{'按住说话'},'The finished AI message')],
                            itertools.repeat(((1,10),{'按住说话'},reply))))
        with patch('dialogue_session.time.sleep'),patch('dialogue_session.time.monotonic',side_effect=itertools.count(0,.3)),patch('dialogue_session.reply_audio_bounds',return_value=[243,1000,555,1169]) as bounds,patch('dialogue_session.audio_duration_visible',return_value=True):
            self.assertEqual(dialogue.wait_audio_ready(1,10,'The finished'),reply)
        self.assertEqual(bounds.call_args.args[1],reply)
        self.assertTrue(all(call.args==('exec-out','screencap','-p') for call in control.adb.call_args_list))
        control.adb.reset_mock();dialogue.read=Mock(return_value=((2,10),{'按住说话'},reply))
        with self.assertRaisesRegex(BridgeError,'Dialogue changed'):dialogue.wait_audio_ready(1,10,reply)
        control.adb.assert_not_called()

    def test_current_bubble_duration_in_tree_avoids_screenshot_but_old_duration_does_not(self):
        reply='The current question is ready to answer.'
        for duration in ("7'",'',"0'"):
            with self.subTest(duration=duration):
                nodes={'0.0':{'clickable':True,'bounds':[243,600,555,769]},
                       '0.0.0':{'text':"9'"},
                       '0.2':{'clickable':True,'bounds':[243,1000,555,1169]},
                       '0.2.0':{'text':duration},'0.3':{'text':reply,'bounds':[243,1200,1226,1400]}}
                control=types.SimpleNamespace(adb=Mock(return_value=b'frame'))
                session=types.SimpleNamespace(control=control,client=types.SimpleNamespace(nodes=nodes))
                dialogue=Dialogue(session);dialogue.read=Mock(return_value=((1,10),{'按住说话'},reply))
                with patch('dialogue_session.time.sleep'),patch('dialogue_session.time.monotonic',side_effect=itertools.count(0,1)),patch('dialogue_session.audio_duration_visible',return_value=False):
                    if duration=="7'":
                        self.assertEqual(dialogue.wait_audio_ready(1,10,reply),reply)
                        control.adb.assert_not_called()
                    else:
                        with self.assertRaisesRegex(BridgeError,'duration is not ready'):dialogue.wait_audio_ready(1,10,reply)
                        self.assertTrue(all(call.args==('exec-out','screencap','-p') for call in control.adb.call_args_list))

    def test_failed_evidence_capture_does_not_hide_or_retry_recording_error(self):
        from PIL import Image
        image=io.BytesIO();Image.new('RGB',(1440,3200)).save(image,format='PNG')
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);text='I enjoy sharing ideas with my friends.'
            (work/f'speech-{hashlib.sha256(text.encode()).hexdigest()[:20]}.wav').touch()
            control=types.SimpleNamespace(WORK=work,adb=Mock(return_value=image.getvalue()),assert_recording_profile=Mock(),
                                          snapshot=Mock(side_effect=['start.png',OSError('capture unavailable')]))
            dialogue=Mock()
            dialogue.read.return_value=((9,10),{'按住说话'},'What would you share online?')
            dialogue.turn.side_effect=BridgeError('hold state missing')
            with patch('dialogue_session.Dialogue',return_value=dialogue):
                with self.assertRaisesRegex(BridgeError,'hold state missing'):
                    run(types.SimpleNamespace(control=control),work,[text]*10)
            event=json.loads((work/'bridge-dialogue.jsonl').read_text())
            self.assertEqual(event['evidence_error'],'capture unavailable')
            self.assertEqual(dialogue.turn.call_count,1)


if __name__=='__main__':unittest.main()
