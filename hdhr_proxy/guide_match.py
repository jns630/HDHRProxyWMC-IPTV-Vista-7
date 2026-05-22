import csv
import logging
import os
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from .m3u_parser import M3UChannel
from .mxf import make_call_sign, write_mxf
from .xmltv import resolve_channel_xmltv_ids

logger = logging.getLogger(__name__)

MXF_NS = "urn:com:dontocsata:xmltv:mxf"


def write_guide_match_utility(
    lineup: List[Dict],
    channel_map: Dict[str, M3UChannel],
    xmltv_xml: str,
    csv_path: str = "HDHRProxyWMC_GuideMatch.generated.csv",
    mapping_path: str = "HDHRProxyWMC_GuideOnly.generated.ini",
    mxf_path: Optional[str] = None,
) -> Tuple[str, str, int]:
    rows = build_guide_match_rows(lineup, channel_map, xmltv_xml, mxf_path=mxf_path)
    csv_path = write_guide_match_csv(rows, csv_path)
    mapping_path = write_guide_only_mapping_file(lineup, rows, mapping_path)
    return csv_path, mapping_path, len(rows)


def write_wmc_auto_match_mxf(
    lineup: List[Dict],
    channel_map: Dict[str, M3UChannel],
    xmltv_xml: str,
    output_path: str = "HDHRProxyWMC_AutoMatch.generated.mxf",
    vista_mode: bool = False,
    epg123_mode: bool = False,
) -> Tuple[str, int]:
    rows = build_guide_match_rows(lineup, channel_map, xmltv_xml)
    filtered_lineup, filtered_channel_map = filter_lineup_to_matched_channels(lineup, channel_map, rows)
    out_path = write_mxf(
        xmltv_xml,
        filtered_lineup,
        filtered_channel_map,
        output_path,
        vista_mode=vista_mode,
        force_ota_match=True,
        epg123_mode=epg123_mode,
    )
    return out_path, len(filtered_lineup)


def filter_lineup_to_matched_channels(
    lineup: List[Dict],
    channel_map: Dict[str, M3UChannel],
    rows: List[Dict[str, str]],
) -> Tuple[List[Dict], Dict[str, M3UChannel]]:
    matched = {row["GuideNumber"] for row in rows}
    filtered_lineup = [item for item in lineup if str(item.get("GuideNumber", "")).strip() in matched]
    filtered_channel_map = {
        guide_number: channel
        for guide_number, channel in channel_map.items()
        if guide_number in matched
    }
    return filtered_lineup, filtered_channel_map


def build_guide_match_rows(
    lineup: List[Dict],
    channel_map: Dict[str, M3UChannel],
    xmltv_xml: str,
    mxf_path: Optional[str] = None,
) -> List[Dict[str, str]]:
    xmltv_ids = resolve_channel_xmltv_ids(xmltv_xml or "", channel_map) if xmltv_xml else {}
    xmltv_program_counts = _xmltv_programme_counts(xmltv_xml or "")
    mxf_services = _load_mxf_services(mxf_path) if mxf_path and os.path.exists(mxf_path) else {}

    rows: List[Dict[str, str]] = []
    for item in lineup:
        guide_number = str(item.get("GuideNumber", "")).strip()
        if not guide_number:
            continue
        channel = channel_map.get(guide_number)
        if channel is None:
            continue

        guide_name = str(item.get("GuideName") or getattr(channel, "name", guide_number)).strip()
        call_sign = make_call_sign(guide_name)
        xmltv_id = xmltv_ids.get(guide_number, "")
        program_count = xmltv_program_counts.get(xmltv_id, 0)
        mxf_meta = mxf_services.get(call_sign, {})

        if not xmltv_id and not mxf_meta and program_count <= 0:
            continue

        rows.append({
            "GuideNumber": guide_number,
            "GuideName": guide_name,
            "PhysicalChannel": str(item.get("PhysicalChannel", "")),
            "ProgramNumber": str(item.get("ProgramNumber", "")),
            "Frequency": str(item.get("Frequency", "")),
            "CallSign": call_sign,
            "MatchName": str(mxf_meta.get("match_name", call_sign)),
            "XMLTV_ID": xmltv_id,
            "Listings": str(program_count),
            "MXFServiceID": str(mxf_meta.get("service_id", "")),
            "MXFServiceName": str(mxf_meta.get("service_name", guide_name)),
            "URL": str(item.get("URL", "")),
        })

    rows.sort(key=lambda row: _guide_number_sort_key(row["GuideNumber"]))
    return rows


def write_guide_match_csv(rows: List[Dict[str, str]], path: str) -> str:
    out_path = os.path.abspath(path)
    fieldnames = [
        "GuideNumber",
        "GuideName",
        "PhysicalChannel",
        "ProgramNumber",
        "Frequency",
        "CallSign",
        "MatchName",
        "XMLTV_ID",
        "Listings",
        "MXFServiceID",
        "MXFServiceName",
        "URL",
    ]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def write_guide_only_mapping_file(lineup: List[Dict], rows: List[Dict[str, str]], path: str) -> str:
    matched = {row["GuideNumber"] for row in rows}
    filtered = [item for item in lineup if str(item.get("GuideNumber", "")).strip() in matched]
    lines = [
        "###########################################",
        "# Generated from the active M3U lineup.",
        "# Filtered to channels that matched XMLTV/MXF guide listings.",
        "# Use this after WMC scan when you only want channels that exist in the guide.",
        "###########################################",
        "",
        "[MAPPING_LIST]",
        f"NUM_CHANNELS={len(filtered)}",
        "",
    ]
    for index, ch in enumerate(filtered, start=1):
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


def _xmltv_programme_counts(xmltv_xml: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    if not xmltv_xml:
        return counts
    try:
        root = ET.fromstring(xmltv_xml)
    except ET.ParseError:
        return counts
    for programme in root.findall("programme"):
        channel_id = (programme.attrib.get("channel") or "").strip()
        if channel_id:
            counts[channel_id] = counts.get(channel_id, 0) + 1
    return counts


def _load_mxf_services(path: str) -> Dict[str, Dict[str, str]]:
    services: Dict[str, Dict[str, str]] = {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        logger.warning("Unable to parse MXF guide for match utility: %s", exc)
        return services

    ns = {"m": MXF_NS}
    for service in root.findall(".//m:Services/m:Service", ns):
        call_sign = (service.attrib.get("callSign") or "").strip()
        if not call_sign:
            continue
        services[call_sign] = {
            "service_id": str(service.attrib.get("id", "")),
            "service_name": str(service.attrib.get("name", "")),
            "match_name": call_sign,
        }
    return services


def _guide_number_sort_key(value: str) -> Tuple[int, int, str]:
    text = str(value or "").strip()
    if "." in text:
        major, minor = text.split(".", 1)
        try:
            return int(major), int(minor), text
        except ValueError:
            return 999999, 999999, text
    try:
        return int(text), 0, text
    except ValueError:
        return 999999, 0, text
