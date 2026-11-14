import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdhr_proxy import vista_guide  # noqa: E402


class VistaHelperLookupTests(unittest.TestCase):
    def test_finds_helper_in_nuitka_onefile_runtime_dir(self):
        with tempfile.TemporaryDirectory() as runtime_dir:
            helper = os.path.join(runtime_dir, vista_guide.VISTA_HELPER_NAME)
            with open(helper, "wb"):
                pass
            compiled = types.SimpleNamespace(
                python_runtime_dir=runtime_dir,
                containing_dir=os.path.dirname(runtime_dir),
            )
            with mock.patch.object(vista_guide, "__compiled__", compiled, create=True):
                self.assertEqual(vista_guide._find_vista_import_helper(), helper)

    def test_finds_helper_beside_standalone_executable(self):
        with tempfile.TemporaryDirectory() as exe_dir:
            helper = os.path.join(exe_dir, vista_guide.VISTA_HELPER_NAME)
            with open(helper, "wb"):
                pass
            argv = [os.path.join(exe_dir, "HDHRProxyWMC-IPTV.exe")]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(vista_guide._find_vista_import_helper(), helper)


if __name__ == "__main__":
    unittest.main()
