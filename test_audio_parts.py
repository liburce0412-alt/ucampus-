import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import tempfile
import unittest
import wave

spec=importlib.util.spec_from_file_location('audio_parts',Path(__file__).parent/'skill/scripts/audio-parts.py')
parts=importlib.util.module_from_spec(spec);spec.loader.exec_module(parts)


class AudioPartsTests(unittest.TestCase):
    def test_finished_stream_flushes_partial_tail_using_the_same_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.wav';stop=root/'stop';stop.touch()
            with wave.open(str(source),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0\4'*16000*12)
            model=Mock();model.transcribe.side_effect=lambda data,**kw:([SimpleNamespace(start=0,end=len(data)/16000,text='test')],None)
            output=root/'parts.jsonl'
            result=parts.transcribe_parts(model,source,output,seconds=5,overlap=1,watch=True,stop=stop)
            rows=[json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual([r['start'] for r in rows],[0,4,8])
            self.assertEqual(rows[-1]['seconds'],4);self.assertEqual(result['audio_through'],12)
            self.assertEqual(model.transcribe.call_count,3)
            with self.assertRaises(FileExistsError):parts.transcribe_parts(model,source,output)


if __name__=='__main__':unittest.main()
