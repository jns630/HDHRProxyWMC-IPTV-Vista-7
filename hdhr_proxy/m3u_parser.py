import re
import urllib.request
import urllib.parse
import logging
import os
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

US_BCAST_FIRST_PHYSICAL_CHANNEL = 2
US_BCAST_LAST_PHYSICAL_CHANNEL = 69
VIRTUAL_PROGRAMS_PER_PHYSICAL_CHANNEL = 16
VIRTUAL_FIRST_PROGRAM_NUMBER = 3
MPEGTS_DYNAMIC_PID_BASE = 0x30
HLS_REDIRECTED_URL_MAX_LENGTH = 1024


class M3UChannel:
    def __init__(self):
        self.name: str = ""
        self.url: str = ""
        self.tvg_id: str = ""
        self.tvg_name: str = ""
        self.tvg_logo: str = ""
        self.tvg_chno: str = ""
        self.group_title: str = ""
        self.ext: Dict[str, str] = {}

    def __repr__(self):
        return f"<M3UChannel {self.name} ({self.tvg_chno or '?'})>"


class M3UParser:
    EXTINF_RE = re.compile(
        r'#EXTINF:(?P<duration>-?\d+)\s*'
        r'(?P<props>.*?)\s*,\s*(?P<name>.*)'
    )
    PROP_RE = re.compile(r'(\w+)\s*=\s*"(.*?)"')

    @classmethod
    def parse_url(cls, url: str) -> List[M3UChannel]:
        logger.info(f"Fetching M3U playlist from URL: {url}")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "VLC/3.0.20 LibVLC/3.0.20",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        master_channel = cls._hls_master_as_channel(raw, url)
        if master_channel:
            return [master_channel]
        return cls._resolve_relative_urls(cls.parse_text(raw), url)

    @classmethod
    def parse_file(cls, path: str, hls_base_url: Optional[str] = None) -> List[M3UChannel]:
        logger.info(f"Reading M3U playlist from file: {path}")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        abs_path = os.path.abspath(path)
        source = hls_base_url or abs_path
        master_channel = cls._hls_master_as_channel(raw, source, local_path=abs_path)
        if master_channel:
            return [master_channel]
        return cls._resolve_relative_urls(cls.parse_text(raw), source)

    @classmethod
    def parse_text(cls, raw: str) -> List[M3UChannel]:
        channels: List[M3UChannel] = []
        lines = raw.strip().splitlines()
        current = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTM3U"):
                continue
            if line.startswith("#EXTINF:"):
                m = cls.EXTINF_RE.match(line)
                if m:
                    current = M3UChannel()
                    current.name = m.group("name").strip()
                    props_str = m.group("props")
                    for pm in cls.PROP_RE.finditer(props_str):
                        key, val = pm.group(1).lower(), pm.group(2)
                        if key == "tvg-id":
                            current.tvg_id = val
                        elif key == "tvg-name":
                            current.tvg_name = val
                        elif key == "tvg-logo":
                            current.tvg_logo = val
                        elif key == "tvg-chno":
                            current.tvg_chno = val
                        elif key == "group-title":
                            current.group_title = val
                        else:
                            current.ext[key] = val
                else:
                    current = M3UChannel()
                    if "," in line:
                        current.name = line.split(",", 1)[1].strip()
            elif line.startswith("#EXT-X-STREAM-INF:"):
                current = M3UChannel()
                attrs_str = line[len("#EXT-X-STREAM-INF:"):].strip()
                for pm in cls.PROP_RE.finditer(attrs_str):
                    key, val = pm.group(1).lower(), pm.group(2)
                    current.ext[key] = val
                if "resolution" in current.ext:
                    current.name = current.ext["resolution"]
                elif "bandwidth" in current.ext:
                    bw = int(current.ext["bandwidth"])
                    current.name = f"{bw // 1000}kbps"
                else:
                    current.name = "HLS Stream"
            elif line.startswith("#EXT-X-MEDIA:"):
                current = M3UChannel()
                attrs_str = line[len("#EXT-X-MEDIA:"):].strip()
                for pm in cls.PROP_RE.finditer(attrs_str):
                    key, val = pm.group(1).lower(), pm.group(2)
                    if key == "uri":
                        current.url = val
                    elif key == "name":
                        current.name = val
                    elif key == "language":
                        current.ext["language"] = val
                    elif key == "type":
                        current.ext["type"] = val
                    else:
                        current.ext[key] = val
                if current.url:
                    channels.append(current)
                    current = None
            elif line.startswith("#"):
                continue
            else:
                if current:
                    current.url = line.strip()
                    channels.append(current)
                    current = None
        logger.info(f"Parsed {len(channels)} channels from M3U")
        return channels

    @staticmethod
    def _resolve_relative_urls(channels: List[M3UChannel], base: str) -> List[M3UChannel]:
        if urllib.parse.urlparse(base).scheme in ("http", "https"):
            for channel in channels:
                if channel.url and not urllib.parse.urlparse(channel.url).scheme:
                    channel.url = urllib.parse.urljoin(base, channel.url)
            return channels

        base_dir = os.path.dirname(base)
        for channel in channels:
            if channel.url and not urllib.parse.urlparse(channel.url).scheme and not os.path.isabs(channel.url):
                channel.url = os.path.abspath(os.path.join(base_dir, channel.url))
        return channels

    @classmethod
    def _hls_master_as_channel(cls, raw: str, source: str, local_path: Optional[str] = None) -> Optional[M3UChannel]:
        if "#EXT-X-STREAM-INF" not in raw or "#EXTINF:" in raw:
            return None
        channel = M3UChannel()
        channel.name = "HLS Stream"
        channel.url = source
        channel.ext["hls_master"] = "1"

        variants = cls._hls_variant_uris(raw)
        if variants:
            channel.ext["hls_variants"] = ",".join(uri for uri, _attrs in variants)

        parsed_source = urllib.parse.urlparse(source)
        if parsed_source.scheme in ("http", "https"):
            selected = cls._select_hls_variant(variants)
            if selected:
                playback_url = urllib.parse.urljoin(source, selected)
                # Keep the canonical channel URL stable for remote HLS sources. Some
                # providers (notably Pluto partner mirrors) redirect the initial master
                # to very long signed variant URLs that expire quickly and can fail in
                # ffmpeg/WMC when we bake them into the lineup too early.
                channel.ext["hls_playback_url"] = cls._stable_remote_hls_url(source, playback_url)
                logger.info("Using remote HLS playback hint: %s", channel.ext["hls_playback_url"])
            else:
                channel.ext["hls_playback_url"] = source
            return channel

        base_path = local_path or source
        base_dir = os.path.dirname(os.path.abspath(base_path))
        missing = []
        existing = []
        for uri, _attrs in variants:
            if urllib.parse.urlparse(uri).scheme:
                existing.append(uri)
                continue
            candidate = os.path.abspath(os.path.join(base_dir, uri))
            if os.path.exists(candidate):
                existing.append(candidate)
            else:
                missing.append(uri)

        if existing:
            # Feed ffmpeg a real media playlist when the local master references files beside it.
            channel.url = existing[0]
            channel.ext["hls_playback_url"] = existing[0]
            logger.info("Using local HLS variant for playback: %s", existing[0])
        if missing and not existing:
            channel.ext["hls_missing_variants"] = ",".join(missing)
            logger.warning(
                "Local HLS master %s references missing variant playlists: %s. "
                "Use --m3u-url for the original URL, add --hls-base-url, or place the variant files beside it.",
                base_path,
                ", ".join(missing),
            )
        return channel

    @staticmethod
    def _stable_remote_hls_url(source: str, playback_url: str) -> str:
        if not playback_url:
            return source
        source_host = urllib.parse.urlparse(source).netloc.lower()
        playback_parts = urllib.parse.urlparse(playback_url)
        if len(playback_url) > HLS_REDIRECTED_URL_MAX_LENGTH:
            return source
        if "jmp2.uk" in source_host and playback_parts.query:
            return source
        return playback_url

    @staticmethod
    def _hls_variant_uris(raw: str) -> List[Tuple[str, Dict[str, str]]]:
        variants: List[Tuple[str, Dict[str, str]]] = []
        expect_variant = False
        attrs: Dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-STREAM-INF:"):
                expect_variant = True
                attrs = {}
                attrs_str = line[len("#EXT-X-STREAM-INF:"):].strip()
                for key, value in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', attrs_str, flags=re.IGNORECASE):
                    attrs[key.lower()] = value.strip().strip('"')
                continue
            if expect_variant:
                if not line.startswith("#"):
                    variants.append((line, attrs))
                    expect_variant = False
                continue
        return variants

    @staticmethod
    def _select_hls_variant(variants: List[Tuple[str, Dict[str, str]]]) -> Optional[str]:
        if not variants:
            return None

        def score(item: Tuple[str, Dict[str, str]]) -> Tuple[int, int]:
            uri, attrs = item
            width = height = 0
            resolution = attrs.get("resolution", "")
            match = re.match(r"(\d+)x(\d+)", resolution)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
            bandwidth = int(re.sub(r"\D", "", attrs.get("average-bandwidth") or attrs.get("bandwidth") or "0") or "0")
            # Prefer a ready-to-transcode 720p-ish variant; WMC is happier when ffmpeg avoids probing every rung.
            if 600 <= height <= 900:
                return (3, bandwidth)
            if 900 < height <= 1080:
                return (2, -bandwidth)
            return (1, bandwidth)

        return max(variants, key=score)[0]


