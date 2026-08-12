"""资产生成器：把工单变成可以直接部署/发布的东西。

产出到 `work/<slug>/assets/`，中英分开：
  llms.txt / llms.en.txt        官方事实索引，传到网站根目录
  jsonld/*.json                 每种页面类型的 JSON-LD，直接贴进 <head>
  snippets/definition.*.html    定义块（首屏用）
  snippets/faq.*.html           FAQ 块，含可见正文 + FAQPage schema
  outlines/*.md                 每个目标问题一份内容大纲（证据页骨架）
  drafts/*.md                   可选：调用已配的 LLM API 出全文初稿

设计分工：**结构性资产由代码确定性生成**（不会漏 schema 字段、不会写错格式）；
**文章正文由 Claude 或 LLM 按 outline 写**（代码写不出好文案）。
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path

import geolib as G

# ---------------------------------------------------------------- 事实卡解析

def parse_facts(slug: str) -> dict:
    """从 content/facts.md 里抽出结构化事实。抽不到就返回空，调用方负责提示。"""
    p = G.project_dir(slug) / "content" / "facts.md"
    if not p.exists():
        return {}
    text = p.read_text("utf-8")
    out = {"definition": "", "numbers": [], "suitable": [], "unsuitable": [], "raw": text}

    # 一句话定义：兼容看板文案「一句话定位」和英文事实卡标题；
    # 整个引用块可能跨多行，要合并，否则会在句子中间截断。
    m = re.search(
        r"(?im)^#{1,6}[ \t]*\**(?:一句话定义|一句话定位|一行定位|"
        r"one[- ]line\s+(?:definition|positioning)|definition|positioning)\**"
        r"[^\n]*\n(.*?)(?=\n#{1,6}[ \t]|\Z)",
        text,
        re.S,
    )
    if m:
        body = m.group(1)
        quoted = [l.strip()[1:].strip() for l in body.split("\n") if l.strip().startswith(">")]
        if quoted:
            line = " ".join(quoted)
        else:
            line = next((l.strip() for l in body.split("\n")
                         if l.strip() and not l.strip().startswith(("#", "-", "|"))), "")
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)      # 去掉 markdown 加粗
        line = re.sub(r"`(.+?)`", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip()
        # 中文换行合并会留下多余空格（"生成 整体方案"、"SaaS： 把"）。
        # 汉字和全角标点两侧的空格都要去掉，否则会带进 JSON-LD description。
        CJK = r"[一-鿿　-〿＀-￯]"
        out["definition"] = re.sub(rf"(?<={CJK}) (?={CJK})", "", line)

    # 关键数字表：| 事实 | 数值 | 来源 | 证据 |
    m = re.search(r"##\s*关键数字.*?\n(.*?)(?=\n##|\Z)", text, re.S)
    if m:
        for row in re.findall(r"^\|([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|", m.group(1), re.M):
            a, b, c = (x.strip() for x in row)
            if a and a not in ("事实", "---", "项") and not set(a) <= set("-: "):
                out["numbers"].append({"fact": a, "value": b, "source": c})

    m = re.search(r"\*\*适合\*\*[：:]?(.*?)(?=\*\*不适合|##|\Z)", text, re.S)
    if m:
        out["suitable"] = [l.strip("- ").strip() for l in m.group(1).split("\n") if l.strip().startswith("-")]
    m = re.search(r"\*\*不适合.*?\*\*[：:]?(.*?)(?=\n##|\Z)", text, re.S)
    if m:
        out["unsuitable"] = [l.strip("- ").strip() for l in m.group(1).split("\n") if l.strip().startswith("-")]
    return out


# ---------------------------------------------------------------- llms.txt

def _has_cjk(value) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", str(value or "")))


def _english_only_revision(plat: str, text: str, facts: str = "") -> str:
    """Return a publishable English-only rewrite, or an empty value on failure.

    Global assets must not silently become mixed-language when an API ignores an
    otherwise explicit prompt.  A second, bounded pass is safer than saving a
    partially Chinese article for later CMS publication.
    """
    import sample as S

    if not _has_cjk(text):
        return text
    prompt = f"""You are the final language editor for a publishable Markdown article.

Rewrite the complete article below in natural, reader-facing English. Translate every Chinese heading, paragraph, table cell, label, FAQ question, and placeholder. The returned Markdown must contain no Chinese characters at all. Keep URLs, exact product names, verified numbers, and supported claims unchanged. Do not add facts. If a value is not supported by the supplied facts, write: "Not publicly disclosed in the official sources reviewed." Return only the complete English Markdown article, with no explanation.

