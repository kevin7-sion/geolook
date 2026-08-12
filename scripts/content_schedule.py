"""Strict daily content automation: question bank -> safe CMS drafts.

This job never creates a public post.  A candidate must pass every gate before
it reaches the CMS: extractable GEO blocks, resolved factual placeholders,
Global-language validation, and high-risk-claim linting.  Every non-push is
recorded in ``geo.json`` so the Automation screen can explain what happened.
"""

from __future__ import annotations

import re

import geolib as G


REASON_LABELS = {
    "draft_not_generated": "初稿未生成",
    "draft_missing": "找不到生成的初稿文件",
    "extract_blocks_missing": "仍缺少必备抽取块",
    "extract_repair_failed": "抽取块自动补齐失败",
    "unresolved_facts": "仍有待补或待确认事实",
    "fact_research_failed": "待补事实自动核查失败",
    "mixed_language": "Global 内容包含中文",
    "high_risk_lint": "存在高风险事实或编造提示",
    "cms_push_failed": "CMS 草稿创建失败",
}


def _config(slug: str) -> dict:
    return G.load_config(slug).get("content_schedule") or {}


def _global_english(text: str) -> bool:
    return not bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _blocked(question_id: str, path: str, reason: str, detail: str = "", **extra) -> dict:
    item = {"question_id": question_id, "path": path, "reason": reason,
            "label": REASON_LABELS.get(reason, reason)}
    if detail:
        item["detail"] = detail
    item.update(extra)
    return item


def _save_outcome(slug: str, cfg: dict, result: dict) -> None:
    """Persist a compact, secret-free status report for the Automation UI."""
    published, blocked, skipped = (result.get("published", []), result.get("blocked", []),
                                   result.get("skipped", []))
    schedule = dict(cfg.get("content_schedule") or {})
    schedule.update({
        "last_run": G.today(),
        "last_run_at": G.now_iso(),
        "last_result": f"已推送 {len(published)} 篇；未推送 {len(blocked)} 篇",
        "last_outcome": {
            "published": published,
            "blocked": blocked,
            "skipped": skipped,
            "selected": result.get("selected", []),
            "message": result.get("message", ""),
        },
    })
    # Preserve unrelated edits made through the dashboard while this long job ran.
    with G.project_lock(slug):
        latest = G.load_config(slug)
        latest["content_schedule"] = schedule
        G.save_config(slug, latest)


def run(slug: str, limit: int | None = None) -> dict:
    """Build safe CMS drafts; rejected drafts are saved locally with a reason."""
    import content_registry
    import generate
    import publish
    import research

    cfg = G.load_config(slug)
    schedule = _config(slug)
    count = max(1, min(int(limit or schedule.get("daily_count") or 1), 10))

    def finish(result: dict) -> dict:
        result.setdefault("published", [])
        result.setdefault("blocked", [])
        result.setdefault("skipped", [])
        result.setdefault("selected", [])
        result.setdefault("message", f"已创建 {len(result['published'])} 篇 CMS 草稿；未公开发布")
        content_registry.save(slug)
        _save_outcome(slug, cfg, result)
        return result

    if publish.missing_env("wisgate_cms"):
        return finish({"ok": False, "error": "WisGate CMS Token 未配置，未生成或推送内容",
                       "message": "未运行：WisGate CMS Token 未配置"})

    selected, skipped = content_registry.next_outlines(slug, count)
    selected_ids = [x["question_id"] for x in selected]
    if not selected:
        return finish({"ok": True, "selected": [], "skipped": skipped,
                       "message": "没有符合条件的未重复选题"})

    made = generate.run(slug, which=["drafts"], with_draft=True, draft_limit=len(selected),
                        draft_outlines=selected).get("assets", [])
    made_set = set(made)
    published, blocked = [], []

    for outline in selected:
        question_id = outline["question_id"]
        rel = f"assets/drafts/{question_id}.md"
        path = G.project_dir(slug) / rel
        if rel not in made_set:
            blocked.append(_blocked(question_id, rel, "draft_not_generated"))
            continue
        if not path.exists():
            blocked.append(_blocked(question_id, rel, "draft_missing"))
            continue

        try:
            text = path.read_text("utf-8", "replace")
            repaired = []

            # Report the most actionable Global failure before attempting a
            # repair that is required to return English-only output anyway.
            if cfg.get("market") == "global" and not _global_english(text):
                blocked.append(_blocked(question_id, rel, "mixed_language"))
                continue

            # Generation already makes one repair pass.  Run the same bounded
            # repair gateway here so scheduled jobs have an explicit hard gate.
            missing = generate._missing_extract_blocks(text)
            if missing:
                fixed = generate.complete_extract_blocks(slug, text, cfg.get("market", "cn"))
                if not fixed.get("ok"):
                    blocked.append(_blocked(question_id, rel, "extract_repair_failed",
                                            fixed.get("error", ""), missing=missing))
                    continue
                text = fixed.get("text", text)
                if fixed.get("changed"):
                    path.write_text(text, "utf-8")
                    repaired.append("extract_blocks")

            # Pending facts are resolved only from official evidence.  When no
            # source supports a claim, research writes the English disclosure
            # that it is not publicly disclosed rather than inventing an answer.
            pending = research.unresolved(text)
            if pending:
                facts = research.resolve(slug, text, cfg.get("market", "cn"))
                if not facts.get("ok"):
                    blocked.append(_blocked(question_id, rel, "fact_research_failed",
                                            facts.get("error", ""), fields=pending))
                    continue
                text = facts.get("text", text)
                if facts.get("changed"):
                    path.write_text(text, "utf-8")
                    repaired.append("facts")

            remaining_blocks = generate._missing_extract_blocks(text)
            if remaining_blocks:
                blocked.append(_blocked(question_id, rel, "extract_blocks_missing",
                                        ", ".join(remaining_blocks), missing=remaining_blocks))
                continue
            remaining_facts = research.unresolved(text)
            if remaining_facts:
                blocked.append(_blocked(question_id, rel, "unresolved_facts",
                                        ", ".join(remaining_facts), fields=remaining_facts))
                continue

            issues = generate.lint_draft(slug, path)
            high = [issue for issue in issues if issue.get("level") == "高"]
            if high:
                blocked.append(_blocked(question_id, rel, "high_risk_lint",
                                        high[0].get("detail", ""), issues=len(high)))
                continue

            result = publish.publish(slug, "wisgate_cms", rel)
            if result.get("ok"):
                published.append({"question_id": question_id, "path": rel,
                                  "url": result.get("url", ""), "repaired": repaired})
            else:
                blocked.append(_blocked(question_id, rel, "cms_push_failed",
                                        result.get("error", "")))
        except Exception as exc:  # keep the rest of the daily batch observable
            blocked.append(_blocked(question_id, rel, "cms_push_failed",
                                    f"{type(exc).__name__}: {exc}"))

    return finish({"ok": True, "selected": selected_ids, "skipped": skipped,
                   "published": published, "blocked": blocked,
                   "message": f"已推送 {len(published)} 篇 CMS 草稿；未推送 {len(blocked)} 篇"})
