# -*- coding: utf-8 -*-
"""Stdlib-only tests for the guide review feature and HTTP server behavior.

Run with:
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy.config import Config  # noqa: E402
from hdhr_proxy.m3u_parser import M3UChannel, build_lineup  # noqa: E402
from hdhr_proxy.mxf import write_mxf  # noqa: E402
from hdhr_proxy.reviews import (  # noqa: E402
    enrich_xmltv_with_reviews,
    extract_review_text,
    merge_review_into_description,
    synthesize_review,
)

MXF_NS = "{urn:com:dontocsata:xmltv:mxf}"

SAMPLE_XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="test">
  <channel id="ch1"><display-name>Channel One</display-name></channel>
  <channel id="ch2"><display-name>News 24</display-name></channel>
  <programme start="20260101050000 +0000" stop="20260101070000 +0000" channel="ch1">
    <title>The Big Movie</title>
    <desc>An action film about heists.</desc>
    <category>Action</category>
    <category>Movie</category>
    <date>2019</date>
    <star-rating><value>8/10</value></star-rating>
    <review type="text" source="Upstream">A critic already reviewed this.</review>
  </programme>
  <programme start="20260101080000 +0000" stop="20260101090000 +0000" channel="ch2">
    <title>Morning News</title>
    <desc>Headlines and weather.</desc>
    <category>News</category>
  </programme>
</tv>
"""


class ReviewEnrichmentTests(unittest.TestCase):
    def test_generates_only_missing_reviews(self):
        enriched, generated = enrich_xmltv_with_reviews(SAMPLE_XMLTV, generate_missing=True)
        self.assertEqual(generated, 1)
        root = ET.fromstring(enriched)
        for programme in root.findall("programme"):
            self.assertIsNotNone(programme.find("review"))

    def test_source_review_untouched(self):
        enriched, _ = enrich_xmltv_with_reviews(SAMPLE_XMLTV)
        root = ET.fromstring(enriched)
        source = root.findall("programme")[0].find("review")
        self.assertEqual(source.text.strip(), "A critic already reviewed this.")
        self.assertEqual(source.attrib.get("source"), "Upstream")

    def test_generated_review_labeled_and_deterministic(self):
        first, _ = enrich_xmltv_with_reviews(SAMPLE_XMLTV)
        second, _ = enrich_xmltv_with_reviews(SAMPLE_XMLTV)
        review_a = extract_review_text(ET.fromstring(first).findall("programme")[1])
        review_b = extract_review_text(ET.fromstring(second).findall("programme")[1])
        self.assertEqual(review_a, review_b)

    def test_generate_missing_disabled(self):
        _, generated = enrich_xmltv_with_reviews(SAMPLE_XMLTV, generate_missing=False)
        self.assertEqual(generated, 0)

    def test_invalid_xml_returns_original(self):
        original = "not xml at all"
        output, generated = enrich_xmltv_with_reviews(original)
        self.assertEqual(output, original)
        self.assertEqual(generated, 0)

    def test_tone_follows_star_rating(self):
        glowing = synthesize_review("Great Film", None, "2020", ["Movie"], "10", "seed-a")
        self.assertIn("Great Film", glowing)
        self.assertTrue(glowing.endswith("."))
        self.assertLess(len(glowing), 400)

    def test_year_appears_in_closer(self):
        review = synthesize_review("Old Film", None, "1987", ["Drama"], "9", "seed-b")
        self.assertIn("(1987)", review)


class ReviewMergeTests(unittest.TestCase):
    def test_merge_appends_to_description(self):
        merged = merge_review_into_description("Plot summary.", "Nice watch.")
        self.assertTrue(merged.startswith("Plot summary."))
        self.assertIn("Review: Nice watch.", merged)

    def test_merge_without_description(self):
        self.assertEqual(merge_review_into_description(None, "Solo."), "Review: Solo.")

    def test_merge_clamps_length(self):
        merged = merge_review_into_description("x" * 1500, "Short.")
        self.assertIsNotNone(merged)
        self.assertLessEqual(len(merged), 1001)

    def test_merge_without_review_keeps_description(self):
        self.assertEqual(merge_review_into_description("Just a plot.", None), "Just a plot.")


