"""发布渠道对接：把成稿/资产一键发到你自己的渠道，并留发布记录。

安全边界（刻意为之，别放松）：
- 凭证只放项目根目录 .env（与引擎 Key 同一套管理，界面可配，已 gitignore）；
- 发布动作只由界面上的明确点击或 CLI 显式命令触发，没有任何自动发布路径；
- 公众号只建草稿（后台人工预览群发）；WordPress 只建草稿文章；
- GitHub 是提交文件到你自己的仓库；Webhook 打到你自己配置的接收端。

发布记录写 work/<slug>/publish.json，效果验收的 external.any 检查器
可以用发布落点的域名做「已被引擎引用」判定，闭环到验收。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re

import requests

import geolib as G

# 渠道注册表：env 是 .env 里的凭证变量；cfg 是存在项目 geo.json publishing.<code> 的非敏感配置
PUBLISHERS = {
    "github": {
        "name": "GitHub 仓库", "env": ["GITHUB_TOKEN"],
        "cfg": [("repo", "owner/repo"), ("branch", "main"), ("dir", "docs/geo")],
        "note": "Contents API 提交 markdown 到你的仓库（配 Pages/静态站即上线）",
    },
    "wordpress": {
        "name": "WordPress", "env": ["WP_USER", "WP_APP_PASSWORD"],
        "cfg": [("site_url", "https://blog.example.com")],
        "note": "REST API 新建草稿文章，登录后台确认后再发布",
    },
    "wechat_draft": {
        "name": "公众号草稿箱", "env": ["WECHAT_APPID", "WECHAT_APPSECRET"],
        "cfg": [("thumb_media_id", "永久素材封面 media_id（草稿必需）")],
        "note": "新建草稿，需在公众号后台预览并群发；服务器 IP 要在白名单",
    },
    "webhook": {
        "name": "自定义 Webhook", "env": ["PUBLISH_WEBHOOK_URL"],
        "cfg": [],
        "note": "POST JSON {title, markdown, html, slug, path} 到你自己的接收端",
    },
    "wisgate_cms": {
        "name": "WisGate CMS",
        "env": ["WISGATE_CMS_TOKEN"],
        "cfg": [
            ("api_base_url", "https://cms.wisgate.ai"),
            ("collection", "blogs"),
            ("title_field", "title"),
            ("body_field", "content"),
            ("body_format", "markdown（WisGate CMS 固定格式；旧的 html 设置会自动迁移）"),
            ("slug_field", "slug"),
            ("status_field", "status"),
            ("draft_value", "draft"),
            ("cover_image_field", "cover_image（留空则不写入）"),
            ("cover_image_value", "Directus 文件 ID；留空不设置封面"),
            ("summary_field", "summary"),
            ("publish_time_field", "publish_time"),
            ("tags_field", "tags"),
            ("tags_format", "json 或 csv"),
            ("additional_tags", "可选，逗号分隔；会与自动标签合并"),
            ("platform_field", "platform"),
            ("platform_value", "wisdom-gate"),
            ("model_field", "model（默认留空）"),
        ],
        "note": "创建含摘要、标签、时间和平台信息的博客草稿；Token 只保存在本机 .env，不会在看板回显",
    },
}


def missing_env(code: str) -> list[str]:
    return [e for e in PUBLISHERS[code]["env"] if not os.environ.get(e)]


def _cfg(slug: str, code: str) -> dict:
    return (G.load_config(slug).get("publishing") or {}).get(code) or {}


# ---------------------------------------------------------------- markdown → html
# 公众号/WordPress 要 HTML。只做最小转换（标题/加粗/链接/列表/代码块/段落），
# 不引第三方库；表格等复杂结构原样进 <p>，发布前在渠道后台肉眼过一遍。

def md2html(md: str) -> str:
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    out, in_code, in_list = [], False, False

    def inline(s):
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    for line in md.splitlines():
        if line.strip().startswith("```"):
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(line.replace("&", "&amp;").replace("<", "&lt;"))
            continue
        m = re.match(r"(#{1,6})\s+(.*)", line)
        li = re.match(r"\s*[-*]\s+(.*)", line) or re.match(r"\s*\d+[.、]\s+(.*)", line)
        if in_list and not li:
            out.append("</ul>")
            in_list = False
        if m:
            n = min(len(m.group(1)) + 1, 6)  # 文内 # 降一级，标题留给渠道的 title 字段
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif li:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(li.group(1))}</li>")
        elif line.strip():
            out.append(f"<p>{inline(line.strip())}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


# ---------------------------------------------------------------- 各渠道实现

def _pub_github(cfg, text, title, fname):
    repo, branch = cfg.get("repo", ""), cfg.get("branch", "main")
    if not repo or "/" not in repo:
        return {"ok": False, "error": "先在设置里配置 repo（owner/repo）"}
    path = (cfg.get("dir", "").strip("/") + "/" + fname).lstrip("/")
    H = {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
         "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    body = {"message": f"geo: publish {title}", "branch": branch,
            "content": base64.b64encode(text.encode()).decode()}
    r0 = requests.get(url, headers=H, params={"ref": branch}, timeout=30)
    if r0.status_code == 200:  # 已存在→更新
        body["sha"] = r0.json().get("sha")
    r = requests.put(url, headers=H, json=body, timeout=30)
    if r.status_code in (200, 201):
        return {"ok": True, "url": r.json().get("content", {}).get("html_url", "")}
    return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}


def _pub_wordpress(cfg, text, title, fname):
    site = (cfg.get("site_url") or "").rstrip("/")
    if not site:
        return {"ok": False, "error": "先在设置里配置 site_url"}
    r = requests.post(f"{site}/wp-json/wp/v2/posts",
                      auth=(os.environ["WP_USER"], os.environ["WP_APP_PASSWORD"]),
                      json={"title": title, "content": md2html(text), "status": "draft"},
                      timeout=30)
    if r.status_code == 201:
        return {"ok": True, "url": r.json().get("link", ""),
                "note": "已建为草稿，到 WordPress 后台确认发布"}
    return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}


def _pub_wechat(cfg, text, title, fname):
    thumb = cfg.get("thumb_media_id", "")
    if not thumb:
        return {"ok": False, "error": "缺封面：先在设置里配置 thumb_media_id（永久素材）"}
    tr = requests.get("https://api.weixin.qq.com/cgi-bin/token",
                      params={"grant_type": "client_credential",
                              "appid": os.environ["WECHAT_APPID"],
                              "secret": os.environ["WECHAT_APPSECRET"]}, timeout=30).json()
    tok = tr.get("access_token")
    if not tok:
        return {"ok": False, "error": f"取 token 失败：{tr.get('errmsg', tr)}"}
    art = {"title": title[:60], "content": md2html(text), "thumb_media_id": thumb,
           "digest": re.sub(r"\s+", " ", text)[:100]}
    r = requests.post(f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={tok}",
                      data=json.dumps({"articles": [art]}, ensure_ascii=False).encode(),
                      timeout=30).json()
    if r.get("media_id"):
        return {"ok": True, "url": "", "note": "已进草稿箱，到公众号后台预览群发"}
    return {"ok": False, "error": f"draft/add 失败：{r.get('errmsg', r)}"}


def _pub_webhook(cfg, text, title, fname):
    r = requests.post(os.environ["PUBLISH_WEBHOOK_URL"],
                      json={"title": title, "markdown": text, "html": md2html(text),
                            "path": fname}, timeout=30)
    if 200 <= r.status_code < 300:
        url = ""
        try:
            url = (r.json() or {}).get("url", "")
        except Exception:  # noqa: BLE001  接收端不回 JSON 也算成功
            pass
        return {"ok": True, "url": url}
    return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _cms_ident(value: str, name: str, default: str) -> str:
    value = str(value or default).strip()
    if not _IDENT.fullmatch(value):
        raise ValueError(f"{name} 必须是字母、数字、下划线组成的字段名")
    return value


def _cms_slug(title: str, fname: str) -> str:
    raw = title or fname.rsplit(".", 1)[0]
    value = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if value:
        return value
    # Chinese-only or punctuation-only titles need a deterministic ASCII slug.
    digest = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:12]
    return f"geolook-{digest}"


def _cms_plaintext(markdown: str) -> str:
    value = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    value = re.sub(r"^---\s*$.*?^---\s*$", "", value, flags=re.M | re.S)
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[`*_#>|]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _cms_summary(markdown: str, title: str) -> str:
    """Use the first reader-facing paragraph as concise CMS summary metadata."""
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    text = re.sub(r"^---\s*$.*?^---\s*$", "", text, flags=re.M | re.S)
    paragraphs = re.split(r"\n\s*\n", text)
    for paragraph in paragraphs:
        candidate = paragraph.strip()
        if not candidate or candidate.startswith("#") or candidate.startswith("|"):
            continue
        candidate = _cms_plaintext(candidate)
        if candidate:
            return candidate[:240].rstrip(" ,;:-")
    return _cms_plaintext(title)[:240]


def _cms_markdown(markdown: str) -> str:
    """Remove GeoLook-only comments before a finished article reaches a CMS."""
    return re.sub(r"<!--.*?-->\s*", "", markdown, flags=re.S).strip() + "\n"


def _cms_tag(value: str) -> str:
    """Return a short phrase suitable for a CMS tag, or an empty string."""
    value = re.sub(r"[`*_#]|\[[^\]]*\]\([^)]+\)", "", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" .,:;!?|/-")
    if not value or len(value) < 2:
        return ""
    return value[:60].rstrip(" .,:;!?|/-")


def _cms_tag_list(values) -> list[str]:
    """Deduplicate user-reviewed or rule-derived tag phrases, preserving order."""
    if isinstance(values, str):
        values = re.split(r"[,\n]", values)
    if not isinstance(values, (list, tuple)):
        return []
    out, seen = [], set()
    for value in values:
        tag = _cms_tag(value)
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            out.append(tag)
    return out[:3]


def _cms_frontmatter_tags(markdown: str) -> list[str]:
    """Prefer the three editorial keywords when a draft already declares them."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", markdown, re.S)
    if not match:
        return []
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower().replace("-", "_") in {
            "keywords", "target_keywords", "target_keyphrases", "tags"
        }:
            return _cms_tag_list(value.strip().strip("[]").replace("\"", "").replace("'", ""))
    return []


