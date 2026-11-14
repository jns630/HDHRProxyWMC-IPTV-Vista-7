"""Exit the Nuitka onefile child when its bootstrap parent disappears.

Nuitka onefile starts a small bootstrap executable which extracts the payload
and then launches the real program as a child process.  If the bootstrap is
killed (logoff, Task Manager, service stop, installer upgrade), Windows does
not automatically terminate that child: it keeps listening on HTTP/SSDP ports
and can also keep ffmpeg children alive.

This watchdog is only enabled for Nuitka onefile builds.  It waits on the
parent process handle and invokes a shutdown callback as soon as the parent
exits, so the child stops tuners and servers instead of becoming an orphan.
"""

import ctypes
import logging
import os
import platform
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_FAILED = 0xFFFFFFFF
_ERROR_INVALID_PARAMETER = 87

_started = False
_lock = threading.Lock()


def _windows_wait_for_parent(parent_pid: int) -> bool:
    """Return True once the parent process has exited."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, parent_pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return True
        logger.debug("Unable to open Nuitka parent process %s: error %s", parent_pid, error)
        return False
    try:
        while True:
            result = kernel32.WaitForSingleObject(handle, 1000)
            if result == _WAIT_OBJECT_0:
                return True
            if result == _WAIT_FAILED:
                logger.debug("Waiting for Nuitka parent process failed; stopping watchdog.")
                return False
    finally:
        kernel32.CloseHandle(handle)


def _posix_wait_for_parent(parent_pid: int) -> bool:
    while True:
        if os.getppid() != parent_pid:
            return True
        time.sleep(1.0)


def install_parent_watchdog(
    enabled: bool,
    shutdown: Callable[[], None],
) -> bool:
    """Start the watchdog once and return whether it is active."""
    global _started
    if not enabled:
        return False
    with _lock:
        if _started:
            return True
        _started = True

    parent_pid = os.getppid()
    if parent_pid <= 0:
        return False

    def worker():
        try:
            if platform.system() == "Windows":
                parent_exited = _windows_wait_for_parent(parent_pid)
            else:
                parent_exited = _posix_wait_for_parent(parent_pid)
        except Exception as exc:
            logger.debug("Parent watchdog failed: %s", exc)
            return
        if parent_exited:
            logger.warning(
                "Nuitka onefile parent process %s exited; shutting down the child.",
                parent_pid,
            )
            try:
                shutdown()
            finally:
                os._exit(0)

    thread = threading.Thread(target=worker, daemon=True, name="nuitka-parent-watchdog")
    thread.start()
    logger.info("Nuitka onefile parent watchdog started (parent pid %s).", parent_pid)
    return True
