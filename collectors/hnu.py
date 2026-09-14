from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "湖南大学", "HNU"
BASE_URL = "https://scc.hnu.edu.cn"
LIST_API = f"{BASE_URL}/module/getonlines"
DETAIL_URL = f"{BASE_URL}/detail/online"
PAGE_SIZE, MAX_PAGES = 15, 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "native", "keyword": "native",
    "location": "native", "company_nature": "native", "industry": "native",
    "education": "local", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _education(record: dict[str, object]) -> str:
    labels = []
    for key, label in (("is_above_college_degree", "大专及以上"), ("is_above_bachelor_degree", "本科及以上"), ("is_above_master_degree", "硕士及以上"), ("is_above_doctor_degree", "博士及以上")):
        if str(record.get(key) or "0") == "1":
            labels.append(label)
    return " ".join(labels)


def _detail_body(content: bytes) -> str:
    soup = BeautifulSoup(content, "lxml")
    return str(soup.select_one(".details-content") or "")


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    title, keyword = filters.get("position", ""), filters.get("keyword", "")
    if title and keyword and title != keyword:
        raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 k 参数")
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    candidates: list[dict[str, object]] = []
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            params = {"start_page": 1, "start": page, "count": PAGE_SIZE, "k": title or keyword, "recruit_type": "正式招聘", "panel_id": "", "professionals": "",
                      "work_city": filters.get("location", ""), "company_property": filters.get("company_nature", ""), "company_industry": filters.get("industry", "")}
            response = client.get(LIST_API, params=params)
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="GET"))
            payload = response.json()
            records = payload.get("data") or []
            pages += 1
            if not records:
                break
            dates = []
            for record in records:
                published = str(record.get("create_time") or "")
                try:
                    dates.append(parse_source_datetime(published))
                except ValueError:
                    continue
                if not in_range(published, start, end):
                    continue
                if filters.get("company") and not contains(record.get("company_name"), filters["company"]):
                    continue
                if filters.get("education") and not contains(_education(record), filters["education"]):
                    continue
                candidates.append(record)
            older_pages = older_pages + 1 if dates and all(d < start for d in dates) else 0
            if older_pages >= 2:
                break
        results = []
        for record in candidates:
            source_id = str(record.get("recruitment_id") or "unknown")
            url = f"{DETAIL_URL}?id={source_id}&menu_id=3231"
            body = ""
            try:
                response = client.get(url)
                save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET"))
                body = _detail_body(response.content)
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": url, "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            results.append({"\u516c\u53f8": str(record.get("company_name") or ""), "\u5c97\u4f4d": str(record.get("title") or ""), "\u5c97\u4f4d\u4e0a\u65b0\u65f6\u95f4": str(record.get("create_time") or ""), "JD": html_to_text(body), "\u6295\u9012\u65b9\u5f0f": extract_application_methods(body, explicit_url=str(record.get("content_source_url") or "")), "\u539f\u59cb\u94fe\u63a5": url})
    LAST_STATS = {"strategy": "json_api_html_detail", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results
