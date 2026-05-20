# Virtual HDHomeRun Proxy for IPTV and Windows Media Center

Virtual HDHomeRun Proxy presents an IPTV or HLS playlist as a SiliconDust-style HDHomeRun tuner. It is designed for Windows Media Center workflows, including older Vista-era systems, while also exposing standard HDHomeRun-compatible HTTP lineup endpoints for tools that can discover or consume network tuners.

The proxy reads channels from an M3U/M3U8 playlist, advertises a virtual tuner on the local network, serves HDHomeRun discovery and lineup metadata, and uses ffmpeg to transcode streams into an MPEG-TS shape that Windows Media Center can tune more reliably.

## What It Does

- Emulates an HDHomeRun tuner device with configurable device ID, model, firmware version, and tuner count.
- Serves HDHomeRun-style HTTP endpoints:
  - `/discover.json`
  - `/lineup.json`
  - `/lineup.xml`
  - `/lineup.m3u`
  - `/lineup_status.json`
  - `/device.xml`
  - `/stream/<channel>`
- Responds to SSDP discovery on UDP port `1900`.
- Responds to legacy HDHomeRun discovery and control traffic on port `65001`.
- Parses local or remote M3U/M3U8 playlists.
- Resolves relative playlist URLs for both local files and remote playlists.
- Detects HLS master playlists and chooses a playable variant when possible.
- Transcodes source streams through ffmpeg to MPEG-TS with MPEG-2 video and AC-3 audio by default.
- Generates a WMC/HDHRProxyIPTV-style mapping list at startup.
- Writes Windows registry defaults that help Windows Media Center identify the virtual tuner as a digital antenna source.

## Project Layout

