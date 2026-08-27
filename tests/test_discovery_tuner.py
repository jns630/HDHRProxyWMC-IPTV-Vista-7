import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy.discovery import DiscoveryServer
from hdhr_proxy.m3u_parser import M3UChannel, build_lineup


class TunerProgramSelectionTests(unittest.TestCase):
    def test_program_zero_starts_current_rf_stream(self):
        channels = []
        for index in range(1, 4):
            channel = M3UChannel()
            channel.name = f"Channel {index}"
            channel.tvg_id = f"ch{index}"
            channel.tvg_chno = str(index)
            channel.url = f"http://127.0.0.1:1/source/{index}.m3u8"
            channels.append(channel)
        lineup, channel_map = build_lineup(channels, base_url="http://127.0.0.1:5004")
        server = DiscoveryServer(
            device_id="104ABCDE",
            base_url="http://127.0.0.1:5004",
            tuner_count=1,
            lineup=lineup,
            channel_map=channel_map,
            ffmpeg_path="missing-ffmpeg",
        )
        state = server._tuner_state[0]
        state["channel"] = "auto6t:485000000"
        state["program"] = "0"
        rf = server._rf_channels[0]
        state["rf"] = rf
        state["filter"] = "0x0000"

        server._set_tuner_target_locked(0, "rtp://169.254.210.93:58236")

        self.assertEqual(state.get("channel_id"), rf["channel_id"])
        self.assertEqual(state.get("target"), "rtp://169.254.210.93:58236")


class DiscoveryListenerWaitTests(unittest.TestCase):
    def test_wait_requires_udp_and_tcp_listeners(self):
        server = DiscoveryServer(
            device_id="104ABCDE",
            base_url="http://127.0.0.1:5004",
            tuner_count=1,
        )

        self.assertFalse(server.wait_for_critical_listeners(timeout=0))

        server._udp_discovery_bound.set()
        self.assertFalse(server.wait_for_critical_listeners(timeout=0))

        def set_control():
            server._tcp_control_bound.set()

        threading.Timer(0.01, set_control).start()
        self.assertTrue(server.wait_for_critical_listeners(timeout=0.5))


class FfmpegReconnectCompatibilityTests(unittest.TestCase):
    def test_legacy_ffmpeg_uses_legacy_reconnect_flags(self):
        server = DiscoveryServer(
            device_id="104ABCDE",
            base_url="http://127.0.0.1:5004",
            tuner_count=1,
        )

        with mock.patch("hdhr_proxy.discovery.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "FFmpeg version 4.1.0\n-has option reconnect\n"
            mock_run.return_value.stderr = ""
            mock_run.return_value.returncode = 0

            self.assertEqual(
                server._ffmpeg_reconnect_args("ffmpeg"),
                ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"],
            )

    def test_modern_ffmpeg_uses_network_error_reconnect_flags(self):
        server = DiscoveryServer(
            device_id="104ABCDE",
            base_url="http://127.0.0.1:5004",
            tuner_count=1,
        )

        with mock.patch("hdhr_proxy.discovery.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "FFmpeg version N-1234\n-- reconnect_on_network_error\nreconnect_at_eof\n"
            mock_run.return_value.stderr = ""
            mock_run.return_value.returncode = 0

            self.assertEqual(
                server._ffmpeg_reconnect_args("ffmpeg"),
                [
                    "-reconnect_at_eof", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "2",
                    "-reconnect_on_network_error", "1",
                ],
            )


if __name__ == "__main__":
    unittest.main()
