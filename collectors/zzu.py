from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.output import save_error_meta, save_raw_html
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "郑州大学", "ZZU"
BASE_URL = "https://job.zzu.edu.cn"
LIST_URL = f"{BASE_URL}/campus"
MAX_PAGES = 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _meta(url: str, kind: str) -> dict[str, object]:
    return {"url": url, "method": "GET", "status_code": 200, "content_type": "text/html; rendered-dom", "capture": kind}


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    records: list[dict[str, str]] = []
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30000)
            for page_no in range(1, MAX_PAGES + 1):
                page.wait_for_selector(".infoList", timeout=15000)
                save_raw_html(raw_dir / f"list_page_{page_no:03d}.html", page.content(), _meta(page.url, "playwright_rendered_dom"))
                rows = []
                for node in page.locator(".infoList").all():
                    anchor = node.locator("a").first
                    parts = [value.strip() for value in node.inner_text().splitlines() if value.strip()]
                    if len(parts) >= 2:
                        rows.append({"title": parts[0], "date": parts[-1], "url": anchor.get_attribute("href") or ""})
                pages += 1
                normal_dates = []
                for row in rows:
                    try:
                        normal_dates.append(parse_source_datetime(row["date"]))
                    except ValueError:
                        continue
                    if in_range(row["date"], start, end):
                        records.append(row)
                older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
                next_link = page.get_by_role("link", name="下一页")
                if older_pages >= 2 or next_link.count() == 0:
                    break
                previous = page.url
                next_link.click()
                page.wait_for_url(lambda value: value != previous, timeout=15000)
            simple = []
            for row in records:
                url = row["url"] if row["url"].startswith("http") else BASE_URL + row["url"]
                source_id = url.rstrip("/").split("/")[-1]
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_selector(".aContent", timeout=15000)
                    rendered = page.content()
                    save_raw_html(raw_dir / f"detail_{source_id}.html", rendered, _meta(page.url, "playwright_rendered_dom"))
                    soup = BeautifulSoup(rendered, "lxml")
                    company_node = soup.select_one(".name.text-primary")
                    content_node = soup.select_one(".aContent")
                    company = company_node.get_text(" ", strip=True) if company_node else ""
                    content = str(content_node or "")
                    text = html_to_text(content)
                    values = {"company": company, "position": row["title"], "keyword": f"{company} {row['title']} {text}"}
                    detail_success += 1
                    if any(filters.get(k) and not contains(values[k], filters[k]) for k in values):
                        continue
                    simple.append({
                        "公司": company, "岗位": row["title"], "岗位上新时间": row["date"],
                        "JD": text, "投递方式": extract_application_methods(content), "原始链接": url,
                    })
                except Exception as exc:
                    detail_failed += 1
                    save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": url, "method": "GET", "capture": "playwright", "error": f"{type(exc).__name__}: {exc}"})
            LAST_STATS = {"strategy": "playwright_rendered_html", "pages": pages, "matched": len(simple), "details_success": detail_success, "details_failed": detail_failed}
            return simple
        finally:
            browser.close()
