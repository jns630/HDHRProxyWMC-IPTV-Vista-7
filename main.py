#!/usr/bin/env python3
"""
Virtual HDHomerun Proxy - Main Entry Point
Runs as a console application or Windows Service.

Usage:
    python main.py --config config.json
    python main.py --m3u-file playlists/channels.m3u --port 5004
    python main.py install           # Install as Windows service
    python main.py remove            # Remove Windows service
    python main.py start             # Start service
    python main.py stop              # Stop service
"""
import argparse
import json
import logging
import os
import platform
import signal
import sys
import threading
import time

from hdhr_proxy.config import Config
from hdhr_proxy.m3u_parser import M3UParser, build_lineup
from hdhr_proxy.discovery import DiscoveryServer, normalize_device_id
from hdhr_proxy.http_server import HDHRHTTPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def wmc_video_codec_for_current_os() -> str:
    if platform.system() != "Windows":
        return "mpeg2video"

    version = sys.getwindowsversion()
    if (version.major, version.minor) <= (6, 0):
        return "mpeg2video"
    return "libx264"


def apply_wmc_video_codec_policy(cfg: Config):
    codec = wmc_video_codec_for_current_os()
    if cfg.ffmpeg_output_codec != codec:
        logger.info(
            "Using %s video for Windows Media Center on this OS (was configured as %s).",
            "H.264/MPEG-4 AVC" if codec == "libx264" else "MPEG-2",
            cfg.ffmpeg_output_codec,
        )
    cfg.ffmpeg_output_codec = codec


def configure_windows_hdhr_sources(cfg: Config):
    if platform.system() != "Windows":
        return

    try:
        import winreg
    except ImportError:
        return

    source_type = "Digital Antenna"
    source = "Digital Antenna"
    model = _registry_model_name(cfg.model_number)
    subkey = r"SOFTWARE\Silicondust\HDHomeRun\Tuners"
    views = [0]
    if hasattr(winreg, "KEY_WOW64_32KEY"):
        views = [winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY]

    wrote_hklm = False
    wrote_any = False
    errors = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in views:
            access = winreg.KEY_WRITE | view
            try:
                tuners_key = winreg.CreateKeyEx(root, subkey, 0, access)
                with tuners_key:
                    tuner_names = [cfg.device_id] + [
                        f"{cfg.device_id}-{tuner_idx}" for tuner_idx in range(cfg.tuner_count)
                    ]
                    for tuner_name in tuner_names:
                        tuner_key = winreg.CreateKeyEx(tuners_key, tuner_name, 0, access)
                        with tuner_key:
                            winreg.SetValueEx(tuner_key, "Model", 0, winreg.REG_SZ, model)
                            winreg.SetValueEx(tuner_key, "SourceType", 0, winreg.REG_SZ, source_type)
                            winreg.SetValueEx(tuner_key, "Source", 0, winreg.REG_SZ, source)
                            winreg.SetValueEx(tuner_key, "Application", 0, winreg.REG_SZ, "Windows Media Center")
                            winreg.SetValueEx(tuner_key, "BDAPIDFilter", 0, winreg.REG_SZ, "Enabled")
                            winreg.SetValueEx(tuner_key, "BDAVCTMode", 0, winreg.REG_SZ, "Normal")
                            winreg.SetValueEx(tuner_key, "ChannelMapping", 0, winreg.REG_SZ, "Native")
                            winreg.SetValueEx(tuner_key, "Channelmap", 0, winreg.REG_SZ, "us-bcast")
                            winreg.SetValueEx(tuner_key, "DeviceID", 0, winreg.REG_SZ, cfg.device_id)
                if root == winreg.HKEY_LOCAL_MACHINE:
                    wrote_hklm = True
                wrote_any = True
            except OSError as e:
                errors.append(str(e))

    if wrote_hklm:
        logger.info("Windows HDHomeRun HKLM tuner source defaults set to Digital Antenna.")
    elif wrote_any:
        reg_path = write_windows_hdhr_registry_file(cfg, model, source_type, source)
        logger.warning(
            "Windows HDHomeRun source defaults were written to HKCU only. "
            "HDHomeRun Setup reads HKLM, so import this file as Administrator: %s",
            reg_path,
        )
    else:
        reg_path = write_windows_hdhr_registry_file(cfg, model, source_type, source)
        logger.warning(
            "Could not write HDHomeRun tuner source registry defaults. "
            "Import this file as Administrator: %s. Last error: %s",
            reg_path,
            errors[-1] if errors else "unknown",
        )