def build_lineup(
    channels: List[M3UChannel],
    base_url: str,
    channel_mapping: Optional[Dict[str, str]] = None,
    tuner_count: int = 2,
) -> Tuple[List[Dict], Dict[str, M3UChannel]]:
    lineup = []
    ch_map: Dict[str, M3UChannel] = {}
    mapping = channel_mapping or {}

    for i, ch in enumerate(channels, start=1):
        physical_channel = _physical_channel_for_index(i)
        program_number = _program_number_for_index(i)
        virtual_minor = _virtual_minor_for_index(i)
        guide_number = ch.tvg_chno or mapping.get(ch.name, "") or f"{physical_channel}.{virtual_minor}"
        frequency = _us_bcast_frequency_for_physical_channel(physical_channel)
        low_freq = frequency - 3000000
        high_freq = frequency + 3000000
        pid_base = MPEGTS_DYNAMIC_PID_BASE + ((i - 1) * 3)
        pmt_pid = pid_base
        video_pid = pid_base + 1
        audio_pid = pid_base + 2
        safe_name = _safe_program_name(ch.name)
        program_pids = f"0,16,17,{pmt_pid},{video_pid},{audio_pid}"
        program_table = (
            f"[{program_number}:{pmt_pid}:{safe_name}:{program_pids}]"
            f"[tsid=0x{physical_channel:04x}]"
        )
        ch_map[guide_number] = ch
        url = f"{base_url}/stream/{guide_number}"
        lineup.append({
            "GuideNumber": guide_number,
            "GuideName": ch.name,
            "URL": url,
            "Modulation": "8vsb",
            "PhysicalChannel": physical_channel,
            "Frequency": frequency,
            "LowFreq": low_freq,
            "HighFreq": high_freq,
            "ProgramNumber": program_number,
            "PMTPID": pmt_pid,
            "VideoPID": video_pid,
            "AudioPID": audio_pid,
            "ProgramPIDs": program_pids,
            "ProgramTable": program_table,
            "SignalStrength": 95,
            "SignalQuality": 95,
            "SymbolQuality": 100,
            "NetworkRate": 8000000,
        })

    return lineup, ch_map


