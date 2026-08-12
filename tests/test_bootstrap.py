import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import bootstrap as B
import generate as GEN
import geolib as G
import sample as S


class WorkDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = G.WORK
        G.WORK = Path(self._tmp.name)
        self.slug = "boottest"
        self.pdir = G.project_dir(self.slug)
        (self.pdir / "evidence").mkdir(parents=True)

    def tearDown(self):
        G.WORK = self._orig
        self._tmp.cleanup()

    def write_config(self, cfg):
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "geo.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")


BASE_CFG = {
    "brand": {"name": "测试品牌", "aliases": [], "site": "https://t.example.com"},
    "competitors": [
        {"name": "竞品A", "aliases": [], "market": "cn", "confirmed": False},
        {"name": "竞品B", "aliases": [], "market": "cn", "confirmed": False},
        {"name": "老牌竞品", "aliases": [], "market": "cn"},  # 旧数据无字段，视为已确认
    ],
    "market": "cn",
    "questions": [{"id": "q001", "group": "推荐", "market": "cn", "text": "有什么好用的工具？"}],
}


class TestHomepageFirst(WorkDirCase):
    def test_root_is_first_page_not_highest_scored(self):
        home = "https://t.example.com/"
        deep = "https://t.example.com/blog/hot-article"
        G.write_jsonl(self.pdir / "evidence" / "pages.jsonl", [
            {"url": home, "title": "首页", "text": "首页正文 " * 50, "word_count": 100},
            {"url": deep, "title": "高分页", "text": "高分页正文 " * 50, "word_count": 100},
        ])
        G.write_json(self.pdir / "audit.json", {"pages": [
            {"url": home, "score": 1},
            {"url": deep, "score": 99},
        ]})
        digest = B._site_digest(self.slug)
        blocks = [b for b in digest.split("## 页面：") if b.strip()]
        self.assertTrue(blocks, "digest 不应为空")
        self.assertIn(home, blocks[0], "摘要首块必须是首页（pages.jsonl 第一条），而不是高分页")
        self.assertNotIn(deep, blocks[0])


class TestGlobalQuestionLanguage(WorkDirCase):
    def test_question_bank_rejects_chinese_rows_marked_as_global(self):
        response = {"questions": [
            {"id": "q101", "group": "推荐", "market": "global", "text": "What is an AI API gateway?"},
            {"id": "q102", "group": "推荐", "market": "global", "text": "什么是 AI API 网关？"},
        ]}
        with mock.patch.object(B, "_ask_json", return_value=response):
            questions = B.question_bank({"name": "Acme"}, "global")
        self.assertEqual([q["id"] for q in questions], ["q101"])

    def test_cleanup_backs_up_and_removes_legacy_chinese_global_questions(self):
        cfg = json.loads(json.dumps(BASE_CFG, ensure_ascii=False)); cfg["market"] = "global"
        cfg["questions"] = [
            {"id": "q101", "group": "推荐", "market": "global", "text": "What is an AI API gateway?"},
            {"id": "q102", "group": "推荐", "market": "global", "text": "什么是 AI API 网关？"},
        ]
        self.write_config(cfg)
        result = B.clean_global_questions(self.slug)
        self.assertEqual((result["kept"], result["removed"]), (1, 1))
        self.assertEqual(len(G.load_config(self.slug)["questions"]), 1)
        self.assertTrue(list((self.pdir / "history").glob("questions-before-global-language-cleanup-*.json")))


class TestCompetitorConfirmation(WorkDirCase):
    def _manual_file(self, answer):
        f = Path(self._tmp.name) / "manual.md"
        f.write_text(
            "# 采样表\n\n## platform: deepseek\n> 国内\n\n"
            f"### q001 · 有什么好用的工具？\n\n```answer\n{answer}\n```\n",
            "utf-8")
        return str(f)

    def test_mentioned_competitor_confirmed_after_import(self):
        self.write_config(json.loads(json.dumps(BASE_CFG, ensure_ascii=False)))
        S.sample_import(self.slug, self._manual_file("我推荐竞品A，它挺好用的。"))
        cfg = G.load_config(self.slug)
        by_name = {c["name"]: c for c in cfg["competitors"]}
        self.assertTrue(by_name["竞品A"].get("confirmed"), "被采样提到的竞品应转正")
        self.assertFalse(by_name["竞品B"].get("confirmed"), "未被提到的竞品保持未确认")

    def test_no_save_when_nothing_changes(self):
        self.write_config(json.loads(json.dumps(BASE_CFG, ensure_ascii=False)))
        S.sample_import(self.slug, self._manual_file("我推荐竞品A。"))
        bak = self.pdir / ".geo.bak"
        n1 = len(list(bak.glob("geo-*.json"))) if bak.exists() else 0
        self.assertEqual(n1, 1, "首次转正应写一次配置（产生一个备份）")
        S.sample_import(self.slug, self._manual_file("我推荐竞品A。"))
        n2 = len(list(bak.glob("geo-*.json")))
        self.assertEqual(n2, 1, "值没有变化时不应再写 geo.json")

    def test_unconfirmed_marked_in_facts_md(self):
        self.write_config(json.loads(json.dumps(BASE_CFG, ensure_ascii=False)))
        md = B.render_facts(self.slug, {"name": "测试品牌"})
        self.assertIn("未经采样确认", md)
        unconfirmed_line = next(l for l in md.splitlines() if "竞品A" in l)
        self.assertIn("未经采样确认", unconfirmed_line)
        confirmed_line = next(l for l in md.splitlines() if "老牌竞品" in l)
        self.assertNotIn("未经采样确认", confirmed_line)