Verified facts, if any:
{facts or '(none)'}

Article to rewrite:
---
{text}
---"""
    result = S.ask(plat, prompt, timeout=300)
    candidate = _clean_internal_block_headings(result.get("answer", "")) if result.get("ok") else ""
    return candidate if candidate and not _has_cjk(candidate) else ""


def _english(value, fallback: str) -> str:
    text = str(value or "").strip()
    return fallback if not text or _has_cjk(text) else text


def _english_list(values) -> list[str]:
    return [_english(v, "") for v in (values or []) if _english(v, "")]


def _unknown(name: str, field: str) -> str:
    return f"{name}'s {field} is not publicly disclosed in the official sources reviewed."


def gen_llms_txt(slug: str, lang: str = "en") -> str:
    cfg = G.load_config(slug)
    f = parse_facts(slug)
    b = cfg["brand"]
    audit = G.read_json(G.project_dir(slug) / "audit.json", {})
    pages = sorted(audit.get("pages", []), key=lambda p: -p["score"])[:12]

    name = _english(b.get("name"), "This product")
    definition = _english(f.get("definition"), _unknown(name, "one-line public positioning"))
    L = [f"# {name}", "", f"> {definition}", "", "## Key facts", ""]
    L.append(f"- Website: {b.get('site', '')}")
    aliases = _english_list(b.get("aliases"))
    if aliases:
        L.append(f"- Also known as: {', '.join(aliases)}")
    for label, key in (("Industry", "industry"), ("For", "target_users")):
        value = _english(b.get(key), "")
        if value:
            L.append(f"- {label}: {value}")
    metric_count = 0
    for n in f.get("numbers", [])[:8]:
        fact, value = _english(n.get("fact"), ""), _english(n.get("value"), "")
        if fact and value:
            L.append(f"- {fact}: {value}")
            metric_count += 1
    if not metric_count:
        L.append(f"- Verified public metrics: {_unknown(name, 'verified public metrics').split(' is ', 1)[-1]}")

    L += ["", "## Important pages", ""]
    page_count = 0
    for p in pages:
        title = (p.get("title") or p["url"]).split("|")[0].split("｜")[0].strip()[:60]
        if _has_cjk(title):
            title = "Official page"
        L.append(f"- [{title or 'Official page'}]({p['url']})")
        page_count += 1
    if not page_count:
        L.append(f"- [Official website]({b.get('site', '')})")

    L += ["", "## Scope", ""]
    suitable, unsuitable = _english_list(f.get("suitable")), _english_list(f.get("unsuitable"))
    if suitable:
        L.extend(f"- Good fit: {s}" for s in suitable[:5])
    if unsuitable:
        L.extend(f"- Not a fit: {s}" for s in unsuitable[:5])
    if not suitable and not unsuitable:
        L.append(f"- Scope boundaries: {_unknown(name, 'supported use cases')}")

    L += ["", "## Disambiguation", "", f"- Canonical name: {name}"]
    parent = _english(b.get("parent"), "")
    if parent:
        L.append(f"- Parent: {parent}" + (f" ({b['parent_url']})" if b.get("parent_url") else ""))
    for line in _english_list(b.get("disambiguation")):
        L.append(f"- {line}")
    if not b.get("disambiguation"):
        L.append("- Not related to similarly-named products in other industries.")
    L += ["", f"<!-- generated by geo skill on {G.today()} -->"]
    return "\n".join(L)


# ---------------------------------------------------------------- JSON-LD

def gen_jsonld(slug: str) -> dict[str, dict]:
    cfg = G.load_config(slug)
    f = parse_facts(slug)
    b = cfg["brand"]
    name = _english(b.get("name"), "This product")
    desc = _english(f.get("definition"), _unknown(name, "one-line public positioning"))
    site = b["site"].rstrip("/")

    org = {
        "@context": "https://schema.org", "@type": "Organization",
        "name": name, "url": site, "description": desc,
        "alternateName": _english_list(b.get("aliases")),
        "sameAs": [x for x in (b.get("same_as") or []) if x and not _has_cjk(x)],
    }
    if b.get("parent"):
        org["parentOrganization"] = {"@type": "Organization", "name": _english(b["parent"], name),
                                     **({"url": b["parent_url"]} if b.get("parent_url") else {})}
    if b.get("founding_date"):
        org["foundingDate"] = b["founding_date"]
    if b.get("knows_about"):
        org["knowsAbout"] = _english_list(b["knows_about"])

    app = {
        "@context": "https://schema.org", "@type": "SoftwareApplication",
        "name": name, "url": site, "description": desc,
        "applicationCategory": _english(b.get("application_category"), "BusinessApplication"),
        "operatingSystem": "Web",
        "publisher": {"@type": "Organization", "name": _english(b.get("parent"), name) if b.get("parent") else name},
    }
    offers = b.get("offers")
    if offers:
        out_offers = []
        for o in offers:
            offer_name = _english(o.get("name"), "Public offer")
            price = str(o.get("price", "")).strip()
            currency = _english(o.get("currency"), "")
            if not price or _has_cjk(price) or not currency or _has_cjk(currency):
                continue
            item = {"@type": "Offer", "name": offer_name, "price": price,
                    "priceCurrency": currency}
            if o.get("desc") and not _has_cjk(o["desc"]):
                item["description"] = o["desc"]
            out_offers.append(item)
        if out_offers:
            app["offers"] = out_offers
    if b.get("audience"):
        audience = _english(b["audience"], "")
        if audience:
            app["audience"] = {"@type": "Audience", "audienceType": audience}

    questions = [q for q in cfg.get("questions", [])
                 if q.get("market") in ("global", "both") and not _has_cjk(q.get("text"))][:8]
    if not questions:
        questions = [{"text": f"What is {name}?"}, {"text": f"Who is {name} for?"},
                     {"text": "What information is publicly available?"}]
    faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q["text"],
         "acceptedAnswer": {"@type": "Answer", "text": _unknown(name, "definitive answer to this question")}}
        for q in questions]}

    article = {
        "@context": "https://schema.org", "@type": "Article",
        "headline": f"{name} | Official information",
        "datePublished": G.today(), "dateModified": G.today(),
        "author": {"@type": "Organization", "name": name},
        "publisher": {"@type": "Organization", "name": name},
        "about": desc,
    }

    breadcrumb = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Home", "item": site},
        {"@type": "ListItem", "position": 2, "name": "Information", "item": site},
    ]}
    return {"organization": org, "software-application": app, "faq-page": faq,
            "article": article, "breadcrumb": breadcrumb}


# ---------------------------------------------------------------- HTML 片段

def gen_definition_block(slug: str, lang: str = "zh") -> str:
    f = parse_facts(slug)
    cfg = G.load_config(slug)
    b = cfg["brand"]
    name = _english(b.get("name"), "This product")
    d = _english(f.get("definition"), _unknown(name, "one-line public positioning"))
    nums = [n for n in f.get("numbers", []) if not _has_cjk(n.get("fact")) and not _has_cjk(n.get("value"))][:4]
    items = "".join(f'\n    <li><strong>{html.escape(n["value"])}</strong> — {html.escape(n["fact"])}</li>'
                    for n in nums)
    if not items:
        items = f'\n    <li>{html.escape(_unknown(name, "verified public metrics"))}</li>'
    dis = _english_list(b.get("disambiguation"))
    dis_html = ("\n  <p class=\"geo-disambiguation\"><small>"
                + " ".join(html.escape(x) for x in dis) + "</small></p>") if dis else ""
    return f"""<!-- Definition block for the hero section and AI extraction. -->
