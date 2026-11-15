import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy import parent_watchdog  # noqa: E402


class ParentWatchdogTests(unittest.TestCase):
    def setUp(self):
        parent_watchdog._started = False

    def test_disabled_watchdog_is_not_started(self):
        shutdown = mock.Mock()
        self.assertFalse(parent_watchdog.install_parent_watchdog(False, shutdown))
        shutdown.assert_not_called()

    def test_watchdog_shuts_down_when_parent_exits(self):
        shutdown_called = threading.Event()

        def shutdown():
            shutdown_called.set()

        with mock.patch.object(parent_watchdog.platform, "system", return_value="Windows"), \
                mock.patch.object(parent_watchdog.os, "getppid", return_value=4242), \
                mock.patch.object(parent_watchdog, "_windows_wait_for_parent", return_value=True), \
                mock.patch.object(parent_watchdog.os, "_exit") as exit_mock:
            self.assertTrue(parent_watchdog.install_parent_watchdog(True, shutdown))
            self.assertTrue(shutdown_called.wait(3))
            exit_mock.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
