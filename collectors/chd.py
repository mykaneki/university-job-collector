from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "长安大学", "CHD"
BASE_URL = "https://51uns.bysjy.com.cn"
LIST_URL = f"{BASE_URL}/Announce/Index"
PAGE_SIZE, MAX_PAGES = 50, 200
SUPPORTED_FILTERS = {"company": "local", "position": "local", "keyword": "native", "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported", "education": "unsupported", "position_type": "unsupported", "employment_type": "unsupported", "student_type": "unsupported"}
LAST_STATS: dict[str, int | str] = {}


def _list(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml"); out = []
    for row in soup.select(".home-hot-sxh li"):
        a = row.select_one("a[href*='/Announce/Details/']")
        if a: out.append({"title": (row.select_one(".title") or a).get_text(" ", strip=True), "company": (row.select_one(".school") or a).get_text(" ", strip=True), "url": urljoin(BASE_URL, str(a.get("href") or ""))})
    return out


def _detail(html: str) -> tuple[str, str, str, str]:
    soup = BeautifulSoup(html, "lxml"); title = (soup.select_one(".info-title") or soup).get_text(" ", strip=True)
    meta = soup.select_one(".info-ct"); text = meta.get_text(" ", strip=True) if meta else ""; date = (re.search(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?", text) or [""])[0]
    company = meta.select_one("span").get_text(" ", strip=True) if meta and meta.select_one("span") else ""
    content = soup.select_one(".info-cont"); return title, company, date, str(content or "")


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME); raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0; results = []
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            # 页面称 StartTime/EndTime 为“举办日期”，实测不能可靠过滤发布时间；
            # 仍传入作为服务端粗筛，最终以详情页发布时间判定。
            params = {"CurrentPage": page, "PageSize": PAGE_SIZE, "SchoolCode": "10710", "Type": 0,
                      "StartTime": start.strftime("%Y-%m-%d"), "EndTime": (end + timedelta(days=1)).strftime("%Y-%m-%d"), "keyword": filters.get("keyword", "")}
            response = client.get(LIST_URL, params=params); save_raw_html(raw_dir / f"list_page_{page:03d}.html", response.content, response_metadata(response, method="GET")); rows = _list(response.text); pages += 1
            if not rows: break
            page_dates = []
            for record in rows:
                source_id = (re.search(r"/Details/(\d+)", record["url"]) or ["unknown", "unknown"])[1]
                try:
                    response = client.get(record["url"]); save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET")); title, company, published, content = _detail(response.text); detail_success += 1
                except Exception as exc:
                    detail_failed += 1; save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": record["url"], "method": "GET", "error": f"{type(exc).__name__}: {exc}"}); continue
                if published:
                    try: page_dates.append(parse_source_datetime(published))
                    except ValueError: pass
                if not published or not in_range(published, start, end): continue
                if filters.get("company") and not contains(company, filters["company"]): continue
                if filters.get("position") and not contains(title, filters["position"]): continue
                results.append({"公司": company, "岗位": title, "岗位上新时间": published, "JD": html_to_text(content), "投递方式": extract_application_methods(content), "原始链接": record["url"]})
            older_pages = older_pages + 1 if page_dates and all(value < start for value in page_dates) else 0
            if older_pages >= 2 or len(rows) < PAGE_SIZE: break
    LAST_STATS = {"strategy": "get_html_detail_date", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results
