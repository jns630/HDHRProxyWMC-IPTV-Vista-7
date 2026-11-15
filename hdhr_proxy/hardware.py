"""Cross-platform hardware encoder/decoder detection for the proxy.

Windows Media Center is the primary target, but the proxy also runs on Linux
and macOS for LAN deployments.  Supported hardware paths:

* NVIDIA NVENC/NVDEC (Windows, Linux; macOS where the driver exists)
* Intel Quick Sync (Windows)
* AMD AMF/VCN (Windows)
* VAAPI (Linux)
* Apple VideoToolbox (macOS)

Detection is best-effort and cached for the lifetime of the process:
1. ask the installed ffmpeg which encoders it was built with,
2. ask the OS which GPU vendors/devices are present,
3. run a one-frame test encode to make sure the driver actually works.

If anything fails the caller falls back to libx264/mpeg2video, so existing
installations keep working unchanged.
"""

import glob
import logging
import os
import platform
import re
import subprocess
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

HW_ENCODERS = {
    "nvenc": "h264_nvenc",
    "qsv": "h264_qsv",
    "amf": "h264_amf",
    "vaapi": "h264_vaapi",
    "videotoolbox": "h264_videotoolbox",
}
_ENCODER_TO_FAMILY = {value: key for key, value in HW_ENCODERS.items()}

# Order candidates by what is most likely to be usable on each OS.
_WINDOWS_ORDER = ("nvenc", "qsv", "amf")
_LINUX_ORDER = ("nvenc", "vaapi")
_MACOS_ORDER = ("videotoolbox", "nvenc")

_CACHE_LOCK = threading.Lock()
_ENCODER_LIST_CACHE: Dict[str, str] = {}
_TESTED_ENCODERS: Dict[tuple, bool] = {}
_GPU_VENDORS_CACHE: Optional[List[str]] = None
_VAAPI_DEVICE_CACHE: Optional[str] = None


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _default_order() -> tuple:
    system = platform.system()
    if system == "Windows":
        return _WINDOWS_ORDER
    if system == "Darwin":
        return _MACOS_ORDER
    return _LINUX_ORDER


def ffmpeg_encoder_list(ffmpeg_path: str) -> str:
    with _CACHE_LOCK:
        if ffmpeg_path in _ENCODER_LIST_CACHE:
            return _ENCODER_LIST_CACHE[ffmpeg_path]
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        text = _decode(result.stdout) + "\n" + _decode(result.stderr)
    except Exception as exc:
        logger.debug("Unable to list ffmpeg encoders for %s: %s", ffmpeg_path, exc)
        text = ""
    with _CACHE_LOCK:
        _ENCODER_LIST_CACHE[ffmpeg_path] = text
    return text


def _run_probe(cmd: List[str], timeout: int = 15) -> str:
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return _decode(result.stdout)
    except Exception:
        return ""


def _windows_gpu_names() -> List[str]:
    for cmd in (
        ["wmic", "path", "win32_VideoController", "get", "name"],
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
        ],
    ):
        text = _run_probe(cmd)
        if text.strip():
            return [line.strip() for line in text.splitlines() if line.strip()]
    return []


def _linux_gpu_names() -> List[str]:
    names = []
    text = _run_probe(["lspci"])
    for line in text.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ("vga", "3d controller", "display controller")):
            names.append(line.split(":", 2)[-1].strip())
    return names


def _macos_gpu_names() -> List[str]:
    text = _run_probe(["system_profiler", "SPDisplaysDataType"], timeout=20)
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("chipset model:"):
            names.append(stripped.split(":", 1)[1].strip())
    return names


def gpu_names() -> List[str]:
    system = platform.system()
    if system == "Windows":
        return _windows_gpu_names()
    if system == "Darwin":
        return _macos_gpu_names()
    return _linux_gpu_names()


