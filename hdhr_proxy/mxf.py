import hashlib
import logging
import os
import subprocess
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .m3u_parser import M3UChannel
from .xmltv import resolve_channel_xmltv_ids

logger = logging.getLogger(__name__)

MXF_NS = "urn:com:dontocsata:xmltv:mxf"
ET.register_namespace("", MXF_NS)
LINEUP_KEY = "HDHRProxy"
VISTA_LINEUP_KEY = "HDHRProxy-Vista"
MCEPG_PUBLIC_KEY = "0024000004800000940000000602000000240000525341310004000001000100B5FC90E7027F67871E773A8FDE8938C81DD402BA65B9201D60593E96C492651E889CC13F1415EBB53FAC1131AE0BD333C5EE6021672D9718EA31A8AEBD0DA0072F25D87DBA6FC90FFD598ED4DA35E44C398C454307E8E33B8426143DAEC9F596836F97C8F74750E5975C64E2189F45DEF46B2A2B1247ADC3652BF5C308055DA9"
_EPG123_STATION_ID_CACHE: Optional[Dict[str, str]] = None
_EPG123_INCLUDED_LINEUPS_CACHE: Optional[List[str]] = None

ROVI_STYLE_KEYWORDS = [
    ("k1", "Educational"),
    ("k1000", "All"),
    ("k2", "Kids"),
    ("k2000", "All"),
    ("k3", "Movies"),
    ("k3000", "All"),
    ("k3001", "Romantic Comedy"),
    ("k3002", "Comedy"),
    ("k3003", "Action"),
    ("k3004", "Adventure"),
    ("k3005", "Science Fiction"),
    ("k3006", "Mystery"),
    ("k3007", "Thriller"),
    ("k3008", "Fantasy"),
    ("k3009", "Drama"),
    ("k3010", "Documentary"),
    ("k3011", "Biography"),
    ("k3012", "Horror"),
    ("k3013", "Romance"),
    ("k4", "Music"),
    ("k4000", "All"),
    ("k5", "News"),
    ("k5000", "All"),
    ("k6", "Paid Programming"),
    ("k6000", "All"),
    ("k7", "Special"),
    ("k7000", "All"),
    ("k8", "Kids"),
    ("k8000", "All"),
    ("k9", "Series"),
    ("k9000", "All"),
    ("k9001", "Animated"),
    ("k9002", "Comedy"),
    ("k9003", "Sitcom"),
    ("k9004", "Talk"),
    ("k9005", "News"),
    ("k9006", "Mystery"),
    ("k9007", "Drama"),
    ("k9008", "Reality"),
    ("k9009", "Documentary"),
    ("k9010", "Science Fiction"),
    ("k9011", "Adventure"),
    ("k9012", "Action"),
    ("k9013", "Biography"),
    ("k9014", "Crime"),
    ("k9015", "Fantasy"),
    ("k9016", "Game Show"),
    ("k9017", "Horror"),
    ("k9018", "Romance"),
    ("k9019", "Thriller"),
    ("k10", "Sports"),
    ("k10000", "All"),
]

ROVI_STYLE_KEYWORD_GROUPS = [
    ("k1", "!KeywordGroup!k1-ovf", "k1000"),
    ("k1", "!KeywordGroup!k1-pri", "k1000"),
    ("k2", "!KeywordGroup!k2-ovf", "k2000"),
    ("k2", "!KeywordGroup!k2-pri", "k2000"),
    ("k3", "!KeywordGroup!k3-ovf", "k3000"),
    ("k3", "!KeywordGroup!k3-pri", "k3000,k3003,k3004,k3011,k3002,k3010,k3009,k3008,k3012,k3006,k3001,k3013,k3005,k3007"),
    ("k4", "!KeywordGroup!k4-ovf", "k4000"),
    ("k4", "!KeywordGroup!k4-pri", "k4000"),
    ("k5", "!KeywordGroup!k5-ovf", "k5000"),
    ("k5", "!KeywordGroup!k5-pri", "k5000"),
    ("k6", "!KeywordGroup!k6-ovf", "k6000"),
    ("k6", "!KeywordGroup!k6-pri", "k6000"),
    ("k7", "!KeywordGroup!k7-ovf", "k7000"),
    ("k7", "!KeywordGroup!k7-pri", "k7000"),
    ("k8", "!KeywordGroup!k8-ovf", "k8000"),
    ("k8", "!KeywordGroup!k8-pri", "k8000"),
    ("k9", "!KeywordGroup!k9-ovf", "k9000"),
    ("k9", "!KeywordGroup!k9-pri", "k9000,k9013,k9014,k9001,k9012,k9003,k9019,k9015,k9016,k9007,k9009,k9017,k9006,k9008,k9018,k9011,k9002,k9010"),
    ("k10", "!KeywordGroup!k10-ovf", "k10000"),
    ("k10", "!KeywordGroup!k10-pri", "k10000"),
]

