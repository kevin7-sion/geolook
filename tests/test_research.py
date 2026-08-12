import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
# The dashboard runs in WSL. Keep this focused unit test runnable on Windows too.
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
import geolib as G
import assistant as A
import generate as GEN
import research as R


class ResearchResolveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original_work = G.WORK
        G.WORK = Path(self.tmp.name)
        self.addCleanup(setattr, G, "WORK", self.original_work)
        self.slug = "acme"
        self.pdir = G.project_dir(self.slug)
        self.pdir.mkdir(parents=True)
        (self.pdir / "geo.json").write_text(json.dumps({
            "brand": {"name": "Acme", "site": "https://acme.example"},
            "market": "global", "questions": [],
        }), "utf-8")

    def test_marks_missing_fact_as_not_publicly_disclosed_without_llm(self):
        article = "# Pricing\n\n(To be added: pricing)"
        with mock.patch.object(R, "find_sources", return_value=[]), \
             mock.patch.object(R, "_rewrite_with_sources") as rewrite:
            result = R.resolve(self.slug, article, "global")

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertIn("Not publicly disclosed in the official sources reviewed for pricing.", result["text"])
        rewrite.assert_not_called()
        record = json.loads((self.pdir / "research" / "latest.json").read_text("utf-8"))
        self.assertEqual(record["outcome"], "not publicly disclosed in checked official sources")

    def test_marks_bare_pending_values_as_unpublished_when_no_source_exists(self):
        article = "| Pricing | 待确认 |\n\nSupport: TBD\n\n（未填写：customer case）"
        with mock.patch.object(R, "find_sources", return_value=[]):
            result = R.resolve(self.slug, article, "global")

        self.assertTrue(result["ok"])
        self.assertNotIn("待确认", result["text"])
        self.assertNotIn("TBD", result["text"])
        self.assertNotIn("未填写", result["text"])
        self.assertIn("Not publicly disclosed in the official sources reviewed for Pricing.", result["text"])
        self.assertIn("Not publicly disclosed in the official sources reviewed for Support.", result["text"])
        self.assertIn("Not publicly disclosed in the official sources reviewed for customer case.", result["text"])

    def test_uses_rewrite_only_after_official_source_is_available(self):
        article = "# Pricing\n\n(To be added: pricing)"
        source = {"url": "https://acme.example/pricing", "title": "Pricing",
                  "snippet": "Pricing is listed here.", "official": True, "kind": "web search"}
        revised = "# Pricing\n\nOfficial pricing details.\n\n## Sources checked\n- https://acme.example/pricing"
        with mock.patch.object(R, "find_sources", return_value=[source]), \
             mock.patch.object(R, "_rewrite_with_sources", return_value=revised) as rewrite:
            result = R.resolve(self.slug, article, "global")

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["text"], revised)
        self.assertTrue(result["sources"][0]["official"])
        self.assertEqual(result["sources"][0]["field"], "pricing")
        rewrite.assert_called_once()
        record = json.loads((self.pdir / "research" / "latest.json").read_text("utf-8"))
        self.assertEqual(record["outcome"], "revised from official sources")

    def test_cleans_pending_markers_left_by_a_source_backed_rewrite(self):
        article = "# Pricing\n\n(To be added: pricing)"
        source = {"url": "https://acme.example/pricing", "title": "Pricing",
                  "snippet": "Pricing is listed here.", "official": True, "kind": "web search"}
        model_text = "# Pricing\n\nRate: TBD\n\n| SLA | 待确认 |"
        with mock.patch.object(R, "find_sources", return_value=[source]), \
             mock.patch.object(R, "_rewrite_with_sources", return_value=model_text):
            result = R.resolve(self.slug, article, "global")

        self.assertTrue(result["ok"])
        self.assertNotIn("TBD", result["text"])
        self.assertNotIn("待确认", result["text"])
        self.assertIn("Not publicly disclosed in the official sources reviewed for Rate.", result["text"])
        self.assertIn("Not publicly disclosed in the official sources reviewed for SLA.", result["text"])

    def test_global_research_never_copies_a_chinese_placeholder_field_into_new_text(self):
        article = "# Pricing\n\n（待补：定价）"
        with mock.patch.object(R, "find_sources", return_value=[]):
            result = R.resolve(self.slug, article, "global")

        self.assertTrue(result["ok"])
        self.assertIn("Not publicly disclosed in the official sources reviewed for this item.", result["text"])

    def test_assistant_context_exposes_checklist_and_safe_navigation(self):
        (self.pdir / "assets" / "outlines").mkdir(parents=True)
        (self.pdir / "assets" / "outlines" / "q001.md").write_text("# Outline", "utf-8")
        (self.pdir / "tasks.json").write_text(json.dumps({"tasks": [{"status": "todo"}]}), "utf-8")
        ctx = A.context(self.slug)

        self.assertEqual(ctx["brand"], "Acme")
        self.assertTrue(any(x["label"] == "内容大纲" and x["ok"] for x in ctx["checklist"]))
        self.assertTrue(any(x["label"] == "待完成行动" and not x["ok"] for x in ctx["checklist"]))
        self.assertEqual({x["route"] for x in ctx["actions"]},
                         {"workbench", "facts", "plan", "siteaudit", "questions", "assets"})

    def test_regeneration_archives_the_existing_draft_before_overwrite(self):
        outlines = self.pdir / "assets" / "outlines"
        outlines.mkdir(parents=True)
        (outlines / "_index.json").write_text(json.dumps([{
            "question_id": "q001", "target_question": "What is Acme?", "market": "global",
            "type": "guide", "facts_to_use": [], "sections": [],
            "requirements": {"min_words": 800, "min_h2": 3},
        }]), "utf-8")
        drafts = self.pdir / "assets" / "drafts"
        drafts.mkdir(parents=True)
        old = "<!-- old draft -->\n\n# Previous"
        (drafts / "q001.md").write_text(old, "utf-8")
        with mock.patch.object(GEN, "draft", return_value="# Replacement"):
            GEN.run(self.slug, which=["drafts"], with_draft=True, draft_limit=1, draft_question="q001")

        self.assertIn("Replacement", (drafts / "q001.md").read_text("utf-8"))
        versions = list((self.pdir / "assets" / "history" / "drafts").glob("q001-*.md"))
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].read_text("utf-8"), old)

    def test_deployable_assets_are_english_and_have_no_template_placeholders(self):
        cfg = json.loads((self.pdir / "geo.json").read_text("utf-8"))
        cfg["market"] = "cn"
        cfg["brand"].update({"industry": "待确认", "target_users": "未填写", "same_as": [],
                              "offers": [{"name": "套餐", "price": "待确认", "currency": "人民币"}]})
        (self.pdir / "geo.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
        (self.pdir / "content").mkdir(parents=True)
        (self.pdir / "content" / "facts.md").write_text(
            "# Facts\n\n## One-line positioning\n\n> （待补：一句话定义）\n", "utf-8")
        (self.pdir / "audit.json").write_text(json.dumps({"pages": []}), "utf-8")

        outputs = [
            GEN.gen_llms_txt(self.slug),
            json.dumps(GEN.gen_jsonld(self.slug), ensure_ascii=False),
            GEN.gen_definition_block(self.slug, "en"),
            GEN.gen_faq_block(self.slug, "en"),
        ]
        for output in outputs:
            self.assertNotRegex(output, r"[\u3400-\u9fff]")
            self.assertNotRegex(output, r"<填|待补|待确认|未填写|TBD")
        self.assertIn("not publicly disclosed", outputs[0])


if __name__ == "__main__":
    unittest.main()
