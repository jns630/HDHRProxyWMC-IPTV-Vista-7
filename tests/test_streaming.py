# -*- coding: utf-8 -*-
"""Stdlib-only tests for ffmpeg helpers and discovery lifecycle behavior."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hdhr_proxy.streamer as streamer  # noqa: E402
from hdhr_proxy.m3u_parser import M3UChannel  # noqa: E402
from hdhr_proxy.discovery import (  # noqa: E402
    DiscoveryServer,
    _open_tuner_log,
    is_valid_device_id,
    normalize_device_id,
)


def _fake_help(stdout=b"", stderr=b""):
    result = mock.Mock()
    result.stdout = stdout
    result.stderr = stderr
    return result


class StreamerHelperTests(unittest.TestCase):
    def setUp(self):
        streamer._FFMPEG_HELP_CACHE.clear()
        streamer._FFMPEG_RECONNECT_OPTIONS.clear()
        streamer._HTTP_PERSISTENT_OPTIONS.clear()
        streamer._HLS_RESOLUTION_CACHE.clear()

    def test_hls_like_source_detection(self):
        self.assertTrue(streamer._is_hls_like_source("https://x/live/master.m3u8"))
        self.assertTrue(streamer._is_hls_like_source("C:\\lists\\local.m3u"))
        self.assertFalse(streamer._is_hls_like_source("https://x/live/segment.ts"))
        self.assertFalse(streamer._is_hls_like_source("rtsp://x/live"))

    def test_bitrate_profile_helpers(self):
        self.assertEqual(streamer._hls_profile_bitrate("4000k"), "4500k")
        self.assertEqual(streamer._hls_profile_bitrate("4m"), "4m")
        self.assertEqual(streamer._vista_profile_bitrate("4000k"), "2500k")
        self.assertEqual(streamer._vista_profile_bitrate("1500k"), "1500k")

    def test_video_encoder_args_for_h264_and_mpeg2(self):
        h264 = streamer.video_encoder_args("libx264", "4000k")
        self.assertIn("libx264", h264)
        self.assertIn("1280x720", h264)
        mpeg2 = streamer.video_encoder_args("mpeg2video", "4000k")
        self.assertIn("mpeg2video", mpeg2)
        self.assertIn("-maxrate:v", mpeg2)
        vista = streamer.video_encoder_args("mpeg2video", "4000k", vista_mode=True)
        self.assertIn("640x360", vista)
        self.assertIn("-threads", vista)

    def test_pluto_header_detection(self):
        self.assertTrue(streamer._needs_pluto_headers("https://service-stitcher.clusters.pluto.tv/x"))
        self.assertFalse(streamer._needs_pluto_headers("https://example.com/x"))

    def test_reconnect_args_are_detected_and_cached(self):
        with mock.patch(
            "hdhr_proxy.streamer.subprocess.run",
            return_value=_fake_help(b"reconnect_on_network_error\n"),
        ) as run:
            first = streamer._ffmpeg_reconnect_args("ffmpeg")
            second = streamer._ffmpeg_reconnect_args("ffmpeg")
        self.assertEqual(first, second)
        self.assertIn("-reconnect_on_network_error", second)
        self.assertEqual(run.call_count, 1)

    def test_legacy_reconnect_flags(self):
        with mock.patch(
            "hdhr_proxy.streamer.subprocess.run",
            return_value=_fake_help(b"ffmpeg-4.1\n-reconnect\n"),
        ):
            args = streamer._ffmpeg_reconnect_args("ffmpeg")
        self.assertEqual(args, ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"])

    def test_http_persistent_uses_single_probe(self):
        with mock.patch(
            "hdhr_proxy.streamer.subprocess.run",
            return_value=_fake_help(b"http_multiple\nreconnect_on_network_error\n"),
        ) as run:
            persistent = streamer._http_persistent_args("ffmpeg")
            streamer._ffmpeg_reconnect_args("ffmpeg")
        self.assertEqual(persistent, ["-http_persistent", "1"])
        self.assertEqual(run.call_count, 1)

    def test_should_keep_original_hls_url_rules(self):
        self.assertTrue(streamer._should_keep_original_hls_url("https://x/master.m3u8", ""))
        self.assertTrue(
            streamer._should_keep_original_hls_url("https://x/master.m3u8", "https://x/" + "a" * 2000)
        )
        self.assertFalse(
            streamer._should_keep_original_hls_url(
                "https://pluto.tv/master.m3u8", "https://pluto.tv/variant.m3u8"
            )
        )

    def test_hls_resolution_is_cached(self):
        class FakeResponse(object):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def geturl(self):
                return "https://example.com/live/master.m3u8"
            def read(self, amount=None):
                return (
                    b"#EXTM3U\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\n"
                    b"high.m3u8\n"
                )

        with mock.patch(
            "hdhr_proxy.streamer.urllib.request.urlopen", return_value=FakeResponse()
        ) as urlopen:
            first = streamer._resolve_hls_source_url("https://example.com/live/master.m3u8")
            second = streamer._resolve_hls_source_url("https://example.com/live/master.m3u8")
        self.assertEqual(first, "https://example.com/live/high.m3u8")
        self.assertEqual(first, second)
        self.assertEqual(urlopen.call_count, 1)

    def test_non_hls_source_is_returned_without_network(self):
        with mock.patch("hdhr_proxy.streamer.urllib.request.urlopen") as urlopen:
            self.assertEqual(
                streamer._resolve_hls_source_url("https://example.com/live.ts"),
                "https://example.com/live.ts",
            )
        urlopen.assert_not_called()

    def test_disabled_ffmpeg_uses_direct_stream(self):
        channel = M3UChannel()
        channel.url = "http://example.com/live.ts"
        session = streamer.StreamSession("2.1", {"2.1": channel}, ffmpeg_enabled=False)
        with mock.patch(
            "hdhr_proxy.streamer.direct_stream", return_value=iter([b"one"])
        ) as direct, mock.patch("hdhr_proxy.streamer.ffmpeg_transcode_stream") as transcode:
            self.assertEqual(list(session.stream()), [b"one"])
        direct.assert_called_once_with("http://example.com/live.ts")
        transcode.assert_not_called()

    def test_enabled_ffmpeg_uses_transcoder(self):
        channel = M3UChannel()
        channel.url = "http://example.com/live.ts"
        session = streamer.StreamSession("2.1", {"2.1": channel}, ffmpeg_enabled=True)
        with mock.patch(
            "hdhr_proxy.streamer.ffmpeg_transcode_stream", return_value=iter([b"two"])
        ) as transcode:
            self.assertEqual(list(session.stream()), [b"two"])
        transcode.assert_called_once()

    def test_unknown_channel_yields_nothing(self):
        session = streamer.StreamSession("9.9", {})
        self.assertEqual(list(session.stream()), [])


class DiscoveryHelperTests(unittest.TestCase):
    def test_device_id_normalization(self):
        normalized = normalize_device_id("104abcde")
        self.assertEqual(len(normalized), 8)
        self.assertEqual(normalized, normalized.upper())
        self.assertTrue(is_valid_device_id(normalized))
        # Short or invalid input is padded/repaired into a valid device id.
        self.assertTrue(is_valid_device_id(normalize_device_id("104ABC")))
        self.assertTrue(is_valid_device_id(normalize_device_id("zzzzzzzz")))
        self.assertFalse(is_valid_device_id("104ABCD"))

    def test_tuner_log_rotates_when_large(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ffmpeg_tuner0.log")
            with open(path, "wb") as f:
                f.write(b"x" * 64)
            with mock.patch("hdhr_proxy.discovery.TUNER_LOG_MAX_BYTES", 16):
                handle = _open_tuner_log(path)
                handle.write(b"new\n")
                handle.close()
            self.assertTrue(os.path.exists(path + ".1"))
            with open(path + ".1", "rb") as f:
                self.assertEqual(len(f.read()), 64)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"new\n")

    def test_stop_sets_event_and_stops_tuners_and_relays(self):
        server = DiscoveryServer(
            device_id="104ABCDE",
            base_url="http://127.0.0.1:5004",
            tuner_count=2,
        )
        relay = mock.Mock()
        server._channel_relay_server = relay

        with mock.patch.object(server, "_stop_tuner_process_locked") as stop_tuner:
            server.stop()

        self.assertTrue(server.stop_event.is_set())
        self.assertEqual(stop_tuner.call_count, 2)
        relay.shutdown.assert_called_once()
        relay.server_close.assert_called_once()

        # A second stop must not try to shut down the same relay again.
        server.stop()
        relay.shutdown.assert_called_once()

    def test_build_udp_command_probes_reconnect_flags_once(self):
        server = DiscoveryServer(
            device_id="104ABCDE",
            base_url="http://127.0.0.1:5004",
            tuner_count=1,
        )
        calls = []

        def fake_reconnect(path):
            calls.append(path)
            return ["-reconnect", "1"]

        with mock.patch.object(server, "_ffmpeg_reconnect_args", side_effect=fake_reconnect), \
                mock.patch.object(server, "_http_persistent_args", return_value=[]):
            cmd = server._build_udp_ffmpeg_cmd("http://example.com/live/stream.m3u8")

        self.assertEqual(len(calls), 1)
        self.assertIn("-reconnect", cmd)
        self.assertIn("pipe:1", cmd)


if __name__ == "__main__":
    unittest.main()