MOVIE_GENRE_KEYWORDS = {
    "action": ("k3003", "isAction"),
    "adventure": ("k3004", None),
    "biography": ("k3011", None),
    "comedy": ("k3002", "isComedy"),
    "documentary": ("k3010", "isDocumentary"),
    "drama": ("k3009", "isDrama"),
    "fantasy": ("k3008", None),
    "horror": ("k3012", "isHorror"),
    "mystery": ("k3006", None),
    "romance": ("k3013", "isRomance"),
    "romantic comedy": ("k3001", "isComedy"),
    "sci-fi": ("k3005", "isScienceFiction"),
    "science fiction": ("k3005", "isScienceFiction"),
    "thriller": ("k3007", "isThriller"),
}

SERIES_GENRE_KEYWORDS = {
    "action": ("k9012", "isAction"),
    "adventure": ("k9011", None),
    "animated": ("k9001", None),
    "animation": ("k9001", None),
    "biography": ("k9013", None),
    "comedy": ("k9002", "isComedy"),
    "crime": ("k9014", None),
    "documentary": ("k9009", "isDocumentary"),
    "drama": ("k9007", "isDrama"),
    "fantasy": ("k9015", None),
    "game show": ("k9016", None),
    "horror": ("k9017", "isHorror"),
    "mystery": ("k9006", None),
    "reality": ("k9008", None),
    "romance": ("k9018", "isRomance"),
    "sci-fi": ("k9010", "isScienceFiction"),
    "science fiction": ("k9010", "isScienceFiction"),
    "sitcom": ("k9003", "isComedy"),
    "talk": ("k9004", None),
    "thriller": ("k9019", "isThriller"),
}


def ns(tag: str) -> str:
    return f"{{{MXF_NS}}}{tag}"


def write_mxf(
    xmltv_xml: str,
    lineup: List[Dict],
    channel_map: Dict[str, M3UChannel],
    output_path: str,
    vista_mode: bool = False,
    force_ota_match: bool = False,
    epg123_mode: bool = False,
) -> str:
    xmltv_ids_by_guide = resolve_channel_xmltv_ids(xmltv_xml, channel_map) if xmltv_xml else {}
    channel_meta = _build_channel_metadata(lineup, channel_map, xmltv_ids_by_guide)
    service_programmes = _collect_programmes(xmltv_xml, channel_meta)
    root = (
        _build_vista_mxf_root(
            channel_meta,
            service_programmes,
            force_ota_match=force_ota_match,
            epg123_mode=epg123_mode,
        )
        if vista_mode or epg123_mode else
        _build_mxf_root(channel_meta, service_programmes, force_ota_match=force_ota_match)
    )

    output_path = os.path.abspath(output_path)
    tree = ET.ElementTree(root)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    logger.info("Wrote %s MXF guide file: %s", "Vista-style" if vista_mode else "Windows 7+ style", output_path)
    return output_path


def import_mxf(output_path: str) -> None:
    loadmxf = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "ehome", "loadmxf.exe")
    if not os.path.exists(loadmxf):
        raise FileNotFoundError(f"loadmxf.exe not found at {loadmxf}")
    logger.info("Importing MXF into Windows Media Center: %s", output_path)
    subprocess.run([loadmxf, "-v", "-i", output_path], check=True)


