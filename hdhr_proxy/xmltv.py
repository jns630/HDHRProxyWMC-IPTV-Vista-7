import copy
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, Optional, Set, Tuple

from .m3u_parser import M3UChannel

logger = logging.getLogger(__name__)


class XMLTVData:
    def __init__(self, raw_xml: str, filtered_xml: str, source: str, matched_ids: Optional[Set[str]] = None):
        self.raw_xml = raw_xml
        self.filtered_xml = filtered_xml
        self.source = source
        self.matched_ids = matched_ids or set()


def load_xmltv(xmltv_file: Optional[str], xmltv_url: Optional[str], channel_map: Dict[str, M3UChannel]) -> Optional[XMLTVData]:
    source = ""
    raw_xml = ""
    if xmltv_url:
        source = xmltv_url
        raw_xml = _fetch_xmltv_url(xmltv_url)
    elif xmltv_file:
        source = xmltv_file
        raw_xml = _read_xmltv_file(xmltv_file)
    else:
        return None

    filtered_xml, matched_ids = _filter_xmltv(raw_xml, channel_map)
    return XMLTVData(raw_xml=raw_xml, filtered_xml=filtered_xml, source=source, matched_ids=matched_ids)


def _fetch_xmltv_url(url: str) -> str:
    logger.info("Fetching XMLTV guide from URL: %s", url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "VirtualHDHR/1.0",
            "Accept": "application/xml,text/xml,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _read_xmltv_file(path: str) -> str:
    logger.info("Reading XMLTV guide from file: %s", path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _filter_xmltv(raw_xml: str, channel_map: Dict[str, M3UChannel]) -> Tuple[str, Set[str]]:
    wanted_ids = {channel.tvg_id.strip() for channel in channel_map.values() if getattr(channel, "tvg_id", "").strip()}

    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        logger.warning("Unable to parse XMLTV guide; serving raw XML only: %s", exc)
        return raw_xml, set()

    matched_ids = _resolve_xmltv_channel_ids(root, channel_map)
    if not matched_ids:
        if not wanted_ids:
            logger.info("XMLTV guide loaded without filtering because no channels expose tvg-id values.")
        else:
            logger.warning("XMLTV guide did not match any playlist channels; serving raw XML only.")
        return raw_xml, set()

    filtered_root = ET.Element(root.tag, root.attrib)
    for child in list(root):
        tag = _local_name(child.tag)
        if tag == "channel" and child.attrib.get("id", "").strip() in matched_ids:
            filtered_root.append(copy.deepcopy(child))
        elif tag == "programme" and child.attrib.get("channel", "").strip() in matched_ids:
            filtered_root.append(copy.deepcopy(child))

    filtered_xml = ET.tostring(filtered_root, encoding="unicode")
    if not filtered_xml.lstrip().startswith("<?xml"):
        filtered_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + filtered_xml
    logger.info("Filtered XMLTV guide to %s matched channels.", len(matched_ids))
    return filtered_xml, matched_ids


def resolve_channel_xmltv_ids(raw_xml: str, channel_map: Dict[str, M3UChannel]) -> Dict[str, str]:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return {}
    display_name_map = _build_display_name_map(root)
    xml_ids = {channel.attrib.get("id", "").strip() for channel in root.findall("channel")}
    resolved: Dict[str, str] = {}
    for guide_number, channel in channel_map.items():
        xmltv_id = _pick_xmltv_channel_id(channel, xml_ids, display_name_map)
        if xmltv_id:
            resolved[guide_number] = xmltv_id
    return resolved


def _resolve_xmltv_channel_ids(root: ET.Element, channel_map: Dict[str, M3UChannel]) -> Set[str]:
    display_name_map = _build_display_name_map(root)
    xml_ids = {channel.attrib.get("id", "").strip() for channel in root.findall("channel")}
    matched_ids: Set[str] = set()
    for channel in channel_map.values():
        xmltv_id = _pick_xmltv_channel_id(channel, xml_ids, display_name_map)
        if xmltv_id:
            matched_ids.add(xmltv_id)
    return matched_ids


def _build_display_name_map(root: ET.Element) -> Dict[str, str]:
    display_name_map: Dict[str, str] = {}
    for channel in root.findall("channel"):
        xmltv_id = (channel.attrib.get("id") or "").strip()
        if not xmltv_id:
            continue
        display_names = []
        for node in channel.findall("display-name"):
            if node.text:
                display_names.append(node.text.strip())
        for name in display_names:
            display_name_map.setdefault(name, xmltv_id)
            normalized = _normalize_channel_name(name)
            if normalized:
                display_name_map.setdefault(normalized, xmltv_id)
    return display_name_map


def _pick_xmltv_channel_id(channel: M3UChannel, xml_ids: Set[str], display_name_map: Dict[str, str]) -> Optional[str]:
    tvg_id = (getattr(channel, "tvg_id", "") or "").strip()
    if tvg_id and tvg_id in xml_ids:
        return tvg_id

    candidates = [
        (getattr(channel, "tvg_name", "") or "").strip(),
        (getattr(channel, "name", "") or "").strip(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in display_name_map:
            return display_name_map[candidate]
        normalized = _normalize_channel_name(candidate)
        if normalized and normalized in display_name_map:
            return display_name_map[normalized]
    return None


def _normalize_channel_name(value: str) -> str:
    text = value.lower().strip()
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
