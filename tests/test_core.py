# -*- coding: utf-8 -*-
"""Stdlib-only tests for configuration, playlist parsing, and XMLTV handling."""
import gzip
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy.config import Config, ConfigError  # noqa: E402
from hdhr_proxy.http_utils import HTTPBodyError, decode_http_body  # noqa: E402
from hdhr_proxy.m3u_parser import M3UChannel, M3UParser, build_lineup  # noqa: E402
from hdhr_proxy.xmltv import (  # noqa: E402
    _filter_xmltv,
    load_xmltv,
    resolve_channel_xmltv_ids,
)

SAMPLE_XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="ch1"><display-name>Channel One</display-name></channel>
  <channel id="ch2"><display-name>News &amp; Weather</display-name></channel>
  <programme start="20260101050000 +0000" stop="20260101070000 +0000" channel="ch1">
    <title>Movie One</title>
  </programme>
  <programme start="20260101070000 +0000" stop="20260101080000 +0000" channel="ch2">
    <title>News One</title>
  </programme>
</tv>
"""


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = Config(None)
        self.assertEqual(cfg.http_port, 5004)
        self.assertEqual(cfg.tuner_count, 4)
        self.assertTrue(cfg.ffmpeg_enabled)
        self.assertEqual(cfg.base_url, "http://0.0.0.0:5004")

    def test_file_values_override_defaults_and_coerce_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "http_port": "8080",
                        "tuner_count": 2,
                        "ffmpeg_enabled": "no",
                        "device_name": "  Living Room  ",
                        "channel_mapping": {"One": "10.1"},
                    },
                    f,
                )
            cfg = Config(path)
        self.assertEqual(cfg.http_port, 8080)
        self.assertEqual(cfg.tuner_count, 2)
        self.assertFalse(cfg.ffmpeg_enabled)
        self.assertEqual(cfg.device_name, "Living Room")
        self.assertEqual(cfg.channel_mapping, {"One": "10.1"})
        self.assertEqual(cfg.config_file, path)

    def test_missing_file_is_reported(self):
        with self.assertRaises(ConfigError):
            Config(os.path.join(tempfile.gettempdir(), "definitely-missing-hdhr.json"))

    def test_invalid_json_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(ConfigError):
                Config(path)

    def test_non_object_json_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([1, 2, 3], f)
            with self.assertRaises(ConfigError):
                Config(path)

    def test_invalid_ranges_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            for payload in (
                {"tuner_count": 0},
                {"tuner_count": "many"},
                {"http_port": 70000},
                {"max_physical_channel": 100},
                {"channel_mapping": ["not", "a", "dict"]},
            ):
                path = os.path.join(tmp, "config.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                with self.assertRaises(ConfigError):
                    Config(path)

    def test_unknown_keys_warn_but_do_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"tuner_count": 1, "typo_setting": True}, f)
            with self.assertLogs("hdhr_proxy.config", level="WARNING"):
                cfg = Config(path)
        self.assertEqual(cfg.tuner_count, 1)

    def test_validate_catches_post_construction_overrides(self):
        cfg = Config(None)
        cfg.tuner_count = 0
        with self.assertRaises(ConfigError):
            cfg.validate()


class HttpBodyTests(unittest.TestCase):
    def test_plain_body_is_unchanged(self):
        self.assertEqual(decode_http_body(b"hello"), b"hello")

    def test_gzip_magic_is_decoded(self):
        payload = gzip.compress(b"#EXTM3U\n")
        self.assertEqual(decode_http_body(payload), b"#EXTM3U\n")

    def test_mislabelled_plain_body_is_used_as_is(self):
        self.assertEqual(
            decode_http_body(b"#EXTM3U\n", headers={"Content-Encoding": "gzip"}),
            b"#EXTM3U\n",
        )

    def test_name_hint_plain_body_is_used_as_is(self):
        self.assertEqual(decode_http_body(b"plain", name="guide.xml.gz"), b"plain")

    def test_corrupt_gzip_magic_raises(self):
        with self.assertRaises(HTTPBodyError):
            decode_http_body(b"\x1f\x8bnot-really-gzip")


class M3UParserTests(unittest.TestCase):
    def test_parses_extinf_properties_and_names(self):
        raw = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="one" tvg-chno="2.1" tvg-name="One HD" '
            'tvg-logo="http://x/one.png" group-title="News",Channel One\n'
            "http://example.com/one.ts\n"
            "#EXTINF:-1,Channel Two\n"
            "http://example.com/two.ts\n"
        )
        channels = M3UParser.parse_text(raw)
        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0].tvg_id, "one")
        self.assertEqual(channels[0].tvg_chno, "2.1")
        self.assertEqual(channels[0].tvg_logo, "http://x/one.png")
        self.assertEqual(channels[0].group_title, "News")
        self.assertEqual(channels[0].name, "Channel One")
        self.assertEqual(channels[0].url, "http://example.com/one.ts")
        self.assertEqual(channels[1].name, "Channel Two")

    def test_remote_relative_urls_are_resolved(self):
        channels = [
            M3UChannel(),
            M3UChannel(),
        ]
        channels[0].url = "live/one.ts"
        channels[1].url = "https://cdn.example/two.ts"
        resolved = M3UParser._resolve_relative_urls(channels, "https://example.com/lists/main.m3u")
        self.assertEqual(resolved[0].url, "https://example.com/lists/live/one.ts")
        self.assertEqual(resolved[1].url, "https://cdn.example/two.ts")

    def test_local_relative_urls_are_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = M3UChannel()
            channel.url = "streams/one.ts"
            resolved = M3UParser._resolve_relative_urls([channel], os.path.join(tmp, "list.m3u"))
        self.assertEqual(
            resolved[0].url,
            os.path.abspath(os.path.join(tmp, "streams", "one.ts")),
        )

    def test_hls_variant_selection_prefers_highest_quality(self):
        variants = M3UParser._hls_variant_uris(
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360\nlow.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\nhigh.m3u8\n'
        )
        self.assertEqual(len(variants), 2)
        self.assertEqual(M3UParser._select_hls_variant(variants), "high.m3u8")

    def test_selected_hls_variant_uses_bandwidth_as_tiebreaker(self):
        variants = M3UParser._hls_variant_uris(
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x720\nslow.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720\nfast.m3u8\n'
        )
        self.assertEqual(M3UParser._select_hls_variant(variants), "fast.m3u8")

    def test_local_master_uses_existing_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = os.path.join(tmp, "master.m3u8")
            variant = os.path.join(tmp, "high.m3u8")
            with open(master, "w", encoding="utf-8") as f:
                f.write(
                    "#EXTM3U\n"
                    '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\nhigh.m3u8\n'
                )
            with open(variant, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
            channels = M3UParser.parse_file(master)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].url, variant)
        self.assertEqual(channels[0].ext.get("hls_playback_url"), variant)

    def test_local_master_reports_missing_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = os.path.join(tmp, "master.m3u8")
            with open(master, "w", encoding="utf-8") as f:
                f.write(
                    "#EXTM3U\n"
                    '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\nmissing.m3u8\n'
                )
            with mock.patch("hdhr_proxy.m3u_parser.logger.warning") as warning:
                channels = M3UParser.parse_file(master)
        self.assertIn("missing.m3u8", channels[0].ext.get("hls_missing_variants", ""))
        warning.assert_called()

    def test_gzipped_local_playlist_is_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "channels.m3u.gz")
            with open(path, "wb") as f:
                f.write(gzip.compress(b"#EXTM3U\n#EXTINF:-1,One\nhttp://x/one.ts\n"))
            channels = M3UParser.parse_file(path)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "One")

    def test_utf8_bom_playlist_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "channels.m3u")
            with open(path, "wb") as f:
                f.write(b"\xef\xbb\xbf#EXTM3U\n#EXTINF:-1,One\nhttp://x/one.ts\n")
            channels = M3UParser.parse_file(path)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "One")

    def test_gzipped_remote_playlist_is_decoded(self):
        class FakeResponse(object):
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return gzip.compress(b"#EXTM3U\n#EXTINF:-1,One\nhttp://x/one.ts\n")

        with mock.patch("hdhr_proxy.m3u_parser.urllib.request.urlopen", return_value=FakeResponse()):
            channels = M3UParser.parse_url("https://example.com/list.m3u")
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "One")


class LineupTests(unittest.TestCase):
    def _channels(self, count):
        channels = []
        for index in range(1, count + 1):
            channel = M3UChannel()
            channel.name = "Channel %d" % index
            channel.tvg_id = "ch%d" % index
            channel.url = "http://example.com/%d.ts" % index
            channels.append(channel)
        return channels

    def test_explicit_channel_numbers_and_mapping_are_used(self):
        channels = self._channels(2)
        channels[0].tvg_chno = "10.1"
        lineup, channel_map = build_lineup(
            channels,
            base_url="http://127.0.0.1:5004",
            channel_mapping={"Channel 2": "20.1"},
        )
        self.assertEqual(lineup[0]["GuideNumber"], "10.1")
        self.assertEqual(lineup[1]["GuideNumber"], "20.1")
        self.assertEqual(lineup[1]["URL"], "http://127.0.0.1:5004/stream/20.1")
        self.assertEqual(channel_map["20.1"].name, "Channel 2")

    def test_automatic_numbers_start_at_two_one(self):
        lineup, _ = build_lineup(self._channels(2), base_url="http://127.0.0.1:5004")
        self.assertEqual(lineup[0]["GuideNumber"], "2.1")
        self.assertEqual(lineup[1]["GuideNumber"], "3.1")

    def test_lineup_metadata_is_populated(self):
        lineup, _ = build_lineup(self._channels(1), base_url="http://127.0.0.1:5004")
        item = lineup[0]
        for key in (
            "GuideNumber", "GuideName", "URL", "PhysicalChannel", "Frequency",
            "ProgramNumber", "PMTPID", "VideoPID", "AudioPID", "ProgramTable",
        ):
            self.assertIn(key, item)
        self.assertGreater(item["Frequency"], 0)
        self.assertIn("tsid=0x", item["ProgramTable"])

    def test_duplicate_guide_numbers_keep_later_stream_source(self):
        channels = self._channels(2)
        channels[0].tvg_chno = "5.1"
        channels[1].tvg_chno = "5.1"
        lineup, channel_map = build_lineup(channels, base_url="http://127.0.0.1:5004")
        self.assertEqual(len(lineup), 2)
        self.assertEqual(channel_map["5.1"].url, "http://example.com/2.ts")

    def test_fixed_programs_per_physical_layout(self):
        lineup, _ = build_lineup(
            self._channels(5),
            base_url="http://127.0.0.1:5004",
            programs_per_physical=2,
        )
        self.assertEqual([item["PhysicalChannel"] for item in lineup], [14, 14, 15, 15, 16])
        self.assertEqual([item["VirtualMinor"] for item in lineup], [1, 2, 1, 2, 1])


class XMLTVTests(unittest.TestCase):
    def _channel_map(self, names):
        channel_map = {}
        for guide_number, (name, tvg_id) in names.items():
            channel = M3UChannel()
            channel.name = name
            channel.tvg_id = tvg_id
            channel_map[guide_number] = channel
        return channel_map

    def test_filters_to_matching_ids(self):
        channel_map = self._channel_map({"2.1": ("Channel One", "ch1")})
        filtered, ids = _filter_xmltv(SAMPLE_XMLTV, channel_map)
        self.assertEqual(ids, {"ch1"})
        self.assertIn('id="ch1"', filtered)
        self.assertIn("Movie One", filtered)
        self.assertNotIn("News One", filtered)

    def test_matches_by_display_name_when_tvg_id_is_missing(self):
        channel_map = self._channel_map({"2.1": ("Channel One", "")})
        filtered, ids = _filter_xmltv(SAMPLE_XMLTV, channel_map)
        self.assertEqual(ids, {"ch1"})
        self.assertIn("Movie One", filtered)

    def test_no_match_returns_raw_xml(self):
        channel_map = self._channel_map({"2.1": ("Nothing", "missing")})
        filtered, ids = _filter_xmltv(SAMPLE_XMLTV, channel_map)
        self.assertEqual(ids, set())
        self.assertEqual(filtered, SAMPLE_XMLTV)

    def test_no_tvg_ids_returns_raw_xml(self):
        channel_map = self._channel_map({"2.1": ("Channel One", "")})
        channel_map["2.1"].name = "Different Display Name"
        channel_map["2.1"].tvg_name = "Also Different"
        filtered, ids = _filter_xmltv(SAMPLE_XMLTV, channel_map)
        self.assertEqual(ids, set())
        self.assertEqual(filtered, SAMPLE_XMLTV)

    def test_malformed_xml_is_served_raw(self):
        channel_map = self._channel_map({"2.1": ("Channel One", "ch1")})
        filtered, ids = _filter_xmltv("<tv><broken>", channel_map)
        self.assertEqual(ids, set())
        self.assertEqual(filtered, "<tv><broken>")

    def test_resolve_channel_ids_uses_display_name_normalization(self):
        channel_map = self._channel_map({"2.1": ("News and Weather", "unknown")})
        resolved = resolve_channel_xmltv_ids(SAMPLE_XMLTV, channel_map)
        self.assertEqual(resolved.get("2.1"), "ch2")

    def test_gzipped_local_guide_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "guide.xml.gz")
            with open(path, "wb") as f:
                f.write(gzip.compress(SAMPLE_XMLTV.encode("utf-8")))
            channel_map = self._channel_map({"2.1": ("Channel One", "ch1")})
            data = load_xmltv(path, None, channel_map)
        self.assertIsNotNone(data)
        self.assertIn("Movie One", data.filtered_xml)


if __name__ == "__main__":
    unittest.main()
