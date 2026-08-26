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
import ctypes
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import threading
import time

from hdhr_proxy.config import Config
from hdhr_proxy.m3u_parser import M3UParser, build_lineup
from hdhr_proxy.discovery import DiscoveryServer, normalize_device_id
from hdhr_proxy.http_server import HDHRHTTPServer
from hdhr_proxy.guide_match import (
    GUIDE_MATCH_CSV_PATH,
    GUIDE_ONLY_INI_PATH,
    build_guide_match_rows,
    filter_lineup_to_matched_channels,
    write_guide_match_csv,
    write_guide_only_mapping_file,
    write_wmc_auto_match_mxf,
)
from hdhr_proxy.mxf import (
    write_mxf,
    import_mxf,
    import_and_map_mxf_with_epg123,
    run_wmc_post_import_tasks,
)
from hdhr_proxy.xmltv import load_xmltv
from hdhr_proxy.vista_guide import (
    import_vista_guide_xml,
    write_vista_guide_xml,
)

logger = logging.getLogger("main")
_INSTANCE_GUARDS = []


def acquire_single_instance_guard(cfg: Config):
    if platform.system() != "Windows":
        return

    mutex_name = "Local\\HDHRProxyWMC-IPTV-port-%s" % int(cfg.http_port)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, True, mutex_name)
    if not handle:
        logger.warning("Could not create single-instance guard for %s.", mutex_name)
        return
    if ctypes.get_last_error() == 183:
        logger.error(
            "Another HDHR proxy is already running on HTTP port %s. "
            "Stop the existing proxy before starting another scan instance.",
            cfg.http_port,
        )
        sys.exit(1)
    _INSTANCE_GUARDS.append(handle)


def configure_logging(log_dir: str = "."):
    root = logging.getLogger()
    if root.handlers:
        return
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "hdhr_proxy_main.log")
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass


def wmc_video_codec_for_current_os(force_vista: bool = False) -> str:
    if force_vista:
        return "mpeg2video"
    if platform.system() != "Windows":
        return "mpeg2video"

    version = sys.getwindowsversion()
    if (version.major, version.minor) <= (6, 0):
        return "mpeg2video"
    return "libx264"


def apply_wmc_video_codec_policy(cfg: Config):
    codec = wmc_video_codec_for_current_os(force_vista=bool(getattr(cfg, "force_vista_mode", False)))
    if cfg.ffmpeg_output_codec != codec:
        logger.info(
            "Using %s video for Windows Media Center on this OS (was configured as %s).",
            "H.264/MPEG-4 AVC" if codec == "libx264" else "MPEG-2",
            cfg.ffmpeg_output_codec,
        )
    if getattr(cfg, "force_vista_mode", False):
        logger.info("Vista compatibility override enabled: forcing Vista WMC MPEG-2 profile for testing.")
    cfg.ffmpeg_output_codec = codec


def import_mxf_for_current_wmc(output_path: str, vista_mode: bool = False, vista_xml_path: str = None) -> bool:
    if vista_mode:
        try:
            import_mxf(output_path, vista_tv_pack=True)
            logger.info("Imported MXF through the installed Vista TV Pack loadmxf.exe.")
            return True
        except FileNotFoundError:
            logger.info(
                "Vista TV Pack loadmxf.exe was not found. Trying the legacy "
                "pre-TV-Pack Vista ehepg guide loader."
            )
        except (RuntimeError, PermissionError, subprocess.CalledProcessError) as exc:
            logger.warning(
                "Vista TV Pack MXF import failed, but the proxy will continue "
                "running for scanning and playback. %s",
                exc,
            )
            return False
        if not vista_xml_path:
            logger.warning(
                "Skipping legacy pre-TV-Pack Vista guide import. Generate the "
                "Vista legacy guide XML from XMLTV by running the proxy with "
                "an XMLTV source and --import-mxf or --import-auto-match-mxf."
            )
            return False
        try:
            import_vista_guide_xml(vista_xml_path)
            return True
        except (OSError, subprocess.CalledProcessError) as exc:
            logger.warning(
                "Vista ehepg guide import failed, but the proxy will continue "
                "running for scanning and playback. Check vista_guide_import.log. %s",
                exc,
            )
            return False
    import_mxf(output_path)
    return True


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
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def resolve_listen_ip(cfg: Config) -> str:
    ip = cfg.listen_ip
    if ip == "0.0.0.0":
        return find_local_ip()
    return ip


