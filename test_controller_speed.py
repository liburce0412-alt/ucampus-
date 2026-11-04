"""Offline checks for the two installed controller optimizations; never uses ADB."""
import importlib.util
import os
from pathlib import Path
from project_paths import SKILL
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = SKILL / 'scripts'
work = tempfile.TemporaryDirectory()
os.environ["UCAMPUS_WORK"] = work.name
sys.path.insert(0, str(SCRIPTS))
import runtime

spec = importlib.util.spec_from_file_location("controller", SCRIPTS / "ucampus-control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


def tearDownModule():
    work.cleanup()


class DeviceBindingTests(unittest.TestCase):
    def setUp(self):
        runtime.serial.cache_clear()
        self.addCleanup(runtime.serial.cache_clear)

    def test_commands_keep_one_validated_explicit_target(self):
        with patch.dict(os.environ, {"UCAMPUS_SERIAL": "usb-a"}), patch.object(runtime, "devices", return_value=[["usb-a", "device"]]) as devices, patch.object(runtime, "adb_path", return_value="adb.exe"), patch.object(runtime.subprocess, "check_output", return_value=b"ok") as execute:
            for _ in range(3):
                self.assertEqual(runtime.adb("shell", "true"), b"ok")
            self.assertEqual(devices.call_count, 1)
            self.assertEqual(execute.call_count, 3)
            for call in execute.call_args_list:
                self.assertEqual(call.args[0][:3], ["adb.exe", "-s", "usb-a"])

    def test_failed_authorization_is_not_cached(self):
        with patch.dict(os.environ, {"UCAMPUS_SERIAL": "usb-a"}), patch.object(runtime, "devices", side_effect=[[["usb-a", "unauthorized"]], [["usb-a", "device"]]]) as devices:
            with self.assertRaises(RuntimeError):
                runtime.serial()
            self.assertEqual(runtime.serial(), "usb-a")
            self.assertEqual(devices.call_count, 2)

    def test_disconnect_does_not_fall_back_to_another_device(self):
        with patch.dict(os.environ, {"UCAMPUS_SERIAL": "usb-a"}), patch.object(runtime, "devices", return_value=[["usb-a", "device"]]) as devices, patch.object(runtime, "adb_path", return_value="adb.exe"), patch.object(runtime.subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, "adb")):
            self.assertEqual(runtime.serial(), "usb-a")
            devices.return_value = [["usb-b", "device"]]
            with self.assertRaises(subprocess.CalledProcessError):
                runtime.adb("shell", "true")
            self.assertEqual(devices.call_count, 1)

    def test_ambiguous_device_selection_still_fails(self):
        with patch.dict(os.environ, {"UCAMPUS_SERIAL": ""}), patch.object(runtime, "devices", return_value=[["usb-a", "device"], ["usb-b", "device"]]):
            with self.assertRaises(RuntimeError):
                runtime.serial()


class SnapshotTests(unittest.TestCase):
    def test_supplied_frame_is_saved_without_recapture(self):
        with patch.object(control, "adb") as adb:
            path = Path(control.snapshot("reused", b"exact-frame"))
            self.assertEqual(path.read_bytes(), b"exact-frame")
            adb.assert_not_called()

    def test_default_snapshot_still_captures_a_fresh_frame(self):
        with patch.object(control, "adb", return_value=b"fresh-frame") as adb:
            path = Path(control.snapshot("fresh"))
            self.assertEqual(path.read_bytes(), b"fresh-frame")
            adb.assert_called_once_with("exec-out", "screencap", "-p")


if __name__ == "__main__":
    unittest.main()