```text
.
|-- main.py                         Main command-line entry point
|-- config.json                     Example runtime configuration
|-- requirements.txt                Runtime notes and optional dependencies
|-- playlists/
|   `-- channels.m3u                Example channel playlist
|-- hdhr_proxy/
|   |-- config.py                   Configuration defaults and loader
|   |-- discovery.py                SSDP, HDHomeRun discovery, and control server
|   |-- http_server.py              HDHomeRun HTTP API and stream routes
|   |-- m3u_parser.py               M3U/M3U8 parser and lineup builder
|   `-- streamer.py                 Direct stream and ffmpeg transcoding helpers
|-- run_proxy_wmc.bat               Simple Windows launcher
|-- run_proxy_vista_py36.bat        Vista/Python 3.6-oriented launcher
|-- add_virtual_wmc_tuners.ps1      Windows Media Center tuner helper
|-- fix_virtual_hdhr_registry.ps1   Registry helper for virtual HDHR setup
|-- prefer_virtual_wmc_tuners.ps1   WMC tuner preference helper
`-- register_virtual_bda_filters.ps1 BDA filter registration helper
```

Generated files such as `HDHRProxyIPTV_MappingList.generated.ini`, `hdhr_control_trace.log`, and `ffmpeg_tuner*.log` are runtime artifacts.

## Requirements

### Minimum

- Windows, Linux, or another OS with Python support for the HTTP proxy pieces.
- Python 3.6 or newer.
- A valid M3U or M3U8 IPTV/HLS playlist.

### Recommended for Windows Media Center

- Windows Media Center installed and working.
- Python 3.6.x for Vista-era compatibility.
- ffmpeg available either on `PATH` or configured in `config.json`.
- Administrator rights when binding discovery ports or importing registry data.

### Optional

- `pywin32` if you want to experiment with Windows service management commands.

The checked-in `requirements.txt` intentionally has no required runtime packages. The core proxy uses the Python standard library.

## Quick Start

1. Put your playlist in `playlists/channels.m3u`, or prepare a remote M3U URL.

2. Edit `config.json` if needed:

   ```json
   {
     "device_id": "104ABCDE",
     "device_name": "Virtual HDHR Proxy",
     "model_number": "HDHR4-2US",
     "firmware_version": "20140101",
     "tuner_count": 2,
     "http_port": 5004,
     "listen_ip": "0.0.0.0",
     "m3u_file": "playlists/channels.m3u",
     "m3u_url": null,
     "ffmpeg_path": "ffmpeg",
     "ffmpeg_enabled": true,
     "ffmpeg_output_codec": "mpeg2video",
     "ffmpeg_audio_codec": "ac3",
     "ffmpeg_bitrate": "4000k"
   }
   ```

3. Start the proxy:

   ```powershell
   python main.py --config config.json
   ```

4. Open the status endpoint in a browser:

   ```text
   http://<your-pc-ip>:5004/
   ```

5. Verify the lineup:

   ```text
   http://<your-pc-ip>:5004/lineup.json
   ```

6. Test a stream:

   ```text
   http://<your-pc-ip>:5004/stream/1.1
   ```

Use the actual guide number shown in `lineup.json`.

## Command-Line Usage

Run with a config file:

```powershell
python main.py --config config.json
```

Run with a local playlist:

```powershell
python main.py --m3u-file playlists/channels.m3u --port 5004
```

Run with a remote playlist:

```powershell
python main.py --m3u-url "https://example.com/playlist.m3u8" --port 5004
```

Run a saved local HLS master playlist while resolving relative URLs against the original web URL:

```powershell
python main.py --m3u-file playlists/master.m3u8 --hls-base-url "https://provider.example/live/master.m3u8"
```

Disable ffmpeg transcoding:

```powershell
python main.py --config config.json --no-ffmpeg
```

Set a specific ffmpeg path:

```powershell
python main.py --config config.json --ffmpeg "C:\Tools\ffmpeg\bin\ffmpeg.exe"
```

Increase the virtual tuner count:

```powershell
python main.py --config config.json --tuners 4
```

## Configuration Reference

| Key | Description | Default |
| --- | --- | --- |
| `device_id` | 8-character HDHomeRun-style hexadecimal device ID. Invalid IDs are normalized at runtime. | `104FFFFF` |
| `device_name` | Friendly device name shown to clients. | `Virtual HDHR Proxy` |
| `model_number` | Advertised HDHomeRun model. | `HDHR4-2US` |
| `firmware_version` | Advertised firmware version. | `20140101` |
| `tuner_count` | Number of simultaneous virtual tuners/streams allowed. | `2` |
| `http_port` | HTTP API and stream port. | `5004` |
| `listen_ip` | Bind address. Use `0.0.0.0` for all interfaces. | `0.0.0.0` |
| `m3u_file` | Path to a local M3U/M3U8 playlist. | `null` |
| `m3u_url` | URL to a remote M3U/M3U8 playlist. | `null` |
| `hls_base_url` | Original URL for a saved local HLS playlist with relative variant paths. | `null` |
| `xmltv_file` | Reserved for XMLTV data. | `null` |
| `xmltv_url` | Reserved for XMLTV data. | `null` |
| `ffmpeg_path` | Path or command name for ffmpeg. | `ffmpeg` |
| `ffmpeg_enabled` | Enables ffmpeg transcoding. | `true` |
| `ffmpeg_output_codec` | Video codec used by HTTP stream transcoding. | `mpeg2video` |
| `ffmpeg_audio_codec` | Audio codec used by HTTP stream transcoding. | `ac3` |
| `ffmpeg_bitrate` | Video bitrate passed to ffmpeg. | `4000k` |
| `udp_bind_ip` | Reserved for UDP stream handling. | `0.0.0.0` |
| `fallback_channel_duration` | Reserved fallback duration value. | `60` |
| `channel_mapping` | Optional mapping from channel names to guide numbers. | `{}` |
| `lineup_source` | Advertised lineup source. The proxy forces this to `Antenna` for WMC behavior. | `Antenna` |

## Playlist Format

A normal IPTV playlist looks like this:

```m3u
#EXTM3U
#EXTINF:-1 tvg-id="1" tvg-chno="1.1" tvg-name="Channel One" group-title="News",Channel One
http://example.com/stream/ch1.ts
#EXTINF:-1 tvg-id="2" tvg-chno="2.1" tvg-name="Channel Two" group-title="Entertainment",Channel Two
http://example.com/stream/ch2.ts
```

The proxy uses:

- `tvg-chno` as the HDHomeRun guide number when present.
- The display name after the comma as the channel name.
- The following non-comment line as the stream URL.

If `tvg-chno` is missing, the proxy assigns guide numbers automatically, starting at `2.1`.

You can also override guide numbers by channel name with `channel_mapping`:

```json
{
  "channel_mapping": {
    "Channel One": "101.1",
    "Movie Channel": "202.1"
  }
}
```

## HTTP API

### Root Status

```text
GET /
```

Returns a JSON status summary with device name, model, firmware version, device ID, tuner count, base URL, lineup URL, and channel count.

### Discovery JSON

```text
GET /discover.json
```

Returns HDHomeRun discovery metadata including `FriendlyName`, `ModelNumber`, `DeviceID`, `TunerCount`, `BaseURL`, and `LineupURL`.

### Lineup JSON

```text
GET /lineup.json
```

Returns the HDHomeRun lineup array. Each channel includes fields such as `GuideNumber`, `GuideName`, `URL`, modulation, frequency, physical channel, program number, and signal values.

### Lineup XML

```text
GET /lineup.xml
```

Returns a simple XML version of the lineup.

### Lineup M3U

```text
GET /lineup.m3u
```

Returns a generated M3U playlist where each entry points back to this proxy's `/stream/<channel>` endpoint.

### Lineup Status

```text
GET /lineup_status.json
```

Returns a simple scan/status response for HDHomeRun-compatible clients.

### Device XML

```text
GET /device.xml
```

Returns UPnP-style device metadata.

### Channel Stream

```text
GET /stream/<GuideNumber>
```

Starts a channel stream. For example:

```text
GET /stream/1.1
```

The HTTP stream response is sent as `video/mpeg` using chunked transfer encoding.

## HDHomeRun Discovery and Control

The proxy starts three discovery/control listeners:

- SSDP discovery on UDP `1900`.
- Legacy HDHomeRun binary discovery on UDP `65001`.
- HDHomeRun control socket on TCP `65001`.

These ports are important for Windows Media Center and HDHomeRun utilities. On Windows, binding UDP `1900` may require Administrator privileges or may conflict with the built-in SSDP Discovery service.

## Windows Media Center Notes

This project is tuned for Windows Media Center-style discovery and playback:

- It advertises itself as a SiliconDust tuner.
- It reports a digital antenna source.
- It creates ATSC-style channel metadata and broadcast frequencies.
- It can write registry values for SiliconDust tuner source defaults.
- It uses MPEG-2 video, AC-3 audio, and MPEG-TS output by default.
- It includes helper PowerShell scripts for WMC and registry setup.

When the proxy starts on Windows, it tries to write HDHomeRun tuner defaults under:

```text
HKLM\SOFTWARE\Silicondust\HDHomeRun\Tuners
HKLM\SOFTWARE\WOW6432Node\Silicondust\HDHomeRun\Tuners
```

If it cannot write HKLM values, it writes a `.reg` file such as:

```text
hdhr_wmc_<device_id>.reg
```

Import that file from an elevated prompt or by using Registry Editor as Administrator.

## ffmpeg Behavior

ffmpeg is used to turn a wide range of IPTV/HLS sources into a WMC-friendly MPEG-TS stream. The default HTTP streaming command targets:

- MPEG-2 video
- AC-3 stereo audio
- 1280x720 output
- 29.97 fps
- MPEG-TS container
- Repeated headers/PAT/PMT data
- Network reconnection flags for remote streams

For the HDHomeRun control path, the proxy can also start ffmpeg-backed UDP output when a client sets tuner target variables.

If ffmpeg is not found, set `ffmpeg_path` in `config.json` to the full executable path.

## Generated Mapping List

At startup, the proxy writes:

```text
HDHRProxyIPTV_MappingList.generated.ini
```

This file contains channel number, frequency, URL, signal, network rate, and program table data derived from the active M3U lineup.

## Running on Vista-Era Systems

The code avoids mandatory third-party runtime dependencies so it can run on older Python environments. For Vista-era systems:

1. Use Python 3.6.x.
2. Keep `ffmpeg.exe` beside the proxy or set `ffmpeg_path`.
3. Use `run_proxy_vista_py36.bat` as a starting point.
4. Avoid installing optional packages unless you know compatible wheels are available.

## Troubleshooting

### The proxy starts, but clients cannot discover it

- Run the proxy as Administrator.
- Check whether another service is already using UDP `1900`.
- Confirm Windows Firewall allows Python on private networks.
- Make sure the client and proxy are on the same subnet.
- Try opening `http://<proxy-ip>:5004/discover.json` from another machine.

