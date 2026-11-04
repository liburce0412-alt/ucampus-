from pathlib import Path
import hashlib
import io
import itertools
import json
import tempfile
import types
import unittest
from unittest.mock import Mock,patch
from dialogue_session import Dialogue,progress,run,reply_audio_bounds,audio_duration_visible
from bridge_client import BridgeError


class DialogueTests(unittest.TestCase):
    def make(self,states):
        control=types.SimpleNamespace(adb=Mock(),play_audio=Mock())
        session=types.SimpleNamespace(control=control,check_audio=Mock())
        dialogue=Dialogue(session)
        dialogue.read=Mock(side_effect=states)
        dialogue.wait_audio_ready=Mock()
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
