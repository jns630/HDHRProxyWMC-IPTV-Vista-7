"""Small ffprobe helper used to decide whether a live stream can be copied.

Windows Media Center only needs a re-encode when the incoming video is not
already H.264 that WMC can decode.  Probing once and caching the result avoids
running ffprobe on every tune while still noticing a changed upstream codec.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PROBE_CACHE_TTL_SECONDS = 120.0
_CACHE: Dict[str, tuple] = {}
_LOCK = threading.Lock()


def resolve_ffprobe_path(ffmpeg_path: str, configured: Optional[str] = None) -> str:
    """Return the ffprobe binary that belongs with the configured ffmpeg."""
    if configured:
        return configured
    ffmpeg_path = ffmpeg_path or "ffmpeg"
    directory, name = os.path.split(ffmpeg_path)
    if name:
        candidate = os.path.join(directory, re.sub(r"ffmpeg", "ffprobe", name, flags=re.IGNORECASE))
        if candidate != ffmpeg_path and os.path.exists(candidate):
            return candidate
    return "ffprobe"


def probe_stream(source_url: str, ffprobe_path: str = "ffprobe", timeout: int = 12) -> Optional[Dict]:
    """Return basic stream metadata, or None when probing is not possible."""
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(source_url)
        if cached and cached[1] > now:
            return dict(cached[0]) if cached[0] is not None else None

    info = _run_probe(source_url, ffprobe_path, timeout)
    with _LOCK:
        _CACHE[source_url] = (info, now + PROBE_CACHE_TTL_SECONDS)
    return dict(info) if info is not None else None


def _run_probe(source_url: str, ffprobe_path: str, timeout: int) -> Optional[Dict]:
    cmd = [
        ffprobe_path,
        "-hide_banner",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        "-analyzeduration", "3000000",
        "-probesize", "3000000",
        "-rw_timeout", "8000000",
        source_url,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except Exception as exc:
        logger.debug("ffprobe failed for %s: %s", source_url, exc)
        return None
    if result.returncode != 0:
        logger.debug(
            "ffprobe returned %s for %s: %s",
            result.returncode,
            source_url,
            result.stderr.decode("utf-8", errors="replace").strip()[:300],
        )
        return None
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return None
    return parse_probe_payload(payload)


def parse_probe_payload(payload: Dict) -> Optional[Dict]:
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        return None
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    fmt = payload.get("format") or {}
    bit_rate = _as_int(fmt.get("bit_rate")) or _as_int(video.get("bit_rate"))
    return {
        "video_codec": (video.get("codec_name") or "").lower(),
        "profile": video.get("profile") or "",
        "level": _as_int(video.get("level")),
        "pix_fmt": (video.get("pix_fmt") or "").lower(),
        "width": _as_int(video.get("width")),
        "height": _as_int(video.get("height")),
        "avg_frame_rate": video.get("avg_frame_rate") or "",
        "bit_rate": bit_rate,
        "audio_codec": (audio.get("codec_name") or "").lower() if audio else "",
    }


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_wmc_compatible_h264(info: Optional[Dict]) -> bool:
    """Return True when the probed video can be passed through to WMC."""
    if not info:
        return False
    if info.get("video_codec") != "h264":
        return False
    pix_fmt = (info.get("pix_fmt") or "").lower()
    if pix_fmt and pix_fmt not in ("yuv420p", "yuvj420p"):
        return False
    profile = (info.get("profile") or "").lower()
    if profile and profile not in ("constrained baseline", "baseline", "main", "high"):
        # High 10 / High 4:2:2 / High 4:4:4 are not decodable by WMC's decoder.
        return False
    level = info.get("level")
    if level and level > 42:
        return False
    return True
