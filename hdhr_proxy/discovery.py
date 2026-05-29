import re
import socket
import struct
import logging
import os
import json
import shutil
import ctypes
import xml.sax.saxutils
import subprocess
import threading
import time
import zlib
import queue
import tempfile
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from .m3u_parser import M3UParser

logger = logging.getLogger(__name__)
CONTROL_TRACE_PATH = os.path.abspath("hdhr_control_trace.log")
_WINMM = ctypes.WinDLL("winmm") if os.name == "nt" else None

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
HDHR_DISCOVERY_PORT = 65001
HDHR_CONTROL_PORT = 65001

HDHR_TYPE_DISCOVER_REQ = 0x0002
HDHR_TYPE_DISCOVER_RPY = 0x0003
HDHR_TYPE_GETSET_REQ = 0x0004
HDHR_TYPE_GETSET_RPY = 0x0005
HDHR_TYPE_UPGRADE_REQ = 0x0006
HDHR_TYPE_UPGRADE_RPY = 0x0007

HDHR_TAG_DEVICE_TYPE = 0x01
HDHR_TAG_DEVICE_ID = 0x02
HDHR_TAG_GETSET_NAME = 0x03
HDHR_TAG_GETSET_VALUE = 0x04
HDHR_TAG_ERROR_MESSAGE = 0x05
HDHR_TAG_TUNER_COUNT = 0x10
HDHR_TAG_LINEUP_URL = 0x27
HDHR_TAG_BASE_URL = 0x2A
HDHR_TAG_DEVICE_AUTH_STR = 0x2B

HDHR_DEVICE_TYPE_WILDCARD = 0xFFFFFFFF
HDHR_DEVICE_TYPE_TUNER = 0x00000001
HDHR_DEVICE_ID_WILDCARD = 0xFFFFFFFF
WSAECONNRESET = 10054
ATSC_PROGRAM_NUMBER = 3

US_BCAST_FREQUENCIES = {
    **{ch: (57 + (ch - 2) * 6) * 1000000 for ch in range(2, 5)},
    5: 79000000,
    6: 85000000,
    **{ch: (177 + (ch - 7) * 6) * 1000000 for ch in range(7, 14)},
    **{ch: (473 + (ch - 14) * 6) * 1000000 for ch in range(14, 70)},
}

SSDP_RESPONSE_TEMPLATE = """\
HTTP/1.1 200 OK\r
CACHE-CONTROL: max-age=60\r
EXT:\r
LOCATION: {base_url}/device.xml\r
SERVER: {os_ver} UPnP/1.0 {product_ver}\r
ST: {st}\r
USN: uuid:{device_id}::urn:schemas-silicondust-com:device:hdhomerun:1\r
\r
"""

FFMPEG_ANALYZE_US = "5000000"
FFMPEG_PROBE_BYTES = "5000000"

DEVICE_ID_CHECKSUM_TABLE = (0xA, 0x5, 0xF, 0x6, 0x7, 0xC, 0x1, 0xB, 0x9, 0x2, 0x8, 0xD, 0x4, 0x3, 0xE, 0x0)


def normalize_device_id(device_id: str) -> str:
    """Return an 8-hex HDHomeRun device id with a valid SiliconDust checksum."""
    clean = re.sub(r"[^0-9A-Fa-f]", "", device_id or "").upper()
    if len(clean) < 7:
        clean = (clean + "104FFFF")[:7]
    prefix = clean[:7]

    for nibble in "0123456789ABCDEF":
        candidate = f"{prefix}{nibble}"
        if is_valid_device_id(candidate):
            return candidate

    return "104FFFFF"


def is_valid_device_id(device_id: str) -> bool:
    try:
        value = int(device_id, 16)
    except (TypeError, ValueError):
        return False
    if not 0 <= value <= 0xFFFFFFFF:
        return False

    checksum = 0
    checksum ^= DEVICE_ID_CHECKSUM_TABLE[(value >> 28) & 0x0F]
    checksum ^= (value >> 24) & 0x0F
    checksum ^= DEVICE_ID_CHECKSUM_TABLE[(value >> 20) & 0x0F]
    checksum ^= (value >> 16) & 0x0F
    checksum ^= DEVICE_ID_CHECKSUM_TABLE[(value >> 12) & 0x0F]
    checksum ^= (value >> 8) & 0x0F
    checksum ^= DEVICE_ID_CHECKSUM_TABLE[(value >> 4) & 0x0F]
    checksum ^= value & 0x0F
    return checksum == 0


def _read_var_length(data: bytes, pos: int) -> Tuple[int, int]:
    if pos >= len(data):
        raise ValueError("Missing TLV length")
    length = data[pos]
    pos += 1
    if length & 0x80:
        if pos >= len(data):
            raise ValueError("Missing extended TLV length")
        length = (length & 0x7F) | (data[pos] << 7)
        pos += 1
    return length, pos


def _write_var_length(length: int) -> bytes:
    if length <= 127:
        return bytes([length])
    return bytes([(length & 0x7F) | 0x80, length >> 7])


def _encode_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _write_var_length(len(value)) + value


def _encode_u32_tlv(tag: int, value: int) -> bytes:
    return _encode_tlv(tag, struct.pack(">I", value & 0xFFFFFFFF))


def _encode_string_tlv(tag: int, value: str) -> bytes:
    return _encode_tlv(tag, value.encode("utf-8") + b"\x00")


def _parse_frame(data: bytes) -> Tuple[int, Dict[int, List[bytes]]]:
    if len(data) < 8:
        raise ValueError("Packet too short")

    frame_type, payload_len = struct.unpack(">HH", data[:4])
    frame_end = 4 + payload_len
    if len(data) < frame_end + 4:
        raise ValueError("Packet truncated")

    expected_crc = struct.unpack("<I", data[frame_end:frame_end + 4])[0]
    actual_crc = zlib.crc32(data[:frame_end]) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        raise ValueError("Bad CRC")

    payload = data[4:frame_end]
    pos = 0
    tags: Dict[int, List[bytes]] = {}
    while pos < len(payload):
        tag = payload[pos]
        pos += 1
        length, pos = _read_var_length(payload, pos)
        if pos + length > len(payload):
            raise ValueError("TLV exceeds payload")
        tags.setdefault(tag, []).append(payload[pos:pos + length])
        pos += length

    return frame_type, tags


def _seal_frame(frame_type: int, payload: bytes) -> bytes:
    frame = struct.pack(">HH", frame_type, len(payload)) + payload
    return frame + struct.pack("<I", zlib.crc32(frame) & 0xFFFFFFFF)


def _tlv_u32(tags: Dict[int, List[bytes]], tag: int, default: int) -> int:
    values = tags.get(tag)
    if not values or len(values[0]) != 4:
        return default
    return struct.unpack(">I", values[0])[0]


def _tlv_string(tags: Dict[int, List[bytes]], tag: int) -> Optional[str]:
    values = tags.get(tag)
    if not values:
        return None
    return values[0].split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _configure_udp_socket(sock: socket.socket):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(1.0)

    # Windows reports ICMP "port unreachable" as WSAECONNRESET on UDP recvfrom.
    # HDHomeRun Setup probes from short-lived sockets, so ignore those resets.
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except OSError:
            pass


def _is_ignorable_udp_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) == WSAECONNRESET or getattr(exc, "errno", None) == WSAECONNRESET


def _set_timer_resolution(enable: bool):
    if not _WINMM:
        return
    try:
        if enable:
            _WINMM.timeBeginPeriod(1)
        else:
            _WINMM.timeEndPeriod(1)
    except Exception:
        pass


