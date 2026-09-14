from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from common.browser import ChromeCdpSession
from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.output import save_error_meta, save_raw_html
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "四川大学", "SCU"
BASE_URL = "https://jy.scu.edu.cn"
LIST_URL = f"{BASE_URL}/index/index/employjob.html"
MAX_PAGES = 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local", "location": "local",
    "company_nature": "local", "industry": "local", "education": "local", "position_type": "local",
    "employment_type": "local", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _list_records(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    result = []
    for row in soup.select("#mycontent li"):
        link, date = row.select_one("a[href*='employjobdetail']"), row.select_one(".list1_time")
        if link and date:
            title = link.get_text(" ", strip=True)
            match = re.search(r"【([^]]+)】\s*$", title)
            result.append({"title": title, "company": match.group(1) if match else "", "published": date.get_text(" ", strip=True), "url": urljoin(BASE_URL, str(link.get("href") or ""))})
    return result


def _detail(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    fields: dict[str, str] = {}
    for table in soup.select("#mycontent table"):
        rows = table.select("tr")
        offset = 0
        if len(rows) >= 2:
            headers = [cell.get_text(" ", strip=True) for cell in rows[0].select("th,td")]
            values = [cell.get_text(" ", strip=True) for cell in rows[1].select("th,td")]
            if "职位名称" in headers and len(headers) == len(values):
                fields.update(zip(headers, values))
                offset = 2
        for row in rows[offset:]:
            cells = row.select("th,td")
            for index in range(0, len(cells) - 1, 2):
                label = cells[index].get_text(" ", strip=True)
                if label:
                    fields[label] = cells[index + 1].get_text(" ", strip=True)
    content = soup.select_one(".asan-page_con")
    return fields | {"content": str(content or "")}


def _list_match(record: dict[str, str], filters: dict[str, str]) -> bool:
    return (
        (not filters.get("position") or contains(record["title"], filters["position"]))
        and (not filters.get("keyword") or contains(record["title"] + record["company"], filters["keyword"]))
    )


def _rendered_meta(url: str, status: int | None, capture: str) -> dict[str, object]:
    return {
        "url": url,
        "method": "GET",
        "status_code": status,
        "content_type": "text/html; rendered-dom",
        "capture": capture,
    }


def _save_failed_render(page: object, path: Path, requested_url: str, response: object | None) -> bool:
    try:
        rendered = page.content()  # type: ignore[attr-defined]
        final_url = str(getattr(page, "url", requested_url))
        status = getattr(response, "status", None) if response is not None else None
        save_raw_html(
            path,
            rendered,
            _rendered_meta(final_url, status, "chrome_cdp_rendered_dom_after_error")
            | {"requested_url": requested_url, "navigation_failed": True},
        )
        return True
    except Exception:
        return False


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME); raw_dir.mkdir(parents=True, exist_ok=True)
    matched = []
    pages = older_pages = detail_success = detail_failed = 0
    with ChromeCdpSession() as chrome:
        browser_page = chrome.new_page()
        for page_no in range(1, MAX_PAGES + 1):
            url = f"{LIST_URL}?type=1&page={page_no}"
            response = browser_page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            browser_page.wait_for_selector("#mycontent li a[href*='employjobdetail']", timeout=20_000)
            rendered = browser_page.content()
            status = response.status if response else None
            save_raw_html(raw_dir / f"list_page_{page_no:03d}.html", rendered, _rendered_meta(browser_page.url, status, "chrome_cdp_rendered_dom"))
            records = _list_records(rendered)
            pages += 1
            if not records:
                break
            dates = [parse_source_datetime(x["published"]) for x in records]
            for record in records:
                same_target_day = start.date() <= parse_source_datetime(record["published"]).date() <= end.date()
                if same_target_day and _list_match(record, filters):
                    matched.append(record)
            older_pages = older_pages + 1 if dates and all(value.date() < start.date() for value in dates) else 0
            if older_pages >= 2:
                break
        results = []
        for index, record in enumerate(matched, 1):
            detail = {}
            response = None
            try:
                response = browser_page.goto(record["url"], wait_until="domcontentloaded", timeout=30_000)
                browser_page.wait_for_selector("#mycontent", timeout=20_000)
                rendered = browser_page.content()
                save_raw_html(raw_dir / f"detail_{index:04d}.html", rendered, _rendered_meta(browser_page.url, response.status if response else None, "chrome_cdp_rendered_dom"))
                detail = _detail(rendered)
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                raw_saved = _save_failed_render(browser_page, raw_dir / f"detail_{index:04d}.failed.html", record["url"], response)
                save_error_meta(raw_dir / f"detail_{index:04d}.error", {"url": record["url"], "method": "GET", "capture": "chrome_cdp", "rendered_dom_saved": raw_saved, "error": f"{type(exc).__name__}: {exc}"})
            published = detail.get("发布时间", "") or record["published"]
            if not in_range(published, start, end):
                continue
            fields = {"location": detail.get("工作地址", ""), "company_nature": detail.get("单位性质", ""), "industry": detail.get("单位行业", ""), "education": detail.get("最低学历要求", ""), "position_type": detail.get("类型", ""), "employment_type": detail.get("性质", "")}
            if any(filters.get(k) and not contains(fields[k], filters[k]) for k in fields):
                continue
            content = detail.get("content", "")
            company = detail.get("单位名称", "") or record["company"]
            if filters.get("company") and not contains(company, filters["company"]):
                continue
            results.append({"公司": company, "岗位": record["title"], "岗位上新时间": published, "JD": html_to_text(content), "投递方式": extract_application_methods(content), "原始链接": record["url"]})
    LAST_STATS = {"strategy": "chrome_cdp_rendered_html", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results