def _cms_primary_tag(title: str) -> str:
    """Use the query portion of a title, never the entire SEO title as one tag."""
    value = _cms_tag(title)
    # Most titles follow "Primary query: explanatory subtitle". The primary
    # query is what belongs in a tag, not the subtitle or the publication year.
    value = re.split(r"\s*[:—–]\s*", value, maxsplit=1)[0]
    value = re.sub(r"\b20\d{2}\b", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" .,:;!?|/-")
    return _cms_tag(value)


def _cms_tags(title: str, markdown: str, additional: str = "") -> list[str]:
    """Create exactly three short, phrase-level candidates for review."""
    tags = _cms_frontmatter_tags(markdown)
    primary = _cms_primary_tag(title)
    if primary:
        tags.insert(0, primary)
    tags.extend(t.strip() for t in str(additional or "").split(",") if t.strip())

    source = f"{title}\n{markdown}"
    lower = source.casefold()
    # These are category phrases, not claims about a provider. They make a
    # useful fallback for drafts that do not include editorial frontmatter.
    if re.search(r"\b(?:ai|llm|unified|multi[ -]model|openai[ -]compatible)\b", lower) and "api" in lower:
        tags.append("AI API gateway")
    if re.search(r"\bmulti[ -]model\b|\bmultiple (?:ai |language )?models\b|\bmodel variety\b", lower):
        tags.append("Multi-model API")
    if re.search(r"\bopenai[ -]compatible\b", lower):
        tags.append("OpenAI-compatible API")
    if re.search(r"\balternatives?\b|\bcompare|\bcomparison\b|\bversus\b|\bvs\.?\b", lower):
        tags.append("API provider comparison")
    if re.search(r"\bdeveloper|\bintegration|\bsdk\b", lower):
        tags.append("API integration")

    # Stable broad fallbacks preserve the CMS requirement for at least three
    # tags. The review dialog always exposes them for replacement.
    for fallback in ("AI API", "Developer tools"):
        if len(_cms_tag_list(tags)) >= 3:
            break
        tags.append(fallback)
    return _cms_tag_list(tags)