### Windows Media Center does not see the tuner

- Confirm the proxy logs show SSDP and HDHomeRun listeners starting.
- Import the generated `hdhr_wmc_<device_id>.reg` file as Administrator.
- Make sure the advertised `DeviceID` is stable between runs.
- Try using an HDHomeRun utility to scan for tuners.
- Restart Windows Media Center services after registry changes.

### Streams fail or stop quickly

- Test the source URL in VLC or ffmpeg directly.
- Confirm `ffmpeg_path` points to a working executable.
- Inspect `ffmpeg_tuner*.log` files when using the HDHomeRun control path.
- Lower `ffmpeg_bitrate` if the network or host CPU is struggling.
- Try a remote M3U URL instead of a locally saved HLS master playlist when variants or segments use relative paths.

### Channel numbers are not what you expect

- Add `tvg-chno` values to the playlist.
- Or use `channel_mapping` in `config.json`.
- Restart the proxy so the lineup and generated mapping list are rebuilt.

### Port 65001 is already in use

HDHomeRun legacy discovery/control uses port `65001`. Stop the conflicting service or run on a machine where that port is available. Some HDHomeRun tools expect this exact port.

## Development

There are currently no required package installs for the core runtime.

Run a basic syntax check:

```powershell
python -m compileall main.py hdhr_proxy
```

Start with the sample playlist:

```powershell
python main.py --config config.json
```

Then visit:

```text
http://127.0.0.1:5004/
http://127.0.0.1:5004/lineup.json
```

## Security Notes

This proxy is intended for trusted local networks. It does not implement authentication, authorization, TLS, playlist sandboxing, or stream URL filtering. Do not expose it directly to the public internet.

## License

No project license file is currently included. Add a license before distributing or publishing the project broadly.