"""Evidence-first lookup for unresolved facts in Workbench content."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup

import geolib as G


BRACKET_PLACEHOLDER = re.compile(
    r"（\s*待补\s*[:：]\s*([^）]+?)\s*）|"
    r"（\s*(?:待确认|未填写)\s*[:：]\s*([^）]+?)\s*）|"
    r"\(\s*(?:to be added|tbd|to be confirmed|not provided)\s*[:：]?\s*([^\)]+?)\s*\)",
    re.I,
)
PENDING_VALUE = re.compile(r"(?:待补|待确认|未填写|TBD|to be added|to be confirmed|not provided)", re.I)
LABEL_PENDING = re.compile(
    r"^(?P<lead>\s*(?:[-*]\s*)?)(?P<field>[^:：|]{2,80}?)\s*[:：]\s*"
    r"(?P<value>待补|待确认|未填写|TBD|to be added|to be confirmed|not provided)\s*$", re.I,
)
SEARCH_URL = "https://html.duckduckgo.com/html/?q={}"


def _add_field(fields: list[str], seen: set[str], field: str):
    field = re.sub(r"[*`#]", "", field).strip(" .:：|- ")
    key = field.lower()
    if field and key not in seen:
        seen.add(key)
        fields.append(field)


def _bare_pending_fields(text: str) -> list[str]:
    """Find simple table and label values that generators leave as pending."""
    fields, seen = [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if "|" in line:
            cells = [cell.strip() for cell in line.split("|")]
            labels = [cell for cell in cells if cell and not PENDING_VALUE.fullmatch(cell)
                      and not set(cell) <= set("-: ")]
            for cell in cells:
                if PENDING_VALUE.fullmatch(cell):
                    _add_field(fields, seen, labels[0] if labels else "this item")
        match = LABEL_PENDING.match(raw)
        if match:
            _add_field(fields, seen, match.group("field"))
        elif "|" not in line and PENDING_VALUE.search(raw):
            _add_field(fields, seen, "this item")
    return fields


def unresolved(text: str) -> list[str]:
    seen, fields = set(), []
    for m in BRACKET_PLACEHOLDER.finditer(text):
        _add_field(fields, seen, next((x for x in m.groups() if x), ""))
    for field in _bare_pending_fields(text):
        _add_field(fields, seen, field)
    return fields


def _host(site: str) -> str:
    return urlparse(site).netloc.lower().removeprefix("www.")


def _official(url: str, host: str) -> bool:
    current = urlparse(url).netloc.lower().removeprefix("www.")
    return bool(current and (current == host or current.endswith("." + host)))


def _search_target(url: str) -> str:
    """Unwrap DuckDuckGo result redirects without following arbitrary links."""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.netloc.lower().endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return url


def _cached_sources(slug: str, field: str, host: str) -> list[dict]:
    pages = G.read_jsonl(G.project_dir(slug) / "evidence" / "pages.jsonl")
    tokens = [x.lower() for x in re.findall(r"[A-Za-z0-9]{3,}|[\u4e00-\u9fff]{2,}", field)]
    out = []
    for page in pages:
        url, text = page.get("url", ""), page.get("text", "")
        if not _official(url, host):
            continue
        low = text.lower()
        if tokens and not any(token in low for token in tokens):
            continue
        out.append({"url": url, "title": page.get("title") or url,
                    "snippet": re.sub(r"\s+", " ", text)[:500], "official": True,
                    "kind": "cached official page"})
        if len(out) == 3:
            break
    return out


def _search_web(query: str, host: str) -> list[dict]:
    """Return public search snippets. Network failures are a normal no-result state."""
    try:
        r = requests.get(SEARCH_URL.format(quote_plus(query)), timeout=10,
                         headers={"User-Agent": "Mozilla/5.0 GeoLook research"})
        r.raise_for_status()
    except requests.RequestException:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for row in soup.select(".result"):
        link = row.select_one(".result__a")
        if not link or not link.get("href"):
            continue
        url = _search_target(link["href"].strip())
        if not url.startswith(("http://", "https://")):
            continue
        snippet = row.select_one(".result__snippet")
        out.append({"url": url, "title": link.get_text(" ", strip=True),
                    "snippet": snippet.get_text(" ", strip=True) if snippet else "",
                    "official": _official(url, host), "kind": "web search"})
        if len(out) == 5:
            break
    return out


def find_sources(slug: str, field: str) -> list[dict]:
    cfg = G.load_config(slug)
    brand = cfg.get("brand", {})
    host = _host(brand.get("site", ""))
    if not host:
        return []
    sources = _cached_sources(slug, field, host)
    urls = {x["url"] for x in sources}
    query = f"site:{host} {brand.get('name', '')} {field}".strip()
    for item in _search_web(query, host):
        if item["url"] not in urls:
            sources.append(item)
            urls.add(item["url"])
    return sources[:5]


def _unpublished_text(field: str, market: str) -> str:
    if market == "global":
        # The fallback itself is deployable content.  Do not copy a Chinese
        # table/header label into an otherwise English global article.
        if re.search(r"[\u3400-\u9fff]", field):
            field = "this item"
        return f"Not publicly disclosed in the official sources reviewed for {field}."
    return f"在本次检查的官方公开来源中，未披露{field}。"


def _replace_unresolved(text: str, market: str) -> str:
    def replace(m: re.Match) -> str:
        field = next((x for x in m.groups() if x), "this item").strip(" .:：")
        return _unpublished_text(field, market)
    text = BRACKET_PLACEHOLDER.sub(replace, text)
    replaced = []
    for raw in text.splitlines(keepends=True):
        ending = "\n" if raw.endswith("\n") else ""
        line = raw[:-1] if ending else raw
        if "|" in line:
            cells = line.split("|")
            values = [cell.strip() for cell in cells]
            labels = [cell for cell in values if cell and not PENDING_VALUE.fullmatch(cell)
                      and not set(cell) <= set("-: ")]
            field = labels[0] if labels else "this item"
            cells = [f" {_unpublished_text(field, market)} " if PENDING_VALUE.fullmatch(cell.strip()) else cell
                     for cell in cells]
            line = "|".join(cells)
        match = LABEL_PENDING.match(line)
        if match:
            line = f"{match.group('lead')}{match.group('field').strip()}: {_unpublished_text(match.group('field'), market)}"
        elif PENDING_VALUE.search(line):
            line = PENDING_VALUE.sub(_unpublished_text("this item", market), line)
        replaced.append(line + ending)
    return "".join(replaced)


def _rewrite_with_sources(slug: str, text: str, market: str, sources: list[dict]) -> str:
    import sample as S

    platform = S.pick_llm()
    if not platform:
        return ""
    official = [s for s in sources if s["official"]]
    citations = "\n".join(
        f"- {s['title']}: {s['url']}\n  Excerpt: {s['snippet'][:500]}" for s in official
    )
    language = "English" if market == "global" else "Chinese"
    prompt = f"""You are an evidence-first GEO editor. Return one complete revised Markdown article in {language}.