def _cms_error(exc_or_response, token: str = "") -> str:
    if isinstance(exc_or_response, Exception):
        msg = f"{type(exc_or_response).__name__}: {exc_or_response}"
    else:
        try:
            payload = exc_or_response.json() or {}
        except (ValueError, TypeError):
            payload = {}
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list) and errors:
            parts = []
            for item in errors[:3]:
                if not isinstance(item, dict):
                    continue
                ext = item.get("extensions") if isinstance(item.get("extensions"), dict) else {}
                field, code = ext.get("field"), ext.get("code")
                detail = str(item.get("message") or "validation failed")
                # Directus may echo the complete article value in a validation message.
                # Do not return that content to a toast or publish history.
                if detail.lstrip().startswith("Value "):
                    detail = "value rejected by field validation"
                prefix = " · ".join(x for x in (str(field or "").strip(), str(code or "").strip()) if x)
                parts.append(f"{prefix}: {detail}" if prefix else detail)
            if parts:
                msg = f"HTTP {exc_or_response.status_code}: " + " | ".join(parts)
            else:
                msg = f"HTTP {exc_or_response.status_code}: CMS validation failed"
        else:
            msg = f"HTTP {exc_or_response.status_code}: {exc_or_response.text[:200]}"
    if token:
        msg = msg.replace(token, "[redacted]")
    return msg.replace("Authorization", "authorization")