def run_wmc_post_import_tasks() -> None:
    if os.name != "nt":
        return
    tasks = [
        r"\Microsoft\Windows\Media Center\ReindexSearchRoot",
    ]
    for task_name in tasks:
        try:
            logger.info("Running WMC post-import task: %s", task_name)
            subprocess.run(
                ["schtasks.exe", "/run", "/tn", task_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning("Unable to run WMC post-import task %s: %s", task_name, exc)


def import_and_map_mxf_with_epg123(output_path: str, verbose: bool = True) -> None:
    epg123_client = _find_epg123_client()
    if not epg123_client:
        raise FileNotFoundError("epg123Client.exe not found. Install EPG123 or use the loadmxf fallback.")

    cmd = [epg123_client, "-I", os.path.abspath(output_path), "-MATCH", "-NOGC"]
    if verbose:
        cmd.append("-VERBOSE")
    logger.info("Importing and auto-mapping guide with EPG123 client: %s", output_path)
    subprocess.run(cmd, check=True)


def _find_epg123_client() -> Optional[str]:
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "epg123", "epg123Client.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "epg123", "epg123Client.exe"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _build_channel_metadata(
    lineup: List[Dict],
    channel_map: Dict[str, M3UChannel],
    xmltv_ids_by_guide: Dict[str, str],
) -> Dict[str, Dict]:
    meta_by_service: Dict[str, Dict] = {}
    fallback_counter = 1
    epg123_station_ids = _load_epg123_station_ids()
    for item in lineup:
        guide_number = str(item.get("GuideNumber", "")).strip()
        if not guide_number:
            continue
        major, minor = _split_guide_number(guide_number)
        channel = channel_map.get(guide_number)
        tvg_id = (getattr(channel, "tvg_id", "") or "").strip()
        if not tvg_id:
            tvg_id = f"guide-{guide_number}"
        xmltv_id = xmltv_ids_by_guide.get(guide_number) or tvg_id
        station_id = _make_station_id(xmltv_id, guide_number, fallback_counter)
        service_id = f"s{fallback_counter}"
        fallback_counter += 1
        guide_name = str(item.get("GuideName") or getattr(channel, "name", guide_number))
        scanned_call_sign = _lineup_scanned_call_sign(item, guide_name)
        meta_by_service[service_id] = {
            "guide_number": guide_number,
            "major": major,
            "minor": minor,
            "service_id": service_id,
            "station_id": _lookup_station_id(epg123_station_ids, scanned_call_sign, station_id),
            "xmltv_id": xmltv_id,
            "source_tvg_id": tvg_id,
            "service_name": guide_name,
            "call_sign": scanned_call_sign,
            "category_hints": _channel_category_hints(channel, guide_name),
        }
    return meta_by_service


def _collect_programmes(xmltv_xml: str, channel_meta: Dict[str, Dict]) -> Dict[str, List[Dict]]:
    programmes_by_service: Dict[str, List[Dict]] = {meta["service_id"]: [] for meta in channel_meta.values()}
    if not xmltv_xml:
        return programmes_by_service

    try:
        root = ET.fromstring(xmltv_xml)
    except ET.ParseError as exc:
        logger.warning("Unable to parse XMLTV for MXF generation: %s", exc)
        return programmes_by_service

    meta_by_xmltv_id: Dict[str, List[Dict]] = {}
    for meta in channel_meta.values():
        meta_by_xmltv_id.setdefault(meta["xmltv_id"], []).append(meta)

    next_program_id = 1
    for programme in root.findall("programme"):
        channel_id = (programme.attrib.get("channel") or "").strip()
        metas = meta_by_xmltv_id.get(channel_id)
        if not metas:
            continue
        start_dt = _parse_xmltv_datetime(programme.attrib.get("start"))
        stop_dt = _parse_xmltv_datetime(programme.attrib.get("stop"))
        if not start_dt or not stop_dt or stop_dt <= start_dt:
            continue
        duration = int((stop_dt - start_dt).total_seconds())
        service_name = metas[0]["service_name"]
        title = _child_text(programme, "title") or service_name
        episode_title = _child_text(programme, "sub-title")
        description = _child_text(programme, "desc")
        season_num, episode_num = _extract_episode_numbers(programme)
        categories = _child_texts(programme, "category") + metas[0].get("category_hints", [])
        is_movie = _is_movie_program(categories) or _looks_like_movie_airing(title, service_name, duration)
        is_news = bool(not is_movie and _is_news_program(categories, title, service_name))
        is_series = bool(
            not is_movie and not is_news and (
                episode_title
                or season_num is not None
                or episode_num is not None
                or _looks_like_series_channel(categories, title, service_name)
            )
        )
        if is_series and not episode_title and title and title != service_name:
            episode_title = title
        elif is_series and not episode_title:
            episode_title = _episode_title_from_description(description)
        elif is_news and not episode_title and title and title != service_name:
            episode_title = title
        keywords, genre_flags = _program_keywords(categories, is_movie, is_series, is_news)
        icon_url = _child_attr(programme, "icon", "src")
        rating, rating_kind = _extract_rating(programme)
        airdate, year = _extract_airdate(programme)
        half_stars = _extract_half_stars(programme)
        if is_movie and not half_stars:
            half_stars = "6"
        elif is_series and not half_stars:
            half_stars = "6"
        base_program = {
            "id": str(next_program_id),
            "uid": "!Program!" + hashlib.md5(
                f"{channel_id}|{programme.attrib.get('start')}|{title}|{episode_title or ''}".encode("utf-8")
            ).hexdigest(),
            "title": title,
            "episode_title": episode_title,
            "description": description,
            "short_description": _short_description(description or episode_title or title),
            "season_num": season_num,
            "episode_num": episode_num,
            "is_series": is_series,
            "is_movie": is_movie,
            "is_news": is_news,
            "keywords": keywords,
            "genre_flags": genre_flags,
            "icon_url": icon_url,
            "guide_image": _guide_image_id(icon_url) if icon_url else None,
            "rating": rating,
            "rating_kind": rating_kind,
            "airdate": airdate,
            "year": year,
            "half_stars": half_stars,
            "start_time": _to_mxf_time(start_dt),
            "duration": str(duration),
        }
        for meta in metas:
            programmes_by_service[meta["service_id"]].append(dict(base_program))
        next_program_id += 1

    for items in programmes_by_service.values():
        items.sort(key=lambda p: p["start_time"])
    return programmes_by_service


def _build_mxf_root(
    channel_meta: Dict[str, Dict],
    service_programmes: Dict[str, List[Dict]],
    force_ota_match: bool = False,
) -> ET.Element:
    root = ET.Element(ns("MXF"))

    _append_assembly(root, "mcepg", "Microsoft.MediaCenter.Guide", [
        ("Lineup", None, None),
        ("Channel", "lineup", None),
        ("Service", None, None),
        ("ScheduleEntry", None, "ScheduleEntries"),
        ("Program", None, None),
        ("Keyword", None, None),
        ("KeywordGroup", None, None),
        ("Person", None, "People"),
        ("ActorRole", "program", None),
        ("DirectorRole", "program", None),
        ("WriterRole", "program", None),
        ("HostRole", "program", None),
        ("GuestActorRole", "program", None),
        ("ProducerRole", "program", None),
        ("GuideImage", None, None),
        ("Affiliate", None, None),
        ("SeriesInfo", None, None),
        ("Season", None, None),
    ])
    _append_assembly(root, "mcstore", "Microsoft.MediaCenter.Store", [
        ("Provider", None, None),
        ("UID", "target", None),
        ("UId", "target", None),
        ("Uid", "target", None),
        ("uid", "target", None),
    ])

    providers = ET.SubElement(root, ns("Providers"))
    ET.SubElement(providers, ns("Provider"), {
        "id": "provider1",
        "name": "HDHRProxyXMLTV",
        "displayName": "HDHRProxy XMLTV",
        "copyright": "Generated by HDHRProxy",
    })

    with_el = ET.SubElement(root, ns("With"), {"provider": "provider1"})
    _append_rovi_metadata(with_el, service_programmes, namespaced=True)
    ET.SubElement(with_el, ns("People"))
    ET.SubElement(with_el, ns("SeriesInfos"))
    ET.SubElement(with_el, ns("Seasons"))

    programs_el = ET.SubElement(with_el, ns("Programs"))
    for service_id in sorted(service_programmes):
        for program in service_programmes[service_id]:
            attrs = _program_mxf_attrs(program, vista_mode=False)
            ET.SubElement(programs_el, ns("Program"), attrs)

    ET.SubElement(with_el, ns("Affiliates"))

    services_el = ET.SubElement(with_el, ns("Services"))
    for meta in sorted(channel_meta.values(), key=lambda m: (m["major"], m["minor"])):
        ET.SubElement(services_el, ns("Service"), {
            "id": meta["service_id"],
            "uid": f"!Service!{meta['call_sign']}",
            "name": meta["service_name"],
            "callSign": meta["call_sign"],
        })

    for meta in sorted(channel_meta.values(), key=lambda m: (m["major"], m["minor"])):
        schedule_entries_el = ET.SubElement(with_el, ns("ScheduleEntries"), {
            "service": meta["service_id"],
        })
        for program in service_programmes.get(meta["service_id"], []):
            attrs = {
                "program": program["id"],
                "duration": program["duration"],
                "startTime": program["start_time"],
            }
            ET.SubElement(schedule_entries_el, ns("ScheduleEntry"), attrs)

    lineups_el = ET.SubElement(with_el, ns("Lineups"))
    lineup_el = ET.SubElement(lineups_el, ns("Lineup"), {
        "id": "l1",
        "uid": f"!Lineup!{LINEUP_KEY}",
        "name": LINEUP_KEY,
        "primaryProvider": "!MCLineup!MainLineup",
    })
    channels_el = ET.SubElement(lineup_el, ns("channels"))
    for meta in sorted(channel_meta.values(), key=lambda m: (m["major"], m["minor"])):
        ET.SubElement(channels_el, ns("Channel"), {
            "uid": f"!Channel!{LINEUP_KEY}!{meta['major']}_{meta['minor']}",
            "lineup": "l1",
            "service": meta["service_id"],
            "matchName": f"OC:{meta['major']}:{meta['minor']}" if force_ota_match else meta["call_sign"],
            "number": str(meta["major"]),
            "subNumber": str(meta["minor"]),
        })

    return root


def _build_vista_mxf_root(
    channel_meta: Dict[str, Dict],
    service_programmes: Dict[str, List[Dict]],
    force_ota_match: bool = False,
    epg123_mode: bool = False,
) -> ET.Element:
    root = ET.Element("MXF")

    _append_assembly_plain(root, "mcepg", "Microsoft.MediaCenter.Guide", [
        ("DeviceGroup", None, None),
        ("Lineup", None, None),
        ("Channel", "lineup", None),
        ("Service", None, None),
        ("ScheduleEntry", None, "ScheduleEntries"),
        ("Program", None, None),
        ("Keyword", None, None),
        ("KeywordGroup", None, None),
        ("Person", None, "People"),
        ("ActorRole", "program", None),
        ("DirectorRole", "program", None),
        ("WriterRole", "program", None),
        ("HostRole", "program", None),
        ("GuestActorRole", "program", None),
        ("ProducerRole", "program", None),
        ("GuideImage", None, None),
        ("Affiliate", None, None),
        ("SeriesInfo", None, None),
        ("Season", None, None),
    ])
    _append_assembly_plain(root, "mcstore", "Microsoft.MediaCenter.Store", [
        ("Provider", None, None),
        ("UId", "target", None),
    ])

    ET.SubElement(root, "DeviceGroup", {
        "uid": "!DeviceGroup!All",
        "name": "All",
        "lastConfigurationChange": _to_device_group_time(datetime.now(timezone.utc)),
        "rank": "0",
        "permitAnyDeviceType": "true",
        "isEnabled": "true",
        "firstRunProcessId": "0",
        "onlyShowDynamicLineups": "false",
    })

    providers = ET.SubElement(root, "Providers")
    ET.SubElement(providers, "Provider", {
        "id": "provider1",
        "name": "EPG123" if epg123_mode else "HDHRProxyXMLTV",
        "displayName": "Electronic Program Guide in 1-2-3" if epg123_mode else "HDHRProxy XMLTV Vista",
        "copyright": "Generated by HDHRProxy",
        "status": "0" if epg123_mode else "0",
    })

    with_el = ET.SubElement(root, "With", {"provider": "provider1"})
    _append_rovi_metadata(with_el, service_programmes, namespaced=False)
    ET.SubElement(with_el, "People")
    ET.SubElement(with_el, "SeriesInfos")
    ET.SubElement(with_el, "Seasons")

    programs_el = ET.SubElement(with_el, "Programs")
    for service_id in sorted(service_programmes):
        for program in service_programmes[service_id]:
            attrs = _program_mxf_attrs(program, vista_mode=True)
            ET.SubElement(programs_el, "Program", attrs)

    ET.SubElement(with_el, "Affiliates")

    services_el = ET.SubElement(with_el, "Services")
    if epg123_mode:
        ET.SubElement(services_el, "Service", {
            "id": "s0",
            "uid": "!Service!DUMMY",
            "name": "DUMMY Station",
            "callSign": "DUMMY",
            "logoImage": "",
        })
    for meta in sorted(channel_meta.values(), key=lambda m: (m["major"], m["minor"])):
        service_attrs = {
            "id": meta["service_id"],
            "uid": f"!Service!EPG123_{meta['station_id']}" if epg123_mode else f"!Service!HDHRProxy_{meta['call_sign']}",
            "name": meta["service_name"],
            "callSign": meta["call_sign"],
            "logoImage": "",
        }
        if not epg123_mode:
            service_attrs["stationId"] = meta["source_tvg_id"]
        ET.SubElement(services_el, "Service", service_attrs)

    for meta in sorted(channel_meta.values(), key=lambda m: (m["major"], m["minor"])):
        schedule_entries_el = ET.SubElement(with_el, "ScheduleEntries", {
            "service": meta["service_id"],
        })
        for program in service_programmes.get(meta["service_id"], []):
            attrs = {
                "program": program["id"],
                "duration": program["duration"],
                "startTime": program["start_time"].replace(".000Z", "Z"),
                "isCC": "true",
                "audioFormat": "2",
            }
            ET.SubElement(schedule_entries_el, "ScheduleEntry", attrs)

    lineups_el = ET.SubElement(with_el, "Lineups")
    if epg123_mode:
        lineup_specs = []
        ota_lineups = [value for value in _load_epg123_included_lineups() if value.upper().startswith("USA-OTA-")]
        if ota_lineups:
            for index, lineup_key in enumerate(ota_lineups, start=1):
                suffix = lineup_key.split("USA-OTA-", 1)[-1]
                lineup_specs.append((f"l{index}", lineup_key, f"EPG123 Local Broadcast Listings ({suffix})"))
        else:
            lineup_specs.append(("l1", "USA-OTA-HDHRPROXY", "EPG123 Local Broadcast Listings (HDHRProxy)"))
    else:
        lineup_specs = [("l1", VISTA_LINEUP_KEY, "HDHRProxy Local Broadcast Listings")]

    for lineup_id, lineup_key, lineup_name in lineup_specs:
        lineup_el = ET.SubElement(lineups_el, "Lineup", {
            "id": lineup_id,
            "uid": f"!MCLineup!{lineup_key}",
            "name": lineup_name,
        })
        channels_el = ET.SubElement(lineup_el, "channels")
        for meta in sorted(channel_meta.values(), key=lambda m: (m["major"], m["minor"])):
            ET.SubElement(channels_el, "Channel", {
                "uid": (
                    f"!Channel!{lineup_key}!{meta['station_id']}_{meta['major']}_{meta['minor']}"
                    if epg123_mode else
                    f"!Channel!{lineup_key}!{meta['service_id']}_{meta['major']}_{meta['minor']}"
                ),
                "lineup": lineup_id,
                "service": meta["service_id"],
                "matchName": f"OC:{meta['major']}:{meta['minor']}",
                "number": str(meta["major"]),
                "subNumber": str(meta["minor"]),
            })
    if epg123_mode:
        dummy_lineup = ET.SubElement(lineups_el, "Lineup", {
            "id": f"l{len(lineup_specs) + 1}",
            "uid": "!MCLineup!ZZZ-DUMMY-EPG123",
            "name": "ZZZ123 Dummy Lineup",
        })
        dummy_channels = ET.SubElement(dummy_lineup, "channels")
        ET.SubElement(dummy_channels, "Channel", {
            "uid": "!Channel!ZZZ-DUMMY-EPG123!DUMMY_-1_0",
            "lineup": f"l{len(lineup_specs) + 1}",
            "service": "s0",
            "matchName": "DUMMY",
            "number": "-1",
            "subNumber": "0",
        })

    return root


def _append_rovi_metadata(with_el: ET.Element, service_programmes: Dict[str, List[Dict]], namespaced: bool) -> None:
    tag = ns if namespaced else (lambda value: value)

    keywords_el = ET.SubElement(with_el, tag("Keywords"))
    for keyword_id, word in ROVI_STYLE_KEYWORDS:
        ET.SubElement(keywords_el, tag("Keyword"), {
            "id": keyword_id,
            "word": word,
        })

    keyword_groups_el = ET.SubElement(with_el, tag("KeywordGroups"))
    for group_name, uid, keywords in ROVI_STYLE_KEYWORD_GROUPS:
        ET.SubElement(keyword_groups_el, tag("KeywordGroup"), {
            "groupName": group_name,
            "uid": uid,
            "keywords": keywords,
        })

    guide_images_el = ET.SubElement(with_el, tag("GuideImages"))
    for image_id, image_url in _guide_images_for_programmes(service_programmes):
        ET.SubElement(guide_images_el, tag("GuideImage"), {
            "id": image_id,
            "imageUrl": image_url,
        })


def _guide_images_for_programmes(service_programmes: Dict[str, List[Dict]]) -> List[Tuple[str, str]]:
    images: Dict[str, str] = {}
    for programmes in service_programmes.values():
        for program in programmes:
            image_id = program.get("guide_image")
            image_url = program.get("icon_url")
            if image_id and image_url:
                images.setdefault(image_id, image_url)
    return sorted(images.items(), key=lambda item: item[0])


def _program_mxf_attrs(program: Dict, vista_mode: bool) -> Dict[str, str]:
    attrs = {
        "id": program["id"],
        "uid": program["uid"],
        "title": program["title"],
        "language": "en",
    }
    if program.get("episode_title"):
        attrs["episodeTitle"] = program["episode_title"]
    if program.get("description"):
        attrs["description"] = program["description"]
    if program.get("short_description"):
        attrs["shortDescription"] = program["short_description"]
    if program.get("season_num") is not None:
        attrs["seasonNumber"] = str(program["season_num"])
    if program.get("episode_num") is not None:
        attrs["episodeNumber"] = str(program["episode_num"])
    if program.get("airdate"):
        attrs["originalAirdate"] = program["airdate"]
    if program.get("year"):
        attrs["year"] = program["year"]
    if program.get("keywords"):
        attrs["keywords"] = ",".join(program["keywords"])
    if program.get("guide_image"):
        attrs["guideImage"] = program["guide_image"]
    if program.get("is_movie"):
        attrs["isMovie"] = "true"
    elif program.get("is_series"):
        attrs["isSeries"] = "true"
    if program.get("half_stars"):
        attrs["halfStars"] = program["half_stars"]
    rating = program.get("rating")
    rating_kind = program.get("rating_kind")
    if rating and rating_kind == "mpaa":
        attrs["mpaaRating"] = rating
    elif rating and rating_kind == "tv":
        attrs["tvRating"] = rating
    for flag in program.get("genre_flags") or []:
        attrs[flag] = "true"
    return attrs


def _append_assembly(root: ET.Element, name: str, namespace_name: str, types: List[Tuple[str, Optional[str], Optional[str]]]) -> None:
    assembly = ET.SubElement(root, ns("Assembly"), {
        "name": name,
        "version": "6.0.6000.0",
        "publicKey": MCEPG_PUBLIC_KEY,
        "cultureInfo": "",
    })
    namespace_el = ET.SubElement(assembly, ns("NameSpace"), {"name": namespace_name})
    for type_name, parent_field, group_name in types:
        attrs = {"name": type_name}
        if parent_field:
            attrs["parentFieldName"] = parent_field
        if group_name:
            attrs["groupName"] = group_name
        ET.SubElement(namespace_el, ns("Type"), attrs)


def _append_assembly_plain(root: ET.Element, name: str, namespace_name: str, types: List[Tuple[str, Optional[str], Optional[str]]]) -> None:
    assembly = ET.SubElement(root, "Assembly", {
        "name": name,
        "version": "6.0.6000.0",
        "publicKey": MCEPG_PUBLIC_KEY,
        "cultureinfo": "",
    })
    namespace_el = ET.SubElement(assembly, "NameSpace", {"name": namespace_name})
    for type_name, parent_field, group_name in types:
        attrs = {"name": type_name}
        if parent_field:
            attrs["parentFieldName"] = parent_field
        if group_name:
            attrs["groupName"] = group_name
        ET.SubElement(namespace_el, "Type", attrs)


def _split_guide_number(guide_number: str) -> Tuple[int, int]:
    if "." in guide_number:
        major, minor = guide_number.split(".", 1)
        return int(major), int(minor)
    return int(guide_number), 0


def _make_station_id(xmltv_id: str, guide_number: str, fallback_counter: int) -> str:
    seed = f"{xmltv_id}|{guide_number}|{fallback_counter}".encode("utf-8")
    digest = hashlib.md5(seed).hexdigest()
    return str(-2000000000 + (int(digest[:8], 16) % 100000000))


def _load_epg123_station_ids() -> Dict[str, str]:
    global _EPG123_STATION_ID_CACHE
    if _EPG123_STATION_ID_CACHE is not None:
        return _EPG123_STATION_ID_CACHE

    cfg_path = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "GaRyan2", "epg123", "epg123.cfg")
    mapping: Dict[str, str] = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            for call_sign, station_id in re.findall(r'<StationID CallSign="([^"]+)">(-?\d+)</StationID>', text):
                mapping[call_sign.strip().upper()] = station_id.strip()
        except OSError as exc:
            logger.debug("Unable to read epg123.cfg station ids: %s", exc)
    _EPG123_STATION_ID_CACHE = mapping
    return mapping


