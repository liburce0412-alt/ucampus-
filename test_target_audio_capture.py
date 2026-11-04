import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import Mock,patch
from target_audio_capture import receive,stop_on_file,main


class TargetCaptureTests(unittest.TestCase):
    def run_rows(self,rows):
        output=io.BytesIO();log=io.StringIO();ready=[]
        with wave.open(output,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
            result=receive([json.dumps(r).encode() for r in rows],wav,10368,1,log,ready.append)
        return result,output,log,ready

    def rows(self):
        return [{'phase':'ready','uid':10368,'mode':'capture','rate':16000,'channels':1},
                {'phase':'pcm','data':base64.b64encode(b'\x00\x04'*1600).decode()},
                {'phase':'captured','bytes':3200,'stopped':False},{'phase':'released','policy_status':1}]

    def test_content_and_release_are_separate_and_pcm_is_not_logged(self):
        result,output,log,ready=self.run_rows(self.rows())
        self.assertEqual(result['seconds'],.1);self.assertEqual(result['peak'],1024)
        self.assertFalse(result['content_verified']);self.assertTrue(result['policy_released'])
        self.assertEqual(len(ready),1);self.assertNotIn('data',log.getvalue())
        output.seek(0)
        with wave.open(output,'rb') as wav:self.assertEqual(wav.getnframes(),1600)

    def test_wrong_uid_or_pcm_before_ready_is_rejected(self):
        rows=self.rows();rows[0]['uid']=2000
        with self.assertRaises(ValueError):self.run_rows(rows)
        with self.assertRaises(ValueError):self.run_rows(self.rows()[1:])

    def test_missing_release_bad_samples_and_mismatched_counts_are_rejected(self):
        with self.assertRaises(RuntimeError):self.run_rows(self.rows()[:-1])
        rows=self.rows();rows[1]['data']='!invalid!'
        with self.assertRaises(ValueError):self.run_rows(rows)
        rows=self.rows();rows[2]['bytes']=2
        with self.assertRaises(ValueError):self.run_rows(rows)

    def test_stop_file_sends_x_to_real_child_and_still_requires_release(self):
        child="import sys; value=sys.stdin.buffer.read(1); sys.stdout.buffer.write(value); sys.exit(0 if value==b'X' else 2)"
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'capture.stop';finished=threading.Event();report={}
            process=subprocess.Popen([sys.executable,'-u','-c',child],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
            watcher=threading.Thread(target=stop_on_file,args=(process,path,finished,report),daemon=True)
            try:
                watcher.start();path.touch();process.wait(timeout=3)
                watcher.join(timeout=1)
                self.assertEqual(process.returncode,0);self.assertEqual(process.stdout.read(),b'X')
                self.assertTrue(report['stop_requested'])
            finally:
                finished.set();watcher.join(timeout=1)
                if process.poll() is None:process.kill();process.wait(timeout=2)
                process.stdin.close();process.stdout.close()
        rows=self.rows();rows[2]['stopped']=True
        self.assertTrue(self.run_rows(rows)[0]['stopped'])
        with self.assertRaises(RuntimeError):self.run_rows(rows[:-1])

    def test_cancel_before_first_samples_is_distinct_from_empty_capture(self):
        rows=[self.rows()[0],{'phase':'captured','bytes':0,'stopped':True},self.rows()[-1]]
        result=self.run_rows(rows)[0]
        self.assertEqual(result['bytes'],0);self.assertEqual(result['rms'],0)
        self.assertTrue(result['stopped']);self.assertFalse(result['content_verified'])
        rows[1]['stopped']=False
        with self.assertRaises(RuntimeError):self.run_rows(rows)

    def test_finished_capture_is_not_written_and_stale_stop_is_rejected(self):
        process=Mock();finished=threading.Event();finished.set()
        stop_on_file(process,Mock(),finished,{})
        process.stdin.write.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.stop';path.touch()
            with patch('sys.argv',['target_audio_capture.py','--uid','10368','--output',str(Path(directory)/'new'),'--stop-file',str(path)]),patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit) as error:main()
                self.assertEqual(error.exception.code,2)


if __name__=='__main__':unittest.main()