def _registry_model_name(model_number: str) -> str:
    model = (model_number or "").lower()
    if "hdhr4" in model:
        return "hdhomerun4_atsc"
    if "hdhr3" in model:
        return "hdhomerun3_atsc"
    return "hdhomerun4_atsc"


def write_windows_hdhr_registry_file(cfg: Config, model: str, source_type: str, source: str) -> str:
    path = os.path.join(os.getcwd(), f"hdhr_wmc_{cfg.device_id}.reg")
    tuner_names = [cfg.device_id] + [f"{cfg.device_id}-{i}" for i in range(cfg.tuner_count)]
    lines = ["Windows Registry Editor Version 5.00", ""]
    for view_prefix in (
        r"HKEY_LOCAL_MACHINE\SOFTWARE\Silicondust\HDHomeRun\Tuners",
        r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Silicondust\HDHomeRun\Tuners",
    ):
        for tuner_name in tuner_names:
            lines.extend([
                f"[{view_prefix}\\{tuner_name}]",
                f'"Model"="{model}"',
                f'"SourceType"="{source_type}"',
                f'"Source"="{source}"',
                '"Application"="Windows Media Center"',
                '"BDAPIDFilter"="Enabled"',
                '"BDAVCTMode"="Normal"',
                '"ChannelMapping"="Native"',
                '"Channelmap"="us-bcast"',
                f'"DeviceID"="{cfg.device_id}"',
                "",
            ])
    with open(path, "w", encoding="utf-16") as f:
        f.write("\r\n".join(lines))
    return path


