"""CMS publishing regression tests: draft-only and no credential leakage."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import publish as P


class _Response:
    def __init__(self, status_code=201, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class WisGateCmsPublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        self.slug = "acme"
        self.project = self.work / self.slug
        (self.project / "content").mkdir(parents=True)
        (self.project / "content" / "article.md").write_text(
            "# Practical API Guide\n\nA short article with **useful** details.", "utf-8"
        )
        (self.project / "geo.json").write_text(json.dumps({
            "publishing": {"wisgate_cms": {
                "api_base_url": "https://cms.wisgate.ai",
                "collection": "blogs",
                "title_field": "title",
                "body_field": "content",
                "body_format": "markdown",
                "slug_field": "slug",
                "status_field": "status",
                "draft_value": "draft",
                "cover_image_field": "cover_image",
                "summary_field": "summary",
                "publish_time_field": "publish_time",
                "tags_field": "tags",
                "tags_format": "json",
                "platform_field": "platform",
                "platform_value": "wisdom-gate",
                "model_field": "model",
            }}
        }), "utf-8")
        self.env = mock.patch.dict(os.environ, {"WISGATE_CMS_TOKEN": "secret-test-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.work_patch = mock.patch.object(P.G, "WORK", self.work)
        self.work_patch.start()
        self.addCleanup(self.work_patch.stop)

    @mock.patch.object(P.requests, "post")
    def test_creates_directus_draft_with_configured_fields(self, post):
        post.return_value = _Response(201, {"data": {"id": "42"}})

        result = P.publish(self.slug, "wisgate_cms", "content/article.md")

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://cms.wisgate.ai/admin/content/blogs/42")
        url = post.call_args.args[0]
        self.assertEqual(url, "https://cms.wisgate.ai/items/blogs")
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret-test-token")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["title"], "Practical API Guide")
        self.assertIn("**useful**", body["content"])
        self.assertEqual(body["slug"], "practical-api-guide")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["summary"], "A short article with useful details.")
        self.assertRegex(body["publish_time"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertGreaterEqual(len(body["tags"]), 3)
        self.assertIn(body["tags"][0], body["title"])
        self.assertEqual(body["platform"], "wisdom-gate")
        self.assertNotIn("model", body)
        self.assertNotIn("cover_image", body)

    @mock.patch.object(P.requests, "post")
    def test_wisgate_cms_defaults_to_markdown_body(self, post):
        post.return_value = _Response(201, {"data": {"id": "45"}})
        cfg = P.G.load_config(self.slug)
        cfg["publishing"]["wisgate_cms"].pop("body_format")
        P.G.save_config(self.slug, cfg)

        self.assertTrue(P.publish(self.slug, "wisgate_cms", "content/article.md")["ok"])
        body = post.call_args.kwargs["json"]
        self.assertTrue(body["content"].startswith("# Practical API Guide"))

    def test_cms_tags_are_short_phrase_candidates(self):
        title = "OpenRouter Alternatives: A 2026 Comparison Guide for AI API Users"
        text = "# " + title + "\n\nThis compares unified AI API gateways and multi-model API providers."

        tags = P._cms_tags(title, text)

        self.assertEqual(tags[0], "OpenRouter Alternatives")
        self.assertIn("AI API gateway", tags)
        self.assertIn("Multi-model API", tags)
        self.assertEqual(len(tags), 3)
        self.assertNotIn("Comparison", tags)
        self.assertLessEqual(max(map(len, tags)), 60)

    @mock.patch.object(P.requests, "post")
    def test_publish_uses_reviewed_tags_override(self, post):
        post.return_value = _Response(201, {"data": {"id": "48"}})

        result = P.publish(self.slug, "wisgate_cms", "content/article.md",
                           options={"tags": ["API gateway", "Model routing", "Developer tools"]})

        self.assertTrue(result["ok"])
        self.assertEqual(post.call_args.kwargs["json"]["tags"],
                         ["API gateway", "Model routing", "Developer tools"])

    def test_preview_does_not_require_a_cms_request(self):
        result = P.preview(self.slug, "wisgate_cms", "content/article.md")

        self.assertTrue(result["ok"])
        self.assertEqual(result["format"], "markdown")
        self.assertEqual(len(result["tags"]), 3)
        self.assertGreater(result["characters"], 0)
        self.assertEqual(result["slug"], "practical-api-guide")
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["platform"], "wisdom-gate")
        self.assertIsInstance(result["issues"], list)

    @mock.patch.object(P.requests, "post")
    def test_legacy_html_configuration_is_migrated_to_markdown_body(self, post):
        post.return_value = _Response(201, {"data": {"id": "47"}})
        cfg = P.G.load_config(self.slug)
        cfg["publishing"]["wisgate_cms"]["body_format"] = "html"
        P.G.save_config(self.slug, cfg)

        self.assertTrue(P.publish(self.slug, "wisgate_cms", "content/article.md")["ok"])
        body = post.call_args.kwargs["json"]
        self.assertIn("**useful**", body["content"])
        self.assertNotIn("<strong>", body["content"])

    @mock.patch.object(P.requests, "post")
    def test_markdown_body_omits_internal_geolook_comments(self, post):
        post.return_value = _Response(201, {"data": {"id": "46"}})
        cfg = P.G.load_config(self.slug)
        cfg["publishing"]["wisgate_cms"]["body_format"] = "markdown"
        P.G.save_config(self.slug, cfg)
        article = self.project / "content" / "article.md"
        article.write_text("<!-- 目标问题 q108 -->\n\n# Practical API Guide\n\nEnglish body.", "utf-8")

        self.assertTrue(P.publish(self.slug, "wisgate_cms", "content/article.md")["ok"])
        body = post.call_args.kwargs["json"]
        self.assertNotIn("目标问题", body["content"])
        self.assertEqual(body["content"], "# Practical API Guide\n\nEnglish body.\n")

    @mock.patch.object(P.requests, "post")
    def test_sends_cover_image_and_csv_tags_when_configured(self, post):
        post.return_value = _Response(201, {"data": {"id": "44"}})
        cfg = P.G.load_config(self.slug)
        settings = cfg["publishing"]["wisgate_cms"]
        settings.update({"cover_image_value": "file-uuid", "additional_tags": "LLM, Integration",
                         "tags_format": "csv", "model_value": ""})
        P.G.save_config(self.slug, cfg)

        self.assertTrue(P.publish(self.slug, "wisgate_cms", "content/article.md")["ok"])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["cover_image"], "file-uuid")
        self.assertIsInstance(body["tags"], str)
        self.assertIn("LLM", body["tags"])

    def test_summary_respects_wisgate_cms_255_character_limit(self):
        text = "# Title\n\n" + ("A sentence with useful implementation detail. " * 20)
        self.assertLessEqual(len(P._cms_summary(text, "Title")), 240)

    @mock.patch.object(P.requests, "post")
    def test_always_sends_canonical_slug_for_non_latin_titles(self, post):
        post.return_value = _Response(201, {"data": {"id": "43"}})
        cfg = P.G.load_config(self.slug)
        cfg["publishing"]["wisgate_cms"]["title_field"] = "headline"
        cfg["publishing"]["wisgate_cms"]["slug_field"] = "url_key"
        P.G.save_config(self.slug, cfg)

        result = P.publish(self.slug, "wisgate_cms", "content/article.md", "中文标题")

        self.assertTrue(result["ok"])
        body = post.call_args.kwargs["json"]
        self.assertRegex(body["slug"], r"^geolook-[a-f0-9]{12}$")
        self.assertEqual(body["url_key"], body["slug"])

    @mock.patch.object(P.requests, "post")
    def test_rejects_unsafe_api_configuration_without_a_request(self, post):
        cfg = P.G.load_config(self.slug)
        cfg["publishing"]["wisgate_cms"]["api_base_url"] = "https://cms.wisgate.ai/admin"
        P.G.save_config(self.slug, cfg)

        result = P.publish(self.slug, "wisgate_cms", "content/article.md")

        self.assertFalse(result["ok"])
        self.assertIn("不能填写 /admin", result["error"])
        post.assert_not_called()

    @mock.patch.object(P.requests, "post")
    def test_failure_does_not_put_token_in_result_or_publish_record(self, post):
        post.return_value = _Response(401, text="Bearer secret-test-token is not valid")

        result = P.publish(self.slug, "wisgate_cms", "content/article.md")

        self.assertFalse(result["ok"])
        self.assertNotIn("secret-test-token", result["error"])
        record = json.loads((self.project / "publish.json").read_text("utf-8"))[-1]
        self.assertNotIn("secret-test-token", json.dumps(record))

    def test_cms_validation_error_does_not_echo_article_content(self):
        article = "<!-- target question q108 -->\n# A private draft title"
        response = _Response(400, {
            "errors": [{"message": f'Value "{article}" is not valid',
                        "extensions": {"field": "content", "code": "FAILED_VALIDATION"}}]
        })

        error = P._cms_error(response, "secret-test-token")

        self.assertIn("content · FAILED_VALIDATION", error)
        self.assertIn("value rejected by field validation", error)
        self.assertNotIn(article, error)

    @mock.patch.object(P.requests, "post")
    def test_content_error_includes_safe_submission_size(self, post):
        post.return_value = _Response(400, {
            "errors": [{"message": "value rejected", "extensions": {
                "field": "content", "code": "VALUE_TOO_LONG"}}]
        })

        result = P.publish(self.slug, "wisgate_cms", "content/article.md")

        self.assertFalse(result["ok"])
        self.assertIn("本次提交", result["error"])
        self.assertNotIn("A short article", result["error"])


if __name__ == "__main__":
    unittest.main()