def _load_epg123_included_lineups() -> List[str]:
    global _EPG123_INCLUDED_LINEUPS_CACHE
    if _EPG123_INCLUDED_LINEUPS_CACHE is not None:
        return _EPG123_INCLUDED_LINEUPS_CACHE

    cfg_path = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "GaRyan2", "epg123", "epg123.cfg")
    included: List[str] = []
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            included = [value.strip() for value in re.findall(r"<IncludedLineup>([^<]+)</IncludedLineup>", text) if value.strip()]
        except OSError as exc:
            logger.debug("Unable to read epg123.cfg included lineups: %s", exc)
    _EPG123_INCLUDED_LINEUPS_CACHE = included
    return included


def _lookup_station_id(station_ids: Dict[str, str], call_sign: str, fallback_station_id: str) -> str:
    if not station_ids:
        return fallback_station_id
    candidates = [call_sign.strip().upper()]
    stripped = re.sub(r"(480P|576P|720P|1080P)$", "", candidates[0])
    if stripped and stripped not in candidates:
        candidates.append(stripped)
    for candidate in candidates:
        if candidate in station_ids:
            return station_ids[candidate]
    return fallback_station_id


def _channel_category_hints(channel: Optional[M3UChannel], guide_name: str) -> List[str]:
    hints: List[str] = []
    if channel is not None:
        for value in (
            getattr(channel, "group_title", ""),
            getattr(channel, "tvg_name", ""),
            getattr(channel, "name", ""),
        ):
            if value:
                hints.append(value)
    if guide_name:
        hints.append(guide_name)
    return hints


