from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import json
import tempfile
import unittest
import wave
import numpy as np

from bridge_client import BridgeError
from screen_frames import check_display_visible
from training_worker import Worker,choices,spoken_choice
from bridge_client import Client


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.worker=Worker.__new__(Worker)
        self.worker.root=Path(self.temp.name)
        self.worker.args=SimpleNamespace(display_deadline=None,skill=Path('.'))
        self.worker.client=SimpleNamespace(nodes={'0':{'text':'Question one'}})
        self.worker.request=0;self.worker.sequence=0;self.worker.event=Mock()
        self.worker.receive=Mock();self.worker.emit=Mock()

    def test_only_unique_complete_candidates_are_automatic(self):
        self.assertEqual(spoken_choice('It was an original idea.',['original','origin','novel']),0)
        self.assertIsNone(spoken_choice('Original or novel?', ['original','origin','novel']))
        self.assertIsNone(spoken_choice('It was originally made.', ['original','origin','novel']))

    def test_options_require_three_unique_inspected_rows(self):
        rows={str(i):{'text':s,'bounds':[400,1500+i*100,1200,1550+i*100]} for i,s in enumerate(('one','two','three'))}
        self.assertEqual(choices(rows,[300,1400,1440,2200]),['one','two','three'])
        rows['3']={'text':'four','bounds':[400,1900,1200,1950]}
        with self.assertRaises(BridgeError):choices(rows,[300,1400,1440,2200])

    def test_question_and_request_id_must_survive_decision_wait(self):
        runner=Mock()
        def answer(timeout):
            message=self.worker.emit.call_args.args[0]
            return {'request_id':message['request_id'],'choice':'B'}
        self.worker.receive.side_effect=answer
        self.assertEqual(self.worker.decide(runner,'meaning',['a','b','c'],{'word':'test'}),1)
        self.worker.receive.return_value={'request_id':'stale','choice':'A'};self.worker.receive.side_effect=None
        with self.assertRaisesRegex(BridgeError,'Stale'):self.worker.decide(runner,'meaning',['a','b','c'],{})
        def changed(timeout):
            self.worker.client.nodes['0']['text']='Question two'
            return answer(timeout)
        self.worker.receive.side_effect=changed
        with self.assertRaisesRegex(BridgeError,'Question changed'):self.worker.decide(runner,'meaning',['a','b','c'],{})

    def test_display_must_cover_stage_and_cleanup_margin(self):
        self.worker.args.display_deadline=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
        self.worker.check(10)
        with self.assertRaisesRegex(BridgeError,'lease'):self.worker.check(45)

    def test_runtime_change_stops_before_next_operation(self):
        self.worker.initial_versions={'code':'old'}
        with patch('training_worker.versions',return_value={'code':'new'}):
            with self.assertRaisesRegex(BridgeError,'Runtime files changed'):self.worker.execute({'command':'observe'})

    def test_one_local_model_serves_multiple_clips(self):
        model=Mock();model.transcribe.return_value=([SimpleNamespace(text=' test ')],None)
        factory=Mock(return_value=model)
        self.worker.model=None;self.worker.manifest={};self.worker.save_manifest=Mock()
        with patch.dict('sys.modules',{'faster_whisper':SimpleNamespace(WhisperModel=factory)}):
            self.assertEqual(self.worker.text([0]),'test');self.assertEqual(self.worker.text([0]),'test')
        factory.assert_called_once();self.assertEqual(model.transcribe.call_count,2)

    def test_pre_completes_two_words_with_only_meaning_decisions(self):
        worker=self.worker;source=worker.root/'capture.wav'
        def audio(seconds):
            with wave.open(str(source),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0\4'*16000*seconds)
        audio(1);source.with_name('ready.json').write_text(json.dumps({'uid':10001,'mode':'capture'}))
        worker.args.uid=10001;worker.args.option_bounds=[300,1400,1440,2200];worker.args.xy=[720,2320]
        worker.runtime=Mock();worker.warm=Mock();worker.text=Mock(side_effect=['alpha','delta'])
        worker.parts=SimpleNamespace(read_part=Mock(return_value=np.r_[np.ones(16000)*.1,np.zeros(8000)]))
        worker.client=Client(8794);worker.session=SimpleNamespace(client=worker.client)
        state={'number':0,'phase':'intro'};names=['alpha','delta']
        def node(text,x=50,y=100,width=200):
            return {'text':text,'enabled':True,'clickable':True,'bounds':[x,y,x+width,y+50]}
        def observe(**kwargs):
            phase=state['phase'];word=names[state['number']];nodes={'0':node('Pre-listening')}
            if phase=='intro':nodes['0.1']=node('进入 Pre-listening')
            elif phase in ('heard','heard_done','meaning','meaning_done'):
                if phase.startswith('heard'):values=[word,'bravo','charlie'];nodes['0.1']=node('点击播放')
                else:values=['意思一','意思二','意思三'];nodes['0.1']=node('选出加粗单词或短语的释义');nodes['0.2']=node(word)
                nodes.update({f'0.{i+3}':node(value,400,1500+i*150) for i,value in enumerate(values)})
                if phase.endswith('_done'):nodes['0.9']=node('下一题')
            else:
                nodes['0.1']=node(word,100,800,1100);nodes['0.2']=node('点击跟读')
                if phase=='recorded':nodes['0.3']=node('下一题' if state['number']==0 else '进入While-listening')
            worker.client.nodes=nodes;worker.client.last={'snapshot_id':'fresh','truncated':False}
        def click(snapshot,ref):
            text=worker.client.nodes[ref]['text'];phase=state['phase']
            if phase=='intro':state['phase']='heard'
            elif phase=='heard':state['phase']='heard_done'
            elif phase=='heard_done':state['phase']='meaning'
            elif phase=='meaning':state['phase']='meaning_done'
            elif phase=='meaning_done':state['phase']='recording'
            elif phase=='recorded' and text=='下一题':state.update(number=1,phase='heard')
            else:raise AssertionError((phase,text))
            return {'dispatched':True}
        worker.client.observe=Mock(side_effect=observe);worker.client.click=Mock(side_effect=click)
        worker.receive.side_effect=lambda timeout:{'request_id':worker.emit.call_args.args[0]['request_id'],'choice':'A'}
        def inject(*args):state['phase']='recorded';return {'policy_released':True,'elapsed_seconds':2}
        with patch('training_worker.time.sleep',side_effect=lambda _:audio(2)),patch('training_worker.subprocess.run',return_value=SimpleNamespace(stdout='{"backend":"sapi"}')),patch('training_worker.inject_once',side_effect=inject) as injection:
            result=worker.pre({'command':'pre','capture':str(source),'enter':True},worker.root/'stage')
        self.assertEqual(result['status'],'complete');self.assertEqual(result['completed_words'],2)
        self.assertEqual(worker.request,2);self.assertEqual(injection.call_count,2)
        self.assertEqual([call.args[0]['kind'] for call in worker.emit.call_args_list],['meaning','meaning'])


class VisibilityTests(unittest.TestCase):
    def test_window_guard_reconnects_once_for_read_only_failure(self):
        error=BridgeError('Bridge connection failed; service and ADB forward must already be ready.')
        client=SimpleNamespace(display_id=7,rpc=Mock(side_effect=[error,{'windows':[{'display_id':7,'focused':True}]}]))
        normal=Mock(return_value=b'\n Window #1 Window{abc cn.unipus.cloud/Main}:\n mDisplayId=7 mHasSurface=true isOnScreen=true isVisible=true ty=BASE_APPLICATION\n')
        check_display_visible(normal,client)
        self.assertEqual([call.args for call in client.rpc.call_args_list],[('windows',),('windows',)])
        for failure,count in ((error,2),(BridgeError('device_locked'),1)):
            client.rpc=Mock(side_effect=failure)
            with self.assertRaises(BridgeError):check_display_visible(normal,client)
            self.assertEqual(client.rpc.call_count,count)

    def test_main_screen_activity_does_not_require_secondary_active_flag(self):
        client=SimpleNamespace(display_id=7,rpc=Mock(return_value={'windows':[{'display_id':7,'focused':True,'active':False}]}))
        window=b'\n Window #1 Window{abc cn.unipus.cloud/Main}:\n mDisplayId=7 mHasSurface=true isOnScreen=true isVisible=true ty=BASE_APPLICATION\n'
        normal=Mock(return_value=window)
        check_display_visible(normal,client)
        normal.assert_called_once_with('shell','dumpsys','window','windows')
        other=b'\n Window #2 Window{xyz other.app/Main}:\n mDisplayId=7 mHasSurface=true isOnScreen=true isVisible=true ty=APPLICATION\n'
        normal.return_value=window+other
        with self.assertRaisesRegex(RuntimeError,'covers'):check_display_visible(normal,client)

    def test_private_display_still_rejects_presentations_and_missing_surfaces(self):
        client=SimpleNamespace(display_id=25,rpc=Mock(return_value={'windows':[{'display_id':25,'focused':True}]}))
        target=b'\n Window #1 Window{abc cn.unipus.cloud/Main}:\n mDisplayId=25 mHasSurface=true isOnScreen=true isVisible=true ty=BASE_APPLICATION\n'
        normal=Mock(return_value=target);check_display_visible(normal,client)
        normal.return_value=target+b'\n Window #2 Window{xyz other.app/Presentation}:\n mDisplayId=25 mHasSurface=true isOnScreen=true isVisible=true ty=PRESENTATION\n'
        with self.assertRaisesRegex(RuntimeError,'covers'):check_display_visible(normal,client)
        normal.return_value=target.replace(b'isVisible=true',b'isVisible=false')
        with self.assertRaisesRegex(RuntimeError,'surface is not visible'):check_display_visible(normal,client)


if __name__=='__main__':unittest.main()
