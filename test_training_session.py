import io
import os
from pathlib import Path
from project_paths import SKILL
import tempfile
import types
import sys
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from PIL import Image
from bridge_client import BridgeError
from training_session import Session, recording_phase, xml_tree


def idle_nodes():
    return {
        '0.1.0.0': {'text':'Example sentence.', 'bounds':[100,780,1100,950]},
        '0.1.4': {'text':'录音','bounds':[663,1278,776,1353]},
    }


class TrainingSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.wav = self.work/'speech.wav'
        self.wav.write_bytes(b'test audio preflight is mocked')
        png = io.BytesIO()
        Image.new('RGB',(1440,3200)).save(png,'PNG')
        self.control = types.SimpleNamespace(
            WORK=self.work, adb=Mock(return_value=png.getvalue()),
            assert_recording_profile=Mock(), recording_at=Mock(return_value=True),
            snapshot=Mock(return_value='evidence.png'), play_audio=Mock())
        self.session = Session(self.control)
        self.session.check_audio = Mock()
        self.session.client.nodes = idle_nodes()

    def test_xml_keeps_ancestry_negative_bounds_and_password_redaction(self):
        root=ET.fromstring(xml_tree({'0.2.1': {'text':'secret','password':True,'bounds':[-5,0,20,40]}}))
        self.assertEqual(root.attrib['coverage'],'visible_only')
        node=root.find('node/node/node')
        self.assertEqual(node.attrib['text'],'[redacted]')
        self.assertEqual(node.attrib['bounds'],'[-5,0][20,40]')

    def test_preparing_recording_and_submitted_are_distinct(self):
        for text,phase in [('准备中…','preparing'),('录音中 点击提交','recording'),('已提交 等待评分','submitted')]:
            self.assertEqual(recording_phase({'0':{'text':text}}),phase)

    def test_speech_waits_for_recording_and_stops_once(self):
        observed=[]
        states=iter(['idle','preparing','preparing','recording','recording','submitted','retry_available'])
        def state(**kwargs):
            value=next(states);observed.append(value);return value
        self.session.state=Mock(side_effect=state)
        self.control.play_audio.side_effect=lambda _:self.assertEqual(observed[-1],'recording')
        result=self.session.speak_turn(self.wav,719,1128,'Example sentence.')
        taps=[c for c in self.control.adb.call_args_list if c.args[:3]==('shell','input','tap')]
        self.assertEqual(len(taps),2)
        self.assertEqual(result['final_state'],'retry_available')
        self.assertEqual(result['platform_acceptance'],'pending_stage_check')
        self.control.play_audio.assert_called_once()

    def test_auto_submission_does_not_start_recording_again(self):
        self.session.state=Mock(side_effect=['idle','recording','submitted','retry_available'])
        self.session.speak_turn(self.wav,719,1128,'Example sentence.')
        self.assertEqual(sum(c.args[:3]==('shell','input','tap') for c in self.control.adb.call_args_list),1)

    def test_digital_route_releases_after_submission_before_saving_wait(self):
        observed=[]
        phases=iter(['idle','recording','recording','submitted','submitted','retry_available'])
        def state(**kwargs):
            value=next(phases);observed.append(value);return value
        self.session.state=Mock(side_effect=state)
        self.control.release_audio=Mock(side_effect=lambda:self.assertEqual(observed,['idle','recording','recording','submitted']))
        result=self.session.speak_turn(self.wav,719,1128,'Example sentence.')
        self.control.release_audio.assert_called_once_with()
        self.assertEqual(result['final_state'],'retry_available')

    def test_preparation_timeout_does_not_play_audio(self):
        self.session.state=Mock(return_value='idle')
        self.session.wait_phase=Mock(side_effect=BridgeError('timed out'))
        with self.assertRaises(BridgeError):
            self.session.speak_turn(self.wav,719,1128,'Example sentence.')
        self.control.play_audio.assert_not_called()

    def test_wrong_sentence_or_coordinate_starts_nothing(self):
        self.session.state=Mock(return_value='idle')
        for text,x in [('Other sentence.',719),('Example sentence.',700)]:
            with self.assertRaises(BridgeError):
                self.session.speak_turn(self.wav,x,1128,text)
        self.control.adb.assert_not_called()

    def test_bottom_recording_uses_full_label_but_rejects_cropped_control(self):
        self.session.client.nodes['0.1.4']['bounds']=[663,2797,776,2872]
        self.session.state=Mock(side_effect=['idle','recording','recording','submitted','retry_available'])
        with patch.dict(os.environ,{'UCAMPUS_SKIP_SCORE':'1'}):
            self.session.speak_turn(self.wav,719,2647,'Example sentence.')
        self.control.adb.assert_any_call('shell','input','tap','719','2647')
        self.control.adb.reset_mock()
        self.session.client.nodes['0.1.4']['bounds']=[663,2970,776,3045]
        self.session.state=Mock(return_value='idle')
        with self.assertRaisesRegex(BridgeError,'coordinates'):
            self.session.speak_turn(self.wav,719,2820,'Example sentence.')
        self.control.adb.assert_not_called()

    def test_pixel_mismatch_blocks_audio(self):
        self.session.state=Mock(side_effect=['idle','recording'])
        self.control.recording_at.return_value=False
        with self.assertRaises(BridgeError):
            self.session.speak_turn(self.wav,719,1128,'Example sentence.')
        self.control.play_audio.assert_not_called()

    def test_wait_is_bounded_and_does_not_accept_preparing(self):
        self.session.state=Mock(return_value='preparing')
        with patch('training_session.time.monotonic',side_effect=[0,1,2,3]):
            with self.assertRaises(BridgeError):
                self.session.wait_phase({'recording'},3,[])
        self.session.state.assert_called_once()

    def test_unknown_stop_result_is_never_replayed(self):
        self.session.state=Mock(side_effect=['idle','recording','recording'])
        original=self.control.adb.return_value
        tap_count=0
        def adb(*args):
            nonlocal tap_count
            if args[:3]==('shell','input','tap'):
                tap_count+=1
                if tap_count==2:raise OSError('lost stop response')
            return original
        self.control.adb.side_effect=adb
        with self.assertRaises(OSError):
            self.session.speak_turn(self.wav,719,1128,'Example sentence.')
        self.assertEqual(tap_count,2)

    def load_followread(self):
        scripts=SKILL / 'scripts'
        source=(scripts/'followread.py').read_text(encoding='utf-8')
        module=types.ModuleType('followread_check')
        module.__file__=str(scripts/'followread.py')
        with patch.dict(os.environ,{'UCAMPUS_WORK':str(self.work)}),patch.object(sys,'path',[str(scripts),*sys.path]):
            exec(compile(source,module.__file__,'exec'),module.__dict__)
        module.control.adb=Mock()
        module.control.snapshot=Mock(return_value='failure.png')
        return module

    def test_followread_preserves_full_inventory_and_explicit_resume(self):
        module=self.load_followread()
        root=ET.fromstring('<hierarchy><node text="跟读以下句子。"/><node text="First."/><node text="Second."/><node text="Third."/></hierarchy>')
        module.ui=Mock(return_value=(root,{}))
        module.score=Mock(return_value=1)
        module.WORK=self.work
        module.STATE=self.work/'result.json'
        module.run(bridge=Mock(),start_at=2)
        module.ui.assert_called_once_with()
        import json
        saved=json.loads(module.STATE.read_text(encoding='utf-8'))
        self.assertNotIn('First.',saved)
        self.assertEqual(saved['Second.']['index'],2)
        with self.assertRaises(ValueError):module.run(start_at=4)
        self.assertEqual(module.bounds(ET.fromstring('<node bounds="[-10,20][30,40]"/>')),[-10,20,30,40])

    def test_scroll_waits_for_stable_target_before_using_coordinates(self):
        module=self.load_followread()
        trees=[ET.fromstring(f'<node text="Next." bounds="[135,{top}][1106,{top+173}]"/>')
               for top in (1800,1400,1147,1147)]
        module.ui=Mock(side_effect=[(tree,{}) for tree in trees])
        with patch.object(module.time,'sleep'):
            root,_=module.settled_ui(Mock(),'Next.')
        self.assertEqual(module.bounds(root),[135,1147,1106,1320])
        self.assertEqual(module.ui.call_count,4)
        module.control.adb.assert_not_called()

    def test_secondary_followread_requires_inventory_before_any_screen_read(self):
        module=self.load_followread();module.ui=Mock()
        bridge=types.SimpleNamespace(client=types.SimpleNamespace(display_id=7))
        with self.assertRaisesRegex(ValueError,'complete sentence list'):module.run(bridge=bridge)
        for sentences in ([],['Same.','Same.'],['not a complete sentence'],[123]):
            with self.assertRaises(ValueError):module.run(bridge=bridge,sentences=sentences)
        module.ui.assert_not_called()

    def test_secondary_fast_resume_uses_inventory_and_bridge_without_score_reads(self):
        module=self.load_followread();module.FAST=True
        root=ET.fromstring('<hierarchy><node text="范例学习"/><node text="跟读以下句子。"/>'
                          '<node><node text="Second." bounds="[135,900][1106,1125]"/>'
                          '<node text="录音" bounds="[663,1447][776,1522]"/></node></hierarchy>')
        parents={child:parent for parent in root.iter() for child in parent}
        module.ui=Mock(return_value=(root,parents));module.score=Mock(side_effect=AssertionError('No score checks in fast mode'))
        module.STATE=self.work/'secondary-results.json'
        import hashlib,json
        (self.work/f'speech-{hashlib.sha256(b"Second.").hexdigest()[:20]}.wav').touch()
        metadata={'platform_acceptance':'pending_stage_check','digital_audio_seconds':2.5,
                  'digital_pcm_bytes':80000,'policy_released':True,'speaker_playback':False}
        bridge=types.SimpleNamespace(client=types.SimpleNamespace(display_id=7),
                                     speak_turn=Mock(return_value={'screenshot':'result.png',**metadata}))
        module.run(bridge=bridge,start_at=2,sentences=['First.','Second.'])
        self.assertTrue(all(call.args==(bridge,) for call in module.ui.call_args_list))
        module.score.assert_not_called();bridge.speak_turn.assert_called_once()
        self.assertEqual(bridge.speak_turn.call_args.args[1:],(719,1297,'Second.'))
        saved=json.loads(module.STATE.read_text())['Second.']
        self.assertEqual(saved['index'],2)
        self.assertEqual({key:saved[key] for key in metadata},metadata)

    def test_missing_visible_sentence_returns_to_bounded_scroll_without_clicking(self):
        module=self.load_followread();root=ET.fromstring('<node text="Different."/>')
        module.ui=Mock(return_value=(root,{}))
        self.assertIs(module.settled_ui(Mock(),'Next.')[0],root)
        module.control.adb.assert_not_called()

    def test_last_fully_visible_row_opens_before_app_repositions_record_control(self):
        module=self.load_followread();module.FAST=True
        root=ET.fromstring('<hierarchy><node text="范例学习"/>'
                          '<node text="Last." bounds="[135,2651][1106,2910]"/></hierarchy>')
        opened=ET.fromstring('<node><node text="Last." bounds="[135,2133][1106,2475]"/>'
                            '<node text="录音" bounds="[663,2797][776,2872]"/></node>')
        parents={child:opened for child in opened}
        module.ui=Mock(return_value=(root,{}));module.settled_ui=Mock(return_value=(root,{}))
        module.wait_expanded=Mock(return_value=(opened,parents,opened[1]))
        module.STATE=self.work/'last-result.json'
        import hashlib
        (self.work/f'speech-{hashlib.sha256(b"Last.").hexdigest()[:20]}.wav').touch()
        bridge=types.SimpleNamespace(client=types.SimpleNamespace(display_id=7),open_sentence=Mock(),
                                     speak_turn=Mock(return_value={'screenshot':'result.png'}))
        module.run(bridge=bridge,sentences=['Last.'])
        bridge.open_sentence.assert_called_once_with('Last.')
        self.assertEqual(bridge.speak_turn.call_args.args[1:],(719,2647,'Last.'))
        module.control.adb.assert_not_called()

    def test_sentence_open_reuses_guarded_semantic_click(self):
        self.session.control.WORK=self.work
        with patch('workflow_session.Runner') as runner:
            self.session.open_sentence('Current sentence.')
        step=runner.return_value.click.call_args.args[0]
        self.assertEqual(step['target'],{'text':'Current sentence.'})
        self.assertIn({'text':'范例学习'},step['before'])

    def test_clipped_last_expansion_scrolls_once_without_repeating_click(self):
        module=self.load_followread()
        clipped=ET.fromstring('<node text="Last." bounds="[135,2651][1106,2992]"/>')
        opened=ET.fromstring('<node><node text="Last." bounds="[135,1551][1106,1892]"/>'
                            '<node text="录音" bounds="[663,2247][776,2322]"/></node>')
        visible=(opened,{child:opened for child in opened})
        module.ui=Mock(side_effect=[(clipped,{}),visible,visible])
        _,_,label=module.wait_expanded(Mock(),'Last.')
        self.assertEqual(label.get('text'),'录音')
        module.control.adb.assert_called_once_with('shell','input','swipe','1150','2400','1150','1300','400')

    def test_expansion_wait_is_read_only_and_rejects_previous_sentence(self):
        module=self.load_followread()
        def tree(sentence):
            root=ET.fromstring(f'<node><node text="{sentence}"/><node text="录音"/></node>')
            return root,{child:root for child in root}
        old,new=tree('Previous.'),tree('Next.')
        module.ui=Mock(side_effect=[old,old,new,new])
        with patch.object(module.time,'sleep'):
            root,_,label=module.wait_expanded(Mock(),'Next.')
        self.assertIs(root,new[0])
        self.assertEqual(label.get('text'),'录音')
        module.control.adb.assert_not_called()
        module.ui=Mock(return_value=old)
        with patch.object(module.time,'monotonic',side_effect=[0,5]):
            with self.assertRaisesRegex(RuntimeError,'no tap retried'):
                module.wait_expanded(Mock(),'Next.')
        module.control.adb.assert_not_called()
        module.control.snapshot.assert_called_once()

    def test_record_label_must_be_stable_after_expansion(self):
        module=self.load_followread()
        def tree(y):
            root=ET.fromstring(f'<node><node text="Current." bounds="[135,900][1106,1125]"/>'
                               f'<node text="录音" bounds="[663,{y}][776,{y+75}]"/></node>')
            return root,{child:root for child in root}
        before,after=tree(1900),tree(1500)
        module.ui=Mock(side_effect=[before,after,after])
        with patch.object(module.time,'sleep'):
            _,_,label=module.wait_expanded(Mock(),'Current.')
        self.assertEqual(module.bounds(label),[663,1500,776,1575])
        self.assertEqual(module.ui.call_count,3)
        module.control.adb.assert_not_called()

    def test_scrolled_page_binds_state_to_the_expanded_sentence(self):
        self.session.client.observe=Mock()
        self.session.client.last={'truncated':False}
        self.session.client.nodes={**idle_nodes(),'0.0':{'text':'范例学习'}}
        self.session.expected_text='Example sentence.'
        self.assertEqual(self.session.state(),'idle')
        self.session.expected_text='A different sentence.'
        with self.assertRaisesRegex(BridgeError,'Left the calibrated'):
            self.session.state()

    def test_pending_submission_waits_for_interaction_without_reading_a_score(self):
        self.session.state=Mock(side_effect=['idle','recording','recording','submitted','submitted','retry_available'])
        result=self.session.speak_turn(self.wav,719,1128,'Example sentence.')
        self.assertEqual(result['final_state'],'retry_available')
        self.assertEqual([x['phase'] for x in result['states']],['recording','submitted','retry_available'])
        self.assertEqual(sum(c.args[:3]==('shell','input','tap') for c in self.control.adb.call_args_list),2)
        self.session.state=Mock(return_value='submitted')
        self.control.adb.reset_mock()
        with patch('training_session.time.monotonic',side_effect=[0,1,2,31]):
            with self.assertRaises(BridgeError):self.session.wait_phase({'retry_available'},30,[])
        self.control.adb.assert_not_called()

    def test_fast_mode_waits_for_saved_recording_without_reading_a_score(self):
        self.session.state=Mock(side_effect=['idle','recording','recording','submitted','submitted','retry_available'])
        with patch.dict(os.environ,{'UCAMPUS_SKIP_SCORE':'1'}):
            result=self.session.speak_turn(self.wav,719,1128,'Example sentence.')
        self.assertEqual(result['final_state'],'retry_available')
        self.assertEqual(result['platform_acceptance'],'pending_stage_check')
        self.assertEqual([x['phase'] for x in result['states']],['recording','submitted','retry_available'])
        self.assertEqual(sum(c.args[:3]==('shell','input','tap') for c in self.control.adb.call_args_list),2)


if __name__=='__main__':
    unittest.main()