def vaapi_device() -> Optional[str]:
    """Return the first usable VAAPI render node on Linux."""
    global _VAAPI_DEVICE_CACHE
    if platform.system() != "Linux":
        return None
    with _CACHE_LOCK:
        if _VAAPI_DEVICE_CACHE is not None:
            return _VAAPI_DEVICE_CACHE or None
    devices = sorted(glob.glob("/dev/dri/renderD*"))
    device = devices[0] if devices else None
    with _CACHE_LOCK:
        _VAAPI_DEVICE_CACHE = device or ""
    return device


def detect_gpu_vendors() -> List[str]:
    """Return hardware families that are plausibly present on this host."""
    global _GPU_VENDORS_CACHE
    with _CACHE_LOCK:
        if _GPU_VENDORS_CACHE is not None:
            return list(_GPU_VENDORS_CACHE)

    system = platform.system()
    names = gpu_names()
    joined = " ".join(names).lower()
    vendors: List[str] = []

    if "nvidia" in joined:
        vendors.append("nvenc")
    if system == "Windows":
        if "intel" in joined:
            vendors.append("qsv")
        if "amd" in joined or "radeon" in joined:
            vendors.append("amf")
    elif system == "Darwin":
        # VideoToolbox is available on all supported Macs.
        vendors.append("videotoolbox")
    else:
        # VAAPI covers Intel/AMD (and some NVIDIA setups) on Linux.
        if vaapi_device():
            vendors.append("vaapi")
        elif "intel" in joined or "amd" in joined or "radeon" in joined:
            vendors.append("vaapi")

    with _CACHE_LOCK:
        _GPU_VENDORS_CACHE = list(vendors)
    if names:
        logger.info("Detected GPU(s): %s", ", ".join(names))
    elif vendors:
        logger.info("Detected hardware acceleration families: %s", ", ".join(vendors))
    return vendors


def _encoder_available(ffmpeg_path: str, encoder: str) -> bool:
    return re.search(r"\b%s\b" % re.escape(encoder), ffmpeg_encoder_list(ffmpeg_path)) is not None


def _test_input_args(encoder: str) -> List[str]:
    family = _ENCODER_TO_FAMILY.get(encoder)
    if family == "vaapi":
        device = vaapi_device()
        if not device:
            return []
        return ["-vaapi_device", device]
    return []


def _test_output_args(encoder: str) -> List[str]:
    if _ENCODER_TO_FAMILY.get(encoder) == "vaapi":
        return ["-vf", "format=nv12,hwupload"]
    return []


