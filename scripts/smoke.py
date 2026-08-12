"""Post-deployment smoke checks for a running GeoLook dashboard.

The checks are read-only: they do not generate drafts, alter project files, or
call an LLM unless ``--check-api`` is explicitly requested.
"""

from __future__ import annotations

import argparse
from typing import Any

import requests

import geolib as G


def _result(name: str, ok: bool, detail: str) -> dict[str, Any]:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")
    return {"name": name, "ok": ok, "detail": detail}


def _get(url: str) -> tuple[bool, Any]:
    try:
        r = requests.get(url, timeout=12)
        if not r.ok:
            return False, f"HTTP {r.status_code}"
        return True, r
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {e}"


def _post(url: str, body: dict) -> tuple[bool, Any]:
    try:
        r = requests.post(url, json=body, timeout=12)
        if not r.ok:
            return False, f"HTTP {r.status_code}"
        return True, r
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {e}"


def run(slug: str, url: str = "http://127.0.0.1:8765", question: str | None = None,
        check_api: bool = False) -> dict:
    """Verify the deployed dashboard and its safe, no-write feature paths."""
    base = url.rstrip("/")
    cfg = G.load_config(slug)
    qid = question or next((q.get("id") for q in cfg.get("questions", []) if q.get("id")), None)
    results: list[dict] = []

    ok, response = _get(base + "/")
    if ok:
        page = response.text
        has_workbench_repairs = "wbCompleteBlocks" in page and "正在补齐" in page
        results.append(_result("dashboard UI", has_workbench_repairs,
                               "补齐缺少块与进行中提示已部署" if has_workbench_repairs
                               else "页面不是包含最新内容工作台的版本"))
    else:
        results.append(_result("dashboard UI", False, str(response)))

    ok, response = _get(f"{base}/api/p/{slug}")
    if ok:
        try:
            project = response.json()
            valid = not project.get("error") and project.get("slug") == slug
            detail = f"项目 {slug} 可读取" if valid else str(project.get("error") or "项目数据不完整")
            results.append(_result("project API", valid, detail))
        except ValueError:
            results.append(_result("project API", False, "响应不是 JSON"))
    else:
        results.append(_result("project API", False, str(response)))

    if qid:
        ok, response = _get(f"{base}/api/workbench/{slug}?qid={qid}")
        if ok:
            try:
                workbench = response.json()
                sources = workbench.get("sources", [])
                valid = bool(workbench.get("question")) and any(
                    x.get("kind") in ("outline", "draft", "content") for x in sources
                )
                results.append(_result("content workbench", valid,
                                       f"问题 {qid} 已加载 {len(sources)} 份内容源" if valid
                                       else f"问题 {qid} 没有可加载的大纲或内容"))
            except ValueError:
                results.append(_result("content workbench", False, "响应不是 JSON"))
        else:
            results.append(_result("content workbench", False, str(response)))
    else:
        results.append(_result("content workbench", False, "项目问题库为空，无法测试工作台"))

    sample_text = "Acme is a tool.\n\n## FAQ\n问：适合谁？\n答：按实际情况确认。"
    ok, response = _post(base + "/api/precheck", {"text": sample_text})
    if ok:
        try:
            precheck = response.json()
            valid = isinstance(precheck.get("blocks"), dict) and "定义" in precheck["blocks"]
            results.append(_result("extract-block precheck", valid,
                                   "预检接口返回五类抽取块结果" if valid else "预检结果结构不正确"))
        except ValueError:
            results.append(_result("extract-block precheck", False, "响应不是 JSON"))
    else:
        results.append(_result("extract-block precheck", False, str(response)))

    if check_api:
        import sample as S
        G.load_env()
        answer = S.ask("custom", "Reply with exactly: OK", timeout=30)
        results.append(_result("custom API", bool(answer.get("ok")),
                               "最小请求成功" if answer.get("ok") else str(answer.get("error") or "无响应")))

    passed = all(item["ok"] for item in results)
    print(f"\n上线后验收：{'通过' if passed else '未通过'}（{sum(x['ok'] for x in results)}/{len(results)} 项）")
    return {"ok": passed, "slug": slug, "url": base, "results": results}


def main():
    p = argparse.ArgumentParser(description="GeoLook 上线后验收（只读）")
    p.add_argument("--slug", required=True)
    p.add_argument("--url", default="http://127.0.0.1:8765")
    p.add_argument("--question", help="指定用于工作台验收的问题 ID")
    p.add_argument("--check-api", action="store_true", help="额外发送一次最小 API 请求，会消耗少量额度")
    a = p.parse_args()
    if not run(a.slug, a.url, a.question, a.check_api)["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
