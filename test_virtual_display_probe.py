from pathlib import Path
import unittest
from unittest.mock import Mock
from bridge_client import BridgeError
from virtual_display_probe import scrcpy_args,return_state


class DisplayProbeTests(unittest.TestCase):
    def test_bounded_probe_preserves_app_content_and_disables_audio(self):
        args=scrcpy_args(Path('scrcpy.exe'),'chosen-device',Path('evidence/display.mkv'))
        self.assertIn('--no-vd-destroy-content',args)
        self.assertIn('--time-limit=18',args)
        self.assertIn('--no-audio',args)
        self.assertIn('--no-window',args)
        self.assertIn('--start-app=cn.unipus.cloud',args)
        self.assertFalse(any('+cn.unipus' in x for x in args))
        self.assertEqual(args[1:3],['-s','chosen-device'])

    def test_app_remaining_in_background_does_not_mean_release_failure(self):
        client=Mock();client.rpc.return_value={'windows':[]}
        client.observe.side_effect=BridgeError('target_not_foreground')
        result=return_state(client)
        self.assertFalse(result['main_ucampus_foreground'])
        self.assertFalse(result['secondary_window_remaining'])
        self.assertEqual(result['training_state_preserved'],'requires_app_readback')
        client.click.assert_not_called()

    def test_unavailable_bridge_cannot_be_reported_as_background_success(self):
        client=Mock();client.rpc.return_value={'windows':[]}
        client.observe.side_effect=BridgeError('device_locked')
        with self.assertRaises(BridgeError):return_state(client)


if __name__=='__main__':unittest.main()
