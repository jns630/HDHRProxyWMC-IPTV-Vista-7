import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy.discovery import DiscoveryServer  # noqa: E402


class CopyFallbackTests(unittest.TestCase):
    def setUp(self):
        self.server = DiscoveryServer(
            device_id="104ABCDE",
            base_url="http://127.0.0.1:5004",
            tuner_count=1,
            output_codec="libx264",
            copy_video="auto",
        )

    def test_command_detects_video_copy(self):
        self.assertTrue(self.server._cmd_uses_video_copy(["ffmpeg", "-c:v", "copy", "-f", "mpegts"]))
        self.assertFalse(self.server._cmd_uses_video_copy(["ffmpeg", "-c:v", "libx264"]))

    def test_copy_is_disabled_after_failure(self):
        info = {
            "video_codec": "h264",
            "profile": "High",
            "level": 40,
            "pix_fmt": "yuv420p",
        }
        with mock.patch("hdhr_proxy.discovery.probe_stream", return_value=info):
            self.assertTrue(self.server._should_copy_video("http://example.com/live"))
            self.server._disable_copy_video_for_source("http://example.com/live")
            self.assertFalse(self.server._should_copy_video("http://example.com/live"))

    def test_vista_never_copies(self):
        self.server.force_vista_mode = True
        self.server.output_codec = "mpeg2video"
        with mock.patch("hdhr_proxy.discovery.probe_stream") as probe:
            self.assertFalse(self.server._should_copy_video("http://example.com/live"))
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
