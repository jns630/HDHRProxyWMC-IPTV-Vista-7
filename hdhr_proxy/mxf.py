import hashlib
import logging
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .m3u_parser import M3UChannel
from .xmltv import resolve_channel_xmltv_ids

logger = logging.getLogger(__name__)

MXF_NS = "urn:com:dontocsata:xmltv:mxf"
ET.register_namespace("", MXF_NS)
LINEUP_KEY = "HDHRProxy"


def ns(tag: str) -> str:
    return f"{{{MXF_NS}}}{tag}"


def write_mxf(
    xmltv_xml: str,
    lineup: List[Dict],
    channel_map: Dict[str, M3UChannel],
    output_path: str,
) -> str:
    xmltv_ids_by_guide = resolve_channel_xmltv_ids(xmltv_xml, channel_map) if xmltv_xml else {}
    channel_meta = _build_channel_metadata(lineup, channel_map, xmltv_ids_by_guide)
    service_programmes = _collect_programmes(xmltv_xml, channel_meta)
    root = _build_mxf_root(channel_meta, service_programmes)

    output_path = os.path.abspath(output_path)
    tree = ET.ElementTree(root)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    logger.info("Wrote MXF guide file: %s", output_path)
    return output_path


def import_mxf(output_path: str) -> None:
    loadmxf = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "ehome", "loadmxf.exe")
    if not os.path.exists(loadmxf):
        raise FileNotFoundError(f"loadmxf.exe not found at {loadmxf}")
    logger.info("Importing MXF into Windows Media Center: %s", output_path)
    subprocess.run([loadmxf, "-v", "-i", output_path], check=True)


def _build_channel_metadata(
    lineup: List[Dict],
    channel_map: Dict[str, M3UChannel],
    xmltv_ids_by_guide: Dict[str, str],
) -> Dict[str, Dict]:
    meta_by_service: Dict[str, Dict] = {}
    fallback_counter = 1
    for item in lineup:
        guide_number = str(item.get("GuideNumber", "")).strip()
        if not guide_number:
            continue
        major, minor = _split_guide_number(guide_number)
        channel = channel_map.get(guide_number)
        tvg_id = (getattr(channel, "tvg_id", "") or "").strip()
        if not tvg_id:
            tvg_id = f"guide-{guide_number}"
        service_id = f"s{major}.{minor}.{fallback_counter}.hdhrproxy"
        xmltv_id = xmltv_ids_by_guide.get(guide_number) or tvg_id
        fallback_counter += 1
        meta_by_service[service_id] = {
            "guide_number": guide_number,
            "major": major,
            "minor": minor,
            "service_id": service_id,
            "xmltv_id": xmltv_id,
            "source_tvg_id": tvg_id,
            "service_name": str(item.get("GuideName") or getattr(channel, "name", guide_number)),
            "call_sign": _make_call_sign(str(item.get("GuideName") or getattr(channel, "name", guide_number))),
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
        title = _child_text(programme, "title") or meta["service_name"]
        episode_title = _child_text(programme, "sub-title")
        description = _child_text(programme, "desc")
        season_num, episode_num = _extract_episode_numbers(programme)
        is_series = bool(episode_title or season_num is not None or episode_num is not None)
        base_program = {
            "id": str(next_program_id),
            "uid": "!Program!" + hashlib.md5(
            f"{channel_id}|{programme.attrib.get('start')}|{title}|{episode_title or ''}".encode("utf-8")
        ).hexdigest(),
            "title": title,
            "episode_title": episode_title,
            "description": description,
            "season_num": season_num,
            "episode_num": episode_num,
            "is_series": is_series,
            "start_time": _to_mxf_time(start_dt),
            "duration": str(duration),
        }
        for meta in metas:
            programmes_by_service[meta["service_id"]].append(dict(base_program))
        next_program_id += 1

    for items in programmes_by_service.values():
        items.sort(key=lambda p: p["start_time"])
    return programmes_by_service


def _build_mxf_root(channel_meta: Dict[str, Dict], service_programmes: Dict[str, List[Dict]]) -> ET.Element:
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
    ET.SubElement(with_el, ns("Keywords"))
    ET.SubElement(with_el, ns("KeywordGroups"))
    ET.SubElement(with_el, ns("GuideImages"))
    ET.SubElement(with_el, ns("People"))
    ET.SubElement(with_el, ns("SeriesInfos"))
    ET.SubElement(with_el, ns("Seasons"))

    programs_el = ET.SubElement(with_el, ns("Programs"))
    for service_id in sorted(service_programmes):
        for program in service_programmes[service_id]:
            attrs = {
                "id": program["id"],
                "uid": program["uid"],
                "title": program["title"],
            }
            if program["episode_title"]:
                attrs["episodeTitle"] = program["episode_title"]
            if program["description"]:
                attrs["description"] = program["description"]
            if program["episode_num"] is not None:
                attrs["episodeNumber"] = str(program["episode_num"])
            if program["is_series"]:
                attrs["isSeries"] = "true"
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
        for index, program in enumerate(service_programmes.get(meta["service_id"], [])):
            attrs = {
                "program": program["id"],
                "duration": program["duration"],
            }
            if index == 0:
                attrs["startTime"] = program["start_time"]
            ET.SubElement(schedule_entries_el, ns("ScheduleEntry"), attrs)

    lineups_el = ET.SubElement(with_el, ns("Lineups"))
    lineup_el = ET.SubElement(lineups_el, ns("Lineup"), {
        "id": "l1",
        "uid": f"!Lineup!{LINEUP_KEY}",
        "name": LINEUP_KEY,
        "primaryProvider": "!MCLineup!MainLineup",
    })
    channels_el = ET.SubElement(lineup_el, ns("Channels"))
    for meta in sorted(channel_meta.values(), key=lambda m: (m["major"], m["minor"])):
        ET.SubElement(channels_el, ns("Channel"), {
            "uid": f"!Channel!{LINEUP_KEY}!{meta['major']}_{meta['minor']}",
            "lineup": "l1",
            "service": meta["service_id"],
            "matchName": meta["call_sign"],
            "number": str(meta["major"]),
            "subNumber": str(meta["minor"]),
        })

    return root


def _append_assembly(root: ET.Element, name: str, namespace_name: str, types: List[Tuple[str, Optional[str], Optional[str]]]) -> None:
    assembly = ET.SubElement(root, ns("Assembly"), {
        "name": name,
        "version": "6.0.6000.0",
        "publicKey": "0024000004800000940000000602000000240000525341310004000001000100B5FC90E7027F67871E773A8FDE8938C81DD402BA65B9201D60593E96C492651E889CC13F1415EBB53FAC1131AE0BD333C5EE6021672D9718EA31A8AEBD0DA0072F25D87DBA6FC90FFD598ED4DA35E44C398C454307E8E33B8426143DAEC9F596836F97C8F74750E5975C64E2189F45DEF46B2A2B1247ADC3652BF5C308055DA9",
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


def _split_guide_number(guide_number: str) -> Tuple[int, int]:
    if "." in guide_number:
        major, minor = guide_number.split(".", 1)
        return int(major), int(minor)
    return int(guide_number), 0


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


def _make_call_sign(name: str) -> str:
    text = "".join(ch for ch in (name or "").upper() if ch.isalnum())
    if not text:
        return "HDHR"
    return text[:24]
