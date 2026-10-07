"""Live end-to-end validation after replacing the swapped-in ffmpeg 3.2.2 binaries.

Builds the real UDP-bridge ffmpeg command via DiscoveryServer._build_udp_ffmpeg_cmd
and actually executes it against a locally served HLS source, asserting:
  * path resolution picks the bundled (now 9.x) build, not the 3.2.2 backup folder
  * the exact input-option set that used to crash with
    "Unrecognized option 'allowed_extensions'" now parses
  * a real transcode (libx264 + ac3 -> mpegts) produces non-zero output
"""

import http.server
import os
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hdhr_proxy.discovery import DiscoveryServer

BUNDLED_DIR = "ffmpeg\\ffmpeg-2026-05-18-git-b4d11dffbf-essentials_build"
PORT = 8811

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


server = DiscoveryServer(
    device_id="104ABCDE",
    base_url="http://127.0.0.1:5004",
    tuner_count=2,
    ffmpeg_path="ffmpeg",  # the config.json value: exercises the directory walk
    ffmpeg_enabled=True,
    output_codec="libx264",
    audio_codec="ac3",
    bitrate="4500k",
)

# --- 1. resolver must pick the bundled build even with the 3.2.2 backup present ---
resolved = server._resolve_ffmpeg_path("ffmpeg")
check(
    "resolver picks bundled 2026 build dir (not 3.2.2 backup)",
    BUNDLED_DIR.lower() in resolved.replace("/", "\\").lower(),
    resolved,
)
vers = subprocess.run([resolved, "-version"], capture_output=True)
check("resolved binary reports ffmpeg 9.x", b"version 9." in vers.stdout)

# --- 2. generate a local HLS source with the new binary ---
tmp = tempfile.mkdtemp(prefix="hdhr_live_")
seg = os.path.join(tmp, "live0.ts")
gen = subprocess.run(
    [
        resolved, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "6",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-c:a", "aac",
        "-f", "mpegts", seg,
    ],
    capture_output=True,
)
check(
    "local segment generated (decode+encode works)",
    gen.returncode == 0 and os.path.exists(seg) and os.path.getsize(seg) > 100_000,
    f"rc={gen.returncode} {gen.stderr.decode(errors='replace')[-300:]}",
)

with open(os.path.join(tmp, "live0.m3u8"), "w", encoding="utf-8", newline="\n") as f:
    f.write(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:7\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:6.0,\nlive0.ts\n#EXT-X-ENDLIST\n"
    )
master = os.path.join(tmp, "master.m3u8")
with open(master, "w", encoding="utf-8", newline="\n") as f:
    f.write(
        "#EXTM3U\n#EXT-X-VERSION:5\n"
        "#HDHR-PROXY-VIDEO-MAP:0:v:0?\n#HDHR-PROXY-AUDIO-MAP:0:a:0?\n"
        "#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=3000000\n"
        f"http://127.0.0.1:{PORT}/live0.m3u8\n"
    )


# --- 3. tiny keep-alive HTTP server for the variant playlist + segment ---
class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=tmp, **kwargs)

    def guess_type(self, path):
        if path.endswith(".m3u8"):
            return "application/vnd.apple.mpegurl"
        if path.endswith(".ts"):
            return "video/mp2t"
        return super().guess_type(path)

    def log_message(self, *args):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# --- 4. build the exact command the UDP bridge runs ---
cmd = server._build_udp_ffmpeg_cmd(master, rf={"program": 3, "physical": 1, "name": "H2303"})
check("cmd contains -allowed_extensions", "-allowed_extensions" in cmd)
check("cmd output target is pipe:1", cmd[-1] == "pipe:1", cmd[-1])
print("cmd:", " ".join(cmd))

# --- 5. execute it (stdout goes to a file instead of the bridge pipe) ---
out = os.path.join(tmp, "out.ts")
try:
    proc = subprocess.run(cmd[:-1] + [out], capture_output=True, timeout=120)
    err = proc.stderr.decode(errors="replace")
    check("ffmpeg exited 0", proc.returncode == 0, f"rc={proc.returncode} tail: {err[-400:]}")
except subprocess.TimeoutExpired:
    check("ffmpeg exited 0", False, "timeout after 120s")
    err = ""
check("no 'Unrecognized option' / 'Option not found'", "Unrecognized option" not in err and "Option not found" not in err)
check(
    "output stream bytes > 100KB",
    os.path.exists(out) and os.path.getsize(out) > 100_000,
    str(os.path.getsize(out) if os.path.exists(out) else 0),
)

ffprobe = os.path.join(os.path.dirname(resolved), "ffprobe.exe")
if os.path.exists(out):
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=format_name,duration:stream=codec_name",
         "-of", "default=nw=1", out],
        capture_output=True,
    )
    print("probe of transcode output:\n" + probe.stdout.decode(errors="replace").strip())

httpd.shutdown()

print("\nRESULT:", "OK" if all(results) else "FAIL")
sys.exit(0 if all(results) else 1)
