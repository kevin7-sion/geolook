import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
if "fcntl" not in sys.modules:
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX, fcntl.LOCK_UN = 2, 8
    fcntl.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = fcntl
try:
    from bs4 import BeautifulSoup as _BeautifulSoup  # noqa: F401
except ModuleNotFoundError:
    bs4 = types.ModuleType("bs4")
    bs4.BeautifulSoup = object
    sys.modules["bs4"] = bs4

import generate


class DraftHeadingCleanupTest(unittest.TestCase):
    def test_internal_numeric_fact_labels_become_reader_facing_headings(self):
        text = "## 数字事实\n\n| Metric | Value |\n|---|---|\n\n## **Numeric facts**\n"
        result = generate._clean_internal_block_headings(text)
        self.assertNotIn("数字事实", result)
        self.assertNotIn("Numeric facts", result)
        self.assertIn("## 关键数据与证据", result)
        self.assertIn("## Key data and evidence", result)

    def test_lint_location_identifies_line_and_section(self):
        text = "# Title\n\n## Evidence\n\nTODO: add a primary source."
        location = generate._lint_location(text, text.index("TODO"))
        self.assertEqual(location, {"line": 5, "section": "Evidence"})


if __name__ == "__main__":
    unittest.main()