<section class="geo-definition">
  <h2>{html.escape(name)}: what it is</h2>
  <p>{html.escape(d)}</p>
  <ul>{items}
  </ul>{dis_html}
</section>
<!-- Keep this definition consistent with llms.txt and JSON-LD description. -->"""


def gen_faq_block(slug: str, lang: str = "zh") -> str:
    cfg = G.load_config(slug)
    name = _english(cfg.get("brand", {}).get("name"), "This product")
    qs = [q for q in cfg.get("questions", [])
          if q.get("market") in ("global", "both") and not _has_cjk(q.get("text"))][:8]
    if not qs:
        qs = [{"text": f"What is {name}?"}, {"text": f"Who is {name} for?"},
              {"text": "What information is publicly available?"}]
    body = "\n".join(
        f"""  <details open>
    <summary><h3>{html.escape(q['text'])}</h3></summary>
    <p>{html.escape(_unknown(name, 'definitive answer to this question'))}</p>
  </details>""" for q in qs)
    return f"""<!-- FAQ block. Answers remain visible in static HTML for crawlers. -->
<section class="geo-faq">
  <h2>FAQ</h2>
{body}
</section>"""


# ---------------------------------------------------------------- 内容大纲

OUTLINE_TMPL = {
    "定义型": ["什么是 {topic}（一句定义 + 展开）", "{topic} 包含哪几部分", "{topic} 的关键数字（表格，每行带来源）",
               "{topic} 和 {alt} 有什么区别（对比表）", "{topic} 适合谁、不适合谁",
               "怎么开始用 {topic}（编号步骤）", "常见问题", "参考来源"],
    "对比型": ["结论先行：谁适合选哪个", "对比维度与口径说明", "核心对比表（同口径 6–10 个维度）",
               "各自的局限（必须写自己的短板）", "按场景怎么选（决策树）", "价格与总拥有成本",
               "常见问题", "参考来源与核验日期"],
    "榜单型": ["评选方法与数据来源（利益披露）", "总览榜单表", "逐个点评（每个含定位/优势/局限/适合谁）",
               "怎么根据自己情况选", "常见问题", "参考来源"],
    "教程型": ["这篇能解决什么问题", "开始前需要准备什么", "分步操作（编号 + 截图位）",
               "常见报错与排查", "进阶技巧", "相关概念解释", "常见问题", "参考来源"],
}

GROUP2TYPE = {"推荐": "榜单型", "比较": "对比型", "替代": "对比型", "价格": "定义型",
              "风险": "定义型", "品牌验证": "定义型", "场景": "教程型"}


def gen_outlines(slug: str) -> list[dict]:
    cfg = G.load_config(slug)
    f = parse_facts(slug)
    b = cfg["brand"]
    comps = [c["name"] for c in cfg.get("competitors", [])
             if c.get("confirmed") is not False]
    out = []
    for q in cfg.get("questions", []):
        if q.get("market") == "global" and _has_cjk(q.get("text", "")):
            G.info(f"跳过非英文 Global 问题 {q.get('id')}；先运行 clean-global-questions 清理")
            continue
        typ = GROUP2TYPE.get(q.get("group", ""), "定义型")
        mk = q.get("market", cfg.get("market", "cn"))
        topic = q["text"].rstrip("？?")
        alt = comps[0] if comps else ("竞品" if mk == "cn" else "alternatives")
        secs = [s.format(topic=b["name"], alt=alt) for s in OUTLINE_TMPL[typ]]
        out.append({
            "question_id": q.get("id"), "market": mk, "type": typ,
            "target_question": q["text"],
            "title_candidates": _titles(q["text"], b["name"], mk),
            "sections": secs,
            "requirements": {
                "min_words": 1200 if typ in ("对比型", "榜单型") else 1000,
                "min_h2": 8, "list_density": ">=0.35",
                "must_have_blocks": ["定义", "数字事实", "对比", "操作步骤", "FAQ"],
                "evidence": "每个数字带来源和核验日期；无法核实的标『待确认』",
            },
            "facts_to_use": [n["fact"] + "：" + n["value"] for n in f.get("numbers", [])[:5]],
        })
    return out


def _titles(question: str, brand: str, market: str) -> list[str]:
    """标题候选：对题性是影响力最强的预测因子（r=0.432），所以标题必须含问题原词。"""
    q = question.rstrip("？?").strip()
    if market == "global":
        return [q, f"{q} — a practical guide ({G.today()[:4]})",
                f"{q} Compared: features, pricing and limits"]
    return [q, f"{q}（{G.today()[:4]} 版）",
            f"{q}｜含对比表、数字和操作步骤", f"{q}——{brand}的答案与边界"]


# ---------------------------------------------------------------- LLM 初稿

def _missing_extract_blocks(text: str) -> list[str]:
    """Return required extraction blocks that the shared pre-check cannot find."""
    import analytics

    report = analytics.precheck(text)
    return [name for name, present in report["blocks"].items() if not present]


def _clean_internal_block_headings(text: str) -> str:
    """Keep audit labels out of reader-facing drafts without removing evidence."""
    text = re.sub(r"(?im)^(#{1,6}\s*)(?:\*\*)?数字事实(?:\*\*)?\s*$",
                  r"\1关键数据与证据", text)
    return re.sub(r"(?im)^(#{1,6}\s*)(?:\*\*)?numeric facts?(?:\*\*)?\s*$",
                  r"\1Key data and evidence", text)


def _repair_extract_blocks(plat: str, text: str, missing: list[str], facts: str,
                           zh: bool) -> str:
    """Ask for one bounded repair pass; never trade factual safety for a score."""
    import sample as S

    if zh:
        prompt = f"""你是严谨的 GEO 编辑。下面是一篇中文初稿，其中缺少这些可抽取结构：{'、'.join(missing)}。