Replace every unresolved placeholder, including pending table values and `TBD` markers, using the official source excerpts below. Copy facts only when the excerpt directly supports them. Do not infer pricing, availability, customer names, dates, model support, benchmarks, or competitor details. For an unresolved item without direct evidence, write exactly that it is not publicly disclosed in the official sources reviewed. Do not leave any pending marker in the result.

Add a concise `## Sources checked` section containing every official URL used. Do not cite third-party search results as product facts.

Official source excerpts:
{citations or '(none)'}

Current article:
---
{text}
---"""
    result = S.ask(platform, prompt, timeout=300)
    return result.get("answer", "") if result.get("ok") else ""


def _require_global_english(text: str, sources: list[dict]) -> str:
    """Reject mixed-language global content instead of saving it to a deployable asset."""
    import generate as GEN
    import sample as S

    platform = S.pick_llm()
    facts = "\n".join(
        f"- {item['title']}: {item['snippet'][:300]}" for item in sources if item.get("official")
    )
    return GEN._english_only_revision(platform, text, facts) if platform else ""


def resolve(slug: str, text: str, market: str = "global") -> dict:
    """Search missing public facts and return a revised in-memory article plus evidence."""
    fields = unresolved(text)
    if not fields:
        return {"ok": True, "changed": False, "text": text, "sources": [],
                "message": "没有待补或待确认的事实"}
    sources: list[dict] = []
    for field in fields[:4]:
        for source in find_sources(slug, field):
            source = {**source, "field": field}
            if source["url"] not in {x["url"] for x in sources}:
                sources.append(source)
    official = [x for x in sources if x["official"]]
    if not official:
        revised = _replace_unresolved(text, market)
        G.write_json(G.project_dir(slug) / "research" / "latest.json", {
            "at": G.now_iso(), "fields": fields, "sources": sources,
            "outcome": "not publicly disclosed in checked official sources",
        })
        return {"ok": True, "changed": revised != text, "text": revised, "sources": sources,
                "message": "未找到可直接引用的官方事实，已标为未公开"}
    revised = _rewrite_with_sources(slug, text, market, sources)
    if not revised:
        revised = _replace_unresolved(text, market)
        message = "官网来源已找到，但 AI 未返回修订；其余待补项已标为未公开"
    else:
        revised = _replace_unresolved(revised, market)
        message = "已根据官方公开来源补充待补事实；其余项已标为未公开"
    # A source-backed LLM rewrite is a complete deployable revision.  If it
    # still mixes Chinese into a Global article, repair it once before return.
    # The no-source branch above remains a deterministic placeholder-only
    # fallback and must keep working even while an LLM service is unavailable.
    if market == "global" and re.search(r"[\u3400-\u9fff]", revised):
        revised = _require_global_english(revised, sources)
        if not revised:
            return {"ok": False, "changed": False, "text": text, "sources": sources,
                    "error": "无法生成全英文内容，已保留原文"}
    G.write_json(G.project_dir(slug) / "research" / "latest.json", {
        "at": G.now_iso(), "fields": fields, "sources": sources,
        "outcome": "revised from official sources",
    })
    return {"ok": True, "changed": revised != text, "text": revised, "sources": sources,
            "message": message}
