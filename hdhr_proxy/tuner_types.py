"""Broadcast standard helpers for the virtual HDHomeRun tuner.

ATSC remains the default and its behavior is intentionally unchanged.  The
DVB-T plan maps virtual physical channels onto European-style UHF channels
(474 MHz, 8 MHz steps by default) and reports DVB-T modulation/channelmap
values through the HDHomeRun control and lineup APIs.
"""
from typing import Optional

ATSC = "atsc"
DVBT = "dvbt"

ATSC_PROGRAM_NUMBER = 3
ATSC_FIRST_PHYSICAL_CHANNEL = 14
ATSC_LAST_PHYSICAL_CHANNEL = 59
ATSC_HALF_BANDWIDTH_HZ = 3000000

# US ATSC broadcast center frequencies in Hz for channels commonly scanned by
# HDHomeRun/WMC.
ATSC_FREQUENCIES = {
    **{ch: (57 + (ch - 2) * 6) * 1000000 for ch in range(2, 5)},
    5: 79000000,
    6: 85000000,
    **{ch: (177 + (ch - 7) * 6) * 1000000 for ch in range(7, 14)},
    **{ch: (473 + (ch - 14) * 6) * 1000000 for ch in range(14, 70)},
}

_TUNER_TYPE_ALIASES = {
    "atsc": ATSC,
    "8vsb": ATSC,
    "us-bcast": ATSC,
    "dvbt": DVBT,
    "dvb-t": DVBT,
    "dvb_t": DVBT,
    "dvb": DVBT,
    "eu-bcast": DVBT,
}


class TunerTypeError(ValueError):
    """Raised when an unsupported tuner type is requested."""


def normalize_tuner_type(value) -> str:
    if value is None:
        return ATSC
    key = str(value).strip().lower()
    if not key:
        return ATSC
    try:
        return _TUNER_TYPE_ALIASES[key]
    except KeyError:
        raise TunerTypeError(
            "Unsupported tuner type %r; use 'atsc' or 'dvbt'." % (value,)
        )


class FrequencyPlan:
    """Frequency/modulation plan for one broadcast standard."""

    def __init__(
        self,
        tuner_type: str = ATSC,
        dvb_first_channel: int = 21,
        dvb_last_channel: int = 69,
        dvb_bandwidth: int = 8,
        dvb_start_frequency: int = 474000000,
        atsc_max_physical_channel: int = ATSC_LAST_PHYSICAL_CHANNEL,
    ):
        self.tuner_type = normalize_tuner_type(tuner_type)
        self.dvb_first_channel = max(1, int(dvb_first_channel))
        self.dvb_last_channel = max(self.dvb_first_channel, int(dvb_last_channel))
        self.dvb_bandwidth = int(dvb_bandwidth)
        self.dvb_start_frequency = int(dvb_start_frequency)
        self.atsc_max_physical_channel = max(
            ATSC_FIRST_PHYSICAL_CHANNEL,
            min(int(atsc_max_physical_channel), ATSC_LAST_PHYSICAL_CHANNEL),
        )

    @property
    def is_dvbt(self) -> bool:
        return self.tuner_type == DVBT

    @property
    def first_physical_channel(self) -> int:
        return self.dvb_first_channel if self.is_dvbt else ATSC_FIRST_PHYSICAL_CHANNEL

    @property
    def last_physical_channel(self) -> int:
        if self.is_dvbt:
            return self.dvb_last_channel
        return self.atsc_max_physical_channel

    @property
    def channelmap(self) -> str:
        return "eu-bcast" if self.is_dvbt else "us-bcast"

    @property
    def modulation(self) -> str:
        return "dvbt" if self.is_dvbt else "8vsb"

    @property
    def features(self) -> str:
        if self.is_dvbt:
            return "channelmap: eu-bcast\nmodulation: dvbt\nauto-modulation: auto"
        return "channelmap: us-bcast\nmodulation: 8vsb qam64 qam256\nauto-modulation: auto auto6t auto8t"

    @property
    def scan_map(self) -> str:
        return self.channelmap

    @property
    def bandwidth_hz(self) -> int:
        return self.dvb_bandwidth * 1000000 if self.is_dvbt else 6000000

    @property
    def half_bandwidth_hz(self) -> int:
        return self.bandwidth_hz // 2

    @property
    def start_frequency(self) -> int:
        return self.dvb_start_frequency if self.is_dvbt else 54000000

    @property
    def end_frequency(self) -> int:
        if self.is_dvbt:
            return self.frequency_for_physical_channel(self.dvb_last_channel)
        return 806000000

    def physical_channel_count(self) -> int:
        return max(1, self.last_physical_channel - self.first_physical_channel + 1)

    def frequency_for_physical_channel(self, physical: int) -> int:
        physical = int(physical)
        if not self.is_dvbt:
            return ATSC_FREQUENCIES.get(physical, 57000000)
        return (
            self.dvb_start_frequency
            + (physical - self.dvb_first_channel) * self.dvb_bandwidth * 1000000
        )

    def low_frequency_for_physical_channel(self, physical: int) -> int:
        return self.frequency_for_physical_channel(physical) - self.half_bandwidth_hz

    def high_frequency_for_physical_channel(self, physical: int) -> int:
        return self.frequency_for_physical_channel(physical) + self.half_bandwidth_hz

    def physical_for_frequency(self, frequency) -> Optional[int]:
        try:
            frequency = int(frequency)
        except (TypeError, ValueError):
            return None

        if not self.is_dvbt:
            for physical, center in ATSC_FREQUENCIES.items():
                if abs(center - frequency) <= ATSC_HALF_BANDWIDTH_HZ:
                    return physical
            if 54000000 <= frequency <= 806000000:
                physical = int((frequency - 54000000) // 6000000) + 2
                if 2 <= physical <= 69:
                    return physical
            return None

        if not (self.start_frequency - self.half_bandwidth_hz <= frequency <= self.end_frequency + self.half_bandwidth_hz):
            return None
        offset = frequency - self.dvb_start_frequency
        if offset < 0:
            return None
        physical = self.dvb_first_channel + int(
            (offset + self.half_bandwidth_hz) // self.bandwidth_hz
        )
        if physical < self.dvb_first_channel or physical > self.dvb_last_channel:
            return None
        if abs(self.frequency_for_physical_channel(physical) - frequency) > self.half_bandwidth_hz:
            return None
        return physical
