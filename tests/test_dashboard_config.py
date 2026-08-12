"""品牌配置保存接口的回归测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import dashboard as DB
import publish as P


class DashboardConfigSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        self.slug = "acme"
        self.path = self.work / self.slug / "geo.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "slug": self.slug,
            "brand": {"name": "Old", "aliases": [], "site": "https://old.example"},
            "market": "cn", "platforms": ["deepseek"], "questions": [{"id": "q001"}],
        }), "utf-8")

    def _post_config(self, body):
        handler = object.__new__(DB.Handler)
        handler.path = f"/api/config/{self.slug}"
        handler._body = lambda: body
        result = []
        handler._json = lambda payload, code=200: result.append((payload, code))
        with mock.patch.object(DB.G, "WORK", self.work):
            handler.do_POST()
        self.assertEqual(len(result), 1)
        return result[0]

    def test_brand_config_persists_and_preserves_unedited_fields(self):
        body = {
            "brand": {"name": "New Brand", "aliases": ["New"], "site": "https://old.example"},
            "market": "both",
        }
        response, code = self._post_config(body)
        self.assertEqual(code, 200)
        self.assertTrue(response["ok"])
        saved = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(saved["brand"]["name"], "New Brand")
        self.assertEqual(saved["brand"]["aliases"], ["New"])
        self.assertEqual(saved["market"], "both")
        self.assertEqual(saved["platforms"], ["deepseek"])
        self.assertEqual(saved["questions"], [{"id": "q001"}])

    def test_rejects_empty_brand_name_without_overwriting_config(self):
        response, code = self._post_config({"brand": {"name": "   "}})
        self.assertEqual(code, 400)
        self.assertFalse(response["ok"])
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["brand"]["name"], "Old")

    def test_wisgate_cms_token_is_allowed_as_a_secret_setting(self):
        self.assertIn("wisgate_cms", P.PUBLISHERS)
        self.assertEqual(P.PUBLISHERS["wisgate_cms"]["env"], ["WISGATE_CMS_TOKEN"])

    def test_publish_preview_returns_keyword_candidates_without_pushing(self):
        handler = object.__new__(DB.Handler)
        handler.path = f"/api/publish-preview/{self.slug}"
        handler._body = lambda: {"platform": "wisgate_cms", "path": "content/article.md"}
        result = []
        handler._json = lambda payload, code=200: result.append((payload, code))
        with mock.patch.object(DB.G, "WORK", self.work), \
             mock.patch.object(P, "preview", return_value={"ok": True, "tags": ["API gateway", "AI API", "Developer tools"]}):
            handler.do_POST()

        self.assertEqual(result, [({"ok": True, "tags": ["API gateway", "AI API", "Developer tools"]}, 200)])


if __name__ == "__main__":
    unittest.main()