只做一次修订：返回完整的中文 Markdown 文章，而不是补丁、说明或待办清单。保留文章主题、原有结构与已核实事实；在同一次输出中补上列出的所有缺失结构，使读者和 AI 能直接抽取答案。

事实安全规则（优先级高于所有结构要求）：
- 不得新增客户名、价格、资质、竞品参数、市场数据或任何无法从原文/下列事实核实的数字。
- 缺少可核实数字时，明确写「待确认」，不要编造三个数字来满足检测。
- 需要对比但没有可核实竞品信息时，只比较适用场景或通用做法，并说明边界。
- 不要把“数字事实”作为标题；将数字自然放入相关段落、表格或来源说明。
- FAQ 必须是一个可见的末尾小节，使用「常见问题」或 "FAQ" 标题，并至少提供 3 组问题与直接答案。
- 直接输出修订后的完整 Markdown 正文，不要解释修改过程。

可使用的已核实事实：
{facts}

原稿：
---
{text}
---"""
    else:
        prompt = f"""You are a rigorous GEO editor. The English draft below is missing these extractable blocks: {', '.join(missing)}.

Make one revision only. Return one complete English Markdown article, not a patch, explanation, or task list. Preserve the topic, existing structure, and verified facts. In the same response, add every missing block listed above so readers and AI systems can extract the answer directly.

