import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
if "fcntl" not in sys.modules:
    fcntl = types.ModuleType("fcntl"); fcntl.LOCK_EX = 2; fcntl.LOCK_UN = 8; fcntl.flock = lambda *a, **k: None
    sys.modules["fcntl"] = fcntl
try:
    import bs4  # noqa: F401
except ModuleNotFoundError:
    bs4 = types.ModuleType("bs4"); bs4.BeautifulSoup = object; sys.modules["bs4"] = bs4
import content_schedule as C


class DailyContentScheduleTest(unittest.TestCase):
    def test_does_not_generate_when_cms_token_is_missing(self):
        with mock.patch("publish.missing_env", return_value=["WISGATE_CMS_TOKEN"]), \
             mock.patch("geolib.load_config", return_value={"market": "global"}), \
             mock.patch("generate.run") as generate:
            result = C.run("acme", 2)
        self.assertFalse(result["ok"])
        self.assertIn("Token", result["error"])
        generate.assert_not_called()

    def test_blocks_mixed_language_global_draft_before_cms_push(self):
        fake_path = mock.mock_open(read_data="# 中文").return_value
        with mock.patch("publish.missing_env", return_value=[]), \
             mock.patch("content_registry.next_outlines", return_value=([{ "question_id":"q101"}], [])), \
             mock.patch("generate.run", return_value={"assets":["assets/drafts/q101.md"]}), \
             mock.patch("geolib.load_config", return_value={"market":"global"}), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value="# 中文"), \
             mock.patch("content_registry.save"), \
             mock.patch("publish.publish") as publish:
            result = C.run("acme", 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["blocked"][0]["reason"], "mixed_language")
        publish.assert_not_called()

    def test_repairs_missing_blocks_then_rejects_if_any_remain(self):
        with mock.patch("publish.missing_env", return_value=[]), \
             mock.patch("content_registry.next_outlines", return_value=([{ "question_id":"q101"}], [])), \
             mock.patch("generate.run", return_value={"assets":["assets/drafts/q101.md"]}), \
             mock.patch("geolib.load_config", return_value={"market":"global", "content_schedule":{}}), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value="# English draft"), \
             mock.patch("generate._missing_extract_blocks", side_effect=[["FAQ"], ["FAQ"]]), \
             mock.patch("generate.complete_extract_blocks", return_value={"ok": True, "changed": False, "text": "# English draft"}), \
             mock.patch("content_registry.save"), \
             mock.patch("research.unresolved", return_value=[]), \
             mock.patch("publish.publish") as publish:
            result = C.run("acme", 1)
        self.assertEqual(result["blocked"][0]["reason"], "extract_blocks_missing")
        publish.assert_not_called()

    def test_resolves_pending_facts_before_push(self):
        fake_path = mock.MagicMock()
        with mock.patch("publish.missing_env", return_value=[]), \
             mock.patch("content_registry.next_outlines", return_value=([{ "question_id":"q101"}], [])), \
             mock.patch("generate.run", return_value={"assets":["assets/drafts/q101.md"]}), \
             mock.patch("geolib.load_config", return_value={"market":"global", "content_schedule":{}}), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value="# English draft\nTBD: pricing"), \
             mock.patch("generate._missing_extract_blocks", return_value=[]), \
             mock.patch("research.unresolved", side_effect=[["pricing"], []]), \
             mock.patch("research.resolve", return_value={"ok": True, "changed": True, "text": "# English draft\nNot publicly disclosed in the official sources reviewed for pricing."}), \
             mock.patch("pathlib.Path.write_text"), \
             mock.patch("generate.lint_draft", return_value=[]), \
             mock.patch("content_registry.save"), \
             mock.patch("publish.publish", return_value={"ok": True, "url": "https://cms.example/items/1"}) as publish:
            result = C.run("acme", 1)
        self.assertEqual(len(result["published"]), 1)
        self.assertEqual(result["published"][0]["repaired"], ["facts"])
        publish.assert_called_once()
