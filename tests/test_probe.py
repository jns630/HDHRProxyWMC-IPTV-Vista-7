import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy.probe import (  # noqa: E402
    is_wmc_compatible_h264,
    parse_probe_payload,
    resolve_ffprobe_path,
)


def payload(codec="h264", profile="High", pix_fmt="yuv420p", level=40):
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "profile": profile,
                "pix_fmt": pix_fmt,
                "level": level,
                "width": 1920,
                "height": 1080,
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"bit_rate": "8000000"},
    }


class ProbeTests(unittest.TestCase):
    def test_parses_video_and_audio_streams(self):
        info = parse_probe_payload(payload())
        self.assertEqual(info["video_codec"], "h264")
        self.assertEqual(info["audio_codec"], "aac")
        self.assertEqual(info["width"], 1920)
        self.assertEqual(info["bit_rate"], 8000000)

    def test_accepts_wmc_compatible_h264(self):
        self.assertTrue(is_wmc_compatible_h264(parse_probe_payload(payload())))
        self.assertTrue(
            is_wmc_compatible_h264(parse_probe_payload(payload(profile="Main", level=31)))
        )

    def test_rejects_non_h264_and_ten_bit(self):
        self.assertFalse(is_wmc_compatible_h264(parse_probe_payload(payload(codec="hevc"))))
        self.assertFalse(
            is_wmc_compatible_h264(parse_probe_payload(payload(profile="High 10", pix_fmt="yuv420p10le")))
        )
        self.assertFalse(
            is_wmc_compatible_h264(parse_probe_payload(payload(profile="High 4:2:2", pix_fmt="yuv422p")))
        )

    def test_rejects_too_high_level(self):
        self.assertFalse(is_wmc_compatible_h264(parse_probe_payload(payload(level=51))))

    def test_resolves_ffprobe_beside_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg = os.path.join(tmp, "ffmpeg.exe")
            ffprobe = os.path.join(tmp, "ffprobe.exe")
            with open(ffmpeg, "wb"):
                pass
            with open(ffprobe, "wb"):
                pass
            self.assertEqual(resolve_ffprobe_path(ffmpeg, None), ffprobe)


if __name__ == "__main__":
    unittest.main()
