# Virtual HDHomeRun Proxy for IPTV and Windows Media Center

Virtual HDHomeRun Proxy presents an IPTV or HLS playlist as a SiliconDust-style HDHomeRun tuner. It is designed for Windows Media Center workflows, including older Vista-era systems, while also exposing standard HDHomeRun-compatible HTTP lineup endpoints for tools that can discover or consume network tuners.

The proxy reads channels from an M3U/M3U8 playlist, advertises a virtual tuner on the local network, serves HDHomeRun discovery and lineup metadata, and uses ffmpeg to transcode streams into an MPEG-TS shape that Windows Media Center can tune more reliably.

## Demo

[![Windows Media Center demo on Vista](assets/demo/windows-vista-wmc-demo.jpg)](https://jns630.github.io/HDHRProxyWMC-IPTV-Vista-7/)

[Open the HTML5 demo player](https://jns630.github.io/HDHRProxyWMC-IPTV-Vista-7/) or [watch the MP4 directly](assets/demo/windows-vista-wmc-demo.mp4).

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
- Transcodes source streams through ffmpeg to WMC-friendly MPEG-TS, using MPEG-2 video on Vista and H.264/MPEG-4 AVC on Windows 7 or newer.
- Generates a WMC/HDHRProxyIPTV-style mapping list at startup.
- Writes Windows registry defaults that help Windows Media Center identify the virtual tuner as a digital antenna source.

## How It Works

At a high level, the proxy turns an IPTV playlist into something that looks and behaves like a SiliconDust HDHomeRun tuner:

1. It loads channels from a local M3U file or a remote M3U URL.
2. It builds a virtual broadcast lineup with guide numbers, physical channels, program numbers, and MPEG-TS PID assignments.
3. It advertises that lineup over HDHomeRun discovery endpoints and sockets so Windows Media Center and other clients can find it.
4. When a client tunes a channel, the proxy picks the right source URL, optionally resolves HLS master playlists to a playable media variant, and starts ffmpeg.
5. ffmpeg repackages the source into a WMC-friendly MPEG-TS stream with stable PAT/PMT headers, AC-3 audio, and the correct video codec for the host OS.
6. While WMC is still deciding which virtual subchannel it wants, the proxy can hold tuner lock and send ATSC PSIP metadata so Media Center keeps the tune alive long enough to finish selection.

The result is that ordinary IPTV sources can behave much more like a real ATSC tuner from WMC's point of view.

## WMC Tune Flow

Windows Media Center does not always tune in one clean step. The proxy is built around that behavior:

- WMC often starts by tuning a scan-style physical frequency such as `auto6t:743000000`.
- It may then request a large basket of PMT PIDs covering many virtual subchannels on that RF.
- The proxy does not immediately start the first channel it sees, because that can launch the wrong service and produce black screen or the wrong station.
- Instead, it keeps tuner lock, sends PSIP tables, and waits for a more specific filter update.
- Once WMC asks for the actual A/V PIDs, the proxy locks to the intended virtual program and starts playback for that exact subchannel.

This behavior is especially important for large generated lineups where many channels share one virtual RF group.

## Recent Playback Behavior

The current build includes a few practical WMC-focused fixes:

- Vista uses MPEG-2 video for WMC compatibility.
- Windows 7 and newer use H.264 / MPEG-4 AVC for WMC playback.
- Use `--vista` to force the Vista MPEG-2 WMC profile on newer systems for client testing.
- HLS master playlists are resolved to a concrete media playlist before ffmpeg starts.
- PMT and A/V PID requests are used to choose the correct virtual channel during WMC playback.
- Broad PMT-only filter requests are deferred until WMC provides enough information to identify the intended program.
- During that deferred window, the proxy keeps lock and emits PSIP instead of dropping to `no signal`.

These fixes were added specifically to prevent wrong-channel playback, black screens, and premature `No TV Signal` failures during WMC tuning.

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
     "tuner_count": 4,
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
| `tuner_count` | Number of simultaneous virtual tuners/streams allowed. | `4` |
| `http_port` | HTTP API and stream port. | `5004` |
| `listen_ip` | Bind address. Use `0.0.0.0` for all interfaces. | `0.0.0.0` |
| `m3u_file` | Path to a local M3U/M3U8 playlist. | `null` |
| `m3u_url` | URL to a remote M3U/M3U8 playlist. | `null` |
| `hls_base_url` | Original URL for a saved local HLS playlist with relative variant paths. | `null` |
| `xmltv_file` | Path to a local XMLTV guide file to serve from the proxy. | `null` |
| `xmltv_url` | URL to a remote XMLTV guide file to fetch and serve from the proxy. | `null` |
| `ffmpeg_path` | Path or command name for ffmpeg. | `ffmpeg` |
| `ffmpeg_enabled` | Enables ffmpeg transcoding. | `true` |
| `ffmpeg_output_codec` | Configured video codec. At runtime, WMC mode forces Vista to `mpeg2video` and Windows 7 or newer to `libx264` H.264/MPEG-4 AVC. | `mpeg2video` |
| `ffmpeg_audio_codec` | Audio codec used by HTTP stream transcoding. | `ac3` |
| `ffmpeg_bitrate` | Video bitrate passed to ffmpeg. | `4000k` |
| `udp_bind_ip` | Reserved for UDP stream handling. | `0.0.0.0` |
| `fallback_channel_duration` | Reserved fallback duration value. | `60` |
| `channel_mapping` | Optional mapping from channel names to guide numbers. | `{}` |
| `lineup_source` | Advertised lineup source. The proxy forces this to `Antenna` for WMC behavior. | `Antenna` |

## Playlist Format

## XMLTV / EPG

The proxy can now load XMLTV guide data from either a local file or a remote URL and serve it back out at:

- `/xmltv.xml`
- `/epg.xml`

Examples:

```powershell
python main.py --m3u-url "https://raw.githubusercontent.com/OwnerPlugins/pluto-tv-m3u/refs/heads/main/pluto-live-US.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml"
```

```powershell
python main.py --m3u-file "us_pluto.m3u" --xmltv-file "guide.xml"
```

Notes:

- If playlist entries include `tvg-id`, the proxy filters the XMLTV output down to just the matching channels and programmes.
- If the playlist does not include `tvg-id`, the proxy serves the full XMLTV file unchanged.
- The served guide URL is `http://<your-pc-ip>:5004/xmltv.xml`.

### Generate and import WMC MXF

The proxy can also convert XMLTV into a Windows Media Center MXF file and optionally import it with `loadmxf.exe`.

When `--vista` is enabled, MXF generation switches to a Vista-oriented guide shape with OTA-style channel matching fields instead of the default Windows 7+ style.

Generate `guide.mxf`:

```powershell
python main.py --m3u-url "https://raw.githubusercontent.com/OwnerPlugins/pluto-tv-m3u/refs/heads/main/pluto-live-US.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --write-mxf
```

Generate and import into WMC:

```powershell
python main.py --m3u-url "https://raw.githubusercontent.com/OwnerPlugins/pluto-tv-m3u/refs/heads/main/pluto-live-US.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --import-mxf
```

Custom MXF path:

```powershell
python main.py --m3u-file "us_pluto.m3u" --xmltv-file "guide.xml" --mxf-file "C:\\Temp\\pluto-guide.mxf" --import-mxf
```

The import step uses:

```powershell
C:\Windows\ehome\loadmxf.exe -v -i guide.mxf
```

On this machine I verified that `loadmxf.exe` accepts the generated MXF structure in a test store.

### WMC guide match utility

Whenever XMLTV is loaded, the proxy now also writes two helper files for Windows Media Center channel attachment:

- `HDHRProxyWMC_GuideMatch.generated.csv`
- `HDHRProxyWMC_GuideOnly.generated.ini`
- `HDHRProxyWMC_AutoMatch.generated.mxf`

What they do:

- The CSV shows the current lineup channel, guide number, call sign, matched XMLTV id, and listing count.
- The guide-only INI contains just the channels that actually matched guide data.
- The auto-match MXF is a filtered guide import file that uses the current lineup's channel numbers for WMC auto-attachment.

This is meant for the WMC flow after scanning. The order matters because WMC has to create its scanned tuner lineup before the guide mapper can attach listings to it:

1. Start the proxy with your M3U playlist and scan the virtual tuner channels in WMC first.
2. Close WMC after the scan finishes so the guide database is not being held open by the UI.
3. Re-run the proxy with the same M3U/XMLTV inputs plus `--map-guide-wmc`.
4. The mapper generates an EPG123-compatible auto-match MXF, imports it, subscribes the imported guide lineup to the scanned WMC tuner lineup, and triggers the WMC reindex task.
5. Reopen WMC and check the Guide.

The actual command-line flag is `--map-guide-wmc`.

You can generate and import the auto-match MXF directly:

```powershell
python main.py --m3u-file "us_pluto.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --import-auto-match-mxf
```

Or with the EXE:

```powershell
.\dist\HDHRProxyWMC-IPTV.exe --m3u-file "us_pluto.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --import-auto-match-mxf
```

With `--vista`, the auto-match MXF also switches to the Vista-oriented MXF shape before import.

After the scan exists inside WMC, run the one-shot utility that maps guide data into the WMC internal database for the current lineup:

```powershell
python main.py --m3u-file "us_pluto.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --map-guide-wmc
```

Or:

```powershell
.\dist\HDHRProxyWMC-IPTV.exe --m3u-file "us_pluto.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --map-guide-wmc
```

For Vista testing, add `--vista` to the same command:

```powershell
.\dist\HDHRProxyWMC-IPTV.exe --m3u-file "us_pluto.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --map-guide-wmc --vista
```

`--map-guide-wmc` uses EPG123 when it is installed, because EPG123 knows how to activate and auto-map imported guide lineups against WMC's scanned tuner channels. If EPG123 is not installed, the proxy falls back to `loadmxf.exe`, but that fallback can import listings without fully attaching them to the scanned channels.

### Guide-only WMC scan mode

If you want WMC to scan only channels that actually matched the guide, start the proxy with:

```powershell
python main.py --m3u-file "us_pluto.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --guide-only-lineup
```

Or with the EXE:

```powershell
.\dist\HDHRProxyWMC-IPTV.exe --m3u-file "us_pluto.m3u" --xmltv-url "https://i.mjh.nz/PlutoTV/us.xml" --guide-only-lineup
```

In this mode, the proxy only advertises channels that matched XMLTV/MXF guide data, so the WMC scan result is already limited to guide-backed channels.

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
- It packs large M3U playlists into adaptive scanned RF subchannels based on the parsed channel count, supporting up to 1024 generated channels.
- It can write registry values for SiliconDust tuner source defaults.
- It keeps Vista on MPEG-2 video for compatibility, and switches Windows 7 or newer to H.264/MPEG-4 AVC video for WMC playback.
- It uses AC-3 stereo audio and MPEG-TS output for WMC streams.
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

ffmpeg is used to turn a wide range of IPTV/HLS sources into a WMC-friendly MPEG-TS stream. The proxy applies an OS-aware video codec policy at startup:

- Windows Vista / Windows version `6.0`: MPEG-2 video (`mpeg2video`)
- Windows 7 or newer / Windows version `6.1+`: H.264/MPEG-4 AVC video (`libx264`)
- AC-3 stereo audio
- 1280x720 output

For Vista-specific client testing on a newer machine, you can force the Vista WMC profile:

```powershell
python main.py --m3u-file "us_pluto.m3u" --vista
```
- 29.97 fps
- MPEG-TS container
- Repeated headers/PAT/PMT data
- Network reconnection flags for remote streams

For the HDHomeRun control path, the proxy also labels the MPEG-TS program map correctly for the selected video codec: MPEG-2 streams use PMT stream type `0x02`, while H.264/MPEG-4 AVC streams use `0x1B`.

For remote HLS playback, the proxy also tries to open the playlist first and, when it detects a master playlist, selects a playable media variant before handing the URL to ffmpeg. This avoids a common WMC black-screen case where ffmpeg opens the master but never reaches a usable stream quickly enough.

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
