import unittest
from unittest.mock import Mock,patch
from screen_frames import FrameCache,ScreenFrames


class FrameTests(unittest.TestCase):
    def test_display_id_is_explicit_and_cannot_fall_back_to_main(self):
        for display in (-1,True,'1',2147483648):
            with self.assertRaises(ValueError):ScreenFrames('adb','device','missing.exe',display)

    def test_never_returns_a_frame_from_before_the_action(self):
        cache=FrameCache();cache.latest=Mock();cache.received=1
        with patch('screen_frames.time.monotonic',return_value=3):
            with self.assertRaises(TimeoutError):cache.png(after=2,timeout=0)
        cache.latest.save.assert_not_called()

    def test_stale_frame_and_decoder_failure_are_not_success(self):
        cache=FrameCache();cache.latest=Mock();cache.received=1
        with patch('screen_frames.time.monotonic',return_value=2):
            with self.assertRaises(TimeoutError):cache.png(max_age=.5,timeout=0)
        cache.error='decode stopped'
        with self.assertRaises(RuntimeError):cache.png()

    def test_latest_image_is_encoded_without_a_device_roundtrip(self):
        cache=FrameCache()
        first=Mock();second=Mock()
        second.save.side_effect=lambda stream,**kwargs:stream.write(b'new PNG')
        cache.put(first);cache.put(second)
        self.assertEqual(cache.png(),b'new PNG')
        first.save.assert_not_called()

    def test_cleanup_removes_only_owned_forward_and_server_file(self):
        frames=object.__new__(ScreenFrames)
        frames.socket=frames.thread=frames.reader=frames.process=None
        frames.port=12345;frames.pushed=True;frames.remote='/data/local/tmp/ucampus-frames-0123abcd.jar'
        frames.command=Mock()
        frames.close()
        self.assertEqual([c.args for c in frames.command.call_args_list],[('forward','--remove','tcp:12345'),('shell','rm','-f',frames.remote)])
        frames.close()
        self.assertEqual(frames.command.call_count,2)


if __name__=='__main__':unittest.main()
