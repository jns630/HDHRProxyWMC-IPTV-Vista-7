import subprocess
import urllib.parse
import urllib.request
import logging
import time
import io
import threading
import re
import os
from typing import Optional, Dict, List, Tuple

from .m3u_parser import M3UParser

logger = logging.getLogger(__name__)

STREAM_READ_CHUNK = 131072  # 128KB
FFMPEG_READ_TIMEOUT = 5.0
FFMPEG_ANALYZE_US = "5000000"
FFMPEG_PROBE_BYTES = "5000000"
FFMPEG_INPUT_OPTIONS = [
    "-fflags", "+genpts+discardcorrupt",
    "-flags", "low_delay",
    "-analyzeduration", FFMPEG_ANALYZE_US,
    "-probesize", FFMPEG_PROBE_BYTES,
    "-rw_timeout", "15000000",
]


def _needs_pluto_headers(source_url: str) -> bool:
    host = urllib.parse.urlparse(source_url or "").netloc.lower()
    return "pluto.tv" in host or "jmp2.uk" in host


def _is_hls_like_source(source_url: str) -> bool:
    parsed = urllib.parse.urlparse(source_url or "")
    scheme = parsed.scheme.lower()
    if scheme and scheme not in ("http", "https", "file") and not re.fullmatch(r"[a-z]", scheme):
        return False
    path = parsed.path if scheme == "file" else (parsed.path or source_url)
    return path.lower().endswith((".m3u8", ".m3u"))


def _hls_profile_bitrate(bitrate: str) -> str:
    text = str(bitrate or "").strip().lower()
    match = re.match(r"^(\d+)([km]?)$", text)
    if not match:
        return bitrate
    amount = int(match.group(1))
    suffix = match.group(2) or ""
    if suffix == "m":
        return f"{amount}m"
    if suffix == "k":
        return f"{amount + 500}k"
    return str(amount + 500)


def _resolve_hls_source_url(source_url: str) -> str:
    parsed = urllib.parse.urlparse(source_url or "")
    if parsed.scheme.lower() not in ("http", "https"):
        return source_url
    if not parsed.path.lower().endswith((".m3u8", ".m3u")):
        return source_url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
        "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
    }
    if _needs_pluto_headers(source_url):
        headers["Origin"] = "https://pluto.tv"
        headers["Referer"] = "https://pluto.tv/"

    try:
        req = urllib.request.Request(source_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            base_url = resp.geturl() or source_url
            raw = resp.read(512 * 1024).decode("utf-8", errors="replace")
    except Exception:
        return source_url

    variants = M3UParser._hls_variant_uris(raw)
    selected = M3UParser._select_hls_variant(variants)
    if not selected:
        return source_url
    playback_url = urllib.parse.urljoin(base_url, selected)
    if _hls_master_has_subtitles(raw, variants, selected):
        return source_url
    if _should_keep_original_hls_url(source_url, playback_url):
        return source_url
    return playback_url


def _should_keep_original_hls_url(source_url: str, playback_url: str) -> bool:
    if not playback_url:
        return True
    source_parts = urllib.parse.urlparse(source_url or "")
    source_host = source_parts.netloc.lower()
    playback_parts = urllib.parse.urlparse(playback_url)
    if "pluto.tv" in source_host and source_parts.path.lower().endswith("/master.m3u8"):
        return False
    if len(playback_url) > 1024:
        return True
    if "jmp2.uk" in source_host and playback_parts.query:
        return True
    return False


def _parse_hls_attribute_list(text: str) -> Dict[str, str]:
    attrs = {}
    for key, value in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text, flags=re.IGNORECASE):
        attrs[key.lower()] = value.strip().strip('"')
    return attrs


def _hls_master_has_subtitles(master_text: str, variants: List[Tuple[str, Dict[str, str]]], selected_uri: str) -> bool:
    subtitle_groups = set()
    for line in master_text.splitlines():
        line = line.strip()
        if not line.upper().startswith("#EXT-X-MEDIA:"):
            continue
        attrs = _parse_hls_attribute_list(line.split(":", 1)[1])
        if (attrs.get("type") or "").upper() == "SUBTITLES" and attrs.get("uri"):
            group_id = attrs.get("group-id")
            if group_id:
                subtitle_groups.add(group_id)
    if not subtitle_groups:
        return False

    for uri, attrs in variants:
        if uri != selected_uri:
            continue
        group_id = attrs.get("subtitles")
        return bool(group_id and group_id in subtitle_groups)
    return False