def run_proxy(cfg: Config):
    acquire_single_instance_guard(cfg)
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
            m3u_path = os.path.abspath(m3u_path)
        channels = M3UParser.parse_file(m3u_path, hls_base_url=cfg.hls_base_url)
    else:
        logger.error("No M3U source configured. Use --m3u-file, --m3u-url, or config.")
        sys.exit(1)

    lineup, channel_map = build_lineup(
        channels,
        base_url=base_url,
        channel_mapping=cfg.channel_mapping,
        tuner_count=cfg.tuner_count,
        max_physical_channel=cfg.get("max_physical_channel", 59),
        programs_per_physical=cfg.get("programs_per_physical"),
    )
    xmltv_data = load_xmltv(cfg.xmltv_file, cfg.xmltv_url, channel_map)
    generated_mxf_path = None
    auto_match_mxf_path = None
    vista_guide_xml_path = None
    if xmltv_data:
        vista_mode = bool(getattr(cfg, "force_vista_mode", False))
        use_epg123_map = bool(getattr(cfg, "map_guide_wmc", False)) and not vista_mode
        if vista_mode and getattr(cfg, "map_guide_wmc", False):
            logger.warning(
                "Vista mode maps guide data through TV Pack loadmxf.exe when "
                "available, with the legacy ehepg XML loader as a pre-TV-Pack fallback."
            )
        logger.info("Loaded XMLTV guide from %s", xmltv_data.source)
        if cfg.write_mxf or cfg.import_mxf:
            mxf_path = write_mxf(
                xmltv_data.filtered_xml,
                lineup,
                channel_map,
                cfg.mxf_file,
                vista_mode=vista_mode,
            )
            generated_mxf_path = mxf_path
            if cfg.import_mxf:
                if vista_mode:
                    vista_guide_xml_path = write_vista_guide_xml(
                        xmltv_data.filtered_xml,
                        lineup,
                        channel_map,
                        os.path.splitext(cfg.mxf_file)[0] + ".vista.xml",
                    )
                import_mxf_for_current_wmc(
                    mxf_path,
                    vista_mode=vista_mode,
                    vista_xml_path=vista_guide_xml_path,
                )
        elif cfg.mxf_file and os.path.exists(cfg.mxf_file):
            generated_mxf_path = os.path.abspath(cfg.mxf_file)
        if cfg.write_auto_match_mxf or cfg.import_auto_match_mxf or xmltv_data:
            auto_match_mxf_path, auto_match_count = write_wmc_auto_match_mxf(
                lineup,
                channel_map,
                xmltv_data.filtered_xml,
                output_path=cfg.auto_match_mxf_file,
                vista_mode=vista_mode,
                epg123_mode=use_epg123_map,
            )
            logger.info("Wrote WMC auto-match MXF (%s matched channels): %s", auto_match_count, auto_match_mxf_path)
            if cfg.import_auto_match_mxf:
                if use_epg123_map:
                    logger.info("Importing lineup-matched guide into the WMC internal database via EPG123 auto-match when available")
                    try:
                        import_and_map_mxf_with_epg123(auto_match_mxf_path)
                    except FileNotFoundError:
                        logger.warning("EPG123 client not found; falling back to loadmxf.exe without automatic channel mapping.")
                        import_mxf(auto_match_mxf_path)
                    except Exception as exc:
                        logger.warning("EPG123 auto-match import failed; falling back to loadmxf.exe. %s", exc)
                        import_mxf(auto_match_mxf_path)
                    run_wmc_post_import_tasks()
                else:
                    if vista_mode and not vista_guide_xml_path:
                        vista_guide_xml_path = write_vista_guide_xml(
                            xmltv_data.filtered_xml,
                            lineup,
                            channel_map,
                            os.path.splitext(cfg.auto_match_mxf_file)[0] + ".vista.xml",
                        )
                    import_mxf_for_current_wmc(
                        auto_match_mxf_path,
                        vista_mode=vista_mode,
                        vista_xml_path=vista_guide_xml_path,
                    )
        guide_rows = build_guide_match_rows(
            lineup,
            channel_map,
            xmltv_data.filtered_xml,
            mxf_path=generated_mxf_path,
        )
        guide_match_csv = write_guide_match_csv(guide_rows, GUIDE_MATCH_CSV_PATH)
        guide_only_mapping = write_guide_only_mapping_file(lineup, guide_rows, GUIDE_ONLY_INI_PATH)
        match_count = len(guide_rows)
        logger.info("Wrote WMC guide match utility (%s matched channels): %s", match_count, guide_match_csv)
        logger.info("Wrote WMC guide-only mapping list: %s", guide_only_mapping)
        if cfg.guide_only_lineup:
            lineup, channel_map = filter_lineup_to_matched_channels(lineup, channel_map, guide_rows)
            logger.info("Guide-only lineup mode enabled: advertising %s matched channels to WMC scan", len(lineup))
    elif cfg.import_mxf and cfg.mxf_file and os.path.exists(cfg.mxf_file):
        import_mxf(cfg.mxf_file)
    elif cfg.write_mxf or cfg.import_mxf:
        logger.error("MXF generation requires --xmltv-file or --xmltv-url. To import an existing file, use --import-mxf path\\to\\guide.mxf.")
        sys.exit(1)
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
        xmltv_data=xmltv_data,
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
        force_vista_mode=bool(getattr(cfg, "force_vista_mode", False)),
    )
    discovery.start()

    if not discovery.wait_for_critical_listeners(timeout=2.0):
        logger.critical(
            "HDHR discovery/control listener failed to bind. "
            "Run repair_hdhr_port.ps1 as Administrator, then restart the proxy."
        )
        stop_event.set()
        http_server.stop()
        return

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
    log_dir = os.path.dirname(os.path.abspath(sys.argv[0])) or "."
    configure_logging(log_dir)
    logger.info("App log path: %s", os.path.join(log_dir, "hdhr_proxy_main.log"))
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
    parser.add_argument("--xmltv-file", help="Path to local XMLTV guide file")
    parser.add_argument("--xmltv-url", help="URL to remote XMLTV guide file")
    parser.add_argument("--mxf-file", default="guide.mxf", help="Output Windows Media Center MXF guide path")
    parser.add_argument("--auto-match-mxf-file", default="HDHRProxyWMC_AutoMatch.generated.mxf", help="Output WMC auto-match MXF path")
    parser.add_argument("--write-mxf", action="store_true", help="Generate a Windows Media Center MXF guide file")
    parser.add_argument(
        "--import-mxf",
        nargs="?",
        const=True,
        metavar="MXF_FILE",
        help="Generate and import the MXF guide, or import the given existing MXF file",
    )
    parser.add_argument("--write-auto-match-mxf", action="store_true", help="Generate a WMC auto-match MXF mapped to the current lineup")
    parser.add_argument("--import-auto-match-mxf", action="store_true", help="Generate and import a WMC auto-match MXF mapped to the current lineup")
    parser.add_argument("--map-guide-wmc", action="store_true", help="Generate and import a lineup-matched guide directly into the WMC internal database")
    parser.add_argument("--guide-only-lineup", action="store_true", help="Only advertise channels that matched XMLTV/MXF guide data")
    parser.add_argument(
        "--hls-base-url",
        help="Original web URL for a saved HLS master playlist that uses relative variant/segment URLs",
    )

    # Device settings
    parser.add_argument("--device-id", default="104FFFFF", help="8-char hex Device ID")
    parser.add_argument("--device-name", default="Virtual HDHR Proxy", help="Friendly name")
    parser.add_argument("--model", default="HDHR4-2US", help="Model number")
    parser.add_argument("--tuners", type=int, default=4, help="Number of virtual tuners")

    # Network
    parser.add_argument("--port", type=int, default=5004, help="HTTP server port")
    parser.add_argument("--listen-ip", default="0.0.0.0", help="Bind address")

    # Transcoding
    parser.add_argument("--ffmpeg", default=None, help="Path to ffmpeg (empty=disable)")
    parser.add_argument("--no-ffmpeg", action="store_true", help="Disable ffmpeg transcoding")
    parser.add_argument("--output-codec", default="mpeg2video", help="ffmpeg output video codec")
    parser.add_argument("--audio-codec", default="ac3", help="ffmpeg output audio codec")
    parser.add_argument("--bitrate", default="4000k", help="ffmpeg output bitrate")
    parser.add_argument("--vista", action="store_true", help="Force Vista-specific WMC codec behavior for client testing")
    parser.add_argument("--vista-subchannels", type=int, help="Vista scan test mode: fixed subchannels per RF, for example 20")

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
    cfg.config_file = args.config

    # Override with CLI args
    if args.m3u_file:
        cfg.m3u_file = os.path.abspath(os.path.expandvars(os.path.expanduser(args.m3u_file)))
        cfg.m3u_url = None
    if args.m3u_url:
        cfg.m3u_url = args.m3u_url
        cfg.m3u_file = None
    if args.hls_base_url:
        cfg.hls_base_url = args.hls_base_url
    if args.xmltv_file:
        cfg.xmltv_file = args.xmltv_file
    if args.xmltv_url:
        cfg.xmltv_url = args.xmltv_url
    if args.mxf_file:
        cfg.mxf_file = args.mxf_file
    if args.auto_match_mxf_file:
        cfg.auto_match_mxf_file = args.auto_match_mxf_file
    if args.write_mxf:
        cfg.write_mxf = True
    if args.import_mxf:
        cfg.import_mxf = True
        if isinstance(args.import_mxf, str):
            cfg.mxf_file = args.import_mxf
    if args.write_auto_match_mxf:
        cfg.write_auto_match_mxf = True
    if args.import_auto_match_mxf:
        cfg.import_auto_match_mxf = True
    if args.map_guide_wmc:
        cfg.map_guide_wmc = True
        cfg.import_auto_match_mxf = True
    if args.guide_only_lineup:
        cfg.guide_only_lineup = True
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
    if args.vista:
        cfg.force_vista_mode = True
    if args.vista_subchannels:
        cfg.programs_per_physical = args.vista_subchannels

    # Windows service commands
    if args.command:
        _handle_service_command(args.command, cfg)
        return

    if cfg.import_mxf and isinstance(args.import_mxf, str) and not cfg.m3u_file and not cfg.m3u_url and not cfg.xmltv_file and not cfg.xmltv_url:
        import_mxf_for_current_wmc(cfg.mxf_file, vista_mode=bool(getattr(cfg, "force_vista_mode", False)))
        return

    run_proxy(cfg)


