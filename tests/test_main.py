# -*- coding: utf-8 -*-
"""Stdlib-only tests for command-line/config precedence."""
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_module  # noqa: E402


class MainConfigPrecedenceTests(unittest.TestCase):
    def _run_main(self, argv):
        with mock.patch.object(main_module, "configure_logging"), \
                mock.patch.object(main_module, "run_proxy") as run_proxy, \
                mock.patch.object(sys, "argv", argv):
            main_module.main()
        return run_proxy.call_args[0][0]

    def test_config_file_values_are_not_replaced_by_cli_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "tuner_count": 2,
                        "http_port": 7001,
                        "device_id": "104ABCDA",
                        "listen_ip": "127.0.0.1",
                        "ffmpeg_bitrate": "2500k",
                        "ffmpeg_enabled": True,
                    },
                    f,
                )
            cfg = self._run_main(["main.py", "--config", config_path])

        self.assertEqual(cfg.tuner_count, 2)
        self.assertEqual(cfg.http_port, 7001)
        self.assertEqual(cfg.device_id, "104ABCDA")
        self.assertEqual(cfg.listen_ip, "127.0.0.1")
        self.assertEqual(cfg.ffmpeg_bitrate, "2500k")

    def test_explicit_cli_values_override_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"tuner_count": 2, "http_port": 7001}, f)
            cfg = self._run_main(
                [
                    "main.py",
                    "--config", config_path,
                    "--tuners", "8",
                    "--port", "8888",
                    "--device-id", "104FFFF2",
                    "--bitrate", "3000k",
                    "--no-ffmpeg",
                ]
            )

        self.assertEqual(cfg.tuner_count, 8)
        self.assertEqual(cfg.http_port, 8888)
        self.assertEqual(cfg.device_id, "104FFFF2")
        self.assertEqual(cfg.ffmpeg_bitrate, "3000k")
        self.assertFalse(cfg.ffmpeg_enabled)

    def test_missing_config_file_exits_with_error(self):
        missing = os.path.join(tempfile.gettempdir(), "missing-hdhr-config.json")
        with mock.patch.object(main_module, "configure_logging"), \
                mock.patch.object(main_module, "run_proxy") as run_proxy, \
                mock.patch.object(sys, "argv", ["main.py", "--config", missing]):
            with self.assertRaises(SystemExit) as raised:
                main_module.main()
        self.assertEqual(raised.exception.code, 2)
        run_proxy.assert_not_called()

    def test_invalid_cli_value_exits_with_error(self):
        with mock.patch.object(main_module, "configure_logging"), \
                mock.patch.object(main_module, "run_proxy") as run_proxy, \
                mock.patch.object(sys, "argv", ["main.py", "--tuners", "0"]):
            with self.assertRaises(SystemExit) as raised:
                main_module.main()
        self.assertEqual(raised.exception.code, 2)
        run_proxy.assert_not_called()


class SafeLogRotationTests(unittest.TestCase):
    def test_locked_log_does_not_raise_during_rollover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proxy.log")
            handler = main_module._SafeRotatingFileHandler(
                path, maxBytes=1, backupCount=1, encoding="utf-8"
            )
            logger = logging.getLogger("safe-rotation-test")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)
            try:
                logger.info("first")
                with mock.patch("logging.handlers.os.rename", side_effect=OSError("locked")):
                    logger.info("second")
            finally:
                logger.removeHandler(handler)
                handler.close()
            with open(path, "r", encoding="utf-8") as f:
                contents = f.read()
        self.assertIn("second", contents)


if __name__ == "__main__":
    unittest.main()
