# -*- coding: utf-8 -*-
"""Tests for opt-in DVB-T support and unchanged ATSC defaults."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy.config import Config, ConfigError  # noqa: E402
from hdhr_proxy.discovery import DiscoveryServer  # noqa: E402
from hdhr_proxy.m3u_parser import M3UChannel, build_lineup  # noqa: E402
from hdhr_proxy.tuner_types import (  # noqa: E402
    DVBT,
    FrequencyPlan,
    normalize_tuner_type,
)


def make_channels(count):
    channels = []
    for index in range(count):
        channel = M3UChannel()
        channel.name = "Channel %d" % index
        channel.url = "http://127.0.0.1:1/source/%d.m3u8" % index
        channels.append(channel)
    return channels


class TunerTypeTests(unittest.TestCase):
    def test_normalizes_defaults_and_aliases(self):
        self.assertEqual(normalize_tuner_type(None), "atsc")
        self.assertEqual(normalize_tuner_type("ATSC"), "atsc")
        self.assertEqual(normalize_tuner_type("dvb-t"), DVBT)
        self.assertEqual(normalize_tuner_type("DVB"), DVBT)
        self.assertEqual(normalize_tuner_type("eu-bcast"), DVBT)

    def test_rejects_unknown_tuner_type(self):
        with self.assertRaises(ValueError):
            normalize_tuner_type("isdb-t")


class DvbConfigTests(unittest.TestCase):
    def test_default_config_is_unchanged_atsc(self):
        config = Config()
        self.assertEqual(config.tuner_type, "atsc")
        plan = config.frequency_plan()
        self.assertFalse(plan.is_dvbt)
        self.assertEqual(plan.channelmap, "us-bcast")
        self.assertEqual(plan.modulation, "8vsb")

    def test_dvb_config_values_are_accepted_and_normalized(self):
        config = Config()
        config.tuner_type = "dvb-t"
        config.dvb_first_channel = 30
        config.dvb_last_channel = 48
        config.dvb_bandwidth = 7
        config.dvb_start_frequency = 500000000
        config.validate()

        self.assertEqual(config.tuner_type, DVBT)
        plan = config.frequency_plan()
        self.assertTrue(plan.is_dvbt)
        self.assertEqual(plan.first_physical_channel, 30)
        self.assertEqual(plan.last_physical_channel, 48)
        self.assertEqual(plan.bandwidth_hz, 7000000)
        self.assertEqual(plan.frequency_for_physical_channel(31), 507000000)

    def test_invalid_dvb_config_is_rejected(self):
        config = Config()
        config.tuner_type = "dvbt"
        config.dvb_bandwidth = 5
        with self.assertRaises(ConfigError):
            config.validate()

        config = Config()
        config.tuner_type = "dvbt"
        config.dvb_first_channel = 40
        config.dvb_last_channel = 30
        with self.assertRaises(ConfigError):
            config.validate()

        config = Config()
        config.tuner_type = "isdb"
        with self.assertRaises(ConfigError):
            config.validate()


class LineupTests(unittest.TestCase):
    def test_atsc_lineup_is_unchanged(self):
        lineup, _ = build_lineup(make_channels(1), base_url="http://127.0.0.1:5004")
        entry = lineup[0]
        self.assertEqual(entry["PhysicalChannel"], 14)
        self.assertEqual(entry["Frequency"], 473000000)
        self.assertEqual(entry["LowFreq"], 470000000)
        self.assertEqual(entry["HighFreq"], 476000000)
        self.assertEqual(entry["Modulation"], "8vsb")
        self.assertEqual(entry["VirtualMajor"], 2)
        self.assertEqual(entry["GuideNumber"], "2.1")

    def test_dvbt_lineup_uses_dvb_frequencies_and_modulation(self):
        lineup, _ = build_lineup(
            make_channels(3), base_url="http://127.0.0.1:5004", tuner_type="dvbt"
        )
        self.assertEqual([entry["PhysicalChannel"] for entry in lineup], [21, 22, 23])
        self.assertEqual([entry["Frequency"] for entry in lineup], [474000000, 482000000, 490000000])
        self.assertEqual([entry["LowFreq"] for entry in lineup], [470000000, 478000000, 486000000])
        self.assertEqual([entry["HighFreq"] for entry in lineup], [478000000, 486000000, 494000000])
        self.assertEqual([entry["Modulation"] for entry in lineup], ["dvbt", "dvbt", "dvbt"])
        self.assertEqual([entry["VirtualMajor"] for entry in lineup], [2, 3, 4])
        self.assertEqual([entry["GuideNumber"] for entry in lineup], ["2.1", "3.1", "4.1"])

    def test_dvbt_lineup_honors_custom_band_plan(self):
        lineup, _ = build_lineup(
            make_channels(2),
            base_url="http://127.0.0.1:5004",
            tuner_type="dvbt",
            dvb_first_channel=30,
            dvb_last_channel=48,
            dvb_bandwidth=7,
            dvb_start_frequency=500000000,
        )
        self.assertEqual(lineup[0]["PhysicalChannel"], 30)
        self.assertEqual(lineup[0]["Frequency"], 500000000)
        self.assertEqual(lineup[0]["LowFreq"], 496500000)
        self.assertEqual(lineup[1]["Frequency"], 507000000)

    def test_dvbt_bandwidth_six_uses_three_mhz_half_band(self):
        plan = FrequencyPlan(tuner_type="dvbt", dvb_bandwidth=6)
        self.assertEqual(plan.frequency_for_physical_channel(21), 474000000)
        self.assertEqual(plan.low_frequency_for_physical_channel(21), 471000000)
        self.assertEqual(plan.high_frequency_for_physical_channel(21), 477000000)


class DvbDiscoveryTests(unittest.TestCase):
    def _server(self, tuner_type, count=2):
        lineup, channel_map = build_lineup(
            make_channels(count),
            base_url="http://127.0.0.1:5004",
            tuner_type=tuner_type,
        )
        return DiscoveryServer(
            device_id="104ABCDE",
            base_url="http://127.0.0.1:5004",
            tuner_count=1,
            lineup=lineup,
            channel_map=channel_map,
            ffmpeg_path="missing-ffmpeg",
            tuner_type=tuner_type,
        )

    def test_atsc_control_values_are_unchanged(self):
        server = self._server("atsc")
        self.assertEqual(server._get_control_value("/sys/channelmap"), "us-bcast")
        self.assertEqual(server._get_control_value("/sys/dvbc_modulation"), "8vsb")
        self.assertEqual(
            server._get_control_value("/sys/features"),
            "channelmap: us-bcast\nmodulation: 8vsb qam64 qam256\nauto-modulation: auto auto6t auto8t",
        )
        self.assertEqual(server._firmware_model_name(), "hdhomerun4_atsc")
        self.assertEqual(server._tuner_state[0]["channelmap"], "us-bcast")

    def test_dvbt_control_values_and_model_name(self):
        server = self._server("dvbt")
        self.assertEqual(server._get_control_value("/sys/channelmap"), "eu-bcast")
        self.assertEqual(server._get_control_value("/sys/dvbc_modulation"), "dvbt")
        self.assertEqual(
            server._get_control_value("/sys/features"),
            "channelmap: eu-bcast\nmodulation: dvbt\nauto-modulation: auto",
        )
        self.assertEqual(server._firmware_model_name(), "hdhomerun4_dvbt")
        self.assertEqual(server._tuner_state[0]["channelmap"], "eu-bcast")

    def test_dvbt_scan_text_and_xml(self):
        server = self._server("dvbt")
        text = server._format_lineup_scan_text()
        self.assertIn("SCANNING: 474000000 (eu-bcast:21)", text)
        self.assertIn("LOCK: dvbt", text)

        xml = server._format_lineup_scan_xml()
        self.assertIn("<Modulation>dvbt</Modulation>", xml)
        self.assertIn("<Frequency>474000000</Frequency>", xml)

    def test_dvbt_scan_probes_and_normalization(self):
        server = self._server("dvbt")
        self.assertTrue(server._looks_like_atsc_scan_probe("auto:482000000", [482000000]))
        self.assertFalse(server._looks_like_atsc_scan_probe("none", []))
        self.assertEqual(server._normalize_scan_map(""), "eu-bcast")
        self.assertEqual(server._normalize_scan_map("dvb-t"), "eu-bcast")
        self.assertEqual(
            server._rf_from_scan_numbers([482000000], server._rf_channels[0]),
            (22, 482000000),
        )
        self.assertEqual(
            server._rf_from_scan_numbers([23], server._rf_channels[0]),
            (23, 490000000),
        )

    def test_dvbt_scan_outside_lineup_folds_onto_virtual_rf(self):
        server = self._server("dvbt", count=4)
        self.assertEqual(server._select_channel_for_tune("auto8t:658167000"), (None, None))
        _, rf = server._virtual_rf_for_current_tune("auto8t:658167000")
        self.assertIsNotNone(rf)
        self.assertIn(int(rf["physical"]), (21, 22, 23, 24))
        # Deterministic and repeating every lineup size so playback resolves the
        # same virtual RF after the scan.
        self.assertEqual(
            server._virtual_rf_for_current_tune("auto8t:658167000")[1]["physical"],
            rf["physical"],
        )
        self.assertEqual(
            server._virtual_rf_for_current_tune("auto8t:690167000")[1]["physical"],
            rf["physical"],
        )

    def test_dvbt_scan_control_sequence_locks_and_holds_psip(self):
        server = self._server("dvbt", count=4)
        server._set_control_value("/tuner0/channel", "auto8t:658167000")
        state = server._tuner_state[0]
        self.assertIn("lock=dvbt", state["status"])
        self.assertNotIn("lock=none", state["status"])
        self.assertIsNotNone(state["rf"])
        self.assertTrue(server._should_hold_scan_psip_only(state))
        self.assertTrue(server._build_dvb_si_sections(state["rf"]))

        physical = int(state["rf"]["physical"])
        program = next(
            rf["program"] for rf in server._rf_channels
            if int(rf["physical"]) == physical
        )
        server._set_control_value("/tuner0/program", str(program))
        self.assertIn(state["channel_id"], server.channel_map)

    def test_atsc_scan_outside_lineup_has_no_lock(self):
        server = self._server("atsc", count=4)
        self.assertEqual(server._virtual_rf_for_current_tune("auto8t:700000000"), (None, None))

    def test_dvbt_si_contains_pat_pmt_and_sdt(self):
        server = self._server("dvbt")
        sections = server._build_dvb_si_sections(server._rf_channels[0])
        table_ids = [(pid, section[0]) for pid, section in sections]
        self.assertIn((0x0000, 0x00), table_ids)
        self.assertIn((0x0011, 0x42), table_ids)
        self.assertTrue(any(pid != 0x0000 and table == 0x02 for pid, table in table_ids))
        for _, section in sections:
            self.assertEqual(server._mpeg_crc32(section), 0)

    def test_dvbt_sdt_splits_large_rf_groups(self):
        server = self._server("dvbt")
        rf_group = [
            {
                "program": 3 + index,
                "name": "Very Long Virtual Channel Name %d" % index,
                "physical": 21,
            }
            for index in range(60)
        ]
        sections = server._make_dvb_sdt_sections(rf_group)
        self.assertGreater(len(sections), 1)
        for index, section in enumerate(sections):
            section_length = ((section[1] & 0x0F) << 8) | section[2]
            self.assertLessEqual(section_length, 1021)
            self.assertEqual(section[6], index)
            self.assertEqual(section[7], len(sections) - 1)
            self.assertEqual(server._mpeg_crc32(section), 0)

    def test_atsc_psip_is_still_used_for_atsc(self):
        server = self._server("atsc")
        sections = server._build_atsc_psip_sections(server._rf_channels[0])
        table_ids = [section[0] for _, section in sections]
        self.assertIn(0x00, table_ids)
        self.assertIn(0xC7, table_ids)
        self.assertIn(0xC8, table_ids)
        self.assertNotIn(0x42, table_ids)


if __name__ == "__main__":
    unittest.main()