Factual-safety rules take priority over formatting:
- Do not add customer names, prices, certifications, competitor specifications, market data, or numbers that cannot be verified from the original draft or the verified facts below.
- If verified numbers are unavailable, say that the item is to be confirmed. Do not invent three numbers just to pass a checker.
- If verified competitor details are unavailable, compare decision criteria or general approaches and state the boundary.
- Do not use "Numeric facts" as a heading; integrate verified numbers into relevant sections, tables, or source notes.
- End with a visible "FAQ" or "Frequently Asked Questions" section containing at least three question-and-answer pairs.
- Write the entire revised article in English, except for exact proper names or source titles.

Verified facts you may use:
{facts}

Original draft:
---
{text}
---"""
    res = S.ask(plat, prompt, timeout=300)
    return _clean_internal_block_headings(res.get("answer", "")) if res.get("ok") else ""


def complete_extract_blocks(slug: str, text: str, market: str = "cn") -> dict:
    """Repair a Workbench draft in memory; the UI decides whether to save it."""
    import sample as S

    missing = _missing_extract_blocks(text)
    if not missing:
        return {"ok": True, "changed": False, "text": text, "missing": [],
                "message": "抽取块已齐全"}
    plat = S.pick_llm()
    if not plat:
        return {"ok": False, "error": "没有可用的 LLM API Key", "text": text,
                "missing": missing}
    facts_data = parse_facts(slug)
    facts = "\n".join(f"- {x['fact']}：{x['value']}" for x in facts_data.get("numbers", []))
    facts = facts or "（无结构化事实；不得编造品牌数据或数字）"
    revised = _clean_internal_block_headings(
        _repair_extract_blocks(plat, text, missing, facts, market != "global")
    )
    if market == "global":
        revised = _english_only_revision(plat, revised, facts)
    if not revised:
        error = ("AI 未返回全英文修订内容，已保留原文" if market == "global"
                 else "AI 未返回可用修订内容")
        return {"ok": False, "error": error, "text": text,
                "missing": missing}
    remaining = _missing_extract_blocks(revised)
    if len(remaining) >= len(missing):
        return {"ok": True, "changed": False, "text": text, "missing": missing,
                "message": "AI 未能补齐更多抽取块，已保留原文"}
    fixed = [name for name in missing if name not in remaining]
    return {"ok": True, "changed": True, "text": revised, "missing": remaining,
            "fixed": fixed, "message": f"已补齐：{'、'.join(fixed)}"}


def draft(slug: str, outline: dict, provider: str | None = None) -> str:
    """用已配置的 LLM API 按大纲出初稿。没有可用 Key 就返回空。"""
    import sample as S

    plat = S.pick_llm(provider)
    if not plat:
        return ""
    cfg = G.load_config(slug)
    f = parse_facts(slug)
    b = cfg["brand"]
    zh = outline["market"] != "global"
    facts = "\n".join(f"- {x}" for x in outline["facts_to_use"]) or "（无结构化事实，只写通用内容，不要编造品牌数据）"
    secs = "\n".join(f"{i+1}. {s}" for i, s in enumerate(outline["sections"]))
    req = outline["requirements"]
    mk = outline["market"]
    comps = [c["name"] for c in cfg.get("competitors", [])
             if (c.get("market") in (mk, "both", None) or mk == "both")
             and c.get("confirmed") is not False]
    comp_rule = (
        "只能提到下面这些真实竞品，**严禁发明任何其它产品名**（不要写「工具A」「某某Pro」这类占位）：\n"
        + "\n".join(f"- {c}" for c in comps)
        if comps else
        "**本项目还没有确认的竞品清单，因此绝对不要在文中点名任何竞品**，"
        "对比部分改成与「通用大模型」「人工手写」等品类做对比。"
    )
    prompt = (
        f"""你是 GEO（生成式引擎优化）内容工程师。按下面的骨架写一篇可直接发布的{'中文' if zh else '英文'}文章。

