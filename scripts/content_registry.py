"""Content lifecycle registry and conservative duplicate guard.

The registry is derived from project files, so it remains correct after a
manual edit or a Git restore.  It records a stable topic fingerprint inside
each generated draft/final file and exposes lifecycle status to the dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

import geolib as G

META_RE = re.compile(r"<!--\s*geolook-content:\s*.*?\s*-->", re.S)


def topic_key(text: str) -> str:
    """A stable language-neutral key used for exact duplicate detection."""
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", (text or "").lower())[:240]


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2}", (text or "").lower())
    return set(words)


def overlap(a: str, b: str) -> float:
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def marker(question_id: str, question: str, lifecycle: str) -> str:
    # Plain attributes make the marker readable without requiring JSON parsing.
    return (f"<!-- geolook-content: question_id={question_id}; lifecycle={lifecycle}; "
            f"topic_key={topic_key(question)}; generated_at={G.now_iso()} -->")


def with_marker(text: str, question_id: str, question: str, lifecycle: str) -> str:
    text = META_RE.sub("", text).lstrip()
    return marker(question_id, question, lifecycle) + "\n\n" + text


def _has_question(text: str, question_id: str, path: Path) -> bool:
    return question_id in text[:1500] or path.name.startswith(question_id + "-")


def build(slug: str) -> dict:
    """Return the current lifecycle and duplicate verdict for every question."""
    pdir = G.project_dir(slug)
    cfg = G.load_config(slug)
    outlines = G.read_json(pdir / "assets" / "outlines" / "_index.json", []) or []
    outline_by_id = {o.get("question_id"): o for o in outlines if o.get("question_id")}
    questions = cfg.get("questions", [])
    records: dict[str, dict] = {}
    for q in questions:
        qid, text = q.get("id", ""), q.get("text", "")
        records[qid] = {"question_id": qid, "question": text, "topic_key": topic_key(text),
                        "status": "not_started", "paths": [], "duplicate": None}
        if q.get("market") == "global" and re.search(r"[\u3400-\u9fff]", text):
            records[qid]["status"] = "invalid_language"
        if qid in outline_by_id:
            records[qid]["status"] = "outline"
            records[qid]["paths"].append(f"assets/outlines/{qid}.md")

    drafts = pdir / "assets" / "drafts"
    for qid, rec in records.items():
        file = drafts / f"{qid}.md"
        if file.exists():
            rec["status"] = "draft"
            rec["paths"].append(f"assets/drafts/{qid}.md")

    cdir = pdir / "content"
    if cdir.exists():
        for file in cdir.glob("*.md"):
            text = file.read_text("utf-8", "replace")
            for qid, rec in records.items():
                if _has_question(text, qid, file):
                    rec["status"] = "final"
                    rec["paths"].append(f"content/{file.name}")

    published = G.read_json(pdir / "publishes.json", []) or []
    published_paths = {r.get("path") for r in published if r.get("ok")}
    for rec in records.values():
        if any(path in published_paths for path in rec["paths"]):
            rec["status"] = "cms_draft"

    completed = [r for r in records.values() if r["status"] in {"draft", "final", "cms_draft"}]
    for rec in records.values():
        for other in completed:
            if other["question_id"] == rec["question_id"]:
                continue
            if rec["topic_key"] and rec["topic_key"] == other["topic_key"]:
                rec["duplicate"] = {"level": "exact", "question_id": other["question_id"],
                                    "question": other["question"], "score": 1.0}
                break
            score = overlap(rec["question"], other["question"])
            if score >= 0.78:
                rec["duplicate"] = {"level": "similar", "question_id": other["question_id"],
                                    "question": other["question"], "score": round(score, 2)}
                break
    return {"generated_at": G.now_iso(), "records": list(records.values())}


def save(slug: str) -> dict:
    registry = build(slug)
    G.write_json(G.project_dir(slug) / "content_registry.json", registry)
    return registry


def next_outlines(slug: str, limit: int) -> tuple[list[dict], list[dict]]:
    """Return unproduced, non-duplicate outlines and an auditable skip list."""
    pdir = G.project_dir(slug)
    registry = build(slug)
    by_id = {x["question_id"]: x for x in registry["records"]}
    outlines = G.read_json(pdir / "assets" / "outlines" / "_index.json", []) or []
    selected, skipped = [], []
    for outline in outlines:
        rec = by_id.get(outline.get("question_id"), {})
        if rec.get("status") in {"draft", "final", "cms_draft"}:
            skipped.append({"question_id": outline.get("question_id"), "reason": rec["status"]})
        elif rec.get("status") == "invalid_language":
            skipped.append({"question_id": outline.get("question_id"), "reason": "invalid_global_language"})
        elif rec.get("duplicate"):
            skipped.append({"question_id": outline.get("question_id"),
                            "reason": "duplicate_" + rec["duplicate"]["level"],
                            "matches": rec["duplicate"]["question_id"]})
        else:
            selected.append(outline)
        if len(selected) >= limit:
            break
    return selected, skipped