class DiscoveryServer:
    def __init__(
        self,
        device_id: str,
        base_url: str,
        tuner_count: int,
        listen_ip: str = "0.0.0.0",
        device_name: str = "Virtual HDHR Proxy",
        model_number: str = "HDHR4-2US",
        firmware_version: str = "20240701",
        stop_event: Optional[threading.Event] = None,
        get_lineup_callback: Optional[Callable] = None,
        channel_map: Optional[Dict] = None,
        lineup: Optional[List[Dict]] = None,
        ffmpeg_path: str = "ffmpeg",
        ffmpeg_enabled: bool = True,
        output_codec: str = "mpeg2video",
        audio_codec: str = "ac3",
        bitrate: str = "4000k",
        force_vista_mode: bool = False,
    ):
        self.device_id = device_id
        self.base_url = base_url
        self.tuner_count = tuner_count
        self.listen_ip = listen_ip
        self.device_name = device_name
        self.model_number = model_number
        self.firmware_version = firmware_version
        self.stop_event = stop_event or threading.Event()
        self.get_lineup_callback = get_lineup_callback
        self.channel_map = channel_map or {}
        self.lineup = lineup or []
        self.ffmpeg_path = self._resolve_ffmpeg_path(ffmpeg_path)
        self.ffmpeg_enabled = ffmpeg_enabled
        self.output_codec = output_codec
        self.audio_codec = audio_codec
        self.bitrate = bitrate
        self.force_vista_mode = force_vista_mode
        self._reported_firmware_version = firmware_version
        self._upgrade_target_firmware_version = self._infer_upgrade_target_version()
        self._lineup_scan_active = False
        self._lineup_scan_map = "us-bcast"
        self._lineup_scan_started_at = 0.0
        self._hls_variant_cache: Dict[str, Tuple[str, float]] = {}
        self._prepared_input_cache: Dict[str, Tuple[str, float]] = {}
        self._state_lock = threading.Lock()
        self._rf_channels = self._build_rf_channel_map()
        self._tuner_state = {
            i: {
                "channel": "none",
                "channelmap": "us-bcast",
                "filter": "0x0000-0x1FFF",
                "program": "none",
                "target": "none",
                "lockkey": "none",
                "status": "ch=none lock=none ss=0 snq=0 seq=0 bps=0 pps=0",
                "streaminfo": "none",
                "rf": None,
                "process": None,
                "log_file": None,
                "stream_stop": None,
                "stream_thread": None,
                "psip_stop": None,
                "psip_thread": None,
                "channel_id": None,
                "source_url": None,
                "temp_source_path": None,
                "stream_announced": False,
                "stream_restart_failures": 0,
                "stream_restart_window_started_at": 0.0,
            }
            for i in range(max(0, tuner_count))
        }

    def start(self):
        threads = [
            threading.Thread(target=self._ssdp_listener, daemon=True, name="ssdp"),
            threading.Thread(target=self._hdhr_legacy_listener, daemon=True, name="hdhr-legacy"),
            threading.Thread(target=self._hdhr_control_listener, daemon=True, name="hdhr-control"),
        ]
        for t in threads:
            t.start()
        logger.info("Discovery servers started (SSDP udp :1900, HDHR udp/tcp :65001)")

    def _make_ssdp_response(self, st: str) -> bytes:
        import platform
        os_ver = platform.platform()
        return SSDP_RESPONSE_TEMPLATE.format(
            base_url=self.base_url,
            os_ver=os_ver,
            product_ver=f"VirtualHDHR/{self.firmware_version}",
            st=st,
            device_id=self.device_id,
        ).encode("utf-8")

    def _ssdp_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        _configure_udp_socket(sock)

        try:
            sock.bind((self.listen_ip, SSDP_PORT))
        except OSError as e:
            logger.warning(f"SSDP bind failed on port {SSDP_PORT}: {e}. "
                           f"SSDP discovery may not work. Try running as administrator.")
            return

        mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            logger.warning(f"SSDP multicast join failed: {e}")

        logger.info("SSDP listener started on udp :1900")
        while not self.stop_event.is_set():
            try:
                data, addr = sock.recvfrom(2048)
                self._handle_ssdp_request(sock, data, addr)
            except socket.timeout:
                continue
            except OSError as e:
                if _is_ignorable_udp_error(e):
                    logger.debug("Ignoring transient SSDP UDP reset")
                    continue
                logger.warning(f"SSDP listener stopped after socket error: {e}")
                break

        sock.close()
        logger.info("SSDP listener stopped")

    def _handle_ssdp_request(self, sock: socket.socket, data: bytes, addr: tuple):
        text = data.decode("utf-8", errors="replace")
        if "M-SEARCH" not in text:
            return

        st_match = re.search(r"^ST:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE)
        if not st_match:
            return
        st = st_match.group(1).strip()

        valid_targets = [
            "urn:schemas-silicondust-com:device:hdhomerun:1",
            "ssdp:all",
            "upnp:rootdevice",
        ]
        if not any(t in st for t in valid_targets):
            return

        resp = self._make_ssdp_response(st)
        try:
            sock.sendto(resp, addr)
            logger.debug(f"SSDP response sent to {addr} for ST={st}")
        except OSError as e:
            logger.debug(f"SSDP sendto failed: {e}")

    def _hdhr_legacy_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        _configure_udp_socket(sock)

        try:
            sock.bind((self.listen_ip, HDHR_DISCOVERY_PORT))
        except OSError as e:
            logger.warning(f"Legacy HDHR bind failed on port {HDHR_DISCOVERY_PORT}: {e}")
            return

        logger.info("HDHR discovery listener started on udp :65001")
        while not self.stop_event.is_set():
            try:
                data, addr = sock.recvfrom(2048)
                self._handle_hdhr_legacy_request(sock, data, addr)
            except socket.timeout:
                continue
            except OSError as e:
                if _is_ignorable_udp_error(e):
                    logger.debug("Ignoring transient HDHR discovery UDP reset")
                    continue
                logger.warning(f"HDHR discovery listener stopped after socket error: {e}")
                break

        sock.close()
        logger.info("HDHR discovery listener stopped")

    def _handle_hdhr_legacy_request(self, sock: socket.socket, data: bytes, addr: tuple):
        try:
            frame_type, tags = _parse_frame(data)
        except ValueError as e:
            logger.debug(f"Ignoring non-HDHR discovery packet from {addr}: {e}")
            return

        if frame_type != HDHR_TYPE_DISCOVER_REQ:
            return

        requested_type = _tlv_u32(tags, HDHR_TAG_DEVICE_TYPE, HDHR_DEVICE_TYPE_WILDCARD)
        requested_id = _tlv_u32(tags, HDHR_TAG_DEVICE_ID, HDHR_DEVICE_ID_WILDCARD)
        device_id = int(self.device_id, 16)

        if requested_type not in (HDHR_DEVICE_TYPE_WILDCARD, HDHR_DEVICE_TYPE_TUNER):
            return
        if requested_id not in (HDHR_DEVICE_ID_WILDCARD, device_id):
            return

        payload = b"".join([
            _encode_u32_tlv(HDHR_TAG_DEVICE_TYPE, HDHR_DEVICE_TYPE_TUNER),
            _encode_u32_tlv(HDHR_TAG_DEVICE_ID, device_id),
            _encode_tlv(HDHR_TAG_TUNER_COUNT, bytes([max(0, min(self.tuner_count, 255))])),
            _encode_string_tlv(HDHR_TAG_DEVICE_AUTH_STR, "virtual"),
            _encode_string_tlv(HDHR_TAG_BASE_URL, self.base_url),
            _encode_string_tlv(HDHR_TAG_LINEUP_URL, f"{self.base_url}/lineup.json"),
        ])
        resp = _seal_frame(HDHR_TYPE_DISCOVER_RPY, payload)

        try:
            sock.sendto(resp, addr)
            logger.info(f"HDHR binary discovery response sent to {addr}")
        except OSError as e:
            logger.debug(f"HDHR discovery sendto failed: {e}")

    def _hdhr_control_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)

        try:
            sock.bind((self.listen_ip, HDHR_CONTROL_PORT))
            sock.listen(8)
        except OSError as e:
            logger.warning(f"HDHR control bind failed on tcp :{HDHR_CONTROL_PORT}: {e}")
            sock.close()
            return

        logger.info("HDHR control listener started on tcp :65001")
        while not self.stop_event.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_hdhr_control_client,
                args=(conn, addr),
                daemon=True,
                name=f"hdhr-control-{addr[0]}",
            ).start()

        sock.close()
        logger.info("HDHR control listener stopped")

    def _handle_hdhr_control_client(self, conn: socket.socket, addr: tuple):
        with conn:
            conn.settimeout(5.0)
            while not self.stop_event.is_set():
                try:
                    data = self._recv_frame(conn)
                    if not data:
                        return
                    frame_type = struct.unpack(">H", data[:2])[0]
                    if frame_type == HDHR_TYPE_UPGRADE_REQ:
                        conn.sendall(self._handle_firmware_upgrade_packet(data))
                        payload_len = struct.unpack(">H", data[2:4])[0]
                        payload = data[4:4 + payload_len]
                        if len(payload) >= 4 and struct.unpack(">I", payload[:4])[0] == 0xFFFFFFFF:
                            return
                        continue
                    if frame_type != HDHR_TYPE_GETSET_REQ:
                        return
                    _, tags = _parse_frame(data)

                    name = _tlv_string(tags, HDHR_TAG_GETSET_NAME) or ""
                    name = self._normalize_control_name(name)
                    set_value = _tlv_string(tags, HDHR_TAG_GETSET_VALUE)
                    if set_value is not None:
                        value = self._set_control_value(name, set_value)
                    else:
                        value = self._get_control_value(name)
                    payload = _encode_string_tlv(HDHR_TAG_GETSET_NAME, name)
                    if value is None:
                        logger.warning("HDHR control unknown variable from %s: %s", addr, name)
                        value = self._default_control_value(name)
                        payload += _encode_string_tlv(HDHR_TAG_GETSET_VALUE, value)
                    else:
                        payload += _encode_string_tlv(HDHR_TAG_GETSET_VALUE, value)
                    conn.sendall(_seal_frame(HDHR_TYPE_GETSET_RPY, payload))
                    logger.info("HDHR control %s: %s%s -> %r", addr, name, f"={set_value}" if set_value is not None else "", value)
                    self._trace_control(addr, name, set_value, value)
                except socket.timeout:
                    return
                except (OSError, ValueError) as e:
                    logger.debug(f"HDHR control client failed for {addr}: {e}")
                    return

    def _recv_frame(self, conn: socket.socket) -> bytes:
        header = self._recv_exact(conn, 4)
        if not header:
            return b""
        _, payload_len = struct.unpack(">HH", header)
        rest = self._recv_exact(conn, payload_len + 4)
        if not rest:
            return b""
        return header + rest

    def _recv_exact(self, conn: socket.socket, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = conn.recv(remaining)
            if not chunk:
                return b""
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _handle_firmware_upgrade_packet(self, data: bytes) -> bytes:
        frame_type, _ = struct.unpack(">HH", data[:4])
        if frame_type != HDHR_TYPE_UPGRADE_REQ:
            return _seal_frame(HDHR_TYPE_UPGRADE_RPY, _encode_string_tlv(HDHR_TAG_ERROR_MESSAGE, "bad upgrade packet"))

        payload_len = struct.unpack(">H", data[2:4])[0]
        payload = data[4:4 + payload_len]
        if len(payload) < 4:
            return _seal_frame(HDHR_TYPE_UPGRADE_RPY, _encode_string_tlv(HDHR_TAG_ERROR_MESSAGE, "bad upgrade position"))

        position = struct.unpack(">I", payload[:4])[0]
        if position == 0xFFFFFFFF:
            self._reported_firmware_version = self._upgrade_target_firmware_version
            logger.info("Virtual firmware upgrade completed; no firmware was written.")
        elif len(payload) != 260:
            logger.debug("Firmware upgrade chunk at %d has unusual payload size %d", position, len(payload))

        # Real devices acknowledge with an empty UPGRADE_RPY frame.
        return _seal_frame(HDHR_TYPE_UPGRADE_RPY, b"")

    def _trace_control(self, addr, name: str, set_value: Optional[str], value: Optional[str]):
        try:
            with open(CONTROL_TRACE_PATH, "a", encoding="utf-8") as f:
                direction = "set" if set_value is not None else "get"
                requested = f"{name}={set_value}" if set_value is not None else name
                f.write(f"{time.strftime('%H:%M:%S')} {addr[0]}:{addr[1]} {direction} {requested} -> {value!r}\n")
        except OSError:
            pass

    def _normalize_control_name(self, name: str) -> str:
        clean = (name or "").replace("\\", "/").strip()
        if not clean:
            return clean
        if not clean.startswith("/"):
            clean = "/" + clean
        aliases = {
            "/sys/hw_model": "/sys/hwmodel",
            "/lineup/locationurl": "/lineup/location_url",
        }
        return aliases.get(clean.lower(), clean)

    def _default_control_value(self, name: str) -> str:
        if "/status" in name:
            rf = self._rf_channels[0] if self._rf_channels else None
            if rf:
                return f"ch=auto:{rf['frequency']} lock=8vsb ss={rf['ss']} snq={rf['snq']} seq={rf['seq']} bps={rf['bps']} pps={rf['bps'] // 188}"
            return "ch=none lock=none ss=0 snq=0 seq=0 bps=0 pps=0"
        if "/streaminfo" in name:
            return self._format_streaminfo_for_physical(self._rf_channels[0]) if self._rf_channels else "none"
        if "/channelmap" in name:
            return "us-bcast"
        if "/channel" in name:
            rf = self._rf_channels[0] if self._rf_channels else None
            return f"auto:{rf['frequency']}" if rf else "none"
        if "/program" in name:
            rf = self._rf_channels[0] if self._rf_channels else None
            return str(rf["program"]) if rf else "none"
        if "/filter" in name:
            return "0x0000-0x1FFF"
        if "/target" in name or "/lockkey" in name or "/scan" in name:
            return "none"
        return "none"

    def _get_control_value(self, name: str) -> Optional[str]:
        firmware_model = self._firmware_model_name()
        values = {
            "/sys/model": firmware_model,
            "/sys/hwmodel": self.model_number,
            "/sys/version": self._reported_firmware_version,
            "/sys/features": "channelmap: us-bcast\nmodulation: 8vsb qam64 qam256\nauto-modulation: auto auto6t auto8t",
            "/sys/copyright": "HDHomeRun virtual tuner proxy",
            "/sys/debug": "VirtualHDHR: ok",
            "/sys/dvbc_modulation": "8vsb",
            "/sys/channelmap": "us-bcast",
            "/sys/tuners": str(self.tuner_count),
            "/lineup/location": "Digital Antenna",
            "/lineup/location_url": f"{self.base_url}/lineup.json",
            "/lineup/scan": self._format_lineup_scan(),
        }
        if name in values:
            return values[name]

        tuner_match = re.match(r"^/tuner(\d+)/(channel|channelmap|filter|program|status|streaminfo|target|lockkey|debug|vchannel|vstatus|scan)$", name)
        if not tuner_match:
            return None
        tuner_idx = int(tuner_match.group(1))
        field = tuner_match.group(2)
        with self._state_lock:
            state = self._tuner_state.get(tuner_idx)
            if not state:
                return None
            if field == "vchannel":
                rf = state.get("rf") or (self._rf_channels[0] if self._rf_channels else None)
                return f"{rf['major']}.{rf['minor']}" if rf else "none"
            if field == "vstatus":
                rf = state.get("rf") or (self._rf_channels[0] if self._rf_channels else None)
                return f"vch={rf['major']}.{rf['minor']} name={rf['name']}" if rf else "none"
            if field == "scan":
                return "none"
            if field == "debug":
                return f"virt=1 channel_id={state.get('channel_id') or 'none'} target={state['target']}"
            return state.get(field, "none")

    def _set_control_value(self, name: str, value: str) -> Optional[str]:
        if name == "/lineup/location":
            return "Digital Antenna"
        if name == "/lineup/scan":
            scan_map = (value or "us-bcast").strip()
            if scan_map.lower() in ("abort", "stop", "none"):
                self._lineup_scan_active = False
                self._lineup_scan_started_at = 0.0
                return "none"
            self._lineup_scan_active = True
            self._lineup_scan_started_at = time.time()
            self._lineup_scan_map = self._normalize_scan_map(scan_map)
            return ""
        if name == "/sys/channelmap":
            return "us-bcast"
        if name == "/sys/restart":
            return "none"

        tuner_match = re.match(r"^/tuner(\d+)/(channel|channelmap|filter|program|target|lockkey|vchannel|scan)$", name)
        if not tuner_match:
            return self._get_control_value(name)

        tuner_idx = int(tuner_match.group(1))
        field = tuner_match.group(2)
        with self._state_lock:
            state = self._tuner_state.get(tuner_idx)
            if not state:
                return None
            if field == "vchannel":
                channel_id, rf = self._select_channel_for_tune(value)
                if channel_id is None:
                    channel_id, rf = self._virtual_rf_for_current_tune(value)
                state["channel_id"] = channel_id
                state["rf"] = rf
                self._refresh_tuner_status(tuner_idx)
                rf = state.get("rf")
                return f"{rf['major']}.{rf['minor']}" if rf else "none"
            if field == "scan":
                channel_id, rf = self._select_channel_for_tune(value)
                if channel_id is None:
                    channel_id, rf = self._virtual_rf_for_current_tune(value)
                state["channel_id"] = channel_id
                state["rf"] = rf
                self._refresh_tuner_status(tuner_idx)
                return "none"
            if field == "channelmap":
                value = "us-bcast"
            state[field] = value or "none"
            if field == "channel":
                previous_rf = state.get("rf") or {}
                previous_physical = int(previous_rf.get("physical") or 0)
                channel_id, rf = self._select_channel_for_tune(value)
                if channel_id is None:
                    channel_id, rf = self._virtual_rf_for_current_tune(value)
                state["channel_id"] = channel_id
                state["rf"] = rf
                state["filter"] = "0x0000-0x1FFF"
                state["program"] = "none"
                current_physical = int((rf or {}).get("physical") or 0)
                if current_physical and previous_physical and current_physical != previous_physical:
                    self._stop_tuner_process_locked(state)
                self._refresh_tuner_status(tuner_idx)
            elif field == "program":
                state["program"] = value or "none"
                if self._program_requests_specific_program(value):
                    channel_id, rf = self._select_program_for_current_tune(state, value)
                    if channel_id is None:
                        channel_id, rf = self._select_channel_for_tune(value)
                        if channel_id is None:
                            channel_id, rf = self._virtual_rf_for_current_tune(value)
                    state["channel_id"] = channel_id
                    state["rf"] = rf
                self._refresh_tuner_status(tuner_idx)
                if self._program_requests_specific_program(value):
                    self._retune_running_target_if_needed_locked(tuner_idx, state)
            elif field == "target":
                if value and value != "none" and not state.get("channel_id") and self._looks_like_atsc_scan_probe(state.get("channel", ""), [int(n) for n in re.findall(r"\d+", state.get("channel", ""))]):
                    state["channel_id"], state["rf"] = self._virtual_rf_for_current_tune(state.get("channel", ""))
                    self._refresh_tuner_status(tuner_idx)
                self._set_tuner_target_locked(tuner_idx, value)
            elif field == "filter":
                if not value or value == "bypass":
                    state["filter"] = "0x0000-0x1FFF"
                if self._filter_requests_playback_pids(state) and not self._should_hold_scan_psip_only(state):
                    self._stop_psip_sender_locked(state)
                self._refresh_tuner_status(tuner_idx)
                if self._filter_looks_specific(state.get("filter")):
                    self._retune_running_target_if_needed_locked(tuner_idx, state)
            elif field == "lockkey":
                if value == "force":
                    state["lockkey"] = "none"
                else:
                    state["lockkey"] = value or "none"
            return state.get(field, "none")

    def _retune_running_target_if_needed_locked(self, tuner_idx: int, state: Dict):
        target = state.get("target")
        if target in (None, "", "none"):
            return

        pid_channel_id, pid_rf = self._select_channel_for_filter_pids(state)
        if state.get("process") and not pid_channel_id and not self._program_requests_specific_program(state.get("program")):
            return
        if pid_channel_id and pid_rf:
            desired_channel_id = pid_channel_id
            desired_rf = pid_rf
        else:
            desired_channel_id = state.get("channel_id")
            desired_rf = state.get("rf")

        if not desired_channel_id or not desired_rf:
            if not state.get("process"):
                self._set_tuner_target_locked(tuner_idx, target)
            return

        desired_key = self._rf_stream_key(desired_rf)
        current_key = state.get("stream_rf_key") or (0, 0)
        if state.get("process") and desired_key != current_key:
            state["channel_id"] = desired_channel_id
            state["rf"] = desired_rf
            logger.info(
                "Retuning tuner%s from %s to %s based on updated program/filter selection",
                tuner_idx,
                current_key,
                desired_key,
            )
            self._set_tuner_target_locked(tuner_idx, target)
            return

        if not state.get("process"):
            self._set_tuner_target_locked(tuner_idx, target)

    def _select_channel_id(self, hint: str) -> Optional[str]:
        if not self.channel_map:
            return None
        if hint in self.channel_map:
            return hint
        numbers = [str(i) for i in re.findall(r"\d+", hint or "")]
        for number in numbers:
            if number in self.channel_map:
                return number
        return next(iter(self.channel_map.keys()))

    def _build_rf_channel_map(self) -> List[Dict]:
        channels = []
        if self.lineup:
            lineup_by_guide = {item.get("GuideNumber"): item for item in self.lineup}
        else:
            lineup_by_guide = {}

        for idx, (guide_number, channel) in enumerate(self.channel_map.items(), start=2):
            item = lineup_by_guide.get(guide_number, {})
            physical_channel = int(item.get("PhysicalChannel") or idx)
            frequency = int(item.get("Frequency") or US_BCAST_FREQUENCIES.get(physical_channel, 57000000))
            low_freq = int(item.get("LowFreq") or frequency - 3000000)
            high_freq = int(item.get("HighFreq") or frequency + 3000000)
            guide = str(guide_number)
            if "." in guide:
                major, minor = guide.split(".", 1)
            else:
                major = str(item.get("VirtualMajor") or physical_channel)
                minor = str(item.get("VirtualMinor") or 1)
            program_number = int(item.get("ProgramNumber") or ATSC_PROGRAM_NUMBER)
            original_pmt_pid = int(item.get("PMTPID") or 0x31)
            original_video_pid = int(item.get("VideoPID") or 0x41)
            original_audio_pid = int(item.get("AudioPID") or 0x51)
            pmt_pid, video_pid, audio_pid = self._sanitize_rf_pids(
                original_pmt_pid,
                original_video_pid,
                original_audio_pid,
                program_number,
            )
            program_pids = f"0,16,17,{pmt_pid},{video_pid},{audio_pid}"
            program_table = item.get("ProgramTable") or ""
            if not program_table or (
                original_pmt_pid != pmt_pid
                or original_video_pid != video_pid
                or original_audio_pid != audio_pid
            ):
                safe_name = self._scanned_call_sign(item, channel, f"CH{major}-{minor}")
                program_table = (
                    f"[{program_number}:{pmt_pid}:{safe_name}:{program_pids}]"
                    f"[tsid=0x{physical_channel:04x}]"
                )
            channels.append({
                "channel_id": guide_number,
                "physical": physical_channel,
                "frequency": frequency,
                "low_freq": low_freq,
                "high_freq": high_freq,
                "major": major,
                "minor": minor,
                "name": self._scanned_call_sign(item, channel, f"CH{major}-{minor}"),
                "program": program_number,
                "pmt_pid": pmt_pid,
                "video_pid": video_pid,
                "audio_pid": audio_pid,
                "program_pids": program_pids,
                "program_table": program_table,
                "ss": int(item.get("SignalStrength") or 95),
                "snq": int(item.get("SignalQuality") or 95),
                "seq": int(item.get("SymbolQuality") or 100),
                # Report the paced transport rate we actually generate. WMC polls
                # this while playing and uses it for buffer/packet expectations.
                "bps": self._transport_bps(),
            })
        return channels

    def _sanitize_rf_pids(self, pmt_pid: int, video_pid: int, audio_pid: int, program_number: int) -> Tuple[int, int, int]:
        if all(32 <= pid <= 0x1FFF for pid in (pmt_pid, video_pid, audio_pid)):
            return pmt_pid, video_pid, audio_pid
        slot = max(0, int(program_number or ATSC_PROGRAM_NUMBER) - ATSC_PROGRAM_NUMBER)
        pid_base = 0x30 + (slot * 3)
        logger.warning(
            "Normalizing out-of-range RF PIDs for program %s: pmt=%s video=%s audio=%s -> base=%s",
            program_number,
            pmt_pid,
            video_pid,
            audio_pid,
            pid_base,
        )
        return pid_base, pid_base + 1, pid_base + 2

    def _select_channel_for_tune(self, hint: str) -> Tuple[Optional[str], Optional[Dict]]:
        if not self._rf_channels:
            return None, None

        text = hint or ""
        numbers = [int(n) for n in re.findall(r"\d+", text)]
        if not numbers:
            return self._select_channel_id(text), self._rf_channels[0]

        # Use the last number as the primary frequency for matching
        freq = numbers[-1]
        for rf in self._rf_channels:
            if (
                freq == rf["physical"]
                or freq == rf["frequency"]
                or freq * 1000000 == rf["frequency"]
                or rf["low_freq"] <= freq <= rf["high_freq"]
                or rf["low_freq"] <= freq * 1000 <= rf["high_freq"]
            ):
                return rf["channel_id"], rf

        return None, None

    def _select_program_for_current_tune(self, state: Dict, value: str) -> Tuple[Optional[str], Optional[Dict]]:
        current_rf = state.get("rf")
        if not current_rf:
            return None, None
        try:
            program = int(value)
        except (TypeError, ValueError):
            return None, None
        if program <= 0:
            return current_rf["channel_id"], current_rf
        for rf in self._rf_channels:
            if (
                int(rf.get("physical") or 0) == int(current_rf.get("physical") or 0)
                and int(rf.get("program") or 0) == program
            ):
                return rf["channel_id"], rf
        return current_rf["channel_id"], current_rf

    def _program_requests_specific_program(self, value: object) -> bool:
        try:
            return int(str(value or "").strip()) > 0
        except (TypeError, ValueError):
            return False

    def _virtual_rf_for_current_tune(self, hint: str) -> Tuple[Optional[str], Optional[Dict]]:
        if not self._rf_channels:
            return None, None
        numbers = [int(n) for n in re.findall(r"\d+", hint or "")]
        if not numbers:
            return None, None
        # Unknown scan frequencies should look like no lock. Reporting lock=8vsb with
        # zero signal makes WMC create transient ghost services and then discard them.
        return None, None

    def _looks_like_atsc_scan_probe(self, text: str, numbers: List[int]) -> bool:
        lowered = (text or "").lower()
        if "none" in lowered:
            return False
        if "auto" in lowered or "us-bcast" in lowered or "8vsb" in lowered:
            return True
        return any(2 <= n <= 69 or 54000000 <= n <= 806000000 for n in numbers)

    def _rf_from_scan_numbers(self, numbers: List[int], fallback: Dict) -> Tuple[int, int]:
        for n in numbers:
            if 54000000 <= n <= 806000000:
                for physical, frequency in US_BCAST_FREQUENCIES.items():
                    if abs(frequency - n) <= 3000000:
                        return physical, frequency
                # Compute physical channel from frequency for probe frequencies
                physical = int((n - 54000000) // 6000000) + 2
                if 2 <= physical <= 69:
                    return physical, n
                return int(fallback.get("physical") or 2), n
        for n in numbers:
            if n in US_BCAST_FREQUENCIES:
                return n, US_BCAST_FREQUENCIES[n]
        return int(fallback.get("physical") or 2), int(fallback.get("frequency") or 57000000)

    def _refresh_tuner_status(self, tuner_idx: int):
        state = self._tuner_state[tuner_idx]
        channel = state.get("channel") or "auto:virtual"
        rf = state.get("rf")
        if state.get("channel_id") and rf:
            state["status"] = (
                f"ch={channel} lock=8vsb ss={rf['ss']} snq={rf['snq']} "
                f"seq={rf['seq']} bps={rf['bps']} pps={rf['bps'] // 188}"
            )
            state["streaminfo"] = self._format_streaminfo_for_physical(rf)
        else:
            state["status"] = f"ch={channel} lock=none ss=0 snq=0 seq=0 bps=0 pps=0"
            state["streaminfo"] = "none"

    def _bitrate_to_bps(self, value: str) -> int:
        text = str(value or "").strip().lower()
        match = re.match(r"^(\d+)([km]?)$", text)
        if not match:
            return 4500000
        amount = int(match.group(1))
        suffix = match.group(2)
        if suffix == "m":
            return amount * 1000000
        if suffix == "k":
            return amount * 1000
        return amount

    def _effective_bitrate(self, source_url: str) -> str:
        if not self._uses_hls_quality_profile(source_url):
            return self.bitrate
        text = str(self.bitrate or "").strip().lower()
        match = re.match(r"^(\d+)([km]?)$", text)
        if not match:
            return self.bitrate
        amount = int(match.group(1))
        suffix = match.group(2) or ""
        if suffix == "m":
            return f"{amount}m"
        if suffix == "k":
            return f"{amount + 500}k"
        return str(amount + 500)

    def _uses_hls_quality_profile(self, source_url: str) -> bool:
        parsed = urllib.parse.urlparse(source_url or "")
        scheme = parsed.scheme.lower()
        if scheme and scheme not in ("http", "https", "file") and not re.fullmatch(r"[a-z]", scheme):
            return False
        path = parsed.path if scheme == "file" else (parsed.path or source_url)
        return path.lower().endswith((".m3u8", ".m3u"))

    def _transport_bps(self) -> int:
        video_bps = self._bitrate_to_bps(self.bitrate)
        # AC3 defaults to 192k; give the MPEG-TS mux enough headroom for audio,
        # PSI tables, PCR timing, encoder bursts, and null packets. Lower mux
        # rates caused "dts < pcr" and WMC decoder stalls on live HLS sources.
        return max(video_bps + 4000000, 19392658)

    def _format_streaminfo(self, rf: Dict) -> str:
        program = int(rf.get("program") or ATSC_PROGRAM_NUMBER)
        pmt_pid = int(rf.get("pmt_pid") or 0x31)
        video_pid = int(rf.get("video_pid") or 0x41)
        audio_pid = int(rf.get("audio_pid") or 0x51)
        tsid = int(rf.get("physical") or 1)
        return (
            f"{program}: {rf['major']}.{rf['minor']} {rf['name']}\n"
            f"tsid=0x{tsid:04X}\n"
            f"pmt=0x{pmt_pid:04X} v=0x{video_pid:04X} a=0x{audio_pid:04X}\n"
        )

    def _format_streaminfo_for_physical(self, rf: Dict) -> str:
        physical = int(rf.get("physical") or 0)
        matches = [item for item in self._rf_channels if int(item.get("physical") or 0) == physical]
        if not matches:
            return self._format_streaminfo(rf)
        return "".join(self._format_streaminfo(item) for item in matches)

    def _format_lineup_scan(self) -> str:
        if self._lineup_scan_active:
            self._lineup_scan_active = False
            return self._format_lineup_scan_xml()

        return ""

    def _format_lineup_scan_xml(self) -> str:
        rows = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<LineupUIResponse>", "<Command>IdentifyPrograms2</Command>"]
        for rf in self._rf_channels:
            physical = int(rf.get("physical") or 2)
            frequency = int(rf.get("frequency") or US_BCAST_FREQUENCIES.get(physical, 57000000))
            guide_number = xml.sax.saxutils.escape(f"{rf['major']}.{rf['minor']}")
            guide_name = xml.sax.saxutils.escape(str(rf.get("name") or "VirtualHD"))
            rows.extend([
                "<Program>",
                "<Modulation>8vsb</Modulation>",
                f"<Frequency>{frequency}</Frequency>",
                f"<TransportStreamID>0x{physical:04X}</TransportStreamID>",
                f"<ProgramNumber>{int(rf.get('program') or ATSC_PROGRAM_NUMBER)}</ProgramNumber>",
                f"<GuideName>{guide_name}</GuideName>",
                f"<GuideNumber>{guide_number}</GuideNumber>",
                "</Program>",
            ])
        rows.append("</LineupUIResponse>")
        return "\r\n".join(rows) + "\r\n"

    def _format_lineup_scan_text(self) -> str:
        if not self._rf_channels:
            return "none"

        lines = []
        for rf in self._rf_channels:
            physical = int(rf.get("physical") or 2)
            frequency = int(rf.get("frequency") or US_BCAST_FREQUENCIES.get(physical, 57000000))
            channelmap = self._lineup_scan_map or "us-bcast"
            scan_line = f"SCANNING: {frequency} ({channelmap}:{physical})"
            lock_line = f"LOCK: 8vsb (ss={rf['ss']} snq={rf['snq']} seq={rf['seq']})"
            tsid_line = f"TSID: 0x{physical:04X}"
            program_line = f"PROGRAM {rf['program']}: {rf['major']}.{rf['minor']} {rf['name']}"
            lines.extend([
                scan_line,
                lock_line,
                tsid_line,
                program_line,
                f"scan: {scan_line}",
                f"scan: {lock_line}",
                f"scan: {tsid_line}",
                f"scan: {program_line[len('PROGRAM '):]}",
            ])
        return "\r\n".join(lines) + "\r\n"

    def _normalize_scan_map(self, scan_map: str) -> str:
        value = (scan_map or "").strip().lower()
        if value in ("", "start", "scan", "auto", "antenna", "digital antenna"):
            return "us-bcast"
        if value in ("cable", "digital cable"):
            return "us-cable"
        return scan_map

    def _safe_channel_name(self, name: str) -> str:
        clean = re.sub(r"[^A-Za-z0-9_.+-]+", "-", name or "VirtualHD")
        return clean.strip("-")[:32] or "VirtualHD"

    def _scanned_call_sign(self, item: Dict, channel, fallback: str) -> str:
        explicit = str(item.get("ScannedCallSign") or item.get("CallSign") or "").strip()
        if explicit:
            return self._safe_channel_name(explicit)[:7]
        return self._safe_channel_name(item.get("GuideName") or getattr(channel, "name", fallback))[:7]

    def _set_tuner_target_locked(self, tuner_idx: int, target: str):
        state = self._tuner_state[tuner_idx]
        if not target or target == "none":
            self._stop_tuner_process_locked(state)
            state["target"] = "none"
            state["target_norm"] = "none"
            state["source_url"] = None
            state["stream_restart_failures"] = 0
            state["stream_restart_window_started_at"] = 0.0
            return

        stream_target = self._normalize_stream_target(target)
        ffmpeg_target = self._ffmpeg_stream_target(stream_target) if stream_target else None
        if not ffmpeg_target:
            logger.warning("Unsupported HDHR target %s", target)
            return

        if (
            not state.get("channel_id")
            and not state.get("rf")
            and self._is_scan_like_tune(state.get("channel"))
            and not self._program_requests_specific_program(state.get("program"))
        ):
            self._stop_tuner_process_locked(state)
            state["target"] = target
            state["target_norm"] = ffmpeg_target
            state["stream_rf_key"] = (0, 0)
            logger.info(
                "Ignoring scan target %s for tuner%s because tuned RF %s is outside the virtual lineup",
                target,
                tuner_idx,
                state.get("channel"),
            )
            return

        proc = state.get("process")
        rf_key = self._rf_stream_key(state.get("rf") or {})
        if state.get("target_norm") == ffmpeg_target and state.get("stream_rf_key") == rf_key and proc and proc.poll() is None:
            # WMC repeats target/filter/status commands while it waits for data. Do not
            # restart ffmpeg on each repeat or WMC will only ever see stream startup.
            self._refresh_tuner_status(tuner_idx)
            return

        self._stop_tuner_process_locked(state)

        pid_channel_id, pid_rf = self._select_channel_for_filter_pids(state)
        if self._should_hold_scan_psip_only(state) and not pid_channel_id:
            inferred_rf = self._representative_rf_for_filter(state) or state.get("rf")
            if inferred_rf:
                state["channel_id"] = inferred_rf.get("channel_id")
                state["rf"] = inferred_rf
                self._refresh_tuner_status(tuner_idx)
                self._start_psip_sender_locked(state, stream_target)
            state["target"] = target
            state["target_norm"] = ffmpeg_target
            state["stream_rf_key"] = self._rf_stream_key(state.get("rf") or {})
            logger.info(
                "Holding tuner%s scan target %s on PSIP only until WMC requests a program number",
                tuner_idx,
                target,
            )
            return
        if pid_channel_id and pid_rf:
            channel_id = pid_channel_id
            state["rf"] = pid_rf
        else:
            if self._should_defer_stream_for_program_selection(state):
                inferred_rf = self._representative_rf_for_filter(state) or state.get("rf")
                if inferred_rf:
                    state["channel_id"] = inferred_rf.get("channel_id")
                    state["rf"] = inferred_rf
                    self._refresh_tuner_status(tuner_idx)
                    self._start_psip_sender_locked(state, stream_target)
                state["target"] = target
                state["target_norm"] = ffmpeg_target
                state["stream_rf_key"] = self._rf_stream_key(state.get("rf") or {})
                logger.info(
                    "Deferring stream start for tuner%s target %s until WMC requests a specific program",
                    tuner_idx,
                    target,
                )
                return
            channel_id = state.get("channel_id") or self._select_channel_id(state.get("program", ""))
        if channel_id not in self.channel_map:
            # WMC often sets target while tuned to a scan RF that is not the actual IPTV channel.
            # Keep the current tuned channel text, but stream the real mapped channel.
            channel_id = self._select_channel_id("")
        state["channel_id"] = channel_id
        if channel_id in self.channel_map:
            for rf in self._rf_channels:
                if rf.get("channel_id") == channel_id:
                    state["rf"] = rf
                    break
        self._refresh_tuner_status(tuner_idx)

        if not channel_id:
            logger.warning("No channels are available to stream to HDHR target %s", target)
            return

        channel = self.channel_map.get(channel_id)
        source_url = getattr(channel, "url", None) if channel else None
        if not source_url:
            logger.warning("Channel %s has no source URL", channel_id)
            return
        original_source_url = source_url
        source_url, temp_source_path = self._prepare_ffmpeg_input_source(source_url)

        missing_variants = getattr(channel, "ext", {}).get("hls_missing_variants") if channel else None
        if missing_variants:
            logger.warning(
                "Channel %s points at an incomplete local HLS master. Missing variants: %s. "
                "Playback will stay black until you use --m3u-url, --hls-base-url, or provide those files.",
                channel_id,
                missing_variants,
            )

        cmd = self._build_udp_ffmpeg_cmd(source_url, state.get("rf"))
        try:
            udp_addr = self._target_to_udp_addr(ffmpeg_target)
            if not udp_addr:
                logger.warning("Unsupported HDHR target %s", target)
                return
            log_path = os.path.abspath(f"ffmpeg_tuner{tuner_idx}.log")
            log_file = open(log_path, "ab", buffering=0)
            log_file.write((f"\n\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} target={stream_target} ffmpeg_target={ffmpeg_target} source={source_url} ===\n").encode("utf-8"))
            log_file.write((f"Python UDP bridge: {udp_addr[0]}:{udp_addr[1]} @ {self._transport_bps()} bps\n").encode("utf-8"))
            log_file.write((" ".join(cmd) + "\n").encode("utf-8", errors="replace"))
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=log_file,
                stdin=subprocess.DEVNULL,
            )
        except OSError as e:
            logger.warning("Unable to start ffmpeg for HDHR target %s: %s", target, e)
            return

        stream_stop = threading.Event()
        stream_thread = threading.Thread(
            target=self._udp_bridge_from_ffmpeg,
            args=(proc, udp_addr, self._transport_bps(), stream_target.lower().startswith("rtp://"), stream_stop, log_file, tuner_idx),
            daemon=True,
            name=f"hdhr-stream-{tuner_idx}",
        )
        stream_thread.start()

        state["target"] = target
        state["target_norm"] = ffmpeg_target
        state["stream_rf_key"] = self._rf_stream_key(state.get("rf") or {})
        state["process"] = proc
        state["log_file"] = log_file
        state["stream_stop"] = stream_stop
        state["stream_thread"] = stream_thread
        state["source_url"] = original_source_url
        state["temp_source_path"] = temp_source_path
        state["stream_announced"] = False
        self._start_psip_sender_locked(state, stream_target)
        logger.info("Streaming channel %s to HDHR target %s (ffmpeg log: %s)", channel_id, target, log_path)

    def _prepare_ffmpeg_input_source(self, source_url: str) -> Tuple[str, Optional[str]]:
        local_master = self._build_local_pluto_master(source_url)
        if local_master:
            return local_master, local_master
        return self._resolve_hls_source_url(source_url), None

    def _build_local_pluto_master(self, source_url: str) -> Optional[str]:
        parsed = urllib.parse.urlparse(source_url or "")
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        if not parsed.path.lower().endswith((".m3u8", ".m3u")):
            return None
        if not self._needs_pluto_headers(source_url):
            return None
        now = time.monotonic()
        cached = self._prepared_input_cache.get(source_url)
        if cached and cached[1] > now and os.path.exists(cached[0]):
            logger.info("Reusing cached local Pluto HLS master for playback: %s", cached[0])
            return cached[0]

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
                "Origin": "https://pluto.tv",
                "Referer": "https://pluto.tv/",
            }
            with urllib.request.urlopen(urllib.request.Request(source_url, headers=headers), timeout=8) as resp:
                base_url = resp.geturl() or source_url
                raw = resp.read(512 * 1024).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("Unable to build local Pluto HLS master for %s: %s", source_url, exc)
            return None

        variants = M3UParser._hls_variant_uris(raw)
        selected = M3UParser._select_hls_variant(variants)
        if not selected:
            return None

        selected_attrs = {}
        for uri, attrs in variants:
            if uri == selected:
                selected_attrs = attrs
                break
        video_url = urllib.parse.urljoin(base_url, selected)
        audio_master_text = raw
        audio_base_url = base_url
        video_url, nested_attrs, nested_raw, nested_base_url = self._resolve_nested_hls_variant(video_url)
        if nested_attrs:
            selected_attrs.update(nested_attrs)
            audio_master_text = nested_raw
            audio_base_url = nested_base_url
        audio_url = self._select_hls_audio_url(audio_master_text, audio_base_url, selected_attrs)
        # The selected Pluto video playlist already carries usable audio. Adding a
        # separate EXT-X-MEDIA audio rendition makes ffmpeg open extra low-bandwidth
        # side streams and can cause WMC-visible stutter.
        audio_url = None
        bandwidth = selected_attrs.get("average-bandwidth") or selected_attrs.get("bandwidth") or "3000000"
        resolution = selected_attrs.get("resolution") or "unknown"

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:5",
        ]
        if audio_url:
            lines.append(
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",LANGUAGE="en",NAME="English",AUTOSELECT=YES,DEFAULT=YES,CHANNELS="2",URI="{audio_url}"'
            )
        audio_map = "0:a:0?"
        video_map = "0:v:0?"
        lines.append(f"#HDHR-PROXY-VIDEO-MAP:{video_map}")
        lines.append(f"#HDHR-PROXY-AUDIO-MAP:{audio_map}")
        stream_inf = f"#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH={bandwidth}"
        if audio_url:
            stream_inf += ',AUDIO="audio"'
        lines.extend([stream_inf, video_url, ""])

        fd, path = tempfile.mkstemp(prefix="hdhr_pluto_", suffix=".m3u8", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        self._prepared_input_cache[source_url] = (path, now + 6)
        logger.info(
            "Using local Pluto HLS master for playback: %s video=%s bandwidth=%s resolution=%s audio=%s",
            path,
            video_url,
            bandwidth,
            resolution,
            audio_url or "none",
        )
        return path

    def _resolve_nested_hls_variant(self, video_url: str) -> Tuple[str, Dict[str, str], str, str]:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
                "Origin": "https://pluto.tv",
                "Referer": "https://pluto.tv/",
            }
            with urllib.request.urlopen(urllib.request.Request(video_url, headers=headers), timeout=8) as resp:
                base_url = resp.geturl() or video_url
                raw = resp.read(256 * 1024).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("Unable to inspect nested HLS variant %s: %s", video_url, exc)
            return video_url, {}, "", video_url

        variants = M3UParser._hls_variant_uris(raw)
        selected = M3UParser._select_hls_variant(variants)
        if not selected:
            return video_url, {}, raw, base_url

        selected_attrs = {}
        for uri, attrs in variants:
            if uri == selected:
                selected_attrs = attrs
                break
        return urllib.parse.urljoin(base_url, selected), selected_attrs, raw, base_url

    def _select_hls_audio_url(self, master_text: str, base_url: str, selected_attrs: Dict[str, str]) -> Optional[str]:
        group_id = (selected_attrs.get("audio") or "").strip()
        if not group_id:
            return None

        entries = []
        matching_entries = []
        for line in master_text.splitlines():
            line = line.strip()
            if not line.upper().startswith("#EXT-X-MEDIA:"):
                continue
            attrs = self._parse_hls_attribute_list(line.split(":", 1)[1])
            if (attrs.get("type") or "").upper() != "AUDIO":
                continue
            uri = attrs.get("uri")
            if not uri:
                continue
            entries.append(attrs)
            if attrs.get("group-id") == group_id:
                matching_entries.append(attrs)

        if not matching_entries:
            return None

        def score(attrs: Dict[str, str]) -> Tuple[int, int, int, int]:
            name = (attrs.get("name") or "").lower()
            language = (attrs.get("language") or "").lower()
            is_descriptive = any(token in name for token in ("description", "descriptive", "audio-description", "ad)"))
            is_english = language in ("en", "eng") or "english" in name
            is_default = (attrs.get("default") or "").upper() == "YES"
            is_autoselect = (attrs.get("autoselect") or "").upper() == "YES"
            return (
                0 if is_descriptive else 1,
                1 if is_english else 0,
                1 if is_default else 0,
                1 if is_autoselect else 0,
            )

        selected = max(matching_entries, key=score)
        selected_name = (selected.get("name") or "").lower()
        selected_is_descriptive = any(token in selected_name for token in ("description", "descriptive", "audio-description", "ad)"))
        if selected_is_descriptive:
            fallback_entries = [entry for entry in entries if score(entry)[0] > 0]
            if fallback_entries:
                selected = max(fallback_entries, key=score)
        return urllib.parse.urljoin(base_url, selected["uri"])

    def _hls_audio_playlist_may_include_video(self, audio_url: str) -> bool:
        lowered = (audio_url or "").lower()
        if self._is_descriptive_hls_audio_url(audio_url):
            return True
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
                "Origin": "https://pluto.tv",
                "Referer": "https://pluto.tv/",
            }
            with urllib.request.urlopen(urllib.request.Request(audio_url, headers=headers), timeout=5) as resp:
                raw = resp.read(128 * 1024).decode("utf-8", errors="replace").lower()
        except Exception as exc:
            logger.debug("Unable to inspect HLS audio playlist %s: %s", audio_url, exc)
            return False
        return "#ext-x-stream-inf" in raw or "/video/" in raw or "hls_300-" in raw or "hls_600-" in raw

    def _is_descriptive_hls_audio_url(self, audio_url: str) -> bool:
        lowered = (audio_url or "").lower()
        return any(token in lowered for token in ("audio-description", "descriptive", "description"))

    def _parse_hls_attribute_list(self, text: str) -> Dict[str, str]:
        attrs = {}
        for key, value in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text, flags=re.IGNORECASE):
            attrs[key.lower()] = value.strip().strip('"')
        return attrs

    def _resolve_hls_source_url(self, source_url: str) -> str:
        parsed = urllib.parse.urlparse(source_url or "")
        if parsed.scheme.lower() not in ("http", "https"):
            return source_url
        if not parsed.path.lower().endswith((".m3u8", ".m3u")):
            return source_url

        now = time.monotonic()
        cached = self._hls_variant_cache.get(source_url)
        if cached and cached[1] > now:
            return cached[0]

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
            }
            if self._needs_pluto_headers(source_url):
                headers["Origin"] = "https://pluto.tv"
                headers["Referer"] = "https://pluto.tv/"
            req = urllib.request.Request(
                source_url,
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                base_url = resp.geturl() or source_url
                raw = resp.read(512 * 1024).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("Unable to inspect HLS source %s: %s", source_url, exc)
            return source_url

        variants = M3UParser._hls_variant_uris(raw)
        selected = M3UParser._select_hls_variant(variants)
        if not selected:
            return source_url

        playback_url = urllib.parse.urljoin(base_url, selected)
        if self._hls_master_has_separate_audio(raw, variants, selected):
            logger.info("Keeping original HLS master because selected variant has separate audio")
            self._hls_variant_cache[source_url] = (source_url, now + 300)
            return source_url
        if self._should_keep_original_hls_url(source_url, playback_url):
            return source_url
        self._hls_variant_cache[source_url] = (playback_url, now + 300)
        logger.info("Using HLS media variant for playback: %s", playback_url)
        return playback_url

    def _hls_master_has_separate_audio(
        self,
        master_text: str,
        variants: List[Tuple[str, Dict[str, str]]],
        selected_uri: str,
    ) -> bool:
        audio_groups = set()
        for line in master_text.splitlines():
            line = line.strip()
            if not line.upper().startswith("#EXT-X-MEDIA:"):
                continue
            attrs = self._parse_hls_attribute_list(line.split(":", 1)[1])
            if (attrs.get("type") or "").upper() == "AUDIO" and attrs.get("uri"):
                group_id = attrs.get("group-id")
                if group_id:
                    audio_groups.add(group_id)
        if not audio_groups:
            return False

        for uri, attrs in variants:
            if uri != selected_uri:
                continue
            group_id = attrs.get("audio")
            return not group_id or group_id in audio_groups
        return False

    def _rf_stream_key(self, rf: Dict) -> Tuple[int, int]:
        return int(rf.get("physical") or 0), int(rf.get("program") or 0)

    def _filter_requests_playback_pids(self, state: Dict) -> bool:
        rf = state.get("rf") or {}
        video_pid = int(rf.get("video_pid") or 0x41)
        audio_pid = int(rf.get("audio_pid") or 0x51)
        requested = self._literal_filter_pids(state.get("filter"))
        return video_pid in requested or audio_pid in requested

    def _filter_looks_specific(self, filter_value: object) -> bool:
        text = str(filter_value or "").strip().lower()
        if not text or text in ("none", "bypass", "r"):
            return False
        return "0x" in text

    def _filter_match_candidates(self, state: Dict) -> Tuple[List[Dict], List[Dict], int]:
        requested = self._requested_filter_pids(state.get("filter"))
        if not requested:
            return [], [], 0
        av_matches = [
            rf for rf in self._rf_channels
            if int(rf.get("video_pid") or 0) in requested
            or int(rf.get("audio_pid") or 0) in requested
        ]
        pmt_matches = [
            rf for rf in self._rf_channels
            if int(rf.get("pmt_pid") or 0) in requested
        ]
        return av_matches, pmt_matches, len(requested)

    def _filter_requires_specific_program(self, state: Dict) -> bool:
        av_matches, pmt_matches, requested_count = self._filter_match_candidates(state)
        if requested_count == 0:
            return False
        # WMC often requests a basket of PMT PIDs for many virtual subchannels before
        # it has committed to one program. Starting the first channel here hijacks
        # playback; wait for a later filter update with a specific program instead.
        return not av_matches and len(pmt_matches) > 1

    def _should_defer_stream_for_program_selection(self, state: Dict) -> bool:
        if self._program_requests_specific_program(state.get("program")):
            return False
        if self._select_channel_for_filter_pids(state)[0] and not self._should_hold_scan_psip_only(state):
            return False

        current_rf = state.get("rf") or {}
        if not current_rf:
            return False

        if not self._is_scan_like_tune(state.get("channel")):
            return False

        filter_text = str(state.get("filter") or "").strip().lower()
        if not filter_text or filter_text in ("none", "bypass", "r"):
            return True
        if self._filter_requires_specific_program(state):
            return True

        av_matches, pmt_matches, requested_count = self._filter_match_candidates(state)
        if requested_count == 0:
            return True

        current_physical = int(current_rf.get("physical") or 0)
        current_matches = [
            rf for rf in av_matches + pmt_matches
            if int(rf.get("physical") or 0) == current_physical
        ]
        return len(current_matches) != 1

    def _should_hold_scan_psip_only(self, state: Dict) -> bool:
        if self._program_requests_specific_program(state.get("program")):
            return False
        if not self._is_scan_like_tune(state.get("channel")):
            return False
        current_rf = state.get("rf") or {}
        if not current_rf:
            return False
        physical = int(current_rf.get("physical") or 0)
        if physical <= 0:
            return False
        # During TV setup, Vista probes program 0 and then tests individual PMT/AV
        # PIDs. Starting a real single-program stream here makes WMC keep only that
        # one subchannel. Keep sending the full RF PSIP until it selects a program.
        return len([rf for rf in self._rf_channels if int(rf.get("physical") or 0) == physical]) > 1

    def _is_scan_like_tune(self, channel_value: object) -> bool:
        channel_text = str(channel_value or "").lower()
        return "auto" in channel_text or "8vsb" in channel_text or "us-bcast" in channel_text

    def _representative_rf_for_filter(self, state: Dict) -> Optional[Dict]:
        av_matches, pmt_matches, requested_count = self._filter_match_candidates(state)
        if requested_count == 0:
            return None

        candidates = av_matches or pmt_matches
        if not candidates:
            return None

        current_rf = state.get("rf") or {}
        current_physical = int(current_rf.get("physical") or 0)
        if current_physical:
            for rf in candidates:
                if int(rf.get("physical") or 0) == current_physical:
                    return rf

        grouped: Dict[int, List[Dict]] = {}
        for rf in candidates:
            physical = int(rf.get("physical") or 0)
            grouped.setdefault(physical, []).append(rf)

        if not grouped:
            return candidates[0]

        best_physical = max(grouped, key=lambda physical: (len(grouped[physical]), -physical))
        best_group = grouped[best_physical]
        best_group.sort(key=lambda rf: int(rf.get("program") or 0))
        return best_group[0]

    def _select_channel_for_filter_pids(self, state: Dict) -> Tuple[Optional[str], Optional[Dict]]:
        av_matches, pmt_matches, requested_count = self._filter_match_candidates(state)
        if requested_count == 0:
            return None, None
        if not av_matches and len(pmt_matches) > 1:
            return None, None

        current_rf = state.get("rf") or {}
        current_physical = int(current_rf.get("physical") or 0)
        if current_physical:
            current_av_matches = [
                rf for rf in av_matches
                if int(rf.get("physical") or 0) == current_physical
            ]
            current_pmt_matches = [
                rf for rf in pmt_matches
                if int(rf.get("physical") or 0) == current_physical
            ]
            if len(current_av_matches) == 1:
                return current_av_matches[0].get("channel_id"), current_av_matches[0]
            if not current_av_matches and len(current_pmt_matches) == 1:
                return current_pmt_matches[0].get("channel_id"), current_pmt_matches[0]
            if len(current_av_matches) > 1 or len(current_pmt_matches) > 1:
                return None, None
        if len(av_matches) == 1:
            return av_matches[0].get("channel_id"), av_matches[0]
        if len(pmt_matches) == 1:
            return pmt_matches[0].get("channel_id"), pmt_matches[0]
        return None, None

    def _requested_filter_pids(self, filter_value: object) -> set:
        text = str(filter_value or "").lower()
        requested = self._literal_filter_pids(text)
        for start, end in re.findall(r"0x([0-9a-f]+)\s*-\s*0x([0-9a-f]+)", text):
            first = int(start, 16)
            last = int(end, 16)
            if first > last:
                first, last = last, first
            requested.update(range(first, min(last, 0x1FFF) + 1))
        return requested

    def _literal_filter_pids(self, filter_value: object) -> set:
        text = str(filter_value or "").lower()
        return {
            int(match.group(1), 16)
            for match in re.finditer(r"(?<!-)\b0x([0-9a-f]+)\b(?!\s*-)", text)
        }

    def _stop_tuner_process_locked(self, state: Dict):
        psip_stop = state.get("psip_stop")
        state["psip_stop"] = None
        state["psip_thread"] = None
        if psip_stop:
            psip_stop.set()

        temp_source_path = state.get("temp_source_path")
        source_url = state.get("source_url")
        state["temp_source_path"] = None
        state["source_url"] = None
        state["stream_announced"] = False
        self._drop_prepared_input_cache_entry(source_url)

        proc = state.get("process")
        state["process"] = None
        stream_stop = state.get("stream_stop")
        state["stream_stop"] = None
        stream_thread = state.get("stream_thread")
        state["stream_thread"] = None
        if stream_stop:
            stream_stop.set()
        log_file = state.get("log_file")
        state["log_file"] = None
        if not proc or proc.poll() is not None:
            if stream_thread:
                stream_thread.join(timeout=1.0)
            if log_file:
                log_file.close()
            if temp_source_path and os.path.exists(temp_source_path) and not self._is_cached_prepared_input(temp_source_path):
                try:
                    os.unlink(temp_source_path)
                except OSError:
                    pass
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if stream_thread:
            stream_thread.join(timeout=1.5)
        if log_file:
            log_file.close()
        if temp_source_path and os.path.exists(temp_source_path) and not self._is_cached_prepared_input(temp_source_path):
            try:
                os.unlink(temp_source_path)
            except OSError:
                pass

    def _is_cached_prepared_input(self, path: str) -> bool:
        now = time.monotonic()
        expired = []
        for key, (cached_path, expires_at) in self._prepared_input_cache.items():
            if expires_at <= now or not os.path.exists(cached_path):
                expired.append(key)
                continue
            if os.path.normcase(os.path.abspath(cached_path)) == os.path.normcase(os.path.abspath(path)):
                return True
        for key in expired:
            cached_path = self._prepared_input_cache.pop(key, (None, 0))[0]
            if cached_path and os.path.exists(cached_path):
                try:
                    os.unlink(cached_path)
                except OSError:
                    pass
        return False

    def _drop_prepared_input_cache_entry(self, source_url: Optional[str]):
        if not source_url:
            return
        cached_path = self._prepared_input_cache.pop(source_url, (None, 0))[0]
        if cached_path and os.path.exists(cached_path):
            try:
                os.unlink(cached_path)
            except OSError:
                pass

    def _target_to_udp_addr(self, target: str) -> Optional[Tuple[str, int]]:
        parsed = urllib.parse.urlparse(target or "")
        if parsed.scheme.lower() not in ("udp", "rtp"):
            return None
        if not parsed.hostname or not parsed.port:
            return None
        return parsed.hostname, int(parsed.port)

    def _udp_bridge_from_ffmpeg(
        self,
        proc: subprocess.Popen,
        addr: Tuple[str, int],
        transport_bps: int,
        use_rtp: bool,
        stop_event: threading.Event,
        log_file,
        tuner_idx: int,
    ):
        stdout = proc.stdout
        if stdout is None:
            return

        packet_size = 1316
        packets_per_burst = 4
        burst_size = packet_size * packets_per_burst
        # WMC recording is more sensitive to short upstream HLS stalls than to a
        # little startup latency. Keep the steady queue deep, but do not hold the
        # first packets too long or WMC sits on a black screen before video appears.
        prebuffer_seconds = 0.40 if use_rtp else 0.20
        max_buffer_seconds = 8.0 if use_rtp else 3.0
        buffer_target_bytes = max(burst_size * 4, int((transport_bps / 8) * prebuffer_seconds))
        buffer_max_bursts = max(192, int((transport_bps / 8) * max_buffer_seconds) // burst_size)
        burst_queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=buffer_max_bursts)
        bytes_sent = 0
        started_at = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def reader():
            try:
                while not stop_event.is_set():
                    burst = stdout.read(burst_size)
                    if not burst:
                        break
                    while not stop_event.is_set():
                        try:
                            burst_queue.put(burst, timeout=0.1)
                            break
                        except queue.Full:
                            continue
            finally:
                try:
                    burst_queue.put_nowait(None)
                except queue.Full:
                    pass

        reader_thread = threading.Thread(
            target=reader,
            daemon=True,
            name=f"hdhr-ffmpeg-reader-{tuner_idx}",
        )
        reader_thread.start()
        _set_timer_resolution(True)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            if hasattr(socket, "SIO_UDP_CONNRESET"):
                try:
                    sock.ioctl(socket.SIO_UDP_CONNRESET, False)
                except OSError:
                    pass

            prebuffered: List[bytes] = []
            buffered_bytes = 0
            prebuffer_deadline = time.monotonic() + prebuffer_seconds
            while not stop_event.is_set() and buffered_bytes < buffer_target_bytes and time.monotonic() < prebuffer_deadline:
                try:
                    burst = burst_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if burst is None:
                    break
                buffered_bytes += len(burst)
                prebuffered.append(burst)

            next_send = time.perf_counter()
            rtp_sequence = 0
            rtp_timestamp = int(time.time() * 90000) & 0xFFFFFFFF
            rtp_ssrc = (0x48444852 << 16 | tuner_idx) & 0xFFFFFFFF
            while not stop_event.is_set():
                if prebuffered:
                    burst = prebuffered.pop(0)
                else:
                    try:
                        burst = burst_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                if burst is None:
                    break
                now = time.perf_counter()
                if next_send > now:
                    delay = next_send - now
                    if delay > 0.003:
                        time.sleep(delay - 0.0015)
                    while time.perf_counter() < next_send and not stop_event.is_set():
                        time.sleep(0)

                burst_sent = 0
                for offset in range(0, len(burst), packet_size):
                    chunk = burst[offset:offset + packet_size]
                    if not chunk:
                        continue
                    try:
                        datagram = self._wrap_rtp_mpegts(chunk, rtp_sequence, rtp_timestamp, rtp_ssrc) if use_rtp else chunk
                        sock.sendto(datagram, addr)
                        bytes_sent += len(datagram)
                        burst_sent += len(chunk)
                        if burst_sent > 0:
                            self._notify_stream_bytes(tuner_idx)
                        if use_rtp:
                            rtp_sequence = (rtp_sequence + 1) & 0xFFFF
                            ticks = max(1, int((len(chunk) * 8 * 90000) / max(transport_bps, 1)))
                            rtp_timestamp = (rtp_timestamp + ticks) & 0xFFFFFFFF
                    except OSError as exc:
                        if not _is_ignorable_udp_error(exc):
                            logger.warning("UDP bridge send failed for tuner%s to %s:%s: %s", tuner_idx, addr[0], addr[1], exc)
                            return

                # Pace whole bursts rather than individual datagrams. Windows timer
                # granularity is too coarse for ~1 ms sleeps, and WMC tolerates short
                # MPEG-TS bursts much better than slow packet-by-packet jitter.
                burst_seconds = (burst_sent * 8) / max(transport_bps, 1)
                now = time.perf_counter()
                next_send = max(next_send + burst_seconds, now - 0.20)
        finally:
            _set_timer_resolution(False)
            try:
                stdout.close()
            except OSError:
                pass
            sock.close()
            elapsed = max(time.monotonic() - started_at, 0.001)
            message = f"Python UDP bridge stopped for tuner{tuner_idx}: {bytes_sent} bytes in {elapsed:.1f}s\n"
            try:
                log_file.write(message.encode("utf-8", errors="replace"))
            except Exception:
                pass
            logger.info("UDP bridge stopped for tuner%s after %.1fs (%d bytes)", tuner_idx, elapsed, bytes_sent)
            self._handle_unexpected_stream_exit(tuner_idx, proc, stop_event, bytes_sent, elapsed)

    def _handle_unexpected_stream_exit(
        self,
        tuner_idx: int,
        proc: subprocess.Popen,
        stop_event: threading.Event,
        bytes_sent: int,
        elapsed: float,
    ):
        restart_target = None
        restart_delay = 0.0
        with self._state_lock:
            state = self._tuner_state.get(tuner_idx)
            if not state or state.get("process") is not proc:
                return
            if stop_event.is_set() or str(state.get("target") or "").lower() == "none":
                return

            target = state.get("target")
            log_file = state.get("log_file")
            temp_source_path = state.get("temp_source_path")
            source_url = state.get("source_url")
            state["process"] = None
            state["log_file"] = None
            state["stream_stop"] = None
            state["stream_thread"] = None
            state["temp_source_path"] = None
            state["source_url"] = None
            state["stream_announced"] = False
            self._drop_prepared_input_cache_entry(source_url)

            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass
            if temp_source_path and os.path.exists(temp_source_path) and not self._is_cached_prepared_input(temp_source_path):
                try:
                    os.unlink(temp_source_path)
                except OSError:
                    pass

            now = time.monotonic()
            window_started = float(state.get("stream_restart_window_started_at") or 0.0)
            if not window_started or now - window_started > 60:
                state["stream_restart_window_started_at"] = now
                state["stream_restart_failures"] = 0

            if bytes_sent >= 1024 * 1024 and elapsed >= 8.0:
                state["stream_restart_failures"] = 0
                restart_delay = 0.35
            else:
                failures = int(state.get("stream_restart_failures") or 0) + 1
                state["stream_restart_failures"] = failures
                if failures > 3:
                    logger.warning(
                        "Not restarting tuner%s stream after %d short failures in %.1fs",
                        tuner_idx,
                        failures,
                        now - float(state.get("stream_restart_window_started_at") or now),
                    )
                    return
                restart_delay = min(2.0, 0.35 * failures)

            restart_target = target

        if not restart_target:
            return

        logger.warning(
            "Restarting tuner%s stream for active WMC target %s after ffmpeg ended (%.1fs, %d bytes)",
            tuner_idx,
            restart_target,
            elapsed,
            bytes_sent,
        )
        if restart_delay > 0:
            time.sleep(restart_delay)
        with self._state_lock:
            state = self._tuner_state.get(tuner_idx)
            if not state:
                return
            if str(state.get("target") or "").lower() != str(restart_target).lower():
                return
            if state.get("process") is not None:
                return
            self._set_tuner_target_locked(tuner_idx, restart_target)

    def _notify_stream_bytes(self, tuner_idx: int):
        with self._state_lock:
            state = self._tuner_state.get(tuner_idx)
            if not state or state.get("stream_announced"):
                return
            state["stream_announced"] = True
            state["stream_restart_failures"] = 0
            state["stream_restart_window_started_at"] = 0.0
            self._stop_psip_sender_locked(state)

    def _normalize_stream_target(self, target: str) -> Optional[str]:
        value = (target or "").strip()
        if value.startswith(("udp://", "rtp://")):
            return value
        match = re.match(r"^(?:udp|rtp)\s+([0-9.]+):(\d+)$", value, re.IGNORECASE)
        if match:
            scheme = value.split(None, 1)[0].lower()
            return f"{scheme}://{match.group(1)}:{match.group(2)}"
        match = re.match(r"^([0-9.]+):(\d+)$", value)
        if match:
            return f"udp://{match.group(1)}:{match.group(2)}"
        return None

    def _ffmpeg_stream_target(self, target: str) -> Optional[str]:
        value = (target or "").strip()
        if value.startswith("udp://"):
            return value
        if value.startswith("rtp://"):
            return "udp://" + value[len("rtp://"):]
        return None

    def _start_psip_sender_locked(self, state: Dict, target: str):
        self._stop_psip_sender_locked(state)
        rf = state.get("rf") or (self._rf_channels[0] if self._rf_channels else None)
        if not rf:
            return
        stop_event = threading.Event()
        state["psip_stop"] = stop_event
        thread = threading.Thread(
            target=self._psip_sender,
            args=(target, rf, stop_event),
            daemon=True,
            name="hdhr-psip",
        )
        state["psip_thread"] = thread
        thread.start()

    def _stop_psip_sender_locked(self, state: Dict):
        psip_stop = state.get("psip_stop")
        state["psip_stop"] = None
        state["psip_thread"] = None
        if psip_stop:
            psip_stop.set()

    def _psip_sender(self, target: str, rf: Dict, stop_event: threading.Event):
        match = re.match(r"^(?:udp|rtp)://([0-9.]+):(\d+)", target)
        if not match:
            return
        psip_sections = self._build_atsc_psip_sections(rf)
        continuity_by_pid: Dict[int, int] = {}
        is_rtp = target.lower().startswith("rtp://")
        addr = (match.group(1), int(match.group(2)))
        sequence = 0
        timestamp = int(time.time() * 90000) & 0xFFFFFFFF
        ssrc = (int(rf.get("physical") or 1) << 16) | int(rf.get("program") or ATSC_PROGRAM_NUMBER)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                started_at = time.monotonic()
                while not stop_event.is_set():
                    warmup = time.monotonic() - started_at < 1.25
                    repeats = 4 if warmup else 1
                    for _ in range(repeats):
                        ts_packets = self._packetize_atsc_psip_sections(psip_sections, continuity_by_pid)
                        for offset in range(0, len(ts_packets), 7):
                            payload = b"".join(ts_packets[offset:offset + 7])
                            datagram = self._wrap_rtp_mpegts(payload, sequence, timestamp, ssrc) if is_rtp else payload
                            sock.sendto(datagram, addr)
                            sequence = (sequence + 1) & 0xFFFF
                            timestamp = (timestamp + 1500) & 0xFFFFFFFF
                    time.sleep(0.025 if warmup else 0.1)
        except OSError as e:
            logger.debug(f"ATSC PSIP sender failed for {target}: {e}")

    def _wrap_rtp_mpegts(self, payload: bytes, sequence: int, timestamp: int, ssrc: int) -> bytes:
        header = bytes([
            0x80,  # RTP v2
            33,    # MP2T payload type
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
        ]) + struct.pack(">II", timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
        return header + payload

    def _build_atsc_psip_packets(self, rf: Dict) -> List[bytes]:
        continuity_by_pid: Dict[int, int] = {}
        return self._packetize_atsc_psip_sections(self._build_atsc_psip_sections(rf), continuity_by_pid)

    def _build_atsc_psip_sections(self, rf: Dict) -> List[Tuple[int, bytes]]:
        rf_group = self._rf_group_for_physical(rf)
        pat_section = self._make_pat_section(rf_group)
        tvct_section = self._make_tvct_section(rf_group)
        mgt_section = self._make_mgt_section(len(tvct_section))
        sections = [(0x0000, pat_section)]
        for item in rf_group:
            pmt_section = self._make_pmt_section(item)
            sections.append((int(item.get("pmt_pid") or 0x31), pmt_section))
        sections.append((0x1FFB, mgt_section))
        sections.append((0x1FFB, tvct_section))
        return sections

    def _packetize_atsc_psip_sections(
        self,
        sections: List[Tuple[int, bytes]],
        continuity_by_pid: Dict[int, int],
    ) -> List[bytes]:
        packets = []
        for pid, section in sections:
            continuity = continuity_by_pid.get(pid, 0)
            section_packets, continuity = self._packetize_psi_section(pid, section, continuity)
            continuity_by_pid[pid] = continuity
            packets.extend(section_packets)
        null_continuity = continuity_by_pid.get(0x1FFF, 0)
        for _ in range(8):
            packets.append(bytes([0x47, 0x1F, 0xFF, 0x10 | (null_continuity & 0x0F)]) + (b"\xFF" * 184))
            null_continuity = (null_continuity + 1) & 0x0F
        continuity_by_pid[0x1FFF] = null_continuity
        return packets

    def _rf_group_for_physical(self, rf: Dict) -> List[Dict]:
        physical = int(rf.get("physical") or 0)
        matches = [item for item in self._rf_channels if int(item.get("physical") or 0) == physical]
        return matches or [rf]

    def _make_pat_section(self, rf_group: List[Dict]) -> bytes:
        first = rf_group[0] if rf_group else {}
        tsid = int(first.get("physical") or 1)
        programs = []
        for rf in rf_group:
            program = int(rf.get("program") or ATSC_PROGRAM_NUMBER)
            pmt_pid = int(rf.get("pmt_pid") or 0x31)
            programs.append(program.to_bytes(2, "big") + (0xE000 | (pmt_pid & 0x1FFF)).to_bytes(2, "big"))
        body = tsid.to_bytes(2, "big") + bytes([0xC1, 0x00, 0x00]) + b"".join(programs)
        return self._make_psi_section(0x00, body)

    def _make_pmt_section(self, rf: Dict) -> bytes:
        program = int(rf.get("program") or ATSC_PROGRAM_NUMBER)
        pcr_pid = int(rf.get("video_pid") or 0x41)
        video_pid = int(rf.get("video_pid") or 0x41)
        audio_pid = int(rf.get("audio_pid") or 0x51)
        video_stream_type = self._mpegts_video_stream_type()
        streams = (
            bytes([video_stream_type]) + (0xE000 | video_pid).to_bytes(2, "big") + (0xF000).to_bytes(2, "big")
            + bytes([0x81]) + (0xE000 | audio_pid).to_bytes(2, "big") + (0xF000).to_bytes(2, "big")
        )
        body = (
            program.to_bytes(2, "big")
            + bytes([0xC1, 0x00, 0x00])
            + (0xE000 | (pcr_pid & 0x1FFF)).to_bytes(2, "big")
            + (0xF000).to_bytes(2, "big")
            + streams
        )
        return self._make_psi_section(0x02, body)

    def _mpegts_video_stream_type(self) -> int:
        codec = (self.output_codec or "").lower()
        if codec in ("h264", "libx264", "mpeg4_h264", "mpeg4-avc", "avc"):
            return 0x1B
        return 0x02

    def _make_tvct_section(self, rf_group: List[Dict]) -> bytes:
        first = rf_group[0] if rf_group else {}
        channels = b"".join(self._make_tvct_channel(rf) for rf in rf_group)
        body = (
            int(first.get("physical") or 1).to_bytes(2, "big")
            + bytes([0xC1, 0x00, 0x00, 0x00, len(rf_group) & 0xFF])
            + channels
            + (0xF000).to_bytes(2, "big")  # additional_descriptors_length=0
        )
        return self._make_psip_section(0xC8, body)

    def _make_tvct_channel(self, rf: Dict) -> bytes:
        name = self._safe_channel_name(rf.get("name", "VirtualHD"))[:7]
        short_name = name.encode("utf-16-be")[:14].ljust(14, b"\x00")
        major = int(rf.get("major") or rf.get("physical") or 2) & 0x3FF
        minor = int(rf.get("minor") or 1) & 0x3FF
        video_pid = int(rf.get("video_pid") or 0x41) & 0x1FFF
        audio_pid = int(rf.get("audio_pid") or 0x51) & 0x1FFF
        pcr_pid = video_pid
        video_stream_type = self._mpegts_video_stream_type()
        service_location = (
            bytes([0xA1, 15])
            + (0xE000 | pcr_pid).to_bytes(2, "big")
            + bytes([2])
            + bytes([video_stream_type])
            + (0xE000 | video_pid).to_bytes(2, "big")
            + b"eng"
            + bytes([0x81])
            + (0xE000 | audio_pid).to_bytes(2, "big")
            + b"eng"
        )
        channel_numbers = 0xF00000 | (major << 10) | minor
        service_flags = (
            (0 << 14)  # ETM_location
            | (0 << 13)  # access_controlled
            | (0 << 12)  # hidden
            | (0 << 11)  # path_select
            | (0 << 10)  # out_of_band
            | (0 << 9)   # hide_guide
            | (0x7 << 6) # reserved
            | 0x02       # ATSC digital television
        )
        return (
            short_name
            + channel_numbers.to_bytes(3, "big")
            + bytes([0x04])  # ATSC 8-VSB
            + int(rf.get("frequency") or 0).to_bytes(4, "big")
            + int(rf.get("physical") or 1).to_bytes(2, "big")
            + int(rf.get("program") or ATSC_PROGRAM_NUMBER).to_bytes(2, "big")
            + service_flags.to_bytes(2, "big")
            + int(major * 100 + minor).to_bytes(2, "big")
            + (0xFC00 | len(service_location)).to_bytes(2, "big")
            + service_location
        )

    def _make_mgt_section(self, tvct_bytes: int) -> bytes:
        table_entry = (
            (0x0000).to_bytes(2, "big")  # terrestrial VCT current
            + (0xE000 | 0x1FFB).to_bytes(2, "big")
            + bytes([0xC1])
            + int(tvct_bytes).to_bytes(4, "big")
            + (0xF000).to_bytes(2, "big")
        )
        body = (
            (0x0001).to_bytes(2, "big")
            + bytes([0xC1, 0x00, 0x00, 0x00])
            + (0x0001).to_bytes(2, "big")
            + table_entry
            + (0xF000).to_bytes(2, "big")
        )
        return self._make_psip_section(0xC7, body)

    def _make_psip_section(self, table_id: int, body: bytes) -> bytes:
        section_length = len(body) + 4
        header = bytes([table_id]) + (0xB000 | section_length).to_bytes(2, "big")
        section = header + body
        return section + self._mpeg_crc32(section).to_bytes(4, "big")

    def _make_psi_section(self, table_id: int, body: bytes) -> bytes:
        section_length = len(body) + 4
        header = bytes([table_id]) + (0xB000 | section_length).to_bytes(2, "big")
        section = header + body
        return section + self._mpeg_crc32(section).to_bytes(4, "big")

    def _packetize_psi_section(self, pid: int, section: bytes, continuity: int) -> Tuple[List[bytes], int]:
        packets = []
        data = b"\x00" + section
        first = True
        while data:
            payload = data[:184]
            data = data[184:]
            header = bytes([
                0x47,
                (0x40 if first else 0x00) | ((pid >> 8) & 0x1F),
                pid & 0xFF,
                0x10 | (continuity & 0x0F),
            ])
            packets.append(header + payload.ljust(184, b"\xFF"))
            first = False
            continuity = (continuity + 1) & 0x0F
        return packets, continuity

    def _mpeg_crc32(self, data: bytes) -> int:
        crc = 0xFFFFFFFF
        for byte in data:
            crc ^= byte << 24
            for _ in range(8):
                if crc & 0x80000000:
                    crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                else:
                    crc = (crc << 1) & 0xFFFFFFFF
        return crc

    def _build_udp_ffmpeg_cmd(self, source_url: str, rf: Optional[Dict] = None) -> List[str]:
        service_id = int(rf.get("program", ATSC_PROGRAM_NUMBER)) if rf else ATSC_PROGRAM_NUMBER
        ts_id = int(rf.get("physical", 1)) if rf else 1
        service_name = rf.get("name", "VirtualHD") if rf else "VirtualHD"
        transport_bps = self._transport_bps()
        effective_bitrate = self._effective_bitrate(source_url)
        pmt_pid = int(rf.get("pmt_pid") or 0x31) if rf else 0x31
        video_pid = int(rf.get("video_pid") or 0x41) if rf else 0x41
        audio_pid = int(rf.get("audio_pid") or 0x51) if rf else 0x51
        input_args = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "info",
            "-nostdin",
            "-fflags", "+genpts+discardcorrupt",
            "-analyzeduration", FFMPEG_ANALYZE_US,
            "-probesize", FFMPEG_PROBE_BYTES,
        ]
        if self._is_network_media_source(source_url):
            input_args.extend([
                "-rw_timeout", "15000000",
                "-http_persistent", "0",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "2",
                "-reconnect_on_http_error", "4xx,5xx",
                "-reconnect_on_network_error", "1",
                "-user_agent", "VLC/3.0.20 LibVLC/3.0.20",
            ])
            parsed = urllib.parse.urlparse(source_url or "")
            if parsed.path.lower().endswith((".m3u8", ".m3u")):
                input_args.extend([
                    "-thread_queue_size", "1024",
                    "-protocol_whitelist", "file,http,https,tcp,tls,crypto,udp,rtp",
                    "-allowed_extensions", "ALL",
                ])
            if self._needs_pluto_headers(source_url):
                input_args.extend([
                    "-headers", "Accept: application/vnd.apple.mpegurl,application/x-mpegURL,*/*\r\nOrigin: https://pluto.tv\r\nReferer: https://pluto.tv/\r\n",
                ])
        elif self._looks_like_local_hls(source_url):
            input_args.extend([
                "-thread_queue_size", "1024",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto,udp,rtp",
                "-allowed_extensions", "ALL",
            ])

        input_args.extend(["-i", source_url])
        video_map = self._local_hls_video_map(source_url)
        audio_map = self._local_hls_audio_map(source_url)
        return input_args + [
            "-map", video_map,
            "-map", audio_map,
            "-fps_mode", "cfr",
            "-dn",
            "-sn",
        ] + self._video_encoder_args(effective_bitrate, self._uses_hls_quality_profile(source_url)) + [
            "-c:a", "ac3",
            "-b:a", "192k",
            "-ar", "48000",
            "-ac", "2",
            "-af", "aresample=async=1000:first_pts=0:min_hard_comp=0.100",
            "-f", "mpegts",
            "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
            "-mpegts_transport_stream_id", str(ts_id),
            "-mpegts_service_id", str(service_id),
            "-mpegts_service_type", "digital_tv",
            "-mpegts_pmt_start_pid", str(pmt_pid),
            "-streamid", f"0:{video_pid}",
            "-streamid", f"1:{audio_pid}",
            "-metadata", "service_provider=VirtualHDHR",
            "-metadata", f"service_name={service_name}",
            "-muxrate", str(transport_bps),
            "-muxpreload", "0.25",
            "-muxdelay", "0.25",
            "-flush_packets", "0",
            "-pat_period", "0.10",
            "pipe:1",
        ]

    def _video_encoder_args(self, effective_bitrate: str, use_hls_profile: bool = False) -> List[str]:
        codec = (self.output_codec or "mpeg2video").lower()
        vista_mode = bool(getattr(self, "force_vista_mode", False))
        frame_size = "720x480" if vista_mode else "1280x720"
        video_bufsize = f"{max(self._bitrate_to_bps(effective_bitrate) // 500, 1000)}k"
        common = [
            "-pix_fmt", "yuv420p",
            "-r", "30000/1001",
            "-s", frame_size,
            "-aspect", "16:9",
            "-b:v", effective_bitrate,
            "-maxrate:v", effective_bitrate,
            "-bufsize:v", video_bufsize,
            "-g", "15",
            "-bf", "0",
        ]
        if codec in ("h264", "libx264", "mpeg4_h264", "mpeg4-avc", "avc"):
            return [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-tune", "zerolatency",
                "-x264-params", "nal-hrd=cbr:force-cfr=1",
                "-profile:v", "high",
                "-level:v", "4.0",
            ] + common
        args = [
            "-c:v", "mpeg2video",
            "-profile:v", "main",
            "-level:v", "main",
        ] + common
        if vista_mode:
            args.extend([
                "-q:v", "3",
                "-intra_vlc", "1",
                "-non_linear_quant", "1",
            ])
        if use_hls_profile:
            args.extend([
                "-qmin", "2",
                "-qmax", "12",
                "-sc_threshold", "0",
            ])
        return args

    def _is_network_media_source(self, source_url: str) -> bool:
        return urllib.parse.urlparse(source_url or "").scheme.lower() in ("http", "https")

    def _needs_pluto_headers(self, source_url: str) -> bool:
        host = urllib.parse.urlparse(source_url or "").netloc.lower()
        return "pluto.tv" in host or "jmp2.uk" in host

    def _should_keep_original_hls_url(self, source_url: str, playback_url: str) -> bool:
        if not playback_url:
            return True
        source_parts = urllib.parse.urlparse(source_url or "")
        source_host = source_parts.netloc.lower()
        playback_parts = urllib.parse.urlparse(playback_url)
        if "pluto.tv" in source_host and source_parts.path.lower().endswith("/master.m3u8"):
            return False
        if len(playback_url) > 1024:
            logger.info("Keeping original HLS URL because resolved variant is too long")
            return True
        if "jmp2.uk" in source_host and playback_parts.query:
            logger.info("Keeping original Pluto-style HLS URL instead of signed variant")
            return True
        return False

    def _looks_like_local_hls(self, source_url: str) -> bool:
        parsed = urllib.parse.urlparse(source_url or "")
        scheme = parsed.scheme.lower()
        if scheme and scheme != "file" and not re.fullmatch(r"[a-z]", scheme):
            return False
        path = parsed.path if scheme == "file" else source_url
        return os.path.splitext(path)[1].lower() in (".m3u8", ".m3u")

    def _local_hls_video_map(self, source_url: str) -> str:
        parsed = urllib.parse.urlparse(source_url or "")
        scheme = parsed.scheme.lower()
        if scheme and scheme != "file" and not re.fullmatch(r"[a-z]", scheme):
            return "0:v:0?"
        path = parsed.path if scheme == "file" else source_url
        if not path or not os.path.isfile(path) or not os.path.basename(path).startswith("hdhr_pluto_"):
            return "0:v:0?"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(8192)
        except OSError:
            return "0:v:0?"
        match = re.search(r"^#HDHR-PROXY-VIDEO-MAP:(\S+)", head, flags=re.MULTILINE)
        if match and match.group(1) in ("0:v:0?", "0:v:1?"):
            return match.group(1)
        return "0:v:0?"

    def _local_hls_audio_map(self, source_url: str) -> str:
        parsed = urllib.parse.urlparse(source_url or "")
        scheme = parsed.scheme.lower()
        if scheme and scheme != "file" and not re.fullmatch(r"[a-z]", scheme):
            return "0:a:0?"
        path = parsed.path if scheme == "file" else source_url
        if not path or not os.path.isfile(path) or not os.path.basename(path).startswith("hdhr_pluto_"):
            return "0:a:0?"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(8192)
        except OSError:
            return "0:a:0?"
        match = re.search(r"^#HDHR-PROXY-AUDIO-MAP:(\S+)", head, flags=re.MULTILINE)
        if match and match.group(1) in ("0:a:0?", "0:a:1?"):
            return match.group(1)
        return "0:a:0?"

    def _resolve_ffmpeg_path(self, ffmpeg_path: str) -> str:
        if ffmpeg_path and os.path.isfile(ffmpeg_path):
            return ffmpeg_path

        resolved = shutil.which(ffmpeg_path or "ffmpeg")
        if resolved:
            return resolved

        candidates = [
            r"D:\WMC_EPG\New folder (4)\hdhr_proxy\ffmpeg\ffmpeg-2026-05-18-git-b4d11dffbf-essentials_build\bin\ffmpeg.exe",
            r"C:\Users\jawwa\Downloads\Compressed\ffmpeg-2026-03-30-git-e54e117998-full_build\ffmpeg-2026-03-30-git-e54e117998-full_build\bin\ffmpeg.exe",
            r"C:\Program Files\NextPVR\Other\ffmpeg.exe",
            r"C:\Program Files (x86)\NPVR\Other\ffmpeg.exe",
            r"C:\Program Files\Common Files\Solveig Multimedia\ffmpeg.exe",
            r"C:\Users\jawwa\Downloads\New folder\ffmpeg.exe",
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                logger.info("Using ffmpeg at %s", candidate)
                return candidate

        return ffmpeg_path or "ffmpeg"

    def _firmware_model_name(self) -> str:
        model = (self.model_number or "").lower()
        if "hdhr4" in model:
            return "hdhomerun4_atsc"
        if "hdhr3" in model:
            return "hdhomerun3_atsc"
        return "hdhomerun4_atsc"

    def _infer_upgrade_target_version(self) -> str:
        firmware_model = self._firmware_model_name()
        match = re.search(r"_(\d{8})$", firmware_model)
        if match:
            return match.group(1)
        if firmware_model == "hdhomerun4_atsc":
            return "20150826"
        if firmware_model.startswith("hdhomerun3_"):
            return "20150406"
        return self.firmware_version