当前年份是 {G.today()[:4]} 年，涉及年份时一律用 {G.today()[:4]}，不要写更早的年份。

目标问题（读者会这样问 AI）：{outline['target_question']}
文章类型：{outline['type']}
品牌：{b['name']}（{b.get('industry','')}）

必须使用的已核实事实（不得改动数值，不得编造新数据）：
{facts}

竞品纪律：
{comp_rule}

章节骨架：
{secs}

硬性要求：
- 正文不少于 {req['min_words']} 词，H2 小节 ≥ {req['min_h2']} 个
- 必须包含：一句可直接摘走的定义、带单位的数字、一个对比表、一个编号步骤块、FAQ
- 不要使用“数字事实”或“Numeric facts”作为文章标题；把已核验数字自然放入定义、比较、步骤或证据来源中
- 末尾必须有一个可见的 FAQ 小节（标题使用「常见问题」、"FAQ" 或 "Frequently Asked Questions"），至少三组问答；每个问题用问号结尾并紧跟直接答案
- 列表密度高一些，要点用无序/有序列表而不是长段落
- 写清楚适用与**不适用**边界，不要只说好话
- **严禁编造**：客户名、价格、资质、市场数据、竞品参数。宁可不写，也不要写占位数据。
  确实需要但手上没有的信息，写成「（待补：xxx）」，不要用假数字凑表格
