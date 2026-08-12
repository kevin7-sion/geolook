import json
import sys
import tempfile
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
import content_registry as R
import generate as GEN
import geolib as G


class ContentRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.old = G.WORK; G.WORK = Path(self.tmp.name); self.addCleanup(setattr, G, "WORK", self.old)
        self.slug = "acme"; p = G.project_dir(self.slug); (p / "assets" / "outlines").mkdir(parents=True)
        (p / "geo.json").write_text(json.dumps({"brand":{"name":"Acme","site":"https://acme.example"},"questions":[
            {"id":"q001","text":"How to choose an AI API gateway?","market":"global"},
            {"id":"q002","text":"How to choose an AI API gateway?","market":"global"},
            {"id":"q003","text":"How to monitor AI API spending?","market":"global"}]}), "utf-8")
        (p / "assets" / "outlines" / "_index.json").write_text(json.dumps([
            {"question_id":"q001","target_question":"How to choose an AI API gateway?"},
            {"question_id":"q002","target_question":"How to choose an AI API gateway?"},
            {"question_id":"q003","target_question":"How to monitor AI API spending?"}]), "utf-8")

    def test_registry_marks_draft_and_exact_duplicate(self):
        p = G.project_dir(self.slug); (p / "assets" / "drafts").mkdir()
        (p / "assets" / "drafts" / "q001.md").write_text(R.with_marker("# Draft", "q001", "How to choose an AI API gateway?", "draft"), "utf-8")
        records = {x["question_id"]: x for x in R.build(self.slug)["records"]}
        self.assertEqual(records["q001"]["status"], "draft")
        self.assertEqual(records["q002"]["duplicate"]["level"], "exact")

    def test_auto_queue_skips_existing_and_duplicate_topics(self):
        p = G.project_dir(self.slug); (p / "assets" / "drafts").mkdir()
        (p / "assets" / "drafts" / "q001.md").write_text("# Draft", "utf-8")
        selected, skipped = R.next_outlines(self.slug, 3)
        self.assertEqual([x["question_id"] for x in selected], ["q003"])
        self.assertEqual({x["reason"] for x in skipped}, {"draft", "duplicate_exact"})

    def test_generated_draft_carries_a_readable_lifecycle_marker(self):
        text = R.with_marker("# English draft", "q003", "How to monitor AI API spending?", "draft")
        self.assertIn("geolook-content: question_id=q003; lifecycle=draft", text)

    def test_replacing_a_marker_does_not_accumulate_old_lifecycle_records(self):
        first = R.with_marker("# Draft", "q003", "How to monitor AI API spending?", "draft")
        final = R.with_marker(first, "q003", "How to monitor AI API spending?", "final")
        self.assertEqual(final.count("geolook-content:"), 1)
        self.assertIn("lifecycle=final", final)