class MxfReviewIntegrationTests(unittest.TestCase):
    def test_mxf_descriptions_include_reviews(self):
        base_url = "http://127.0.0.1:5004"
        channels = []
        for index in range(1, 3):
            channel = M3UChannel()
            channel.name = "Channel %d" % index
            channel.tvg_id = "ch%d" % index
            channel.url = "%s/stream/%d.1" % (base_url, index)
            channels.append(channel)
        lineup, channel_map = build_lineup(channels, base_url=base_url)
        out_path = os.path.join(tempfile.mkdtemp(), "guide_test.mxf")
        write_mxf(SAMPLE_XMLTV, lineup, channel_map, out_path)
        descriptions = [
            node.attrib.get("description", "")
            for node in ET.parse(out_path).getroot().iter(MXF_NS + "Program")
        ]
        self.assertTrue(any("Review:" in text for text in descriptions))
        self.assertTrue(any("A critic already reviewed this." in text for text in descriptions))


class HttpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hdhr_proxy.http_server import HDHRHTTPServer

        channels = []
        for index in range(1, 4):
            channel = M3UChannel()
            channel.name = "Chan %d" % index
            channel.tvg_id = "id%d" % index
            channel.url = "http://127.0.0.1:1/stream/%d.ts" % index
            channels.append(channel)
        cls.config = Config(None)
        cls.lineup, cls.channel_map = build_lineup(channels, base_url="http://127.0.0.1:0")
        cls.server = HDHRHTTPServer(
            host="127.0.0.1",
            port=0,
            lineup=cls.lineup,
            channel_map=cls.channel_map,
            config=cls.config,
        )
        cls.server.start()
        cls.port = cls.server._server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _url(self, path):
        return "http://127.0.0.1:%s%s" % (self.port, path)

    def test_discover_json(self):
        body = json.loads(urllib.request.urlopen(self._url("/discover.json"), timeout=5).read())
        self.assertEqual(body["DeviceID"], self.config.device_id)

    def test_head_has_no_body(self):
        response = urllib.request.urlopen(
            urllib.request.Request(self._url("/lineup.json"), method="HEAD"), timeout=5
        )
        self.assertEqual(response.read(), b"")
        self.assertGreater(int(response.headers.get("Content-Length") or 0), 0)

    def test_root_reports_guide_reviews(self):
        body = json.loads(urllib.request.urlopen(self._url("/"), timeout=5).read())
        self.assertIs(body.get("GuideReviews"), True)

    @unittest.skipUnless(os.name == "nt", "socket handle cleanup is Windows-specific")
    def test_stream_stops_promptly_when_terminated(self):
        import hdhr_proxy.http_server as http_module

        class StubStreamSession(object):
            def __init__(self, *args, **kwargs):
                pass

            def stream(self):
                for _ in range(1000):
                    yield b"\x47" * 188

        original = http_module.StreamSession
        http_module.StreamSession = StubStreamSession
        try:
            response = urllib.request.urlopen(self._url("/stream/3.1"), timeout=5)
            self.assertEqual(response.status, 200)
            response.fp.raw._sock.close()
        finally:
            http_module.StreamSession = original

    def test_lineup_json_while_stream_active(self):
        # A slow fake stream must not block other endpoints (threading server).
        ready = threading.Event()

        class StubStreamSession(object):
            def __init__(self, *args, **kwargs):
                pass

            def stream(self):
                ready.set()
                for _ in range(50):
                    yield b"\x47" * 188

        import hdhr_proxy.http_server as http_module

        original = http_module.StreamSession
        http_module.StreamSession = StubStreamSession
        try:
            stream_thread = threading.Thread(
                target=lambda: urllib.request.urlopen(self._url("/stream/2.1"), timeout=10).read(),
                daemon=True,
            )
            stream_thread.start()
            self.assertTrue(ready.wait(5))
            body = json.loads(urllib.request.urlopen(self._url("/lineup.json"), timeout=5).read())
            self.assertEqual(body[0]["GuideNumber"], "2.1")
        finally:
            http_module.StreamSession = original


if __name__ == "__main__":
    unittest.main()
