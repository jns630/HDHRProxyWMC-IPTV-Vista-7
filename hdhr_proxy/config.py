import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "device_id": "104FFFFF",
    "device_name": "Virtual HDHR Proxy",
    "model_number": "HDHR4-2US",
    "firmware_version": "20140101",
    "tuner_count": 4,
    "http_port": 5004,
    "listen_ip": "0.0.0.0",
    "m3u_url": None,
    "m3u_file": None,
    "hls_base_url": None,
    "xmltv_file": None,
    "xmltv_url": None,
    "mxf_file": "guide.mxf",
    "auto_match_mxf_file": "HDHRProxyWMC_AutoMatch.generated.mxf",
    "write_mxf": False,
    "import_mxf": False,
    "write_auto_match_mxf": False,
    "import_auto_match_mxf": False,
    "map_guide_wmc": False,
    "guide_only_lineup": False,
    "force_vista_mode": False,
    "programs_per_physical": None,
    "max_physical_channel": 59,
    "ffmpeg_path": "ffmpeg",
    "ffmpeg_enabled": True,
    "ffmpeg_output_codec": "mpeg2video",
    "ffmpeg_audio_codec": "ac3",
    "ffmpeg_bitrate": "4000k",
    "udp_bind_ip": "0.0.0.0",
    "fallback_channel_duration": 60,
    "channel_mapping": {},
    "lineup_source": "Antenna",
    "config_file": None,
}

# Keep validation in one place so a typo or impossible value produces a clear
# message instead of a traceback deep inside the socket or ffmpeg setup code.
_INT_SETTINGS = {
    "tuner_count": (1, 255),
    "http_port": (1, 65535),
    "max_physical_channel": (2, 69),
    "fallback_channel_duration": (1, 86400),
}
_OPTIONAL_INT_SETTINGS = {
    "programs_per_physical": (1, 252),
}
_BOOL_SETTINGS = (
    "write_mxf",
    "import_mxf",
    "write_auto_match_mxf",
    "import_auto_match_mxf",
    "map_guide_wmc",
    "guide_only_lineup",
    "force_vista_mode",
    "ffmpeg_enabled",
)


class ConfigError(Exception):
    """Raised when a configuration file is missing, malformed, or invalid."""


def _coerce_bool(key: str, value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    raise ConfigError("Setting %r must be true or false (got %r)." % (key, value))


def _coerce_int(key: str, value, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ConfigError("Setting %r must be an integer (got %r)." % (key, value))
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError("Setting %r must be an integer (got %r)." % (key, value))
    if not minimum <= number <= maximum:
        raise ConfigError(
            "Setting %r must be between %s and %s (got %s)."
            % (key, minimum, maximum, number)
        )
    return number


class Config:
    def __init__(self, config_path: Optional[str] = None):
        self._data = dict(DEFAULT_CONFIG)
        if config_path:
            user_cfg = self._load_file(config_path)
            self._data["config_file"] = config_path
            unknown = sorted(set(user_cfg) - set(DEFAULT_CONFIG))
            for key in unknown:
                logger.warning("Ignoring unknown configuration setting %r.", key)
            self._data.update(user_cfg)
        self._validate()

    @staticmethod
    def _load_file(config_path: str) -> dict:
        if not os.path.exists(config_path):
            raise ConfigError("Configuration file not found: %s" % config_path)
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
        except ValueError as exc:
            raise ConfigError("Configuration file %s is not valid JSON: %s" % (config_path, exc))
        except OSError as exc:
            raise ConfigError("Unable to read configuration file %s: %s" % (config_path, exc))
        if not isinstance(user_cfg, dict):
            raise ConfigError(
                "Configuration file %s must contain a JSON object at the top level." % config_path
            )
        return user_cfg

    def _validate(self):
        for key, (minimum, maximum) in _INT_SETTINGS.items():
            self._data[key] = _coerce_int(key, self._data.get(key), minimum, maximum)
        for key, (minimum, maximum) in _OPTIONAL_INT_SETTINGS.items():
            value = self._data.get(key)
            if value is None or value == "":
                self._data[key] = None
            else:
                self._data[key] = _coerce_int(key, value, minimum, maximum)
        for key in _BOOL_SETTINGS:
            self._data[key] = _coerce_bool(key, self._data.get(key))

        for key in ("device_id", "device_name", "model_number", "firmware_version", "listen_ip"):
            value = self._data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError("Setting %r must be a non-empty string." % key)
            self._data[key] = value.strip()

        for key in ("m3u_file", "m3u_url", "hls_base_url", "xmltv_file", "xmltv_url", "ffmpeg_path"):
            value = self._data.get(key)
            if value is not None and not isinstance(value, str):
                raise ConfigError("Setting %r must be a string or null." % key)

        mapping = self._data.get("channel_mapping")
        if mapping is None:
            self._data["channel_mapping"] = {}
        elif not isinstance(mapping, dict):
            raise ConfigError("Setting 'channel_mapping' must be a JSON object.")

    def __getattr__(self, name):
        if name.startswith("_"):
            return super().__getattribute__(name)
        if name in self._data:
            return self._data[name]
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._data[name] = value

    def validate(self):
        """Validate the current values, raising ConfigError on invalid settings."""
        self._validate()

    def get(self, key, default=None):
        return self._data.get(key, default)

    @property
    def as_dict(self):
        return dict(self._data)

    @property
    def base_url(self):
        return f"http://{self.listen_ip}:{self.http_port}"

    @property
    def lineup_url(self):
        return f"{self.base_url}/lineup.json"