class TestDraftPromptCompetitors(WorkDirCase):
    def test_unconfirmed_competitors_excluded_from_prompt(self):
        self.write_config(json.loads(json.dumps(BASE_CFG, ensure_ascii=False)))
        outline = {
            "market": "cn", "target_question": "有什么好用的工具？", "type": "对比",
            "facts_to_use": [], "sections": ["开头", "对比"],
            "requirements": {"min_words": 800, "min_h2": 3},
        }
        prompts = []

        def fake_ask(plat, prompt, timeout=300):
            prompts.append(prompt)
            return {"ok": True, "answer": "# 初稿"}

        with mock.patch.object(S, "available", return_value=True), \
             mock.patch.object(S, "ask", side_effect=fake_ask):
            GEN.draft(self.slug, outline, provider="deepseek")
        prompt = prompts[0] if prompts else ""
        self.assertNotIn("竞品A", prompt, "confirmed:false 的竞品不得进初稿 prompt")
        self.assertNotIn("竞品B", prompt)
        self.assertIn("老牌竞品", prompt, "无 confirmed 字段的旧数据视为已确认")

    def test_draft_repairs_missing_extract_blocks_once_when_coverage_improves(self):
        self.write_config(json.loads(json.dumps(BASE_CFG, ensure_ascii=False)))
        outline = {
            "market": "cn", "target_question": "有什么好用的工具？", "type": "对比",
            "facts_to_use": ["已核实指标 A：1 个", "已核实指标 B：2 个", "已核实指标 C：3 个"],
            "sections": ["开头", "对比"],
            "requirements": {"min_words": 800, "min_h2": 3},
        }
        first = "## 开头\n测试品牌是一种工具。\n"
        repaired = """## 定义
测试品牌是一种工具。

## 数字事实
- 已核实指标 A：1 个
- 已核实指标 B：2 个
- 已核实指标 C：3 个

## 对比
|维度|测试品牌|通用做法|
|---|---|---|
|适用场景|待确认|待确认|

## 操作步骤
步骤 1：确认需求。

## FAQ
问：适合谁？
答：请按实际需求确认。
"""
        with mock.patch.object(S, "available", return_value=True), \
             mock.patch.object(S, "ask", side_effect=[
                 {"ok": True, "answer": first}, {"ok": True, "answer": repaired}]) as ask:
            result = GEN.draft(self.slug, outline, provider="deepseek")

        self.assertEqual(result, repaired.replace("## 数字事实", "## 关键数据与证据"))
        self.assertEqual(ask.call_count, 2, "缺抽取块时应只额外修订一次")

    def test_workbench_repair_returns_a_better_draft_without_writing_files(self):
        self.write_config(json.loads(json.dumps(BASE_CFG, ensure_ascii=False)))
        first = "## 开头\n测试品牌是一种工具。\n"
        repaired = """## 定义
测试品牌是一种工具。

## 数字事实
- 指标 A：1 个
- 指标 B：2 个
- 指标 C：3 个

## 对比
|维度|测试品牌|通用做法|
|---|---|---|
|适用场景|待确认|待确认|

## 操作步骤
步骤 1：确认需求。

## FAQ
问：适合谁？
答：请按实际需求确认。
"""
        with mock.patch.object(S, "available", return_value=True), \
             mock.patch.object(S, "ask", return_value={"ok": True, "answer": repaired}) as ask:
            result = GEN.complete_extract_blocks(self.slug, first, "cn")

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["text"], repaired.replace("## 数字事实", "## 关键数据与证据"))
        self.assertEqual(ask.call_count, 1, "手动补齐只调用一次 AI")
        self.assertFalse((self.pdir / "assets" / "drafts").exists(), "补齐前不应擅自写文件")

    def test_global_repair_requires_an_english_full_article(self):
        captured = {}

        def fake_ask(plat, prompt, timeout=300):
            captured["prompt"] = prompt
            return {"ok": True, "answer": "# English article"}

        with mock.patch.object(S, "ask", side_effect=fake_ask):
            GEN._repair_extract_blocks("custom", "# Draft", ["FAQ"], "- Fact: value", False)

        self.assertIn("complete English Markdown article", captured["prompt"])
        self.assertIn("entire revised article in English", captured["prompt"])

    def test_global_repair_rewrites_mixed_language_output_before_returning_it(self):
        self.write_config(json.loads(json.dumps(BASE_CFG, ensure_ascii=False)))
        mixed = "# English guide\n\n## 常见问题\n\n问：适合谁？\n答：开发者。"
        english = "# English guide\n\n## FAQ\n\nQ: Who is it for?\nA: Developers."
        with mock.patch.object(S, "pick_llm", return_value="custom"), \
             mock.patch.object(GEN, "_missing_extract_blocks", side_effect=[["FAQ"], []]), \
             mock.patch.object(S, "ask", side_effect=[
                 {"ok": True, "answer": mixed}, {"ok": True, "answer": english}]):
            result = GEN.complete_extract_blocks(self.slug, "# Draft", "global")

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], english)
        self.assertNotRegex(result["text"], r"[\u3400-\u9fff]")


if __name__ == "__main__":
    unittest.main()
