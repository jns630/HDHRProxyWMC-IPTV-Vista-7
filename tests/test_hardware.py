import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy import hardware  # noqa: E402
from hdhr_proxy.hardware import hw_decode_args, hw_encoder_args, select_hw_encoder  # noqa: E402


class HardwareHelperTests(unittest.TestCase):
    def test_software_preference_disables_hardware(self):
        self.assertIsNone(select_hw_encoder("ffmpeg", "off"))
        self.assertEqual(hw_decode_args("h264_nvenc", "off"), [])

    def test_nvenc_args_are_wmc_friendly(self):
        args = hw_encoder_args("h264_nvenc", "4000k", "1920x1080", "8000k")
        self.assertEqual(args[:2], ["-c:v", "h264_nvenc"])
        self.assertIn("-profile:v", args)
        self.assertIn("high", args)
        self.assertIn("-bf", args)
        self.assertIn("0", args)
        self.assertIn("-rc", args)
        self.assertIn("cbr", args)

    def test_qsv_and_amf_args_use_their_own_options(self):
        qsv = hw_encoder_args("h264_qsv", "4000k", "1920x1080", "8000k")
        self.assertIn("veryfast", qsv)
        amf = hw_encoder_args("h264_amf", "4000k", "1920x1080", "8000k")
        self.assertIn("lowlatency", amf)

    def test_decode_args_follow_encoder_family(self):
        self.assertEqual(hw_decode_args("h264_nvenc", "auto"), ["-hwaccel", "cuda"])
        self.assertEqual(hw_decode_args("h264_qsv", "auto"), ["-hwaccel", "qsv"])
        self.assertEqual(hw_decode_args("h264_amf", "auto"), ["-hwaccel", "d3d11va"])
        self.assertEqual(hw_decode_args("h264_videotoolbox", "auto"), ["-hwaccel", "videotoolbox"])

    def test_vaapi_args_use_render_node_and_hwupload(self):
        with mock.patch("hdhr_proxy.hardware.vaapi_device", return_value="/dev/dri/renderD128"):
            args = hw_encoder_args("h264_vaapi", "4000k", "1920x1080", "8000k")
            self.assertEqual(args[:4], ["-vaapi_device", "/dev/dri/renderD128", "-c:v", "h264_vaapi"])
            self.assertIn("-vf", args)
            self.assertIn("format=nv12,hwupload", args)
            self.assertEqual(
                hw_decode_args("h264_vaapi", "auto"),
                ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"],
            )

    def test_videotoolbox_args_use_realtime(self):
        args = hw_encoder_args("h264_videotoolbox", "4000k", "1920x1080", "8000k")
        self.assertEqual(args[:2], ["-c:v", "h264_videotoolbox"])
        self.assertIn("-realtime", args)

    def test_default_order_depends_on_platform(self):
        with mock.patch("hdhr_proxy.hardware.platform.system", return_value="Windows"):
            self.assertEqual(hardware._default_order(), hardware._WINDOWS_ORDER)
        with mock.patch("hdhr_proxy.hardware.platform.system", return_value="Linux"):
            self.assertEqual(hardware._default_order(), hardware._LINUX_ORDER)
        with mock.patch("hdhr_proxy.hardware.platform.system", return_value="Darwin"):
            self.assertEqual(hardware._default_order(), hardware._MACOS_ORDER)


if __name__ == "__main__":
    unittest.main()