def _child_texts(parent: ET.Element, tag: str) -> List[str]:
    values: List[str] = []
    for node in parent.findall(tag):
        text = (node.text or "").strip()
        if text:
            values.append(text)
    return values


def _child_attr(parent: ET.Element, tag: str, attr: str) -> Optional[str]:
    node = parent.find(tag)
    if node is None:
        return None
    value = (node.attrib.get(attr) or "").strip()
    return value or None


def _short_description(value: Optional[str], limit: int = 255) -> Optional[str]:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "."


def _guide_image_id(image_url: str) -> str:
    digest = hashlib.md5(image_url.encode("utf-8")).hexdigest()
    return "i" + str(1 + (int(digest[:12], 16) % 2147483000))


def _is_movie_program(categories: List[str]) -> bool:
    lowered = [_normalize_category(category) for category in categories]
    return any(
        category in ("movie", "movies", "film", "films", "feature film", "cinema")
        or category.startswith("movie/")
        or category.startswith("movies/")
        or re.search(r"\b(movie|movies|film|films|cinema)\b", category)
        for category in lowered
    )


def _looks_like_movie_airing(title: str, service_name: str, duration: int) -> bool:
    combined = _normalize_category(f"{title} {service_name}")
    if re.search(r"\b(movie|movies|film|films|cinema)\b", combined):
        return True
    return duration >= 5400 and not re.search(
        r"\b(news|weather|sports|music|court|judge|game show|talk|reality|kids|cartoon|anime)\b",
        combined,
    )


