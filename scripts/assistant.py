"""Context-aware GEO assistant for the local dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import geolib as G


QUICK_ACTIONS = [
    {"label": "内容工作台", "route": "workbench"},
    {"label": "品牌事实库", "route": "facts"},
    {"label": "行动计划", "route": "plan"},
    {"label": "站点体检", "route": "siteaudit"},
    {"label": "问题库", "route": "questions"},
    {"label": "部署资产", "route": "assets"},
]


def _count(path: Path, pattern: str) -> int:
    return len(list(path.glob(pattern))) if path.exists() else 0


def context(slug: str) -> dict:
    import generate

    pdir = G.project_dir(slug)
    cfg = G.load_config(slug)
    facts = generate.parse_facts(slug)
    tasks = G.read_json(pdir / "tasks.json", {}).get("tasks", [])
    audit = G.read_json(pdir / "audit.json", {})
    questions = cfg.get("questions", [])
    checklist = [
        {"label": "品牌一句话定义", "ok": bool(facts.get("definition")), "route": "facts"},
        {"label": "可核实数字", "ok": bool(facts.get("numbers")), "route": "facts"},
        {"label": "问题库", "ok": bool(questions), "detail": f"{len(questions)} 题", "route": "questions"},
        {"label": "内容大纲", "ok": _count(pdir / "assets" / "outlines", "*.md") > 0,
         "detail": f"{_count(pdir / 'assets' / 'outlines', '*.md')} 份", "route": "workbench"},
        {"label": "AI 初稿", "ok": _count(pdir / "assets" / "drafts", "*.md") > 0,
         "detail": f"{_count(pdir / 'assets' / 'drafts', '*.md')} 份", "route": "workbench"},
        {"label": "待完成行动", "ok": not any(t.get("status") in ("todo", "doing", "blocked") for t in tasks),
         "detail": f"{sum(t.get('status') in ('todo', 'doing', 'blocked') for t in tasks)} 项", "route": "plan"},
        {"label": "站点抽取块", "ok": not bool(audit.get("block_gap")), "route": "siteaudit"},
    ]
    return {"slug": slug, "brand": cfg.get("brand", {}).get("name", slug), "checklist": checklist,
            "actions": QUICK_ACTIONS}


def ask(slug: str, message: str) -> dict:
    import sample as S

    ctx = context(slug)
    platform = S.pick_llm()
    if not platform:
        return {"ok": False, "error": "没有可用的 LLM API Key", "context": ctx}
    compact = {
        "brand": ctx["brand"],
        "checklist": [{"item": x["label"], "ok": x["ok"], "detail": x.get("detail", "")}
                      for x in ctx["checklist"]],
    }
    prompt = f"""You are AtlasGEO, a practical GEO expert inside a local dashboard.
Answer the user's question in the same language as the user. Use only the project context below. Do not invent product facts, metrics, search results, competitors, pricing, or completed tasks. Say what is unknown and name the relevant dashboard screen to inspect. Keep the answer concise and operational.

Project context:
{json.dumps(compact, ensure_ascii=False)}

User question:
{message}"""
    result = S.ask(platform, prompt, timeout=120)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "AI 未返回内容", "context": ctx}
    return {"ok": True, "answer": result.get("answer", ""), "context": ctx}
