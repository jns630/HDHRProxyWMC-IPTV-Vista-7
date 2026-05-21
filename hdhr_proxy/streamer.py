import subprocess
import urllib.parse
import urllib.request
import logging
import time
import io
import threading
from typing import Optional, Dict

from .m3u_parser import M3UParser

logger = logging.getLogger(__name__)

STREAM_READ_CHUNK = 131072  # 128KB
FFMPEG_READ_TIMEOUT = 5.0
FFMPEG_INPUT_OPTIONS = [
    "-fflags", "+genpts+discardcorrupt",
    "-flags", "low_delay",
    "-analyzeduration", "3000000",
    "-probesize", "3000000",
    "-rw_timeout", "15000000",
]


def _needs_pluto_headers(source_url: str) -> bool:
    host = urllib.parse.urlparse(source_url or "").netloc.lower()
    return "pluto.tv" in host or "jmp2.uk" in host


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
            raw = resp.read(512 * 1024).decode("utf-8", errors="replace")
    except Exception:
        return source_url

    variants = M3UParser._hls_variant_uris(raw)
    selected = M3UParser._select_hls_variant(variants)
    if not selected:
        return source_url
    playback_url = urllib.parse.urljoin(source_url, selected)
    if _should_keep_original_hls_url(source_url, playback_url):
        return source_url
    return playback_url


def _should_keep_original_hls_url(source_url: str, playback_url: str) -> bool:
    if not playback_url:
        return True
    source_host = urllib.parse.urlparse(source_url or "").netloc.lower()
    playback_parts = urllib.parse.urlparse(playback_url)
    if len(playback_url) > 1024:
        return True
    if "jmp2.uk" in source_host and playback_parts.query:
        return True
    return False


def video_encoder_args(output_codec: str, bitrate: str):
    codec = (output_codec or "mpeg2video").lower()
    if codec in ("h264", "libx264", "mpeg4_h264", "mpeg4-avc", "avc"):
        return [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-profile:v", "high",
            "-level:v", "4.0",
            "-b:v", bitrate,
            "-maxrate:v", bitrate,
            "-bufsize:v", str(int(bitrate.rstrip("k")) * 2) + "k",
            "-g", "15",
            "-bf", "0",
            "-pix_fmt", "yuv420p",
            "-r", "30000/1001",
            "-s", "1280x720",
            "-aspect", "16:9",
        ]
    # mpeg2video — avoid VBV constraints that cause "impossible bitrate constraints" error
    return [
        "-c:v", "mpeg2video",
        "-profile:v", "main",
        "-level:v", "main",
        "-b:v", bitrate,
        "-g", "15",
        "-bf", "0",
        "-pix_fmt", "yuv420p",
        "-r", "30000/1001",
        "-s", "1280x720",
        "-aspect", "16:9",
    ]


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
):
    source_url = _resolve_hls_source_url(source_url)
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-fflags", "+genpts+discardcorrupt",
        "-flags", "low_delay",
        "-analyzeduration", "3000000",
        "-probesize", "3000000",
        "-allowed_extensions", "ALL",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto,udp,rtp",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "2",
        "-reconnect_on_network_error", "1",
        "-rw_timeout", "15000000",
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
        "-dn",
        "-sn",
    ])
    cmd += video_encoder_args(output_codec, bitrate) + [
        "-c:a", audio_codec,
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-af", "aresample=async=1:first_pts=0",
        "-f", output_format,
        "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
        "-mpegts_transport_stream_id", "1",
        "-mpegts_service_id", "3",
        "-mpegts_service_type", "digital_tv",
        "-metadata", "service_provider=VirtualHDHR",
        "-metadata", "service_name=VirtualHDHR",
        "-muxrate", "19392658",
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
    ):
        self.channel_id = channel_id
        self.channel_map = channel_map
        self.ffmpeg_enabled = ffmpeg_enabled
        self.ffmpeg_path = ffmpeg_path
        self.output_codec = output_codec
        self.audio_codec = audio_codec
        self.bitrate = bitrate
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
        )
