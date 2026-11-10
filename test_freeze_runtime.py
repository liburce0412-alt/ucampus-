from pathlib import Path
import tempfile
import unittest
from freeze_runtime import freeze,verify


class FreezeTests(unittest.TestCase):
    def test_snapshot_keeps_only_runtime_and_detects_later_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';(source/'skill/scripts').mkdir(parents=True)
            (source/'training_worker.py').write_text('pass\n')
            (source/'test_private.py').write_text('not runtime')
            (source/'secret.env').write_text('not runtime')
            (source/'skill/scripts/runtime.py').write_text('pass\n')
            target=root/'release';result=freeze(target,source)
            self.assertEqual(result['files'],2);self.assertFalse((target/'secret.env').exists())
            verify(target)
            with self.assertRaises(FileExistsError):freeze(target,source)
            (target/'training_worker.py').write_text('changed')
            with self.assertRaises(ValueError):verify(target)


if __name__=='__main__':unittest.main()
