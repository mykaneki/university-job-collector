from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from common.browser import ChromeCdpSession
from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.output import save_error_meta, save_raw_html, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "西南石油大学", "SWPU"
BASE_URL = "https://jyzx.swpu.edu.cn"
LIST_URL = f"{BASE_URL}/module/news?menu_id=21758&type_id=10229"
MAX_PAGES = 200
SUPPORTED_FILTERS = {
    "company": "unsupported", "position": "local", "keyword": "native",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _rendered_meta(url: str, status: int | None, capture: str) -> dict[str, object]:
    return {"url": url, "method": "GET", "status_code": status, "content_type": "text/html; rendered-dom", "capture": capture}


def _response_meta(response: object) -> dict[str, object]:
    headers = getattr(response, "headers", {})
    request = getattr(response, "request", None)
    return {
        "url": str(getattr(response, "url", "")),
        "method": str(getattr(request, "method", "GET")),
        "status_code": getattr(response, "status", None),
        "content_type": headers.get("content-type", "") if isinstance(headers, dict) else "",
        "capture": "playwright_network_response",
        "raw_representation": "original_response_body",
    }


def _save_failed_render(page: object, path: Path, requested_url: str, response: object | None) -> bool:
    try:
        rendered = page.content()  # type: ignore[attr-defined]
        final_url = str(getattr(page, "url", requested_url))
        status = getattr(response, "status", None) if response is not None else None
        save_raw_html(path, rendered, _rendered_meta(final_url, status, "chrome_cdp_rendered_dom_after_error") | {"requested_url": requested_url, "navigation_failed": True})
        return True
    except Exception:
        return False


def _list_records(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    records = []
    for item in soup.select("#data_html li.item"):
        anchor = item.select_one("a.item-link[href]")
        date = item.select_one(".item-time")
        if anchor and date:
            records.append({"title": anchor.get("title") or anchor.get_text(" ", strip=True), "published": date.get_text(" ", strip=True), "url": urljoin(BASE_URL, str(anchor.get("href") or ""))})
    return records


def _detail(html: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("#data_details .dh-tit")
    published = ""
    for node in soup.select("#data_details .dh-info .time"):
        match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", node.get_text(" ", strip=True))
        if match:
            published = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
            break
    content = soup.select_one("#data_details .details-content")
    return title.get_text(" ", strip=True) if title else "", published, str(content or "")


def _list_match(record: dict[str, str], filters: dict[str, str]) -> bool:
    return (
        (not filters.get("position") or contains(record["title"], filters["position"]))
        and (not filters.get("keyword") or contains(record["title"], filters["keyword"]))
    )


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, str]] = []
    with ChromeCdpSession() as chrome:
        page = chrome.new_page()
        response = page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("#data_html", state="attached", timeout=20_000)
        if filters.get("keyword"):
            keyword = filters["keyword"]
            page.locator("#keyword").fill(keyword)
            def is_search_data_response(item: object) -> bool:
                query = parse_qs(urlparse(str(getattr(item, "url", ""))).query)
                return (
                    "/module/getnotices" in str(getattr(item, "url", ""))
                    and query.get("k") == [keyword]
                    and query.get("count") == ["15"]
                    and query.get("start_page") == ["1"]
                )
            with page.expect_response(is_search_data_response, timeout=20_000) as response_info:
                page.locator("#search_btn").click()
            search_response = response_info.value
            save_raw_json(raw_dir / "filter_keyword.xhr.json", search_response.body(), _response_meta(search_response))
            page.wait_for_selector("#data_html li", state="attached", timeout=20_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
        status = response.status if response else None
        for page_no in range(1, MAX_PAGES + 1):
            page.wait_for_selector("#data_html", state="attached", timeout=20_000)
            rendered = page.content()
            save_raw_html(raw_dir / f"list_page_{page_no:03d}.html", rendered, _rendered_meta(page.url, status if page_no == 1 else None, "chrome_cdp_rendered_dom"))
            rows = _list_records(rendered)
            pages += 1
            if not rows:
                break
            dates = []
            for row in rows:
                try:
                    dates.append(parse_source_datetime(row["published"]))
                except ValueError:
                    continue
                if in_range(row["published"], start, end) and _list_match(row, filters):
                    matched.append(row)
            older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
            next_item = page.locator(".paginationjs-next:not(.disabled)")
            if older_pages >= 2 or next_item.count() == 0:
                break
            before = rows[0]["url"]
            next_item.locator("a").click()
            page.wait_for_function("before => document.querySelector('#data_html a.item-link')?.href !== before", arg=before, timeout=20_000)

        results = []
        for record in matched:
            source_id = parse_qs(urlparse(record["url"]).query).get("id", ["unknown"])[0]
            title, published, content = record["title"], record["published"], ""
            final_url = record["url"]
            response = None
            try:
                response = page.goto(record["url"], wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector("#data_details", timeout=20_000)
                rendered = page.content()
                final_url = page.url
                save_raw_html(raw_dir / f"detail_{source_id}.html", rendered, _rendered_meta(page.url, response.status if response else None, "chrome_cdp_rendered_dom"))
                detail_title, detail_date, content = _detail(rendered)
                title, published = detail_title or title, detail_date or published
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                raw_saved = _save_failed_render(page, raw_dir / f"detail_{source_id}.failed.html", record["url"], response)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": record["url"], "method": "GET", "capture": "chrome_cdp", "rendered_dom_saved": raw_saved, "error": f"{type(exc).__name__}: {exc}"})
            if not in_range(published, start, end):
                continue
            results.append({"公司": "", "岗位": title, "岗位上新时间": published, "JD": html_to_text(content), "投递方式": extract_application_methods(content), "原始链接": final_url})
    LAST_STATS = {"strategy": "chrome_cdp_rendered_html", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results