def _is_news_program(categories: List[str], title: str, service_name: str) -> bool:
    combined = _normalize_category(" ".join(categories + [title, service_name]))
    return bool(re.search(r"\b(news|weather|headline|headlines|newsmagazine|current affairs)\b", combined))


def _looks_like_series_channel(categories: List[str], title: str, service_name: str) -> bool:
    combined = _normalize_category(" ".join(categories + [title, service_name]))
    if re.search(r"\b(news|weather|sports|music|movie|movies|film|films|cinema|shopping|qvc|hsn)\b", combined):
        return False
    if title and service_name and _normalize_category(title) == _normalize_category(service_name):
        return True
    return bool(re.search(
        r"\b(series|sitcom|drama|comedy|crime|mystery|anime|cartoon|kids|classic tv|tv shows|episodes)\b",
        combined,
    ))


def _episode_title_from_description(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    text = re.sub(r"\s+", " ", description).strip()
    if not text:
        return None
    sentence = re.split(r"(?<=[.!?])\s+", text, 1)[0].strip()
    return _short_description(sentence or text, limit=90)


def _program_keywords(categories: List[str], is_movie: bool, is_series: bool, is_news: bool = False) -> Tuple[List[str], List[str]]:
    keywords: List[str] = []
    flags: List[str] = []

    def add_keyword(keyword_id: str) -> None:
        if keyword_id not in keywords:
            keywords.append(keyword_id)

    def add_flag(flag: Optional[str]) -> None:
        if flag and flag not in flags:
            flags.append(flag)

    normalized = [_normalize_category(category) for category in categories]
    normalized_text = " ".join(normalized)

    if is_movie:
        add_keyword("k3")
        add_keyword("k3000")
        for token, (keyword_id, flag) in MOVIE_GENRE_KEYWORDS.items():
            if token in normalized_text:
                add_keyword(keyword_id)
                add_flag(flag)
    elif is_news or any(token in normalized_text for token in ("news", "current affairs")):
        add_keyword("k5")
        add_keyword("k5000")
        add_flag("isGeneric")
    elif any(token in normalized_text for token in ("sport", "football", "soccer", "basketball", "baseball", "cricket")):
        add_keyword("k10")
        add_keyword("k10000")
    elif any(token in normalized_text for token in ("music", "concert")):
        add_keyword("k4")
        add_keyword("k4000")
        add_flag("isMusic")
    elif any(token in normalized_text for token in ("kids", "children", "family")):
        add_keyword("k2")
        add_keyword("k2000")
    elif any(token in normalized_text for token in ("education", "educational")):
        add_keyword("k1")
        add_keyword("k1000")
    elif is_series or categories:
        add_keyword("k9")
        add_keyword("k9000")

    if not is_movie:
        for token, (keyword_id, flag) in SERIES_GENRE_KEYWORDS.items():
            if token in normalized_text:
                add_keyword(keyword_id)
                add_flag(flag)

    return keywords, flags


def _normalize_category(value: str) -> str:
    text = value.strip().lower()
    text = text.replace("&amp;", "&")
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def _extract_rating(programme: ET.Element) -> Tuple[Optional[str], Optional[str]]:
    for rating in programme.findall("rating"):
        value = _child_text(rating, "value")
        if not value:
            continue
        normalized = value.strip().upper().replace("TV ", "TV-")
        system = (rating.attrib.get("system") or "").strip().lower()
        if system in ("mpaa", "film", "movie") or normalized in ("G", "PG", "PG-13", "R", "NC-17"):
            return normalized, "mpaa"
        if normalized.startswith("TV-") or system in ("v-chip", "tv", "ustv"):
            return normalized, "tv"
        return normalized, None
    return None, None


def _extract_airdate(programme: ET.Element) -> Tuple[Optional[str], Optional[str]]:
    raw = _child_text(programme, "date")
    if not raw:
        return None, None
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}", digits[0:4]
    if len(digits) >= 4:
        return None, digits[0:4]
    return None, None


