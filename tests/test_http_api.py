# -*- coding: utf-8 -*-
"""Stdlib-only tests for the HDHomeRun-compatible HTTP API."""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy import __version__  # noqa: E402
from hdhr_proxy.config import Config  # noqa: E402
from hdhr_proxy.http_server import (  # noqa: E402
    HDHRHTTPServer,
    HDHRRequestHandler,
    _build_logo_url_map,
    _logo_image_id,
)
from hdhr_proxy.m3u_parser import M3UChannel, build_lineup  # noqa: E402
from hdhr_proxy.xmltv import XMLTVData  # noqa: E402

SAMPLE_XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv><channel id="id1"><display-name>Chan 1</display-name></channel>
<programme start="20260101000000 +0000" stop="20260101010000 +0000" channel="id1">
<title>Show</title></programme></tv>
"""


def _channels():
    channels = []
    for index in range(1, 4):
        channel = M3UChannel()
        channel.name = 'Chan "Quoted" & %d' % index
        channel.tvg_id = "id%d" % index
        channel.tvg_name = 'Logo "Name" %d' % index
        channel.tvg_logo = "http://example.com/logo%d.png" % index
        channel.url = "http://127.0.0.1:1/stream/%d.ts" % index
        channels.append(channel)
    return channels


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = Config(None)
        cls.lineup, cls.channel_map = build_lineup(
            _channels(), base_url="http://127.0.0.1:0"
        )
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

    def setUp(self):
        HDHRRequestHandler.active_streams = {}
        HDHRRequestHandler.xmltv_data = None

    def _url(self, path):
        return "http://127.0.0.1:%s%s" % (self.port, path)

    def _get(self, path):
        return urllib.request.urlopen(self._url(path), timeout=5)

    def test_discover_and_lineup_endpoints(self):
        root = json.loads(self._get("/").read())
        self.assertEqual(root["ProxyVersion"], __version__)
        self.assertEqual(root["Channels"], 3)

        discover = json.loads(self._get("/discover.json").read())
        self.assertEqual(discover["TunerCount"], self.config.tuner_count)
        self.assertEqual(discover["LineupURL"], discover["BaseURL"] + "/lineup.json")

        lineup = json.loads(self._get("/lineup.json").read())
        self.assertEqual(len(lineup), 3)
        self.assertEqual(lineup[0]["GuideNumber"], "2.1")
        self.assertEqual(lineup[0]["URL"], "http://127.0.0.1:0/stream/2.1")

        status = json.loads(self._get("/lineup_status.json").read())
        self.assertEqual(status["Source"], "Antenna")
        self.assertEqual(status["ScanInProgress"], 0)

    def test_lineup_xml_is_well_formed_and_escaped(self):
        body = self._get("/lineup.xml").read().decode("utf-8")
        root = ET.fromstring(body)
        names = [node.text for node in root.iter("GuideName")]
        self.assertEqual(len(names), 3)
        self.assertIn('Chan "Quoted" & 1', names)

    def test_lineup_m3u_escapes_attribute_quotes(self):
        body = self._get("/lineup.m3u").read().decode("utf-8")
        self.assertIn('tvg-name="Logo &quot;Name&quot; 1"', body)
        self.assertIn(',Chan "Quoted" & 1', body)
        self.assertIn("#EXTM3U", body)

    def test_device_xml_is_well_formed_and_escaped(self):
        old_name = self.config.device_name
        self.config.device_name = 'Living & "Room" <1>'
        try:
            body = self._get("/device.xml").read().decode("utf-8")
        finally:
            self.config.device_name = old_name
        root = ET.fromstring(body)
        friendly = root.find(".//{urn:schemas-upnp-org:device-1-0}friendlyName")
        self.assertEqual(friendly.text, 'Living & "Room" <1>')

    def test_unknown_route_and_channel_return_404(self):
        with self.assertRaises(urllib.error.HTTPError) as missing_route:
            self._get("/nope")
        self.assertEqual(missing_route.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as missing_channel:
            self._get("/stream/99.1")
        self.assertEqual(missing_channel.exception.code, 404)

    def test_head_stream_does_not_start_a_session(self):
        from unittest import mock

        with mock.patch("hdhr_proxy.http_server.StreamSession") as session:
            response = urllib.request.urlopen(
                urllib.request.Request(self._url("/stream/2.1"), method="HEAD"), timeout=5
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"")
        session.assert_not_called()
        self.assertEqual(HDHRRequestHandler.active_streams, {})

    def test_tuner_limit_is_enforced(self):
        old_count = self.config.tuner_count
        self.config.tuner_count = 2
        HDHRRequestHandler.active_streams = {
            "a": threading.Event(),
            "b": threading.Event(),
        }
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self._get("/stream/2.1")
        finally:
            self.config.tuner_count = old_count
            HDHRRequestHandler.active_streams = {}
        self.assertEqual(error.exception.code, 503)

    def test_same_channel_can_be_tuned_twice_and_is_tracked_separately(self):
        from unittest import mock

        release = threading.Event()
        both_started = threading.Event()
        created_lock = threading.Lock()
        created = [0]

        class BlockingSession(object):
            def __init__(self, *args, **kwargs):
                with created_lock:
                    created[0] += 1
                    if created[0] >= 2:
                        both_started.set()

            def stream(self):
                release.wait(5)
                yield b"\x47" * 188

        errors = []

        def open_stream():
            try:
                urllib.request.urlopen(self._url("/stream/3.1"), timeout=5).read()
            except Exception as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)

        with mock.patch("hdhr_proxy.http_server.StreamSession", BlockingSession):
            threads = [threading.Thread(target=open_stream) for _ in range(2)]
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(both_started.wait(5))
                deadline = time.time() + 5
                while time.time() < deadline and len(HDHRRequestHandler.active_streams) < 2:
                    time.sleep(0.01)
                self.assertEqual(len(HDHRRequestHandler.active_streams), 2)
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(HDHRRequestHandler.active_streams, {})

    def test_xmltv_endpoint_is_404_without_guide_and_serves_filtered_guide(self):
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self._get("/xmltv.xml")
        self.assertEqual(missing.exception.code, 404)

        HDHRRequestHandler.xmltv_data = XMLTVData(
            raw_xml=SAMPLE_XMLTV,
            filtered_xml=SAMPLE_XMLTV,
            source="test",
        )
        body = self._get("/epg.xml").read().decode("utf-8")
        self.assertIn("<title>Show</title>", body)

    def test_logo_identity_is_deterministic_and_map_is_built(self):
        first = _logo_image_id("http://example.com/a.png")
        second = _logo_image_id("http://example.com/a.png")
        self.assertEqual(first, second)
        self.assertNotEqual(first, _logo_image_id("http://example.com/b.png"))
        logos = _build_logo_url_map(self.channel_map)
        self.assertEqual(len(logos), 3)
        self.assertIn(_logo_image_id("http://example.com/logo1.png"), logos)


if __name__ == "__main__":
    unittest.main()
