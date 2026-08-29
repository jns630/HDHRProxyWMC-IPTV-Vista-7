#!/usr/bin/env python3
"""Ad-hoc verification for the WMC recording black-screen fix.

1. Unit-tests _mpegts_burst_has_pes_start (null/PAT/PMT padding vs real PES).
2. Drives the real DiscoveryServer._udp_bridge_from_ffmpeg against a fake
   ffmpeg process that emits real content, then stalls into pure null
   padding (the exact black-recording failure seen in ffmpeg_tuner0.log).
   The new content-starvation watchdog must terminate the stalled process.
3. Sanity check: a healthy stream that keeps producing PES must NOT be
   terminated.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hdhr_proxy.discovery as discovery
from hdhr_proxy.discovery import (
    DiscoveryServer,
    FFMPEG_CONTENT_STALL_SECONDS,
    FFMPEG_FIRST_CONTENT_TIMEOUT_SECONDS,
    TS_PACKET_SIZE,
    _mpegts_burst_has_pes_start,
)

BURST = 5264  # 4 x 1316; what the bridge reads per iteration


def ts_packet(pid: int, payload: bytes, pusi: bool = False, af: bytes = b"") -> bytes:
    assert len(payload) + len(af) <= 184
    header = bytearray(4)
    header[0] = 0x47
    header[1] = (0x40 if pusi else 0x00) | ((pid >> 8) & 0x1F)
    header[2] = pid & 0xFF
    header[3] = 0x30 if af else 0x10
    body = bytes(af) + payload
    return bytes(header) + body + b"\xff" * (184 - len(body))


def null_packet() -> bytes:
    return ts_packet(0x1FFF, b"\xff" * 184)


def make_burst(packets) -> bytes:
    raw = b"".join(packets)
    assert len(raw) % 188 == 0
    return raw


def content_burst() -> bytes:
    """A CBR-muxer-style burst: mostly nulls plus a video PES start (PID 0x41)
    and an audio PES start (PID 0x51) with a PCR adaptation field on video."""
    video = ts_packet(
        0x41,
        b"\x00\x00\x01\xe0" + b"\x80\x00\x00" + b"\x07\x10\x00\x01\x65\x4e" + b"\x00" * 150,
        pusi=True,
        af=b"\x07\x42\x00\x24\x9f\xf0\x00\x00",
    )
    audio = ts_packet(0x51, b"\x00\x00\x01\xbd" + b"\x07\x81\x00\x01" + b"\x00" * 160, pusi=True)
    pmt = ts_packet(0x31, b"\x02" + b"\xb0" + b"\x1d" + b"\x00\x03" + b"\x00" * 20, pusi=True)
    packets = [null_packet() for _ in range(24)] + [pmt, video, null_packet(), audio]
    packets += [null_packet() for _ in range(28 - len(packets))]
    return make_burst(packets)


def padding_burst(count=BURST // TS_PACKET_SIZE) -> bytes:
    """Pure CBR null padding + occasional PSI, i.e. what the mux emits while
    the upstream source is stalled."""
    packets = []
    for i in range(count):
        if i == 13:  # PAT keeps flowing during stalls
            packets.append(ts_packet(0x0000, b"\x00" + b"\xb0\x0d\x00\x01" + b"\xc1\x00\x00" + b"\x00\x01\xc1\x00\x00", pusi=True))
        else:
            packets.append(null_packet())
    return make_burst(packets)


class FakeStdout:
    """Acts like proc.stdout: serves queued content bursts, then pads with
    null packets forever (a stalled-but-alive CBR muxer)."""

    def __init__(self, content_bursts):
        self._chunks = list(content_bursts)
        self._closed = threading.Event()
        self._lock = threading.Lock()

    def read(self, n):
        while True:
            with self._lock:
                if self._closed.is_set():
                    return b""
                if self._chunks:
                    return self._chunks.pop(0)
            time.sleep(0.005)
            return padding_burst()

    def close(self):
        self._closed.set()

    def force_close(self):
        self._closed.set()


class FakeProc:
    def __init__(self, content_bursts):
        self.stdout = FakeStdout(content_bursts)
        self.terminated = False

    def poll(self):
        return None  # ffmpeg "running"

    def terminate(self):
        self.terminated = True
        self.stdout.force_close()  # unblock the bridge reader


class FakeServer:
    """Duck-typed stand-in for DiscoveryServer with small buffers."""

    def __init__(self):
        self.force_vista_mode = False
        self.unexpected_exit = None

    def _should_smooth_segmented_input(self, use_rtp, source_url):
        return True

    def _udp_bridge_buffer_seconds(self, use_rtp, source_url):
        return 0.05, 1.0

    def _wrap_rtp_mpegts(self, chunk, seq, ts, ssrc):
        return chunk

    def _notify_stream_bytes(self, tuner_idx):
        pass

    def _handle_unexpected_stream_exit(self, tuner_idx, proc, stop_event, bytes_sent, elapsed):
        self.unexpected_exit = (bytes_sent, elapsed)


def run_bridge(proc):
    stop_event = threading.Event()
    log_io = open(os.devnull, "wb", buffering=0)
    server = FakeServer()
    thread = threading.Thread(
        target=DiscoveryServer._udp_bridge_from_ffmpeg,
        args=(server, proc, ("127.0.0.1", 59999), 4000000, True, stop_event, log_io, 0, "https://x/stall-test.m3u8"),
        daemon=True,
    )
    thread.start()
    return server, stop_event, thread

def test_pes_detector():
    video = ts_packet(0x41, b"\x00\x00\x01\xe0" + b"\x00" * 100, pusi=True)
    audio = ts_packet(0x51, b"\x00\x00\x01\xbd" + b"\x00" * 100, pusi=True)
    pat = ts_packet(0x0000, b"\x00\xb0\x0d\x00\x01\xc1\x00\x00", pusi=True)
    pmt = ts_packet(0x31, b"\x02\xb0\x1d\x00\x03\xc1\x00\x00", pusi=True)
    pcr_only = ts_packet(0x41, b"", pusi=False, af=b"\x07\x42\x00\x24\x9f\xf0\x00\x00")

    cases = [
        ("null padding burst", make_burst([null_packet()] * 28), False),
        ("PAT/PMT/PCR tables only", make_burst(([pat, pmt, pcr_only] * 9)[:28]), False),
        ("single video PES in nulls", make_burst([null_packet()] * 20 + [video] + [null_packet()] * 7), True),
        ("single audio PES in nulls", make_burst([null_packet()] * 20 + [audio] + [null_packet()] * 7), True),
        ("video PES after PCR adaptation field", make_burst([null_packet()] * 20 + [
            ts_packet(0x41, b"\x00\x00\x01\xe0" + b"\x00" * 100, pusi=True,
                      af=b"\x07\x42\x00\x24\x9f\xf0\x00\x00")] + [null_packet()] * 7), True),
        ("full-size content burst", content_burst(), True),
        ("full-size padding burst", padding_burst(), False),
        ("garbage bytes (no sync)", b"\xde\xad\xbe\xef" * 400, False),
    ]
    for name, data, expected in cases:
        got = _mpegts_burst_has_pes_start(data)
        assert got == expected, f"PES detector failed: {name}: expected {expected}, got {got}"
    print(f"  PES detector: {len(cases)}/{len(cases)} cases pass")


def test_stalled_stream_is_restarted():
    content = [content_burst() for _ in range(40)]  # ~210 KB, fast prebuffer
    proc = FakeProc(content)
    server, stop_event, thread = run_bridge(proc)
    deadline = time.monotonic() + FFMPEG_CONTENT_STALL_SECONDS + FFMPEG_FIRST_CONTENT_TIMEOUT_SECONDS + 8.0
    while time.monotonic() < deadline:
        if proc.terminated:
            break
        time.sleep(0.05)
    assert proc.terminated, "watchdog never terminated the stalled stream"
    assert server.unexpected_exit is not None, "watchdog killed proc but restart handler did not run"
    bytes_sent, elapsed = server.unexpected_exit
    assert bytes_sent >= 1024 * 1024 and elapsed >= 8.0, (
        f"restart would be counted as a failure: bytes_sent={bytes_sent}, elapsed={elapsed:.1f}s"
    )
    stop_event.set()
    thread.join(timeout=3.0)
    print(f"  Stalled stream: terminated after {elapsed:.1f}s, {bytes_sent / 1024:.0f} KiB sent; "
          f"restart handler fired with healthy-restart classification")


def test_healthy_stream_is_not_restarted():
    class EndlessContentStdout:
        def __init__(self):
            self._closed = threading.Event()

        def read(self, n):
            if self._closed.is_set():
                return b""
            return content_burst()

        def close(self):
            self._closed.set()

    proc = FakeProc([])
    proc.stdout = EndlessContentStdout()
    server, stop_event, thread = run_bridge(proc)
    time.sleep(3.0)
    assert not proc.terminated, "watchdog killed a healthy stream with continuous PES"
    stop_event.set()
    proc.stdout.close()
    thread.join(timeout=3.0)
    print("  Healthy stream: still running after 3s (no false restart)")


def test_dead_source_gets_restarted():
    # Speed the test up: the bridge reads these module globals at runtime.
    discovery.FFMPEG_CONTENT_STALL_SECONDS = 1.0
    discovery.FFMPEG_FIRST_CONTENT_TIMEOUT_SECONDS = 1.0
    try:
        proc = FakeProc([])  # produces only null padding, never any PES
        server, stop_event, thread = run_bridge(proc)
        deadline = time.monotonic() + FFMPEG_FIRST_CONTENT_TIMEOUT_SECONDS + 8.0
        while time.monotonic() < deadline:
            if proc.terminated:
                break
            time.sleep(0.05)
        assert proc.terminated, "first-content watchdog never terminated a contentless stream"
        stop_event.set()
        thread.join(timeout=3.0)
    finally:
        discovery.FFMPEG_CONTENT_STALL_SECONDS = 8.0
        discovery.FFMPEG_FIRST_CONTENT_TIMEOUT_SECONDS = 12.0
    print("  Contentless stream: first-content watchdog terminated it (will restart)")


if __name__ == "__main__":
    print("Running black-screen fix verification...")
    test_pes_detector()
    test_stalled_stream_is_restarted()
    test_healthy_stream_is_not_restarted()
    test_dead_source_gets_restarted()
    print("ALL CHECKS PASSED")