def _extract_half_stars(programme: ET.Element) -> Optional[str]:
    star_rating = programme.find("star-rating")
    if star_rating is None:
        return None
    value = _child_text(star_rating, "value")
    if not value:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*$", value)
    try:
        if match:
            score = float(match.group(1))
            scale = float(match.group(2))
            if scale > 0:
                return str(max(0, min(10, int(round((score / scale) * 10)))))
        score = float(value)
        return str(max(0, min(10, int(round(score * 2)))))
    except ValueError:
        return None


def _child_text(parent: ET.Element, tag: str) -> Optional[str]:
    node = parent.find(tag)
    if node is None or node.text is None:
        return None
    text = node.text.strip()
    return text or None


def _extract_episode_numbers(programme: ET.Element) -> Tuple[Optional[int], Optional[int]]:
    season_num = None
    episode_num = None
    for node in programme.findall("episode-num"):
        system = (node.attrib.get("system") or "").lower()
        text = (node.text or "").strip()
        if not text:
            continue
        if system == "xmltv_ns":
            parts = text.split(".")
            try:
                if parts and parts[0] != "":
                    season_num = int(parts[0]) + 1
                if len(parts) > 1 and parts[1] != "":
                    episode_num = int(parts[1]) + 1
            except ValueError:
                pass
        elif system == "onscreen":
            numbers = [int(part) for part in text.replace("S", " ").replace("E", " ").split() if part.isdigit()]
            if len(numbers) >= 1 and season_num is None:
                season_num = numbers[0]
            if len(numbers) >= 2 and episode_num is None:
                episode_num = numbers[1]
    return season_num, episode_num


