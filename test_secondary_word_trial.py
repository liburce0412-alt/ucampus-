from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from secondary_word_trial import display_adb
from screen_frames import check_capture_display


class SecondaryWordTests(unittest.TestCase):
    def test_native_capture_is_mapped_to_the_same_single_virtual_screen(self):
        normal=Mock(side_effect=[b'Display: mDisplayId=0\nDisplay: mDisplayId=10 (organized)',
                                b'Display 11529215046282202226 (Virtual display): displayName="scrcpy"\n',b'PNG'])
        client=SimpleNamespace(display_id=10,observe=Mock(),last={'truncated':False})
        command=display_adb(normal,client,capture_id=11529215046282202226)
        self.assertEqual(command('exec-out','screencap','-p'),b'PNG')
        self.assertEqual(normal.call_args.args,('exec-out','screencap','-p','-d','11529215046282202226'))
        client.observe.assert_called_once_with(full=True)

    def test_native_capture_rejects_stale_or_ambiguous_display_mapping(self):
        for displays,capture in ((b'Display: mDisplayId=0\nDisplay: mDisplayId=8',11529215046282202226),
                                 (b'Display: mDisplayId=10\nDisplay: mDisplayId=11',11529215046282202226),
                                 (b'Display: mDisplayId=10',4630946654109872275)):
            normal=Mock(side_effect=[displays,b'Display 11529215046282202226 (Virtual display): displayName="scrcpy"\n'])
            with self.assertRaises(ValueError):check_capture_display(normal,10,capture)
        for capture in (0,-1,True,2**64):
            with self.assertRaises(ValueError):check_capture_display(Mock(),10,capture)

    def test_taps_and_frames_never_use_the_main_display(self):
        normal=Mock();client=SimpleNamespace(display_id=7,observe=Mock(),last={'truncated':False})
        frames=SimpleNamespace(display_id=7,png=Mock(return_value=b'secondary PNG'))
        command=display_adb(normal,client,frames)
        self.assertEqual(command('exec-out','screencap','-p'),b'secondary PNG')
        normal.assert_not_called()
        command('shell','input','tap','720','2320')
        normal.assert_called_once_with('shell','input','-d','7','tap','720','2320')
        with self.assertRaises(ValueError):command('shell','uiautomator','dump')

    def test_mismatched_or_incomplete_displays_cannot_receive_input(self):
        normal=Mock();client=SimpleNamespace(display_id=7,observe=Mock(),last={'truncated':True})
        with self.assertRaises(ValueError):display_adb(normal,client,SimpleNamespace(display_id=0))
        command=display_adb(normal,client,SimpleNamespace(display_id=7))
        with self.assertRaises(RuntimeError):command('shell','input','tap','720','2320')
        normal.assert_not_called()


if __name__=='__main__':unittest.main()
