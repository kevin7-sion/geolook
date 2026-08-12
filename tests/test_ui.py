import re
import unittest
from pathlib import Path

UI = Path(__file__).parent.parent / "scripts" / "ui.html"


class DocumentLangCase(unittest.TestCase):
    """回归：界面语言切换必须同步 <html lang>（WCAG 3.1.1，issue #1）。"""

    def setUp(self):
        self.html = UI.read_text("utf-8")

    def test_default_lang_is_zh_cn(self):
        self.assertIn('<html lang="zh-CN">', self.html)

    def test_geolook_favicon_is_declared(self):
        self.assertIn('<link rel="icon" type="image/png" href="/favicon.png">', self.html)

    def test_facts_page_uses_market_specific_llms_file(self):
        self.assertIn("D.market==='global'?[['llms.en.txt','查看 llms.en.txt']]", self.html)
        self.assertIn("/assets/${file}", self.html)

    def test_workbench_can_generate_a_draft_for_the_open_outline(self):
        self.assertIn('onclick="wbDraft()">${q&&q.market===\'global\'?\'一键生成完整英文初稿\':\'一键生成完整 AI 初稿\'}', self.html)
        self.assertIn("'--question':qid", self.html)
        self.assertIn("'--asset':'drafts'", self.html)
        self.assertIn("if(R==='workbench')await loadWB(qid)", self.html)

    def test_ui_shows_content_lifecycle_and_safe_auto_draft_queue(self):
        self.assertIn("内容状态：", self.html)
        self.assertIn("自动内容队列", self.html)
        self.assertIn("runAction('auto-drafts'", self.html)
        self.assertIn("不会发布", self.html)
        self.assertIn("lifecycle=)draft", self.html)

    def test_global_question_bank_can_clean_legacy_chinese_rows(self):
        self.assertIn("清理中文题", self.html)
        self.assertIn("/api/clean-global-questions/", self.html)
        self.assertIn("Global 项目只能保存英文 global 问题", self.html)

    def test_keyword_mining_question_picker_supports_all_and_group_selection(self):
        self.assertIn("全选全部", self.html)
        self.assertIn("清空选择", self.html)
        self.assertIn("全选本类", self.html)
        self.assertIn('class="exp-group-check"', self.html)
        self.assertIn("function expSelectAll(on)", self.html)
        self.assertIn("function expSetGroup(group,on)", self.html)
        self.assertIn("function expSelectionChanged()", self.html)
        self.assertIn("box.indeterminate=selected>0&&selected<choices.length", self.html)

    def test_ui_can_configure_a_daily_cms_draft_schedule(self):
        self.assertIn("每日 CMS 草稿", self.html)
        self.assertIn("editContentSchedule()", self.html)
        self.assertIn("saveContentSchedule()", self.html)
        self.assertIn("不会自动公开发布", self.html)

    def test_daily_cms_draft_schedule_has_a_prominent_automation_center(self):
        self.assertIn("['automation','内容自动化']", self.html)
        self.assertIn("每日自动创建 WisGate CMS 草稿", self.html)
        self.assertIn("function vAutomation()", self.html)
        self.assertIn("saveAutomationCenter()", self.html)
        self.assertIn("CMS Status 固定为 draft", self.html)

    def test_automation_center_clearly_separates_active_and_unsaved_settings(self):
        self.assertIn("已生效配置", self.html)
        self.assertIn("修改配置", self.html)
        self.assertIn("保存后才会生效", self.html)
        self.assertIn('id="automation-unsaved"', self.html)
        self.assertIn("function automationDraftChanged()", self.html)
        self.assertIn("按当前配置立即运行", self.html)

    def test_automation_center_shows_last_pushed_and_not_pushed_reasons(self):
        self.assertIn("上一次自动化结果", self.html)
        self.assertIn("未推送原因（内容已保留在本地草稿，可在内容工作台修订）", self.html)
        self.assertIn("已创建 CMS Draft", self.html)
        self.assertIn("查看草稿", self.html)
        self.assertIn("严格推送门槛", self.html)
        self.assertIn("自动核查待补事实，查不到则标为未公开", self.html)

    def test_saving_schedule_settings_preserves_last_automation_outcome(self):
        self.assertIn("content_schedule:{...old,enabled,daily_count:count,time}", self.html)

    def test_workbench_can_regenerate_and_restore_a_previous_draft(self):
        self.assertIn('onclick="wbRegenerate()"', self.html)
        self.assertIn('onclick="wbRestorePrevious()"', self.html)
        self.assertIn("重新生成 AI 初稿", self.html)
        self.assertIn("回溯上一篇内容", self.html)
        self.assertIn("await loadWB(qid,'draft')", self.html)
        self.assertIn("function wbPreviousSource()", self.html)

    def test_workbench_can_repair_missing_extract_blocks_without_rewriting_a_draft(self):
        self.assertIn('onclick="wbCompleteBlocks()"', self.html)
        self.assertIn("'补齐缺少块'", self.html)
        self.assertIn("/api/draft-repair/", self.html)
        self.assertIn("请核对后保存", self.html)
        self.assertIn("正在补齐缺少块，请等待模型返回", self.html)
        self.assertIn("WB.repairing=true;render()", self.html)
        self.assertIn("WB.cur.kind!=='outline'&&ck&&ck.blocks", self.html)

    def test_workbench_can_search_and_safely_fill_unresolved_facts(self):
        self.assertIn('onclick="wbResearch()"', self.html)
        self.assertIn("/api/research/'+SLUG", self.html)
        self.assertIn("正在搜索官方来源…", self.html)
        self.assertIn("只有“官网”来源会被用于自动补充", self.html)
        self.assertIn("请先打开初稿或成稿", self.html)

    def test_geo_assistant_has_checklist_chat_and_fast_navigation(self):
        self.assertIn('id="assistant-fab"', self.html)
        self.assertIn('onclick="openAssistant()"', self.html)
        self.assertIn("function openAssistant()", self.html)
        self.assertNotIn("['assistant','GEO 助手']", self.html)
        self.assertIn("/api/assistant/'+SLUG", self.html)
        self.assertIn("项目检查清单", self.html)
        self.assertIn("function assistantSend()", self.html)
        self.assertIn("renderAssistantPopup()", self.html)

    def test_ulang_updates_document_lang(self):
        m = re.search(
            r"document\.documentElement\.lang\s*=\s*(\{[^}]*\})\[ULANG\]", self.html
        )
        self.assertIsNotNone(m, "ULANG 未同步到 document.documentElement.lang")
        mapping = m.group(1)
        self.assertIn("zh:'zh-CN'", mapping)
        self.assertIn("en:'en'", mapping)
        self.assertIn("ja:'ja'", mapping)

    def test_custom_api_configuration_fields_exist(self):
        self.assertIn('id="k-base"', self.html)
        self.assertIn('id="k-path"', self.html)
        self.assertIn('id="k-market"', self.html)
        self.assertIn('k.name_env', self.html)

    def test_brand_config_save_invalidates_stale_settings_cache(self):
        self.assertIn('SET_CFG=null;PROJECTS=null;', self.html)
        self.assertIn("保存失败：'+(r.error||'服务未返回原因')", self.html)

    def test_publishing_tokens_are_hidden_in_the_settings_modal(self):
        self.assertIn('class="input pub-env"', self.html)
        self.assertIn('type="password"', self.html)

    def test_only_an_ai_draft_can_be_promoted_to_final_content(self):
        self.assertIn("WB.cur&&WB.cur.kind==='draft'?`<button", self.html)
        self.assertIn("请先从大纲生成完整 AI 初稿，再发布为成稿", self.html)
        self.assertIn("await loadWB(qid,'content');", self.html)

    def test_api_error_responses_keep_the_server_error_message(self):
        self.assertIn("error:data.error||`HTTP ${r.status}`", self.html)

    def test_cms_publish_has_keyword_review_before_push(self):
        self.assertIn("/api/publish-preview/'+SLUG", self.html)
        self.assertIn("CMS 发布前审核", self.html)
        self.assertIn("publishCmsReviewed()", self.html)
        self.assertIn("options:{tags}", self.html)
        self.assertIn("关键词 / Tags（固定 3 个）", self.html)
        self.assertIn("请填写完整的 3 个关键词标签", self.html)

    def test_cms_publish_review_shows_metadata_and_located_lint_issues(self):
        self.assertIn('>Title</div>', self.html)
        self.assertIn("r.title||'未设置标题'", self.html)
        self.assertIn("const title=($('#cms-review-title')?.textContent||'').trim()", self.html)
        self.assertIn("path:rel,title,options:{tags}", self.html)
        self.assertIn("<label>Summary</label>", self.html)
        self.assertIn(">Slug</div>", self.html)
        self.assertIn("Status: ${esc(r.status||'draft')}", self.html)
        self.assertIn("第 ${esc(String(i.line||'?'))} 行", self.html)
        self.assertIn("保持原样并创建 CMS 草稿", self.html)


if __name__ == "__main__":
    unittest.main()
