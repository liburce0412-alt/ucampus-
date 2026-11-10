import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock,patch

from bridge_client import Client,BridgeError,OutcomeUnknown
from workflow_session import Runner,Paused,validate
from audio_injection_trial import inject_once,REMOTE_PCM


def node(text,**flags):
    return {'text':text,'enabled':True,'clickable':True,'bounds':[0,0,50,50],**flags}


def click(before,target,after):
    return {'action':'click','before':[{'text':before}],'target':{'text':target},'expect':{'text':after}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.client=Client(8794)
        self.pages=[{'0':node('First'),'0.1':node('Next')},{'0':node('Second'),'0.1':node('Finish')},{'0':node('Complete')}]
        self.page=0;self.read_count=0
        def observe(**kwargs):
            self.read_count+=1
            self.client.nodes=self.pages[self.page]
            self.client.last={'snapshot_id':str(self.read_count),'truncated':False}
        def send(*args):
            self.page+=1
            return {'dispatched':True}
        self.client.observe=Mock(side_effect=observe);self.client.click=Mock(side_effect=send)
        self.session=types.SimpleNamespace(client=self.client)
        self.runner=Runner(self.session,self.root/'run',self.root)

    def plan(self):return {'version':1,'steps':[click('First','Next','Second'),click('Second','Finish','Complete')]}

    def test_one_session_finishes_route_and_emits_only_summary(self):
        result=self.runner.run(self.plan())
        self.assertEqual(result['completed'],2);self.assertEqual(result['clicks'],2)
        self.assertIs(self.runner.client,self.client)
        self.assertLess(len(json.dumps(result)),200)
        report=json.loads((self.root/'run/checkpoint.json').read_text())
        self.assertEqual(report['status'],'complete');self.assertEqual(len(report['steps']),2)
        self.assertNotIn('nodes',report)

    def test_lost_click_response_stops_without_replaying_or_marking_complete(self):
        self.client.click.side_effect=OutcomeUnknown('Lost response')
        with self.assertRaises(OutcomeUnknown):self.runner.run(self.plan())
        self.client.click.assert_called_once()
        report=json.loads((self.root/'run/checkpoint.json').read_text())
        self.assertEqual(report['status'],'needs_review');self.assertEqual(report['in_flight'],1)
        self.assertEqual(report['completed'],0)

    def test_navigation_waits_through_empty_root_without_reclicking(self):
        original=self.client.observe.side_effect
        empty=True
        def loading(**kwargs):
            nonlocal empty
            if self.page==1 and empty:
                empty=False;raise BridgeError('no_active_window')
            original(**kwargs)
        self.client.observe.side_effect=loading
        with patch('workflow_session.time.sleep'):
            result=self.runner.run({'version':1,'steps':self.plan()['steps'][:1]})
        self.assertEqual(result['completed'],1);self.client.click.assert_called_once()
        report=json.loads((self.root/'run/checkpoint.json').read_text())
        self.assertEqual(report['transition_reads'],1)

    def test_empty_root_timeout_and_foreign_or_locked_screen_never_reclick(self):
        original=self.client.observe.side_effect
        for error in ('no_active_window','target_not_foreground','device_locked'):
            with self.subTest(error=error):
                self.page=0;self.client.click.reset_mock()
                self.runner=Runner(self.session,self.root/error,self.root)
                def unreadable(**kwargs):
                    if self.page:raise BridgeError(error)
                    original(**kwargs)
                self.client.observe.side_effect=unreadable
                step={**self.plan()['steps'][0],'timeout':1}
                ticks=iter(range(100))
                with patch('workflow_session.time.sleep'),patch('workflow_session.time.monotonic',side_effect=lambda:next(ticks)):
                    with self.assertRaisesRegex(BridgeError,'Expected state' if error=='no_active_window' else error):
                        self.runner.run({'version':1,'steps':[step]})
                self.client.click.assert_called_once()
                self.assertEqual(self.runner.transition_reads,1 if error=='no_active_window' else 0)

    def test_rejected_clicks_refresh_the_same_question_before_dispatch(self):
        original=self.client.click.side_effect
        errors=iter(('ui_changed','node_changed',None))
        def reject_then_send(*args):
            error=next(errors)
            if error:raise BridgeError(error)
            return original(*args)
        self.client.click.side_effect=reject_then_send
        with patch('workflow_session.time.sleep'):
            result=self.runner.run({'version':1,'steps':self.plan()['steps'][:1]})
        self.assertEqual(result['completed'],1);self.assertEqual(self.runner.rejected_clicks,2)
        self.assertEqual(self.client.click.call_count,3);self.assertEqual(self.page,1)

    def test_rejected_click_retry_limit_and_changed_anchor(self):
        self.client.click.side_effect=BridgeError('node_changed')
        with patch('workflow_session.time.sleep'):
            with self.assertRaisesRegex(BridgeError,'node_changed'):self.runner.run(self.plan())
        self.assertEqual(self.client.click.call_count,3)
        self.client.click.reset_mock();self.runner=Runner(self.session,self.root/'changed',self.root)
        def moved(*args):
            self.page=1;raise BridgeError('ui_changed')
        self.client.click.side_effect=moved
        with patch('workflow_session.time.sleep'):
            with self.assertRaisesRegex(BridgeError,'anchor changed'):self.runner.run(self.plan())
        self.client.click.assert_called_once()

    def test_existing_checkpoint_cannot_be_reused(self):
        (self.root/'run').mkdir()
        with self.assertRaises(FileExistsError):self.runner.run(self.plan())
        self.client.observe.assert_not_called()

    def test_page_change_between_guard_and_dispatch_sends_nothing(self):
        original=self.client.observe.side_effect
        def moved(**kwargs):
            if self.read_count==1:self.page=1
            original(**kwargs)
        self.client.observe.side_effect=moved
        with self.assertRaises(BridgeError):self.runner.run(self.plan())
        self.client.click.assert_not_called()

    def test_preexisting_postcondition_does_not_replay_submission(self):
        self.pages[0]['0.2']=node('Second')
        with self.assertRaisesRegex(BridgeError,'already present'):self.runner.run(self.plan())
        self.client.click.assert_not_called()

    def select_plan(self):
        return {'version':1,'steps':[{'action':'select','before':[{'text':'Question'}],'target':{'text':'Choice'}}]}

    def test_selection_uses_ancestor_and_requires_readback(self):
        self.pages=[
            {'0':node('Question'),'0.1':node(''),'0.1.1':node('Choice',clickable=False)},
            {'0':node('Question'),'0.1':node('',checked=True),'0.1.1':node('Choice',clickable=False)}]
        result=self.runner.run(self.select_plan())
        self.assertEqual(result['completed'],1)
        self.assertEqual(self.client.click.call_args.args[1],'0.1')

    def test_selection_readback_waits_through_empty_root(self):
        self.pages=[{'0':node('Question'),'0.1':node('Choice')},
                    {'0':node('Question'),'0.1':node('Choice',checked=True)}]
        original=self.client.observe.side_effect
        empty=True
        def loading(**kwargs):
            nonlocal empty
            if self.page and empty:
                empty=False;raise BridgeError('no_active_window')
            original(**kwargs)
        self.client.observe.side_effect=loading
        with patch('workflow_session.time.sleep'):self.runner.run(self.select_plan())
        self.client.click.assert_called_once();self.assertEqual(self.runner.transition_reads,1)

    def test_selected_option_is_not_toggled_off(self):
        self.pages=[{'0':node('Question'),'0.1':node('Choice',selected=True)}]
        self.runner.run(self.select_plan());self.client.click.assert_not_called()

    def test_rejected_selection_that_becomes_selected_is_not_toggled(self):
        self.pages=[{'0':node('Question'),'0.1':node('Choice')}]
        def changed(*args):
            self.pages[0]['0.1']['checked']=True;raise BridgeError('node_changed')
        self.client.click.side_effect=changed
        with patch('workflow_session.time.sleep'):
            self.runner.run(self.select_plan())
        self.client.click.assert_called_once()

    def test_question_change_after_selection_stops_before_submit(self):
        self.pages=[{'0':node('Question'),'0.1':node('Choice')},{'0':node('Other question'),'0.1':node('Choice',checked=True)}]
        with self.assertRaises(BridgeError):self.runner.run(self.select_plan())
        self.client.click.assert_called_once()

    def test_pause_preserves_in_flight_step_and_never_retries(self):
        def send(*args):
            self.runner.stop.touch()
            return {'dispatched':True}
        self.client.click.side_effect=send
        with self.assertRaises(Paused):self.runner.run(self.plan())
        report=json.loads((self.root/'run/checkpoint.json').read_text())
        self.assertEqual(report['status'],'paused');self.assertEqual(report['completed'],0)
        self.client.click.assert_called_once()

    def test_all_steps_are_validated_before_any_action(self):
        plan=self.plan();plan['steps'][1]['extra_command']='arbitrary'
        with self.assertRaises(ValueError):self.runner.run(plan)
        self.client.observe.assert_not_called()
        for invalid in (True,-1,901):
            plan=self.plan();plan['steps'][0]['timeout']=invalid
            with self.assertRaises(ValueError):validate(plan,self.root)

    def test_capture_starts_before_playback_and_flushes_on_failure(self):
        self.runner.uid=10001
        directory=self.root/'capture-step';output=directory/'audio'
        events=[]
        def start(*args,**kwargs):
            output.mkdir();(output/'ready.json').write_text(json.dumps({'uid':10001,'mode':'capture'}))
            events.append('ready')
            return process
        def finish(**kwargs):
            self.assertTrue((output/'capture.stop').exists());events.append('release')
            (output/'result.json').write_text(json.dumps({'policy_released':True,'seconds':2}))
        process=Mock(returncode=0);process.poll.return_value=None;process.wait.side_effect=finish
        def fail(_):
            events.append('play');raise OutcomeUnknown('Lost playback response')
        self.runner.click=fail
        with patch('workflow_session.subprocess.Popen',side_effect=start):
            with self.assertRaises(OutcomeUnknown):self.runner.capture({**click('First','Next','Second'),'action':'capture','seconds':5},directory)
        self.assertEqual(events,['ready','play','release'])

    def test_capture_exit_is_checked_while_window_is_empty(self):
        self.runner.uid=10001
        directory=self.root/'capture-step';output=directory/'audio'
        def start(*args,**kwargs):
            output.mkdir();(output/'ready.json').write_text(json.dumps({'uid':10001,'mode':'capture'}))
            return process
        def finish(**kwargs):
            self.assertTrue((output/'capture.stop').exists())
            (output/'result.json').write_text(json.dumps({'policy_released':True,'seconds':2}))
        process=Mock(returncode=0);process.poll.return_value=0;process.wait.side_effect=finish
        def play(_):self.client.observe.side_effect=BridgeError('no_active_window')
        self.runner.click=Mock(side_effect=play)
        with patch('workflow_session.subprocess.Popen',side_effect=start),patch('workflow_session.time.sleep'):
            with self.assertRaisesRegex(OutcomeUnknown,'Capture ended'):
                self.runner.capture({**click('First','Next','Second'),'action':'capture','seconds':5},directory)
        self.runner.click.assert_called_once();process.wait.assert_called_once()

    def test_playback_clock_requires_progress_and_remaining_media_before_reset(self):
        self.runner.uid=10001
        box=[596,2883,843,3131]
        step={'action':'capture','before':[{'text':'While-listening'}],'target':{'bounds':box},
              'clock':'00:00/00:03','seconds':6,'timeout':4}
        validate({'version':1,'steps':[step]},self.root)
        for label,states,error in (
                ('finished',[(0,0),(1,1),(3,3),(3,0)],None),
                ('early',[(1,1),(1.1,0)],'reset before'),
                ('never_started',[(0,0),(5,0)],'outcome unknown')):
            with self.subTest(label=label):
                directory=self.root/label;output=directory/'audio';now=[0];playing=[False]
                readings=iter(states);self.client.click.reset_mock()
                def observe(**kwargs):
                    moment,current=next(readings) if playing[0] else (0,0);now[0]=moment
                    self.client.nodes={'0':node('While-listening'),'0.1':node(f'00:0{current}/00:03'),
                                       '0.2':node('',bounds=box)}
                    self.client.last={'snapshot_id':'fresh','truncated':False}
                def play(*args):playing[0]=True;return {'dispatched':True}
                def start(*args,**kwargs):
                    output.mkdir();(output/'ready.json').write_text(json.dumps({'uid':10001,'mode':'capture'}))
                    return process
                def finish(**kwargs):
                    self.assertTrue((output/'capture.stop').exists())
                    (output/'result.json').write_text(json.dumps({'policy_released':True,'seconds':now[0]}))
                process=Mock(returncode=0);process.poll.return_value=None;process.wait.side_effect=finish
                self.client.observe.side_effect=observe;self.client.click.side_effect=play
                with patch('workflow_session.subprocess.Popen',side_effect=start),patch('workflow_session.time.monotonic',side_effect=lambda:now[0]):
                    if error:
                        with self.assertRaisesRegex(OutcomeUnknown,error):self.runner.capture(step,directory)
                    else:self.assertTrue(self.runner.capture(step,directory)['policy_released'])
                self.client.click.assert_called_once();process.wait.assert_called_once()

    def test_playback_plan_rejects_ambiguous_end_modes_and_uncalibrated_bounds(self):
        step={'action':'capture','before':[{'text':'While-listening'}],'target':{'bounds':[596,2883,843,3131]},
              'clock':'00:00/02:41','seconds':180,'timeout':170}
        for change in ({'expect':{'text':'Post-listening'}},{'clock':'00:00/00:00'},
                       {'timeout':30},{'target':{'bounds':[0,0,2000,3200]}}):
            with self.assertRaises(ValueError):validate({'version':1,'steps':[{**step,**change}]},self.root)

    def test_transcriber_finishes_after_release_even_when_capture_fails(self):
        import threading
        self.runner.uid=10001
        for released in (True,False):
            with self.subTest(released=released):
                directory=self.root/f'capture-{released}';output=directory/'audio'
                ended=threading.Event()
                def start(*args,**kwargs):
                    output.mkdir();(output/'ready.json').write_text(json.dumps({'uid':10001,'mode':'capture'}))
                    return process
                def finish(**kwargs):
                    self.assertTrue((output/'capture.stop').exists())
                    (output/'result.json').write_text(json.dumps({'policy_released':released,'seconds':2}))
                def transcribe(source,stop,destination):
                    while not stop.exists():ended.wait(.005)
                    self.assertTrue((output/'result.json').exists());ended.set()
                    raise ValueError('decoder failed')
                process=Mock(returncode=0);process.poll.return_value=None;process.wait.side_effect=finish
                self.runner.capture_transcriber=transcribe;self.runner.click=Mock()
                with patch('workflow_session.subprocess.Popen',side_effect=start),patch.object(self.runner,'observe',return_value=True),patch.object(self.runner,'present',return_value=True):
                    with self.assertRaisesRegex(BridgeError,'transcription failed' if released else 'release not confirmed'):
                        self.runner.capture({**click('First','Next','Second'),'action':'capture','seconds':5},directory)
                self.assertTrue(ended.is_set());self.runner.click.assert_called_once()


class InjectionOwnershipTests(unittest.TestCase):
    def test_failed_upload_removes_only_owned_temporary_path_and_restores_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wav=root/'test.wav';wav.write_bytes(b'validated elsewhere')
            normal_adb=Mock();normal_play=Mock();normal_check=Mock()
            session=types.SimpleNamespace(control=types.SimpleNamespace(adb=normal_adb,play_audio=normal_play),check_audio=normal_check)
            runtime=types.SimpleNamespace(serial=lambda:'example:5555',adb_path=lambda:'adb',adb=Mock(side_effect=[OSError('partial push'),b'']))
            with patch('audio_injection_trial.pcm16',return_value=b'\0\1'),patch('audio_injection_trial.subprocess.run',return_value=types.SimpleNamespace(returncode=1)):
                with self.assertRaises(OSError):inject_once(session,runtime,wav,10001,root/'run',Mock())
            self.assertEqual(runtime.adb.call_args.args,('shell','rm','-f',REMOTE_PCM))
            self.assertIs(session.control.adb,normal_adb);self.assertIs(session.control.play_audio,normal_play)

    def test_existing_remote_audio_is_never_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wav=root/'test.wav';wav.touch();runtime=Mock();runtime.serial.return_value='example:5555'
            with patch('audio_injection_trial.pcm16',return_value=b'\0\1'),patch('audio_injection_trial.subprocess.run',return_value=types.SimpleNamespace(returncode=0)):
                with self.assertRaises(RuntimeError):inject_once(Mock(),runtime,wav,10001,root/'run',Mock())
            runtime.adb.assert_not_called()


if __name__=='__main__':unittest.main()
