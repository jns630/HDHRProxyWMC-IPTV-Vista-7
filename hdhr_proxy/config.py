import os
import json
from typing import Optional

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
    "ffmpeg_path": r"D:\WMC_EPG\New folder (4)\hdhr_proxy\ffmpeg\ffmpeg-2026-05-18-git-b4d11dffbf-essentials_build\bin\ffmpeg.exe",
    "ffmpeg_enabled": True,
    "ffmpeg_output_codec": "mpeg2video",
    "ffmpeg_audio_codec": "ac3",
    "ffmpeg_bitrate": "4000k",
    "udp_bind_ip": "0.0.0.0",
    "fallback_channel_duration": 60,
    "channel_mapping": {},
    "lineup_source": "Antenna",
}


class Config:
    def __init__(self, config_path: Optional[str] = None):
        self._data = dict(DEFAULT_CONFIG)
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            self._data.update(user_cfg)

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