def _physical_channel_for_index(index: int) -> int:
    physical_count = US_BCAST_LAST_PHYSICAL_CHANNEL - US_BCAST_FIRST_PHYSICAL_CHANNEL + 1
    slot = (max(index, 1) - 1) // VIRTUAL_PROGRAMS_PER_PHYSICAL_CHANNEL
    return US_BCAST_FIRST_PHYSICAL_CHANNEL + (slot % physical_count)


def _program_number_for_index(index: int) -> int:
    slot = (max(index, 1) - 1) % VIRTUAL_PROGRAMS_PER_PHYSICAL_CHANNEL
    return VIRTUAL_FIRST_PROGRAM_NUMBER + slot


def _virtual_minor_for_index(index: int) -> int:
    return ((max(index, 1) - 1) % VIRTUAL_PROGRAMS_PER_PHYSICAL_CHANNEL) + 1


def _us_bcast_frequency_for_physical_channel(physical: int) -> int:
    # US ATSC broadcast center frequencies in Hz for channels commonly scanned by HDHomeRun/WMC.
    if 2 <= physical <= 4:
        return (57 + (physical - 2) * 6) * 1000000
    if physical == 5:
        return 79000000
    if physical == 6:
        return 85000000
    if 7 <= physical <= 13:
        return (177 + (physical - 7) * 6) * 1000000
    if 14 <= physical <= 69:
        return (473 + (physical - 14) * 6) * 1000000
    return 57000000


def _safe_program_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.+-]+", "-", name or "VirtualHD")
    return clean.strip("-")[:32] or "VirtualHD"
