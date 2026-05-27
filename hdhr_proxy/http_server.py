import json
import logging
import threading
import xml.sax.saxutils
import hashlib
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .streamer import StreamSession
from .xmltv import XMLTVData

logger = logging.getLogger(__name__)


def get_base_url(config) -> str:
    return getattr(config, "advertised_base_url", config.base_url)


def make_device_xml(
    device_id: str,
    friendly_name: str,
    model_number: str,
    firmware_version: str,
    base_url: str,
    tuner_count: int,
) -> str:
    return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <device>
    <deviceType>urn:schemas-silicondust-com:device:hdhomerun:1</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>SiliconDust</manufacturer>
    <manufacturerURL>http://www.silicondust.com</manufacturerURL>
    <modelDescription>Virtual HDHomeRun</modelDescription>
    <modelName>{model_number}</modelName>
    <modelNumber>{model_number}</modelNumber>
    <modelURL>http://www.silicondust.com</modelURL>
    <serialNumber>{device_id}</serialNumber>
    <UDN>uuid:{device_id}</UDN>
    <presentationURL>{base_url}</presentationURL>
  </device>
</root>"""


class HDHRRequestHandler(BaseHTTPRequestHandler):
    server_version = "VirtualHDHR/1.0"
    sys_version = ""

    lineup: List[Dict] = []
    channel_map: Dict = {}
    config = None
    xmltv_data: Optional[XMLTVData] = None
    on_stream_start: Optional[Callable] = None
    on_stream_stop: Optional[Callable] = None
    active_streams: Dict[str, threading.Event] = {}
    logo_urls: Dict[str, str] = {}
    logo_cache: Dict[str, tuple] = {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        logger.info("HTTP GET %s", path)

        handlers = {
            "/discover.json": self._handle_discover_json,
            "/lineup.json": self._handle_lineup_json,
            "/lineup.xml": self._handle_lineup_xml,
            "/lineup.m3u": self._handle_lineup_m3u,
            "/xmltv.xml": self._handle_xmltv_xml,
            "/epg.xml": self._handle_xmltv_xml,
            "/lineup_status.json": self._handle_lineup_status,
            "/device.xml": self._handle_device_xml,
        }

        if path in handlers:
            handlers[path]()
        elif path.startswith("/stream/"):
            channel_id = path[len("/stream/") :]
            self._handle_stream(channel_id, query)
        elif path.startswith("/logos/"):
            self._handle_logo(path[len("/logos/") :])
        elif path in ("/", ""):
            self._handle_root()
        else:
            self.send_error(404, "Not Found")

    def _handle_root(self):
        body = json.dumps(
            {
                "FriendlyName": self.config.device_name,
                "ModelNumber": self.config.model_number,
                "FirmwareVersion": self.config.firmware_version,
                "DeviceID": self.config.device_id,
                "TunerCount": self.config.tuner_count,
                "BaseURL": get_base_url(self.config),
                "LineupURL": f"{get_base_url(self.config)}/lineup.json",
                "Channels": len(self.lineup),
                "XMLTVURL": f"{get_base_url(self.config)}/xmltv.xml" if self.xmltv_data else None,
            },
            indent=2,
        )
        self._send_json(body)

    def _handle_discover_json(self):
        body = json.dumps(
            {
                "FriendlyName": self.config.device_name,
                "ModelNumber": self.config.model_number,
                "FirmwareName": "hdhr4_linux",
                "FirmwareVersion": self.config.firmware_version,
                "DeviceID": self.config.device_id,
                "DeviceAuth": "virtual",
                "TunerCount": self.config.tuner_count,
                "BaseURL": get_base_url(self.config),
                "LineupURL": f"{get_base_url(self.config)}/lineup.json",
                "XMLTVURL": f"{get_base_url(self.config)}/xmltv.xml" if self.xmltv_data else None,
            },
            indent=2,
        )
        self._send_json(body)

    def _handle_lineup_json(self):
        self._send_json(json.dumps(self.lineup, indent=2))

    def _handle_lineup_xml(self):
        rows = ['<?xml version="1.0" encoding="UTF-8"?>', "<Lineup>"]
        for item in self.lineup:
            rows.append("  <Program>")
            for key in ("GuideNumber", "GuideName", "URL"):
                value = xml.sax.saxutils.escape(str(item.get(key, "")))
                rows.append(f"    <{key}>{value}</{key}>")
            rows.append("  </Program>")
        rows.append("</Lineup>")
        self._send_xml("\r\n".join(rows) + "\r\n")

    def _handle_lineup_m3u(self):
        rows = ["#EXTM3U"]
        for item in self.lineup:
            guide = item.get("GuideNumber", "")
            name = item.get("GuideName", guide)
            channel = self.channel_map.get(guide)
            tvg_id = xml.sax.saxutils.escape(getattr(channel, "tvg_id", "") or "")
            tvg_name = xml.sax.saxutils.escape(getattr(channel, "tvg_name", "") or name)
            rows.append(
                f"#EXTINF:-1 channel-id=\"{guide}\" tvg-id=\"{tvg_id}\" tvg-name=\"{tvg_name}\" tvg-chno=\"{guide}\",{name}"
            )
            rows.append(str(item.get("URL", "")))
        self._send_text("\r\n".join(rows) + "\r\n", "audio/x-mpegurl; charset=utf-8")

    def _handle_xmltv_xml(self):
        if not self.xmltv_data:
            self.send_error(404, "XMLTV not configured")
            return
        self._send_xml(self.xmltv_data.filtered_xml)

    def _handle_logo(self, logo_name: str):
        logo_id = logo_name.rsplit(".", 1)[0]
        remote_url = self.logo_urls.get(logo_id)
        if not remote_url:
            self.send_error(404, "Logo not found")
            return

        cached = self.logo_cache.get(logo_id)
        if cached:
            content_type, data = cached
        else:
            try:
                req = urllib.request.Request(
                    remote_url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    },
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content_type = resp.headers.get("Content-Type") or _logo_content_type(remote_url)
                    data = resp.read(2 * 1024 * 1024)
            except Exception as exc:
                logger.warning("Unable to fetch channel logo %s from %s: %s", logo_id, remote_url, exc)
                self.send_error(502, "Unable to fetch logo")
                return
            self.logo_cache[logo_id] = (content_type, data)

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _handle_lineup_status(self):
        body = json.dumps(
            {
                "ScanInProgress": 0,
                "ScanPossible": 1,
                "Source": self.config.lineup_source,
                "SourceList": [self.config.lineup_source],
                "SupportedTypes": [self.config.lineup_source],
            },
            indent=2,
        )
        self._send_json(body)

    def _handle_device_xml(self):
        xml = make_device_xml(
            device_id=self.config.device_id,
            friendly_name=self.config.device_name,
            model_number=self.config.model_number,
            firmware_version=self.config.firmware_version,
            base_url=get_base_url(self.config),
            tuner_count=self.config.tuner_count,
        )
        encoded = xml.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(encoded)

    def _handle_stream(self, channel_id: str, query: Dict):
        del query
        if channel_id not in self.channel_map:
            self.send_error(404, f"Channel {channel_id} not found")
            return

        active_count = len(self.active_streams)
        if active_count >= self.config.tuner_count:
            logger.warning("Max tuners (%s) reached, rejecting %s", self.config.tuner_count, channel_id)
            self.send_error(503, "All tuners in use")
            return

        stop_event = threading.Event()
        self.active_streams[channel_id] = stop_event

        if self.on_stream_start:
            self.on_stream_start(channel_id)

        try:
            session = StreamSession(
                channel_id=channel_id,
                channel_map=self.channel_map,
                ffmpeg_enabled=self.config.ffmpeg_enabled,
                ffmpeg_path=self.config.ffmpeg_path,
                output_codec=self.config.ffmpeg_output_codec,
                audio_codec=self.config.ffmpeg_audio_codec,
                bitrate=self.config.ffmpeg_bitrate,
                vista_mode=bool(getattr(self.config, "force_vista_mode", False)),
            )

            self.send_response(200)
            self.send_header("Content-Type", "video/mpeg")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Connection", "close")
            self.end_headers()

            for chunk in session.stream():
                if stop_event.is_set():
                    break
                if not chunk:
                    continue
                chunk_len = hex(len(chunk))[2:].encode("ascii")
                self.wfile.write(chunk_len + b"\r\n" + chunk + b"\r\n")
                self.wfile.flush()

            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

        except Exception as e:
            logger.error("Stream error for %s: %s", channel_id, e)
            try:
                self.send_error(500, f"Stream error: {e}")
            except OSError:
                pass
        finally:
            self.active_streams.pop(channel_id, None)
            if self.on_stream_stop:
                self.on_stream_stop(channel_id)
            logger.info("Stream ended for channel %s", channel_id)

    def _send_json(self, body: str):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_xml(self, body: str):
        self._send_text(body, "application/xml; charset=utf-8")

    def _send_text(self, body: str, content_type: str):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        logger.debug("HTTP: %s", fmt % args)

    def do_HEAD(self):
        self.do_GET()


class HDHRHTTPServer:
    def __init__(
        self,
        host: str,
        port: int,
        lineup: List[Dict],
        channel_map: Dict,
        config,
        xmltv_data: Optional[XMLTVData] = None,
    ):
        self.host = host
        self.port = port
        self.lineup = lineup
        self.channel_map = channel_map
        self.config = config
        self.xmltv_data = xmltv_data
        self._server = None
        self._thread = None

    def start(self):
        HDHRRequestHandler.lineup = self.lineup
        HDHRRequestHandler.channel_map = self.channel_map
        HDHRRequestHandler.config = self.config
        HDHRRequestHandler.xmltv_data = self.xmltv_data
        HDHRRequestHandler.active_streams = {}
        HDHRRequestHandler.logo_urls = _build_logo_url_map(self.channel_map)
        HDHRRequestHandler.logo_cache = {}

        self._server = HTTPServer((self.host, self.port), HDHRRequestHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="http-server",
        )
        self._thread.start()
        logger.info("HTTP server started on http://%s:%s", self.host, self.port)

    def stop(self):
        if self._server:
            self._server.shutdown()
            logger.info("HTTP server stopped")


def _build_logo_url_map(channel_map: Dict) -> Dict[str, str]:
    logos: Dict[str, str] = {}
    for channel in channel_map.values():
        logo_url = (getattr(channel, "tvg_logo", "") or "").strip()
        if not logo_url:
            continue
        logo_id = _logo_image_id(logo_url)
        logos.setdefault(logo_id, logo_url)
    return logos


def _logo_image_id(image_url: str) -> str:
    digest = hashlib.md5(image_url.encode("utf-8")).hexdigest()
    return "i" + str(1 + (int(digest[:12], 16) % 2147483000))


def _logo_content_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return "image/jpeg"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".gif"):
        return "image/gif"
    return "image/png"
