from __future__ import annotations

import base64, logging, re, zlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin
from bs4 import BeautifulSoup

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.browser import ChromeCdpSession
from common.output import save_error_meta, save_raw_html
from common.time import in_range, now_shanghai, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "重庆大学", "CQU"
BASE_URL = "https://cqu.cqbys.com"; ENTRY_URL = f"{BASE_URL}/campus"; PAGE_URL = f"{BASE_URL}/campus/index/domain/cqu/city//page/{{page}}"
MAX_PAGES = 200
SUPPORTED_FILTERS = {"company": "local", "position": "local", "keyword": "native", "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported", "education": "unsupported", "position_type": "unsupported", "employment_type": "unsupported", "student_type": "unsupported"}
LAST_STATS: dict[str, int | str] = {}
BLOCK_RE = re.compile(r'unzip\("([A-Za-z0-9+/=]+)"\)\.substr\((\d+)\)\)\.substr\((\d+)\)')
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?")


def _blocks(html: str) -> str:
    out = []
    for encoded, a, b in BLOCK_RE.findall(html):
        inflated = zlib.decompress(base64.b64decode(encoded)).decode(); decoded = base64.b64decode(inflated[int(a):]).decode(); out.append(decoded[int(b):])
    return "\n".join(out)


def _list(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    if not soup.select_one("a[href*='/campus/view/id/']"):
        soup = BeautifulSoup(_blocks(html), "lxml")
    out = []
    for a in soup.select("a[href*='/campus/view/id/']"):
        row = a.find_parent("ul")
        match = DATE_RE.search(row.get_text(" ", strip=True) if row else "")
        if match: out.append({"title": a.get_text(" ", strip=True), "published": match.group(), "url": urljoin(BASE_URL, str(a.get("href") or ""))})
    return out


def _detail(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    company = soup.select_one("a.name.text-primary")
    content = soup.select_one(".aContent")
    return company.get_text(" ", strip=True) if company else "", str(content or "")


def _rendered_meta(url: str, status: int | None) -> dict[str, object]:
    return {"url": url, "method": "GET", "status_code": status, "content_type": "text/html; rendered-dom", "capture": "chrome_cdp_rendered_dom"}


def _save_failed_render(page: object, path: Path, requested_url: str, response: object | None) -> bool:
    try:
        rendered = page.content()  # type: ignore[attr-defined]
        final_url = str(getattr(page, "url", requested_url))
        status = getattr(response, "status", None) if response is not None else None
        save_raw_html(path, rendered, _rendered_meta(final_url, status) | {"requested_url": requested_url, "capture": "chrome_cdp_rendered_dom_after_error", "navigation_failed": True})
        return True
    except Exception:
        return False


def _native_time_window(start: datetime) -> str:
    age = (now_shanghai().date() - start.date()).days
    return next((str(days) for days in (1, 3, 7, 14, 30, 60) if days > age), "")


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    matched = []
    seen_pages: set[tuple[str, ...]] = set()
    with ChromeCdpSession() as chrome:
        browser_page = chrome.new_page()
        native_time = _native_time_window(start)
        for page_no in range(1, MAX_PAGES + 1):
            base = ENTRY_URL if page_no == 1 else PAGE_URL.format(page=page_no)
            keyword = filters.get("keyword", "")
            query = {"keyword": keyword, "time": native_time}
            query = {key: value for key, value in query.items() if value}
            url = f"{base}?{urlencode(query)}" if query else base
            response = browser_page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            browser_page.wait_for_selector(".campus-index-front-list", timeout=20_000)
            rendered = browser_page.content()
            save_raw_html(raw_dir / f"list_page_{page_no:03d}.html", rendered, _rendered_meta(browser_page.url, response.status if response else None))
            rows = _list(rendered)
            pages += 1
            if not rows:
                break
            signature = tuple(row["url"] for row in rows)
            if signature in seen_pages:
                break
            seen_pages.add(signature)
            dates = [parse_source_datetime(x["published"]) for x in rows]
            matched.extend(x for x in rows if in_range(x["published"], start, end) and (not filters.get("position") or contains(x["title"], filters["position"])))
            older_pages = older_pages + 1 if dates and all(x < start for x in dates) else 0
            if older_pages >= 2:
                break
        results = []
        for record in matched:
            source_id = record["url"].rsplit("/", 1)[-1]
            company = content = ""
            response = None
            try:
                response = browser_page.goto(record["url"], wait_until="domcontentloaded", timeout=30_000)
                browser_page.wait_for_selector(".aContent", timeout=20_000)
                rendered = browser_page.content()
                save_raw_html(raw_dir / f"detail_{source_id}.html", rendered, _rendered_meta(browser_page.url, response.status if response else None))
                company, content = _detail(rendered)
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                raw_saved = _save_failed_render(browser_page, raw_dir / f"detail_{source_id}.failed.html", record["url"], response)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": record["url"], "method": "GET", "capture": "chrome_cdp", "rendered_dom_saved": raw_saved, "error": f"{type(exc).__name__}: {exc}"})
            if filters.get("company") and not contains(company, filters["company"]):
                continue
            results.append({"公司": company, "岗位": record["title"], "岗位上新时间": record["published"], "JD": html_to_text(content), "投递方式": extract_application_methods(content), "原始链接": record["url"]})
    LAST_STATS = {"strategy": "chrome_cdp_rendered_html", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results