def _cms_preview(cfg: dict, text: str, title: str, fname: str) -> dict:
    cms_markdown = _cms_markdown(text)
    return {
        "title": title,
        "summary": _cms_summary(text, title),
        "slug": _cms_slug(title, fname),
        "tags": _cms_tags(title, cms_markdown, cfg.get("additional_tags", "")),
        "format": "markdown",
        "characters": len(cms_markdown),
        "status": str(cfg.get("draft_value") or "draft").strip() or "draft",
        "platform": str(cfg.get("platform_value") or "wisdom-gate").strip() or "wisdom-gate",
        "model": str(cfg.get("model_value") or "").strip(),
        "filename": fname,
    }


def _pub_wisgate_cms(cfg, text, title, fname, options=None):
    """Create a draft through a Directus-compatible items endpoint.

    The CMS schema is configurable because the admin URL alone does not prove
    the collection's field names. This function never sends a publish status.
    """
    base = (cfg.get("api_base_url") or "https://cms.wisgate.ai").strip().rstrip("/")
    if not re.fullmatch(r"https://[^/]+", base):
        return {"ok": False, "error": "api_base_url 必须是 https:// 主机地址，不能填写 /admin 路径"}
    collection = str(cfg.get("collection") or "blogs").strip().strip("/")
    if not _IDENT.fullmatch(collection):
        return {"ok": False, "error": "collection 必须是简单字段名，例如 blogs"}
    try:
        title_field = _cms_ident(cfg.get("title_field"), "title_field", "title")
        body_field = _cms_ident(cfg.get("body_field"), "body_field", "content")
        slug_field = _cms_ident(cfg.get("slug_field"), "slug_field", "slug")
        status_field = _cms_ident(cfg.get("status_field"), "status_field", "status")
        cover_image_field = _cms_ident(cfg.get("cover_image_field"), "cover_image_field", "cover_image")
        summary_field = _cms_ident(cfg.get("summary_field"), "summary_field", "summary")
        publish_time_field = _cms_ident(cfg.get("publish_time_field"), "publish_time_field", "publish_time")
        tags_field = _cms_ident(cfg.get("tags_field"), "tags_field", "tags")
        platform_field = _cms_ident(cfg.get("platform_field"), "platform_field", "platform")
        model_field = _cms_ident(cfg.get("model_field"), "model_field", "model")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # `content` uses Directus's Markdown rich-text interface. Older GeoLook
    # configurations exposed an HTML option, which made raw tags visible in CMS.
    # Keep that value backward-compatible, but always submit Markdown here.
    fmt = "markdown"
    tags_format = str(cfg.get("tags_format") or "json").strip().lower()
    if tags_format not in {"json", "csv"}:
        return {"ok": False, "error": "tags_format 只能是 json 或 csv"}
    token = os.environ["WISGATE_CMS_TOKEN"]
    options = options if isinstance(options, dict) else {}
    cms_markdown = _cms_markdown(text)
    slug_value = _cms_slug(title, fname)
    if "tags" in options:
        tags = _cms_tag_list(options.get("tags"))
        if len(tags) != 3:
            return {"ok": False, "error": "请确认恰好 3 个关键词标签"}
    else:
        tags = _cms_tags(title, cms_markdown, cfg.get("additional_tags", ""))
    body = {
        title_field: title,
        body_field: cms_markdown,
        slug_field: slug_value,
        status_field: str(cfg.get("draft_value") or "draft").strip() or "draft",
        summary_field: _cms_summary(text, title),
        publish_time_field: G.now_iso(),
        tags_field: tags if tags_format == "json" else ", ".join(tags),
        platform_field: str(cfg.get("platform_value") or "wisdom-gate").strip() or "wisdom-gate",
    }
    # WisGate CMS requires this canonical field even when a custom mapping is used.
    body["slug"] = slug_value
    cover_image = str(cfg.get("cover_image_value") or "").strip()
    if cover_image:
        body[cover_image_field] = cover_image
    # Omit an empty model value so Directus leaves the nullable field blank.
    if str(cfg.get("model_value") or "").strip():
        body[model_field] = str(cfg["model_value"]).strip()
    url = f"{base}/items/{collection}"
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {token}",
                                        "Accept": "application/json",
                                        "Content-Type": "application/json"},
                          json=body, timeout=30)
    except requests.RequestException as exc:
        return {"ok": False, "error": _cms_error(exc, token)}
    if r.status_code not in (200, 201):
        error = _cms_error(r, token)
        if body_field == "content":
            error += f"（本次提交 {len(body[body_field])} 个字符，格式：{fmt}）"
        return {"ok": False, "error": error}
    try:
        payload = r.json() or {}
    except ValueError:
        payload = {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    item_id = data.get("id") if isinstance(data, dict) else None
    admin_url = f"{base}/admin/content/{collection}"
    if item_id is not None:
        admin_url += f"/{item_id}"
    return {"ok": True, "url": admin_url,
            "note": "已导入为 CMS 草稿，请在后台核对后再发布"}


_IMPL = {"github": _pub_github, "wordpress": _pub_wordpress,
         "wechat_draft": _pub_wechat, "webhook": _pub_webhook,
         "wisgate_cms": _pub_wisgate_cms}


# ---------------------------------------------------------------- 入口与记录

def _read_source(slug: str, rel: str) -> tuple[str, str]:
    """rel 限定在 content/ 或 assets/ 下，返回 (文本, 文件名)。

    校验解析后的真实路径归属，防 content/../ 这类穿越。"""
    pdir = G.project_dir(slug).resolve()
    target = (pdir / rel).resolve()
    if not any(target.is_relative_to(pdir / d) for d in ("content", "assets")):
        raise ValueError("只允许发布 content/ 或 assets/ 下的文件")
    return target.read_text("utf-8"), target.name


def _title_of(text: str, fname: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else fname.rsplit(".", 1)[0]


def records(slug: str) -> list[dict]:
    return G.read_json(G.project_dir(slug) / "publish.json", []) or []


def preview(slug: str, code: str, rel: str, title: str = "") -> dict:
    """Build a no-side-effect review payload for a publishing channel."""
    if code not in PUBLISHERS:
        return {"ok": False, "error": f"未知渠道 {code}"}
    try:
        text, fname = _read_source(slug, rel)
    except (ValueError, FileNotFoundError):
        return {"ok": False, "error": f"文件不可用：{rel}"}
    title = title or _title_of(text, fname)
    if code == "wisgate_cms":
        data = _cms_preview(_cfg(slug, code), text, title, fname)
        # Re-run lint for the exact final being published. The aggregate draft
        # report can include unrelated files and may be stale after edits.
        try:
            import generate
            path = (G.project_dir(slug) / rel).resolve()
            data["issues"] = generate.lint_draft(slug, path)
        except Exception:  # noqa: BLE001 - a preview must remain available
            data["issues"] = []
        return {"ok": True, **data}
    return {"ok": False, "error": "该渠道暂不支持发布前预览"}


def publish(slug: str, code: str, rel: str, title: str = "", options=None) -> dict:
    if code not in PUBLISHERS:
        return {"ok": False, "error": f"未知渠道 {code}"}
    miss = missing_env(code)
    if miss:
        return {"ok": False, "error": "缺凭证：" + "、".join(miss)}
    try:
        text, fname = _read_source(slug, rel)
    except (ValueError, FileNotFoundError):
        return {"ok": False, "error": f"文件不可用：{rel}"}
    title = title or _title_of(text, fname)
    if code == "wisgate_cms":
        res = _IMPL[code](_cfg(slug, code), text, title, fname, options)
    else:
        res = _IMPL[code](_cfg(slug, code), text, title, fname)
    entry = {"at": G.now_iso(), "platform": code, "platform_name": PUBLISHERS[code]["name"],
             "path": rel, "title": title, "ok": res.get("ok", False),
             "url": res.get("url", ""), "note": res.get("note", ""),
             "error": res.get("error", "")}
    rows = records(slug)
    rows.append(entry)
    G.write_json(G.project_dir(slug) / "publish.json", rows[-200:])
    return {**res, "record": entry}