def write_hdhrproxy_mapping_file(lineup, path: str = "HDHRProxyIPTV_MappingList.generated.ini") -> str:
    lines = [
        "###########################################",
        "# Generated from the active M3U lineup.",
        "# Compatible with the HDHRProxyIPTV mapping-list idea:",
        "# Channel + LowFreq/HighFreq + URL + Program_table.",
        "###########################################",
        "",
        "[MAPPING_LIST]",
        f"NUM_CHANNELS={len(lineup)}",
        "",
    ]

    for index, ch in enumerate(lineup, start=1):
        lines.extend([
            f"[CH{index}]",
            f"Channel={ch.get('PhysicalChannel', index + 1)}",
            f"LowFreq={ch.get('LowFreq', ch.get('Frequency', 57000000))}",
            f"HighFreq={ch.get('HighFreq', ch.get('Frequency', 57000000))}",
            "Protocol=HTTP",
            f"URLGet={ch.get('URL', '')}",
            "UDPsource=",
            "InternalPIDFiltering=N",
            "ExternalPIDFiltering=N",
            f"Signal_Strength={ch.get('SignalStrength', 95)}",
            f"Signal_Quality={ch.get('SignalQuality', 95)}",
            f"Symbol_Quality={ch.get('SymbolQuality', 100)}",
            f"Network_Rate={ch.get('NetworkRate', 19392658)}",
            f"Program_table={ch.get('ProgramTable', '')}",
            "",
        ])

    out_path = os.path.abspath(path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def find_local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def resolve_listen_ip(cfg: Config) -> str:
    ip = cfg.listen_ip
    if ip == "0.0.0.0":
        return find_local_ip()
    return ip


def run_proxy(cfg: Config):
    apply_wmc_video_codec_policy(cfg)

    normalized_device_id = normalize_device_id(cfg.device_id)
    if normalized_device_id != cfg.device_id.upper():
        logger.warning(
            "Configured Device ID %s is not a valid HDHomeRun ID; advertising %s instead.",
            cfg.device_id,
            normalized_device_id,
        )
        cfg.device_id = normalized_device_id
    cfg.lineup_source = "Antenna"
    configure_windows_hdhr_sources(cfg)

    logger.info("=== Virtual HDHomerun Proxy ===")
    logger.info(f"Device ID: {cfg.device_id}")
    logger.info(f"Model: {cfg.model_number}")
    logger.info(f"Tuners: {cfg.tuner_count}")

    # Resolve listen IP for discovery responses (actual interface IP, not 0.0.0.0)
    tuner_ip = resolve_listen_ip(cfg)
    base_url = f"http://{tuner_ip}:{cfg.http_port}"
    cfg.advertised_base_url = base_url
    logger.info(f"Base URL: {base_url}")

    # --- Load M3U ---
    channels = []
    if cfg.m3u_url:
        channels = M3UParser.parse_url(cfg.m3u_url)
    elif cfg.m3u_file:
        m3u_path = cfg.m3u_file
        if not os.path.isabs(m3u_path):
            m3u_path = os.path.join(os.path.dirname(__file__), m3u_path)
        channels = M3UParser.parse_file(m3u_path, hls_base_url=cfg.hls_base_url)
    else:
        logger.error("No M3U source configured. Use --m3u-file, --m3u-url, or config.")
        sys.exit(1)

    lineup, channel_map = build_lineup(
        channels,
        base_url=base_url,
        channel_mapping=cfg.channel_mapping,
        tuner_count=cfg.tuner_count,
    )
    logger.info(f"Lineup has {len(lineup)} channels")
    mapping_path = write_hdhrproxy_mapping_file(lineup)
    logger.info(f"Wrote HDHRProxyIPTV-style mapping list: {mapping_path}")
    for ch in lineup[:10]:
        logger.debug(f"  {ch['GuideNumber']:>5} - {ch['GuideName']}")

    # --- Start HTTP Server ---
    http_server = HDHRHTTPServer(
        host=cfg.listen_ip,
        port=cfg.http_port,
        lineup=lineup,
        channel_map=channel_map,
        config=cfg,
    )
    http_server.start()

    # --- Start Discovery ---
    stop_event = threading.Event()

    def get_lineup():
        return lineup

    discovery = DiscoveryServer(
        device_id=cfg.device_id,
        base_url=base_url,
        tuner_count=cfg.tuner_count,
        listen_ip=cfg.listen_ip,
        device_name=cfg.device_name,
        model_number=cfg.model_number,
        firmware_version=cfg.firmware_version,
        stop_event=stop_event,
        get_lineup_callback=get_lineup,
        channel_map=channel_map,
        lineup=lineup,
        ffmpeg_path=cfg.ffmpeg_path,
        ffmpeg_enabled=cfg.ffmpeg_enabled,
        output_codec=cfg.ffmpeg_output_codec,
        audio_codec=cfg.ffmpeg_audio_codec,
        bitrate=cfg.ffmpeg_bitrate,
    )
    discovery.start()

    logger.info("Proxy running. Press Ctrl+C to stop.")

    # Wait for shutdown
    shutdown_event = threading.Event()

    def handle_signal(sig, frame):
        logger.info("Shutdown signal received...")
        stop_event.set()
        http_server.stop()
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        shutdown_event.wait()
    except KeyboardInterrupt:
        pass

    logger.info("Proxy stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Virtual HDHomerun Proxy Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --m3u-file playlists/channels.m3u --port 5004
  python main.py --m3u-url "http://provider.com/playlist.m3u8" --port 5004
  python main.py install     # Install as Windows service
  python main.py remove      # Remove Windows service
        """,
    )

    # Config source
    parser.add_argument("--config", help="Path to JSON config file")

    # M3U sources
    parser.add_argument("--m3u-file", help="Path to local M3U/M3U8 file")
    parser.add_argument("--m3u-url", help="URL to remote M3U/M3U8 playlist")
    parser.add_argument(
        "--hls-base-url",
        help="Original web URL for a saved HLS master playlist that uses relative variant/segment URLs",
    )

    # Device settings
    parser.add_argument("--device-id", default="104FFFFF", help="8-char hex Device ID")
    parser.add_argument("--device-name", default="Virtual HDHR Proxy", help="Friendly name")
    parser.add_argument("--model", default="HDHR4-2US", help="Model number")
    parser.add_argument("--tuners", type=int, default=2, help="Number of virtual tuners")

    # Network
    parser.add_argument("--port", type=int, default=5004, help="HTTP server port")
    parser.add_argument("--listen-ip", default="0.0.0.0", help="Bind address")

    # Transcoding
    parser.add_argument("--ffmpeg", default=None, help="Path to ffmpeg (empty=disable)")
    parser.add_argument("--no-ffmpeg", action="store_true", help="Disable ffmpeg transcoding")
    parser.add_argument("--output-codec", default="mpeg2video", help="ffmpeg output video codec")
    parser.add_argument("--audio-codec", default="ac3", help="ffmpeg output audio codec")
    parser.add_argument("--bitrate", default="4000k", help="ffmpeg output bitrate")

    # Windows service commands
    parser.add_argument(
        "command",
        nargs="?",
        choices=["install", "remove", "start", "stop"],
        help="Windows service management commands",
    )

    args = parser.parse_args()

    # Build config
    cfg = Config(args.config)

    # Override with CLI args
    if args.m3u_file:
        cfg.m3u_file = args.m3u_file
    if args.m3u_url:
        cfg.m3u_url = args.m3u_url
    if args.hls_base_url:
        cfg.hls_base_url = args.hls_base_url
    if args.port:
        cfg.http_port = args.port
    if args.listen_ip:
        cfg.listen_ip = args.listen_ip
    if args.device_id:
        cfg.device_id = args.device_id
    if args.device_name:
        cfg.device_name = args.device_name
    if args.model:
        cfg.model_number = args.model
    if args.tuners:
        cfg.tuner_count = args.tuners

    if args.ffmpeg is not None:
        cfg.ffmpeg_path = args.ffmpeg
    if args.no_ffmpeg:
        cfg.ffmpeg_enabled = False
    if args.output_codec:
        cfg.ffmpeg_output_codec = args.output_codec
    if args.audio_codec:
        cfg.ffmpeg_audio_codec = args.audio_codec
    if args.bitrate:
        cfg.ffmpeg_bitrate = args.bitrate

    # Windows service commands
    if args.command:
        _handle_service_command(args.command, cfg)
        return

    run_proxy(cfg)


def _handle_service_command(cmd: str, cfg: Config):
    try:
        import win32serviceutil
        import win32service
        import servicemanager
    except ImportError:
        logger.error(
            "pywin32 is required for Windows service support. "
            "Install it: pip install pywin32"
        )
        sys.exit(1)

    service_name = "VirtualHDHRProxy"
    service_display_name = "Virtual HDHomerun Proxy"

    if cmd == "install":
        logger.info(f"Installing Windows service '{service_name}'...")
        # Build the python command with current config
        python_exe = sys.executable
        script_path = os.path.abspath(__file__)
        if cfg.m3u_file:
            cfg_arg = f'--m3u-file "{cfg.m3u_file}"'
        elif cfg.m3u_url:
            cfg_arg = f'--m3u-url "{cfg.m3u_url}"'
        else:
            cfg_arg = f'--config "{cfg.as_dict}"'
            logger.warning("No M3U source configured; service may not start correctly.")

        cmd_line = f'{python_exe} "{script_path}"'

        # Use win32serviceutil to install
        import subprocess
        subprocess.run(
            [
                python_exe, "-m", "win32serviceutil", "InstallService",
                python_exe, script_path,
                service_name, service_display_name,
            ],
            check=False,
        )
        logger.info(f"Service '{service_name}' installed.")

    elif cmd == "remove":
        logger.info(f"Removing Windows service '{service_name}'...")
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "win32serviceutil", "RemoveService", service_name],
            check=False,
        )

    elif cmd == "start":
        logger.info(f"Starting Windows service '{service_name}'...")
        win32serviceutil.StartService(service_name)

    elif cmd == "stop":
        logger.info(f"Stopping Windows service '{service_name}'...")
        win32serviceutil.StopService(service_name)


if __name__ == "__main__":
    main()