def _encoder_works(ffmpeg_path: str, encoder: str) -> bool:
    key = (ffmpeg_path, encoder)
    with _CACHE_LOCK:
        if key in _TESTED_ENCODERS:
            return _TESTED_ENCODERS[key]

    extra = _test_input_args(encoder)
    if _ENCODER_TO_FAMILY.get(encoder) == "vaapi" and not extra:
        works = False
    else:
        try:
            result = subprocess.run(
                [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel", "error",
                ] + extra + [
                    "-f", "lavfi",
                    "-i", "color=c=black:s=640x360:r=1",
                ] + _test_output_args(encoder)
                + ([] if _ENCODER_TO_FAMILY.get(encoder) == "vaapi" else ["-pix_fmt", "yuv420p"])
                + [
                    "-frames:v", "1",
                    "-c:v", encoder,
                    "-b:v", "1000k",
                    "-f", "null", "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            works = result.returncode == 0
            if not works:
                logger.info(
                    "Hardware encoder %s is present but failed its test encode; falling back. %s",
                    encoder,
                    _decode(result.stderr).strip()[:300],
                )
        except Exception as exc:
            logger.info("Hardware encoder %s test failed: %s", encoder, exc)
            works = False

    with _CACHE_LOCK:
        _TESTED_ENCODERS[key] = works
    return works


def select_hw_encoder(ffmpeg_path: str, preference: str = "auto") -> Optional[str]:
    """Return a working ffmpeg H.264 hardware encoder name or None.

    ``preference`` may be ``auto``/``off`` or one of ``nvenc``/``qsv``/``amf``/
    ``vaapi``/``videotoolbox`` (or a raw ffmpeg encoder name).
    """
    pref = (preference or "auto").strip().lower()
    if pref in ("off", "none", "false", "0", "software", "cpu"):
        return None
    if pref in HW_ENCODERS:
        candidates = [HW_ENCODERS[pref]]
    elif pref in _ENCODER_TO_FAMILY:
        candidates = [pref]
    else:
        if pref not in ("auto", ""):
            logger.warning("Unknown hardware acceleration setting %r; using auto detection.", pref)
        vendors = detect_gpu_vendors()
        ordered = [family for family in vendors if family in HW_ENCODERS]
        ordered.extend(family for family in _default_order() if family not in ordered)
        candidates = [HW_ENCODERS[family] for family in ordered]

    for encoder in dict.fromkeys(candidates):
        if not _encoder_available(ffmpeg_path, encoder):
            logger.debug("ffmpeg build does not include %s", encoder)
            continue
        if _encoder_works(ffmpeg_path, encoder):
            logger.info("Using hardware H.264 encoder: %s", encoder)
            return encoder
    return None


def hw_decode_args(hw_encoder: Optional[str], preference: str = "auto") -> List[str]:
    """Return safe ffmpeg input options for hardware decoding.

    Hardware decoding is optional.  By default only the ``auto``/``on``
    preferences enable it so a broken driver cannot stop playback.  Frames are
    downloaded back to system memory (no ``-hwaccel_output_format``) so normal
    software scaling and the MPEG-TS muxer keep working.
    """
    pref = (preference or "off").strip().lower()
    if pref in ("off", "none", "false", "0", "software", "cpu"):
        return []
    if not hw_encoder:
        return []
    family = _ENCODER_TO_FAMILY.get(hw_encoder)
    if family == "nvenc":
        return ["-hwaccel", "cuda"]
    if family == "qsv":
        return ["-hwaccel", "qsv"]
    if family == "amf":
        return ["-hwaccel", "d3d11va"]
    if family == "vaapi":
        device = vaapi_device()
        return ["-hwaccel", "vaapi", "-hwaccel_device", device] if device else []
    if family == "videotoolbox":
        return ["-hwaccel", "videotoolbox"]
    return []


def hw_encoder_args(
    encoder: str,
    bitrate: str,
    frame_size: str,
    bufsize: str,
    profile: str = "high",
    level: str = "4.0",
) -> List[str]:
    """Build WMC-friendly CBR H.264 args for a hardware encoder."""
    family = _ENCODER_TO_FAMILY.get(encoder)
    if family == "vaapi":
        device = vaapi_device()
        if not device:
            return []
        return [
            "-vaapi_device", device,
            "-c:v", encoder,
            "-vf", "format=nv12,hwupload",
            "-r", "30000/1001",
            "-s", frame_size,
            "-aspect", "16:9",
            "-b:v", bitrate,
            "-maxrate:v", bitrate,
            "-bufsize:v", bufsize,
            "-g", "15",
            "-bf", "0",
            "-profile:v", profile,
            "-level:v", level,
        ]

    common = [
        "-c:v", encoder,
        "-pix_fmt", "yuv420p",
        "-r", "30000/1001",
        "-s", frame_size,
        "-aspect", "16:9",
        "-b:v", bitrate,
        "-maxrate:v", bitrate,
        "-bufsize:v", bufsize,
        "-g", "15",
        "-bf", "0",
        "-profile:v", profile,
        "-level:v", level,
    ]
    if family == "nvenc":
        common.extend([
            "-preset", "p1",
            "-tune", "ll",
            "-rc", "cbr",
            "-zerolatency", "1",
            "-forced-idr", "1",
        ])
    elif family == "qsv":
        common.extend([
            "-preset", "veryfast",
            "-look_ahead", "0",
        ])
    elif family == "amf":
        common.extend([
            "-usage", "lowlatency",
            "-quality", "speed",
        ])
    elif family == "videotoolbox":
        common.extend([
            "-realtime", "1",
            "-allow_sw", "1",
        ])
    return common
