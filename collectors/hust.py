from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "华中科技大学", "HUST"
BASE_URL = "https://job.hust.edu.cn"
FIRST_PAGE = f"{BASE_URL}/searchJob.do?fbsj=&type=2"
MAX_PAGES = 200
SUPPORTED_FILTERS = {
    "company": "unsupported", "position": "local", "keyword": "local",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _page_url(page: int) -> str:
    return FIRST_PAGE if page == 1 else f"{BASE_URL}/searchJob_{page}.do?fbsj=&type=2"


def _records(content: bytes) -> list[dict[str, str]]:
    soup = BeautifulSoup(content, "lxml")
    rows = []
    for li in soup.select(".zplist li"):
        link = li.select_one('a[href*="/zpinfo1/"]')
        date = li.select_one(".n2")
        if not link or not date:
            continue
        published = date.get_text(" ", strip=True).strip("[]")
        rows.append({"title": link.get_text(" ", strip=True), "date": published, "url": urljoin(BASE_URL, link.get("href", ""))})
    return rows


def _detail(content: bytes) -> str:
    soup = BeautifulSoup(content, "lxml")
    node = soup.select_one(".content")
    return str(node or "")


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, str]] = []
    LOGGER.info("[%s] strategy=get_html", TAG)
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            response = client.get(_page_url(page))
            save_raw_html(raw_dir / f"list_page_{page:03d}.html", response.content, response_metadata(response, method="GET"))
            records = _records(response.content)
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page, len(records))
            if not records:
                break
            dates = [parse_source_datetime(r["date"]) for r in records]
            for record in records:
                if in_range(record["date"], start, end):
                    if filters.get("position") and not contains(record["title"], filters["position"]):
                        continue
                    if filters.get("keyword") and not contains(record["title"], filters["keyword"]):
                        continue
                    matched.append(record)
            older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
            if older_pages >= 2:
                break
        results = []
        for record in matched:
            source_id = (re.search(r"/(\d+)\.htm", record["url"]) or [None, "unknown"])[1]
            body = ""
            try:
                response = client.get(record["url"])
                save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET"))
                body = _detail(response.content)
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": record["url"], "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            results.append({"\u516c\u53f8": "", "\u5c97\u4f4d": record["title"], "\u5c97\u4f4d\u4e0a\u65b0\u65f6\u95f4": record["date"], "JD": html_to_text(body), "\u6295\u9012\u65b9\u5f0f": extract_application_methods(body), "\u539f\u59cb\u94fe\u63a5": record["url"]})
    LAST_STATS = {"strategy": "get_html", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results