def _parse_xmltv_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split()
    stamp = parts[0]
    offset = parts[1] if len(parts) > 1 else "+0000"
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(stamp[:len(datetime.now().strftime(fmt))], fmt)
            tz = _parse_xmltv_offset(offset)
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _parse_xmltv_offset(value: str) -> timezone:
    text = (value or "+0000").strip()
    if text in ("Z", "UTC"):
        return timezone.utc
    sign = -1 if text.startswith("-") else 1
    digits = text.lstrip("+-")
    if len(digits) < 4:
        return timezone.utc
    hours = int(digits[:2])
    minutes = int(digits[2:4])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _to_mxf_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _to_device_group_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_call_sign(name: str) -> str:
    text = "".join(ch for ch in (name or "").upper() if ch.isalnum())
    if not text:
        return "HDHR"
    return text[:24]


def _lineup_scanned_call_sign(item: Dict, fallback_name: str) -> str:
    explicit = str(item.get("ScannedCallSign") or item.get("CallSign") or "").strip()
    if explicit:
        return _safe_atsc_call_sign(explicit)
    return _safe_atsc_call_sign(fallback_name)


def _safe_atsc_call_sign(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.+-]+", "-", name or "VirtualHD")
    return (clean.strip("-")[:7] or "Virtual")[:7]