- 直接输出 Markdown 正文，不要解释、不要前后缀"""
    )
    res = S.ask(plat, prompt, timeout=300)
    text = _clean_internal_block_headings(res.get("answer", "")) if res.get("ok") else ""
    if not text:
        return ""
    if not zh:
        text = _english_only_revision(plat, text, facts)
        if not text:
            G.info("初稿未能生成全英文内容，未保存混合语言版本")
            return ""

    # The first prompt is deliberately strict, but models still occasionally omit a
    # table, FAQ, or step block.  Repair only when the shared pre-check finds a
    # concrete gap, and retain the original if the repair does not improve coverage.
    missing = _missing_extract_blocks(text)
    if not missing:
        return text
    G.info(f"初稿缺少抽取块：{'、'.join(missing)}；尝试自动优化一次")
    revised = _clean_internal_block_headings(_repair_extract_blocks(plat, text, missing, facts, zh))
    if not zh:
        revised = _english_only_revision(plat, revised, facts)
    if not revised:
        G.info("抽取块自动优化未返回内容，保留原初稿")
        return text
    remaining = _missing_extract_blocks(revised)
    if len(remaining) < len(missing):
        fixed = [name for name in missing if name not in remaining]
        G.info(f"抽取块自动优化完成：补齐 {'、'.join(fixed)}")
        return revised
    G.info("抽取块自动优化没有提升覆盖度，保留原初稿")
    return text


# ---------------------------------------------------------------- 初稿风险检查

FAKE_HINTS = [
    (r"工具\s*[A-Z一二三四五六七八九十]\b", "出现「工具A/工具一」这类占位竞品名"),
    (r"某某|XX公司|xxx公司|示例公司", "出现占位公司名"),
    (r"(?i)\b(acme|foobar|example corp|competitor [a-z])\b", "出现占位英文品牌名"),
]


def _lint_location(text: str, offset: int) -> dict:
    """Return a reader-meaningful location for a lint finding."""
    before = text[:max(0, offset)]
    headings = re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", before)
    return {"line": before.count("\n") + 1, "section": headings[-1][:100] if headings else "开头"}


def lint_draft(slug: str, path: Path) -> list[dict]:
    """交付前的编造风险检查。宁可误报，也不能让编造内容进客户交付包。"""
    import re as _re

    cfg = G.load_config(slug)
    f = parse_facts(slug)
    text = path.read_text("utf-8")
    known = {cfg["brand"]["name"], *cfg["brand"].get("aliases", [])}
    known |= {c["name"] for c in cfg.get("competitors", [])}
    for c in cfg.get("competitors", []):
        known |= set(c.get("aliases", []) or [])

    issues = []
    for m in _re.finditer(r"(?i)\b(?:todo|tbd)\b|待补(?:充)?", text):
        issues.append({"level": "中", "type": "未完成占位", "detail": "出现待补或 TODO 标记，发布前应补充、删除或明确标为未公开",
                       "excerpt": text[max(0, m.start() - 30):m.end() + 30].replace("\n", " "),
                       **_lint_location(text, m.start())})
    for pat, desc in FAKE_HINTS:
        for m in _re.finditer(pat, text):
            issues.append({"level": "高", "type": "疑似编造", "detail": desc,
                           "excerpt": text[max(0, m.start() - 30):m.end() + 30].replace("\n", " "),
                           **_lint_location(text, m.start())})

    # 事实卡里没有的数字，且没标「待确认/待补」→ 需人工核
    known_values = {n["value"] for n in f.get("numbers", [])}
    for m in _re.finditer(r"[^\n|]*?(\d[\d,\.]*\s*(?:%|％|万|亿|倍|元|美元|港币|HK\$|\$|人|家|天|小时|分钟))[^\n|]*", text):
        seg, val = m.group(0), m.group(1)
        if any(val in v or v in val for v in known_values):
            continue
        if "待确认" in seg or "待补" in seg:
            continue
        issues.append({"level": "中", "type": "未核实数字", "detail": f"`{val}` 不在事实卡里且未标注待确认",
                       "excerpt": seg.strip()[:90], **_lint_location(text, m.start())})

    year = G.today()[:4]
    for m in _re.finditer(r"20\d{2}\s*年", text):
        if m.group(0).strip() != f"{year}年":
            issues.append({"level": "低", "type": "年份存疑", "detail": f"出现 {m.group(0)}，当前是 {year} 年",
                           "excerpt": text[max(0, m.start() - 25):m.end() + 25].replace("\n", " "),
                           **_lint_location(text, m.start())})
    # 同类问题合并，避免刷屏
    seen, out = set(), []
    for i in issues:
        k = (i["type"], i["detail"])
        if k in seen:
            continue
        seen.add(k)
        out.append(i)
    return out


def lint_all(slug: str) -> dict:
    d = G.project_dir(slug) / "assets" / "drafts"
    files = sorted(d.glob("*.md")) if d.exists() else []
    report = {"slug": slug, "checked_at": G.now_iso(), "files": {}}
    total = 0
    for p in files:
        iss = lint_draft(slug, p)
        report["files"][p.name] = iss
        total += len(iss)
    report["total_issues"] = total
    report["high"] = sum(1 for v in report["files"].values() for i in v if i["level"] == "高")
    G.write_json(d / "_lint.json", report) if files else None
    return report


# ---------------------------------------------------------------- 主流程

ASSETS = ["llms", "jsonld", "snippets", "outlines"]


def run(slug: str, which: list[str] | None = None, with_draft: bool = False,
        draft_limit: int = 3, draft_question: str | None = None,
        draft_outlines: list[dict] | None = None) -> dict:
    cfg = G.load_config(slug)
    market = cfg.get("market", "cn")
    adir = G.project_dir(slug) / "assets"
    which = which or ASSETS
    made: list[str] = []

    if "llms" in which:
        (adir).mkdir(parents=True, exist_ok=True)
        # Deployable machine-readable files are English regardless of the sampling market.
        llms = gen_llms_txt(slug, "en")
        (adir / "llms.txt").write_text(llms, "utf-8")
        (adir / "llms.en.txt").write_text(llms, "utf-8")
        made += ["assets/llms.txt", "assets/llms.en.txt"]

    if "jsonld" in which:
        d = adir / "jsonld"
        d.mkdir(parents=True, exist_ok=True)
        for name, obj in gen_jsonld(slug).items():
            (d / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
            made.append(f"assets/jsonld/{name}.json")

    if "snippets" in which:
        d = adir / "snippets"
        d.mkdir(parents=True, exist_ok=True)
        for lang in ["en"]:
            (d / f"definition.{lang}.html").write_text(gen_definition_block(slug, lang), "utf-8")
            (d / f"faq.{lang}.html").write_text(gen_faq_block(slug, lang), "utf-8")
            made += [f"assets/snippets/definition.{lang}.html", f"assets/snippets/faq.{lang}.html"]

    outlines = []
    if "outlines" in which:
        d = adir / "outlines"
        d.mkdir(parents=True, exist_ok=True)
        outlines = gen_outlines(slug)
        for o in outlines:
            body = [f"# 内容大纲 · {o['target_question']}", "",
                    f"- 目标问题 ID：`{o['question_id']}` ｜ 市场：{o['market']} ｜ 类型：{o['type']}",
                    "", "## 标题候选（对题性 r=0.432，标题必须含问题原词）", ""]
            body += [f"{i+1}. {t}" for i, t in enumerate(o["title_candidates"])]
            body += ["", "## 章节骨架", ""]
            body += [f"{i+1}. {s}" for i, s in enumerate(o["sections"])]
            body += ["", "## 硬性要求", "",
                     f"- 正文 ≥ {o['requirements']['min_words']} 词，H2 ≥ {o['requirements']['min_h2']} 个",
                     f"- 必备抽取块：{'、'.join(o['requirements']['must_have_blocks'])}",
                     f"- 列表密度 {o['requirements']['list_density']}",
                     f"- 证据：{o['requirements']['evidence']}", ""]
            if o["facts_to_use"]:
                body += ["## 可用的已核实事实", ""] + [f"- {x}" for x in o["facts_to_use"]] + [""]
            (d / f"{o['question_id']}.md").write_text("\n".join(body), "utf-8")
        made.append(f"assets/outlines/（{len(outlines)} 份）")
        G.write_json(adir / "outlines" / "_index.json", outlines)
    elif with_draft:
        # 单篇初稿直接复用已有大纲，避免工作台按钮重建并覆盖整批大纲。
        outlines = G.read_json(adir / "outlines" / "_index.json", []) or []
        if not isinstance(outlines, list) or not outlines:
            G.die("还没有大纲，先运行生成资产并包含 outlines")

    if with_draft and outlines:
        d = adir / "drafts"
        d.mkdir(parents=True, exist_ok=True)
        draft_outlines = draft_outlines or outlines
        if draft_question:
            draft_outlines = [o for o in outlines if o["question_id"] == draft_question]
            if not draft_outlines:
                G.die(f"找不到问题 {draft_question} 对应的大纲，无法生成初稿")
        for o in draft_outlines[:draft_limit]:
            G.info(f"起草 {o['question_id']} · {o['target_question'][:30]}…")
            text = draft(slug, o)
            if text:
                target = d / f"{o['question_id']}.md"
                if target.exists():
                    history = adir / "history" / "drafts"
                    history.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    (history / f"{o['question_id']}-{stamp}.md").write_bytes(target.read_bytes())
                    G.info(f"  已归档上一版初稿：history/drafts/{o['question_id']}-{stamp}.md")
                target.write_text(
                    __import__("content_registry").with_marker(
                        f"<!-- AI draft: verify facts before publication · {G.today()} -->\n\n" + text,
                        o["question_id"], o["target_question"], "draft"), "utf-8")
                made.append(f"assets/drafts/{o['question_id']}.md")
            else:
                G.info("  没有可用的 LLM API Key，跳过起草")
                break
        rep = lint_all(slug)
        if rep.get("total_issues"):
            G.info(f"初稿风险检查：{rep['total_issues']} 项（高风险 {rep['high']} 项）"
                   f" → assets/drafts/_lint.json。**发布前必须人工核实**")

    import content_registry
    registry = content_registry.save(slug)
    index = {"slug": slug, "generated_at": G.now_iso(), "market": market, "assets": made,
             "content_registry": "content_registry.json", "content_counts": {
                 s: sum(1 for r in registry["records"] if r["status"] == s)
                 for s in ("not_started", "outline", "draft", "final", "cms_draft")}}
    G.write_json(adir / "index.json", index)
    G.info(f"生成 {len(made)} 项资产 → {adir}")
    return index


def auto_drafts(slug: str, limit: int = 3) -> dict:
    """Generate the next safe review-only draft queue; never publishes content."""
    import content_registry
    selected, skipped = content_registry.next_outlines(slug, max(1, limit))
    if not selected:
        content_registry.save(slug)
        G.info("自动内容队列为空：所有题目已有内容或被重复保护拦截")
        return {"selected": [], "skipped": skipped, "generated": []}
    result = run(slug, which=["drafts"], with_draft=True, draft_limit=len(selected),
                 draft_question=None, draft_outlines=selected)
    return {"selected": [x["question_id"] for x in selected], "skipped": skipped,
            "generated": [x for x in result.get("assets", []) if x.startswith("assets/drafts/")]}