SERVICE_NAME = "VirtualHDHRProxy"
SERVICE_DISPLAY_NAME = "Virtual HDHomerun Proxy"


def _service_bin_path(cfg: Config) -> str:
    parts = ['"{}"'.format(sys.executable), '"{}"'.format(os.path.abspath(__file__))]
    if getattr(cfg, "m3u_file", None):
        parts.append('--m3u-file "{}"'.format(cfg.m3u_file))
    elif getattr(cfg, "m3u_url", None):
        parts.append('--m3u-url "{}"'.format(cfg.m3u_url))
    if getattr(cfg, "config_file", None):
        parts.append('--config "{}"'.format(cfg.config_file))
    return " ".join(parts)


def _run_sc(*sc_args: str) -> int:
    completed = subprocess.run(
        ["sc.exe"] + list(sc_args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    if completed.returncode != 0:
        logger.warning("sc.exe %s failed: %s", " ".join(sc_args), (completed.stdout or "").strip())
    return completed.returncode


def _handle_service_command(cmd: str, cfg: Config):
    if platform.system() != "Windows":
        logger.error("Windows service commands are only available on Windows.")
        sys.exit(1)

    if cmd == "install":
        logger.info("Installing Windows service '%s'...", SERVICE_NAME)
        _run_sc(
            "create", SERVICE_NAME,
            "binPath=", _service_bin_path(cfg),
            "start=", "auto",
            "DisplayName=", SERVICE_DISPLAY_NAME,
        )
        _run_sc(
            "description", SERVICE_NAME,
            "Virtual HDHomeRun proxy for IPTV and Windows Media Center.",
        )
        # A console process does not perform the SCM handshake, so the
        # service control manager reports it as unresponsive even while the
        # proxy itself is running. Point users at a proper wrapper if needed.
        logger.warning(
            "The virtual tuner runs as a console process; SCM may report error "
            "1053 on start while the proxy still works. Use a wrapper such as "
            "NSSM if you need full service lifecycle support."
        )
    elif cmd == "remove":
        logger.info("Removing Windows service '%s'...", SERVICE_NAME)
        _run_sc("stop", SERVICE_NAME)
        _run_sc("delete", SERVICE_NAME)
    elif cmd == "start":
        logger.info("Starting Windows service '%s'...", SERVICE_NAME)
        _run_sc("start", SERVICE_NAME)
    elif cmd == "stop":
        logger.info("Stopping Windows service '%s'...", SERVICE_NAME)
        _run_sc("stop", SERVICE_NAME)


if __name__ == "__main__":
    main()