def _hls_source_has_subtitles(source_url: str) -> bool:
    parsed = urllib.parse.urlparse(source_url or "")
    scheme = parsed.scheme.lower()
    if scheme and scheme not in ("http", "https", "file") and not re.fullmatch(r"[a-z]", scheme):
        return False

    text = ""
    if scheme in ("http", "https"):
        if _needs_pluto_headers(source_url):
            return False
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
            }
            with urllib.request.urlopen(urllib.request.Request(source_url, headers=headers), timeout=5) as resp:
                text = resp.read(512 * 1024).decode("utf-8", errors="replace")
        except Exception:
            return False
    else:
        path = parsed.path if scheme == "file" else source_url
        if not path or not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(512 * 1024)
        except OSError:
            return False

    for line in text.splitlines():
        line = line.strip()
        if not line.upper().startswith("#EXT-X-MEDIA:"):
            continue
        attrs = _parse_hls_attribute_list(line.split(":", 1)[1])
        if (attrs.get("type") or "").upper() == "SUBTITLES" and attrs.get("uri"):
            return True
    return False


def _escape_ffmpeg_filter_filename(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _hls_subtitle_burn_filter(source_url: str) -> Optional[str]:
    if not _hls_source_has_subtitles(source_url):
        return None
    # The subtitles filter opens HLS subtitle playlists in a separate demuxer
    # context that does not inherit the input protocol whitelist. Leave playback
    # unfiltered so streams do not fail on https subtitle renditions.
    return None


def video_encoder_args(output_codec: str, bitrate: str, use_hls_profile: bool = False, vista_mode: bool = False):
    codec = (output_codec or "mpeg2video").lower()
    frame_size = "720x480" if vista_mode else "1280x720"
    bitrate_text = str(bitrate or "").strip().lower()
    match = re.match(r"^(\d+)([km]?)$", bitrate_text)
    if match:
        amount = int(match.group(1))
        suffix = match.group(2) or "k"
        bitrate_bps = amount * (1000000 if suffix == "m" else 1000)
    else:
        bitrate_bps = 4000000
    video_bufsize = f"{max(bitrate_bps // 500, 1000)}k"
    base_args = [
        "-pix_fmt", "yuv420p",
        "-r", "30000/1001",
        "-s", frame_size,
        "-aspect", "16:9",
    ]
    if codec in ("h264", "libx264", "mpeg4_h264", "mpeg4-avc", "avc"):
        return [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-x264-params", "nal-hrd=cbr:force-cfr=1",
            "-profile:v", "high",
            "-level:v", "4.0",
            "-b:v", bitrate,
            "-maxrate:v", bitrate,
            "-bufsize:v", video_bufsize,
            "-g", "15",
            "-bf", "0",
        ] + base_args
    # mpeg2video — avoid VBV constraints that cause "impossible bitrate constraints" error
    args = [
        "-c:v", "mpeg2video",
        "-profile:v", "main",
        "-level:v", "main",
        "-b:v", bitrate,
        "-maxrate:v", bitrate,
        "-bufsize:v", video_bufsize,
        "-g", "15",
        "-bf", "0",
    ] + base_args
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


def ffmpeg_available(ffmpeg_path: str = "ffmpeg") -> bool:
    try:
        subprocess.run(
            [ffmpeg_path, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def resolve_stream_url(channel_id: str, channel_map: Dict[str, object]) -> Optional[str]:
    ch = channel_map.get(channel_id)
    if ch is None:
        return None
    return ch.url if hasattr(ch, "url") else None


def direct_stream(source_url: str):
    req = urllib.request.Request(
        source_url,
        headers={
            "User-Agent": "VLC/3.0.20 LibVLC/3.0.20",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        while True:
            chunk = resp.read(STREAM_READ_CHUNK)
            if not chunk:
                break
            yield chunk


def ffmpeg_transcode_stream(
    source_url: str,
    ffmpeg_path: str,
    output_codec: str = "mpeg2video",
    audio_codec: str = "ac3",
    bitrate: str = "4000k",
    output_format: str = "mpegts",
    vista_mode: bool = False,
):
    source_url = _resolve_hls_source_url(source_url)
    use_hls_profile = _is_hls_like_source(source_url)
    effective_bitrate = _hls_profile_bitrate(bitrate) if use_hls_profile else bitrate
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-fflags", "+genpts+discardcorrupt",
        "-flags", "low_delay",
        "-analyzeduration", FFMPEG_ANALYZE_US,
        "-probesize", FFMPEG_PROBE_BYTES,
        "-allowed_extensions", "ALL",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto,udp,rtp",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "2",
        "-reconnect_on_network_error", "1",
        "-rw_timeout", "15000000",
        "-thread_queue_size", "1024",
        "-user_agent", "VLC/3.0.20 LibVLC/3.0.20",
    ]
    if _needs_pluto_headers(source_url):
        cmd.extend([
            "-headers", "Accept: application/vnd.apple.mpegurl,application/x-mpegURL,*/*\r\nOrigin: https://pluto.tv\r\nReferer: https://pluto.tv/\r\n",
        ])
    cmd.extend([
        "-i", source_url,
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-fps_mode", "cfr",
        "-dn",
        "-sn",
    ])
    subtitle_filter = _hls_subtitle_burn_filter(source_url)
    if subtitle_filter:
        cmd.extend(["-vf", subtitle_filter])
    cmd += video_encoder_args(output_codec, effective_bitrate, use_hls_profile=use_hls_profile, vista_mode=vista_mode) + [
        "-c:a", audio_codec,
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-af", "aresample=async=1000:first_pts=0:min_hard_comp=0.100",
        "-f", output_format,
        "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
        "-mpegts_transport_stream_id", "1",
        "-mpegts_service_id", "3",
        "-mpegts_service_type", "digital_tv",
        "-metadata", "service_provider=VirtualHDHR",
        "-metadata", "service_name=VirtualHDHR",
        "-muxrate", "19392658",
        "-muxpreload", "0.02",
        "-muxdelay", "0.02",
        "-flush_packets", "1",
        "-pat_period", "0.10",
        "pipe:1",
    ]
    logger.debug("ffmpeg command: %s", " ".join(cmd))

    buf_size = STREAM_READ_CHUNK * 4
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=buf_size,
    )

    stderr_thread = threading.Thread(
        target=_log_stderr,
        args=(proc,),
        daemon=True,
        name=f"ffmpeg-stderr-{id(proc)}",
    )
    stderr_thread.start()

    try:
        while True:
            chunk = proc.stdout.read(STREAM_READ_CHUNK)
            if not chunk:
                break
            yield chunk
            if proc.poll() is not None and not chunk:
                break
    except GeneratorExit:
        proc.kill()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def _log_stderr(proc: subprocess.Popen):
    try:
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                logger.debug(f"[ffmpeg] {text}")
    except Exception:
        pass


class StreamSession:
    def __init__(
        self,
        channel_id: str,
        channel_map: Dict,
        ffmpeg_enabled: bool = True,
        ffmpeg_path: str = "ffmpeg",
        output_codec: str = "mpeg2video",
        audio_codec: str = "ac3",
        bitrate: str = "4000k",
        vista_mode: bool = False,
    ):
        self.channel_id = channel_id
        self.channel_map = channel_map
        self.ffmpeg_enabled = ffmpeg_enabled
        self.ffmpeg_path = ffmpeg_path
        self.output_codec = output_codec
        self.audio_codec = audio_codec
        self.bitrate = bitrate
        self.vista_mode = vista_mode
        self._generator = None

    def stream(self):
        ch = self.channel_map.get(self.channel_id)
        if ch is None:
            logger.warning("Channel %s not found in map", self.channel_id)
            return

        source_url = ch.url
        logger.info("Starting stream for channel %s: %s", self.channel_id, source_url)

        yield from ffmpeg_transcode_stream(
            source_url,
            self.ffmpeg_path,
            output_codec=self.output_codec,
            audio_codec=self.audio_codec,
            bitrate=self.bitrate,
            vista_mode=self.vista_mode,
        )
